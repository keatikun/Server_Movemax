from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from config import MONGO_URI, SECRET_KEY
from datetime import datetime

# Setup Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# Setup SocketIO (ใช้ eventlet หรือ gevent)
socketio = SocketIO(app, cors_allowed_origins="*")

# Setup MongoDB
client = MongoClient(MONGO_URI)
db = client["Movemax"]
messages_col = db["messages"]
users_col = db["users"]  # เพิ่มบรรทัดนี้ถ้ายังไม่ได้ประกาศ

@app.route('/')
def index():
    return "✅ Chat server is running with WebSocket!"

# ✅ API: ดึง users ทั้งหมด
@app.route('/api/users', methods=['GET'])
def get_all_users():
    users = list(users_col.find({}, {"_id": 0}))  # ไม่แสดง _id
    return jsonify(users), 200

# WebSocket Event: เข้าร่วมห้องแชท 1:1
@socketio.on('join')
def handle_join(data):
    room = data.get("room")
    join_room(room)
    emit('status', {'msg': f"{data.get('username')} joined room {room}"}, room=room)

# WebSocket Event: ส่งข้อความ
@socketio.on('message')
def handle_message(data):
    room = data.get("room")
    message = {
        "from": data.get("from"),
        "to": data.get("to"),
        "text": data.get("text"),
        "timestamp": datetime.utcnow().isoformat()
    }
    # บันทึกข้อความลง MongoDB
    messages_col.insert_one(message)
    # ส่งข้อความไปให้ทุกคนในห้อง
    emit('message', message, room=room)


# (Optional) REST API ดึงข้อความย้อนหลัง
@app.route('/chat/<user1>/<user2>', methods=['GET'])
def get_messages(user1, user2):
    room1 = f"{user1}_{user2}"
    room2 = f"{user2}_{user1}"
    messages = messages_col.find({
        "$or": [
            {"from": user1, "to": user2},
            {"from": user2, "to": user1}
        ]
    }).sort("timestamp", 1)
    return {'messages': list(messages)}, 200

if __name__ == '__main__':
    socketio.run(app, debug=True, host="0.0.0.0", port=8080)
