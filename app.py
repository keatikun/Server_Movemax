from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from config import MONGO_URI, SECRET_KEY
from datetime import datetime
from flask_cors import CORS

# Setup Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app)

# Setup SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# Setup MongoDB
client = MongoClient(MONGO_URI)
db = client["Movemax"]
users_col = db["users"]
chats_col = db["chats"]
messages_col = db["messages"]

@app.route('/')
def index():
    return "✅ Chat server is running with WebSocket!"

# API: ดึง users ทั้งหมด
@app.route('/api/users', methods=['GET'])
def get_all_users():
    users = list(users_col.find({}, {"_id": 0}))
    return jsonify(users), 200

# API: ดึงแชททั้งหมดของ user พร้อมข้อความล่าสุด
@app.route('/api/chats/<userId>', methods=['GET'])
def get_chats(userId):
    user_chats = list(chats_col.find({"members": userId}))
    chat_summaries = []

    for chat in user_chats:
        chat_id = str(chat["_id"])
        last_msg = messages_col.find({"chatId": chat_id}).sort("timestamp", -1).limit(1)
        last_msg = list(last_msg)
        
        other_members = [m for m in chat["members"] if m != userId]

        chat_summaries.append({
            "chatId": chat_id,
            "members": chat["members"],
            "chatWith": other_members,
            "lastMessage": last_msg[0]["text"] if last_msg else None,
            "lastTimestamp": last_msg[0]["timestamp"] if last_msg else None
        })

    # เรียงตาม lastTimestamp ใหม่ล่าสุดก่อน
    chat_summaries.sort(key=lambda x: x["lastTimestamp"] or "", reverse=True)

    return jsonify(chat_summaries), 200

# API: ดึงข้อความย้อนหลังใน chatId
@app.route('/api/messages/<chatId>', methods=['GET'])
def get_messages(chatId):
    messages = list(messages_col.find({"chatId": chatId}).sort("timestamp", 1))
    for m in messages:
        m["_id"] = str(m["_id"])  # แปลง ObjectId เป็น str
    return jsonify({"messages": messages}), 200

# WebSocket Event: เข้าร่วมห้อง chat ตาม chatId
@socketio.on('join')
def handle_join(data):
    chat_id = data.get("chatId")
    username = data.get("username")
    join_room(chat_id)
    emit('status', {'msg': f"{username} joined chat {chat_id}"}, room=chat_id)

# WebSocket Event: ส่งข้อความ
@socketio.on('message')
def handle_message(data):
    chat_id = data.get("chatId")
    message = {
        "chatId": chat_id,
        "from": data.get("from"),
        "to": data.get("to"),
        "text": data.get("text"),
        "timestamp": datetime.utcnow().isoformat()
    }
    messages_col.insert_one(message)
    emit('message', message, room=chat_id)

if __name__ == '__main__':
    socketio.run(app, debug=True, host="0.0.0.0", port=8080)
