from flask import Flask, jsonify
from flask_socketio import SocketIO, emit
from pymongo import MongoClient
import os

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # รองรับ CORS จากทุกที่

# เชื่อมต่อ MongoDB Atlas
mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/"  # อย่าลืมใส่ password จริงตอน deploy

client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

@app.route('/')
def index():
    return "✅ Flask + MongoDB + WebSocket is running"

@app.route('/users')
def get_users():
    users = list(users_col.find())
    for user in users:
        user['_id'] = str(user['_id'])
    return jsonify(users)

@app.route('/messages')
def get_messages():
    messages = list(messages_col.find())
    for msg in messages:
        msg['_id'] = str(msg['_id'])
    return jsonify(messages)

# -------------------------------
# 🔴 WebSocket Events
# -------------------------------

@socketio.on('connect')
def handle_connect():
    print("🟢 Client connected")
    emit('server_response', {'message': 'Connected to WebSocket server'})

@socketio.on('disconnect')
def handle_disconnect():
    print("🔴 Client disconnected")

@socketio.on('new_message')
def handle_new_message(data):
    """
    ตัวอย่าง: {
        "sender": "UserA",
        "message": "Hello"
    }
    """
    print("📩 New message received:", data)
    if 'sender' in data and 'message' in data:
        messages_col.insert_one(data)
        emit('message_broadcast', data, broadcast=True)  # ส่งไปยัง client ทุกคน
    else:
        emit('server_response', {'error': 'Invalid message format'})

# -------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)
