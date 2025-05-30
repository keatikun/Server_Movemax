from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from config import MONGO_URI, SECRET_KEY
from datetime import datetime, timezone
from flask_cors import CORS

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*")

client = MongoClient(MONGO_URI)
db = client["Movemax"]

# Collections
admins_col = db["admins"]
users_col = db["users"]
chats_col = db["chats"]
messages_col = db["messages"]

@app.route('/')
def index():
    return "Chat server is running!"

@app.route('/api/users', methods=['GET'])
def get_all_users():
    users = list(users_col.find({}, {"_id": 0}))
    return jsonify(users), 200

@app.route('/api/admins', methods=['GET'])
def get_all_admins():
    admins = list(admins_col.find({}, {"_id": 0}))
    return jsonify(admins), 200

@app.route('/chat/<user1>/<user2>', methods=['GET'])
def get_messages(user1, user2):
    messages = list(messages_col.find({
        "$or": [
            {"from": user1, "to": user2},
            {"from": user2, "to": user1}
        ]
    }).sort("timestamp", 1))
    for m in messages:
        m["_id"] = str(m["_id"])
    return jsonify({'messages': messages}), 200

def generate_room_name(user1, user2):
    return "_".join(sorted([user1, user2]))

@socketio.on('join')
def on_join(data):
    user1 = data.get("user1")
    user2 = data.get("user2")
    room = generate_room_name(user1, user2)
    join_room(room)
    emit('status', {'msg': f"{user1} joined room {room}"}, room=room)

# ✅ เพิ่มห้องของแต่ละ user เพื่อใช้ emit ไปยัง user โดยตรง
@socketio.on('join_user_room')
def join_user_room(data):
    user_id = data.get('userId')
    if user_id:
        join_room(user_id)

@app.route('/chat/mark_read/<user1>/<user2>', methods=['POST'])
def mark_as_read(user1, user2):
    result = messages_col.update_many(
        {"from": user2, "to": user1, "is_read": False},
        {"$set": {"is_read": True}}
    )

    # ✅ แจ้ง client ให้ refresh unread count
    socketio.emit('update_unread', to=user1)

    return jsonify({"marked_as_read": result.modified_count}), 200

@app.route('/chat/unread_count/<user_id>', methods=['GET'])
def get_unread_count(user_id):
    pipeline = [
        {"$match": {"to": user_id, "is_read": False}},
        {"$group": {"_id": "$from", "count": {"$sum": 1}}}
    ]
    result = list(messages_col.aggregate(pipeline))
    return jsonify({"unread_counts": result}), 200

@app.route('/chat/last_messages/<user_id>', methods=['GET'])
def get_last_messages(user_id):
    pipeline = [
        {"$match": {"$or": [{"from": user_id}, {"to": user_id}]}},
        {"$sort": {"timestamp": -1}},
        {"$group": {
            "_id": {
                "room": {
                    "$cond": {
                        "if": {"$gt": ["$from", "$to"]},
                        "then": {"$concat": ["$from", "_", "$to"]},
                        "else": {"$concat": ["$to", "_", "$from"]}
                    }
                }
            },
            "last_message": {"$first": "$$ROOT"}
        }},
        {"$replaceRoot": {"newRoot": "$last_message"}}
    ]
    result = list(messages_col.aggregate(pipeline))
    for msg in result:
        msg["_id"] = str(msg["_id"])
    return jsonify({"last_messages": result}), 200

@socketio.on('typing')
def on_typing(data):
    sender = data.get("from")
    receiver = data.get("to")
    room = generate_room_name(sender, receiver)
    emit('typing', {"from": sender}, room=room)

@socketio.on('stop_typing')
def on_stop_typing(data):
    sender = data.get("from")
    receiver = data.get("to")
    room = generate_room_name(sender, receiver)
    emit('stop_typing', {"from": sender}, room=room)

@socketio.on('send_message')
def on_send_message(data):
    sender = data.get("from")
    receiver = data.get("to")
    room = generate_room_name(sender, receiver)

    message = {
        "from": sender,
        "to": receiver,
        "text": data.get("text"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "is_read": False
    }

    result = messages_col.insert_one(message)
    message["_id"] = str(result.inserted_id)

    if not chats_col.find_one({"room": room}):
        chats_col.insert_one({
            "room": room,
            "participants": [sender, receiver],
            "created_at": datetime.now(timezone.utc).isoformat()
        })

    # ✅ ส่ง message ไปยังห้องแชท
    emit('receive_message', message, room=room)

    # ✅ ส่ง event ไปยังผู้รับ เพื่อ refresh chat list
    socketio.emit('new_message', message, to=receiver)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080)
