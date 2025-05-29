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
    logged_in_user = request.args.get('me')
    if not logged_in_user:
        return jsonify({"error": "Missing 'me' parameter"}), 400  # ❌ ถ้าไม่ได้ส่ง me มา

    admins = list(admins_col.find({}, {"_id": 0}))

    for admin in admins:
        other_user = admin['username']

        # ✅ หาข้อความล่าสุดระหว่าง logged_in_user กับแต่ละ admin
        last_msg = messages_col.find_one(
            {"$or": [
                {"from": logged_in_user, "to": other_user},
                {"from": other_user, "to": logged_in_user}
            ]},
            sort=[("timestamp", -1)]
        )
        admin["lastMessage"] = last_msg["text"] if last_msg else ""

        # ✅ นับข้อความที่ยังไม่ได้อ่าน
        unread_count = messages_col.count_documents({
            "from": other_user,
            "to": logged_in_user,
            "read": False
        })
        admin["unreadCount"] = unread_count

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

# สร้างชื่อห้องให้ standardized เช่น admin001_user001
def generate_room_name(user1, user2):
    return "_".join(sorted([user1, user2]))

@socketio.on('join')
def on_join(data):
    user1 = data.get("user1")
    user2 = data.get("user2")
    room = generate_room_name(user1, user2)
    join_room(room)
    emit('status', {'msg': f"{user1} joined room {room}"}, room=room)

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
        "read": False  # ✅ เพิ่มการบันทึกสถานะยังไม่อ่าน
    }

    # insert message
    result = messages_col.insert_one(message)
    message["_id"] = str(result.inserted_id)

    # check if chat room exists
    if not chats_col.find_one({"room": room}):
        chats_col.insert_one({
            "room": room,
            "participants": [sender, receiver],
            "created_at": datetime.now(timezone.utc).isoformat()
        })

    emit('receive_message', message, room=room)

@app.route('/mark-as-read/<sender>/<receiver>', methods=['POST'])
def mark_as_read(sender, receiver):
    result = messages_col.update_many(
        {"from": sender, "to": receiver, "read": False},
        {"$set": {"read": True}}
    )
    return jsonify({"updated": result.modified_count}), 200


if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080)
