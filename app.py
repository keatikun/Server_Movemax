import os
from flask import Flask, request, jsonify
from flask_pymongo import PyMongo
from flask_socketio import SocketIO, emit, join_room
from datetime import datetime

app = Flask(__name__)

# อ่าน MongoDB URI จาก environment variable
app.config["MONGO_URI"] = os.environ.get("MONGO_URI", "mongodb://localhost:27017/chatdb")

mongo = PyMongo(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# API สำหรับทดสอบ
@app.route("/")
def index():
    return "Chat Server is running"

# WebSocket event: เข้าร่วมห้องแชท
@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)
    emit('status', {'msg': f'{data["username"]} has entered the room.'}, room=room)

# WebSocket event: ส่งข้อความ
@socketio.on('send_message')
def handle_send_message(data):
    room = data['room']
    message = {
        'username': data['username'],
        'text': data['text'],
        'timestamp': datetime.utcnow(),
        'room': room,
        'type': data.get('type', 'text'),  # text / image / file
        'file_url': data.get('file_url', None)
    }
    # บันทึกลง MongoDB
    mongo.db.messages.insert_one(message)

    # ส่งข้อความกลับไปยังห้องแบบเรียลไทม์
    emit('receive_message', {
        'username': message['username'],
        'text': message['text'],
        'timestamp': message['timestamp'].isoformat(),
        'type': message['type'],
        'file_url': message['file_url']
    }, room=room)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
