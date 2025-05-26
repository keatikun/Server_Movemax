from flask import Flask, jsonify, request
from flask_socketio import SocketIO, join_room, leave_room, emit
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv
import datetime
import os
import eventlet

# โหลด environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback_secret_key')  # fallback เผื่อไม่มีใน env

# เปิดใช้ WebSocket และเปิด CORS
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# เชื่อมต่อ MongoDB
mongo_uri = os.getenv("MONGO_URI")
client = MongoClient(mongo_uri)
db = client.mydatabase
users_col = db.users
chats_col = db.chats
messages_col = db.messages

# === Helper Functions ===
def serialize_user(user):
    return {
        "id": str(user["_id"]),
        "userId": user["userId"],
        "username": user["username"],
        "displayName": user["displayName"],
        "avatarUrl": user.get("avatarUrl", ""),
        "isOnline": user.get("isOnline", False),
        "lastSeen": user.get("lastSeen").isoformat() if user.get("lastSeen") else None
    }

def serialize_message(msg):
    return {
        "id": str(msg["_id"]),
        "chatId": str(msg["chatId"]),
        "senderId": msg["senderId"],
        "text": msg["text"],
        "sentAt": msg["sentAt"].isoformat(),
        "readBy": [str(r) for r in msg.get("readBy", [])],
        "type": msg.get("type", "text")
    }

# === REST API ===
@app.route('/api/users', methods=['GET'])
def get_users():
    users = list(users_col.find())
    return jsonify([serialize_user(u) for u in users])

# === WebSocket Events ===

@socketio.on('connect_user')
def connect_user(data):
    userId = data.get('userId')
    if not userId:
        return
    users_col.update_one(
        {"userId": userId},
        {"$set": {"isOnline": True, "lastSeen": datetime.datetime.utcnow()}}
    )
    emit('user_status', {"userId": userId, "isOnline": True}, broadcast=True)

@socketio.on('disconnect')
def on_disconnect():
    print("Client disconnected")

@socketio.on('join_chat')
def join_chat(data):
    chat_id = data.get('chatId')
    user_id = data.get('userId')
    if chat_id and user_id:
        join_room(chat_id)
        emit('status', {'msg': f'User {user_id} joined chat {chat_id}'}, room=chat_id)

@socketio.on('leave_chat')
def leave_chat(data):
    chat_id = data.get('chatId')
    user_id = data.get('userId')
    if chat_id and user_id:
        leave_room(chat_id)
        emit('status', {'msg': f'User {user_id} left chat {chat_id}'}, room=chat_id)

@socketio.on('send_message')
def send_message(data):
    chat_id = data.get('chatId')
    sender_id = data.get('senderId')
    text = data.get('text')

    if not all([chat_id, sender_id, text]):
        return

    now = datetime.datetime.utcnow()
    message = {
        "chatId": ObjectId(chat_id),
        "senderId": sender_id,
        "text": text,
        "sentAt": now,
        "readBy": [],
        "type": "text"
    }

    messages_col.insert_one(message)

    chats_col.update_one(
        {"_id": ObjectId(chat_id)},
        {"$set": {
            "lastMessage": {
                "text": text,
                "sentAt": now,
                "senderId": sender_id
            }
        }}
    )

    emit('new_message', serialize_message(message), room=chat_id)

# === Start Server ===
eventlet.monkey_patch()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host='0.0.0.0', port=port)
