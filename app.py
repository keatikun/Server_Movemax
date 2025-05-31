from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from datetime import datetime, timezone
from flask_cors import CORS
from config import MONGO_URI, SECRET_KEY  # import จาก config.py

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

# เก็บ mapping socket_id -> user_id สำหรับติดตามสถานะออนไลน์
connected_users = {}

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
    # สร้างชื่อห้องแบบไม่สนใจลำดับ
    return "_".join(sorted([user1, user2]))

@socketio.on('join')
def on_join(data):
    user1 = data.get("user1")
    user2 = data.get("user2")
    room = generate_room_name(user1, user2)
    join_room(room)
    emit('status', {'msg': f"{user1} joined room {room}"}, room=room)

@socketio.on('join_user_room')
def join_user_room(data):
    user_id = data.get('userId')
    if user_id:
        join_room(user_id)
        # บันทึก socket_id -> user_id
        connected_users[request.sid] = user_id
        # อัปเดตสถานะออนไลน์ในฐานข้อมูล
        users_col.update_one({"username": user_id}, {"$set": {"is_online": True}})
        admins_col.update_one({"username": user_id}, {"$set": {"is_online": True}})
        # แจ้ง client ทั้งหมดว่ามี user นี้ออนไลน์
        socketio.emit('user_status_changed', {'userId': user_id, 'is_online': True})

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    user_id = connected_users.get(sid)
    if user_id:
        # อัปเดตสถานะ offline
        users_col.update_one({"username": user_id}, {"$set": {"is_online": False}})
        admins_col.update_one({"username": user_id}, {"$set": {"is_online": False}})
        connected_users.pop(sid, None)
        # แจ้ง client ว่ามี user ออฟไลน์
        socketio.emit('user_status_changed', {'userId': user_id, 'is_online': False})

@app.route('/chat/mark_read/<user1>/<user2>', methods=['POST'])
def mark_as_read(user1, user2):
    result = messages_col.update_many(
        {"from": user2, "to": user1, "is_read": False},
        {"$set": {"is_read": True}}
    )
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
    admins = list(admins_col.find({}, {"_id": 0, "username": 1}))
    usernames = [a["username"] for a in admins]
    last_messages = []
    for username in usernames:
        if username == user_id:
            continue
        last = messages_col.find_one(
            {
                "$or": [
                    {"from": user_id, "to": username},
                    {"from": username, "to": user_id}
                ]
            },
            sort=[("timestamp", -1)]
        )
        if last:
            last["_id"] = str(last["_id"])
            last_messages.append(last)
        else:
            last_messages.append({
                "from": user_id,
                "to": username,
                "text": "",
                "timestamp": None,
                "is_read": True
            })
    return jsonify({"last_messages": last_messages}), 200

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

    emit('receive_message', message, room=room)
    socketio.emit('new_message', message, to=receiver)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080)
