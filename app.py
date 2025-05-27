from flask import Flask, jsonify
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from config import MONGO_URI, SECRET_KEY
from datetime import datetime
from flask_cors import CORS

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*")

client = MongoClient(MONGO_URI)
db = client["Movemax"]
messages_col = db["messages"]
users_col = db["users"]
chats_col = db["chats"]  # สำหรับเก็บข้อมูลห้องแชท (ถ้ามี)

@app.route('/')
def index():
    return "Chat server is running!"

@app.route('/api/users', methods=['GET'])
def get_all_users():
    users = list(users_col.find({}, {"_id": 0}))
    return jsonify(users), 200

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

# เมื่อ client เข้าร่วมห้องแชท
@socketio.on('join')
def on_join(data):
    room = data.get("room")  # ชื่อห้อง เช่น user1_user2 (เรียงชื่อให้เหมือนกันเสมอ)
    username = data.get("username")
    join_room(room)
    emit('status', {'msg': f"{username} joined room {room}"}, room=room)

# เมื่อ client ส่งข้อความเข้ามา
@socketio.on('send_message')
def on_send_message(data):
    room = data.get("room")
    message = {
        "from": data.get("from"),
        "to": data.get("to"),
        "text": data.get("text"),
        "timestamp": datetime.utcnow().isoformat()
    }
    # บันทึกข้อความลง MongoDB แล้วได้ ObjectId มา
    result = messages_col.insert_one(message)
    message["_id"] = str(result.inserted_id)  # แปลง ObjectId เป็น string

    # ส่งข้อความกลับไปยังห้อง
    emit('receive_message', message, room=room)
    
if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080)
