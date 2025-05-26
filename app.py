from flask import Flask, jsonify, request
from flask_socketio import SocketIO, join_room, leave_room, emit
from pymongo import MongoClient
from bson import ObjectId
import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")

# เชื่อมต่อ MongoDB (แก้ URI ให้ตรงกับของคุณ)
client = MongoClient("mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority")
db = client.mydatabase
users_col = db.users
chats_col = db.chats
messages_col = db.messages

# ฟังก์ชันช่วยแปลง ObjectId เป็น string และแปลงวันที่
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

# REST API: ดึง user list
@app.route('/api/users', methods=['GET'])
def get_users():
    users = list(users_col.find())
    return jsonify([serialize_user(u) for u in users])

# WebSocket: ผู้ใช้เชื่อมต่อ แจ้งตัวตน (userId)
@socketio.on('connect_user')
def connect_user(data):
    userId = data.get('userId')
    if not userId:
        return

    # อัปเดตสถานะออนไลน์ และ lastSeen
    users_col.update_one(
        {"userId": userId},
        {"$set": {"isOnline": True, "lastSeen": datetime.datetime.utcnow()}}
    )
    emit('user_status', {"userId": userId, "isOnline": True}, broadcast=True)

# WebSocket: ผู้ใช้ disconnect อัปเดตสถานะออฟไลน์
@socketio.on('disconnect')
def on_disconnect():
    # คุณอาจจะต้องเก็บ mapping userId <-> sid ในระบบจริง
    # ตัวอย่างนี้ไม่เก็บไว้ ต้องปรับตามระบบจริงของคุณ
    pass

# WebSocket: เข้าร่วมห้องแชท (chatId)
@socketio.on('join_chat')
def join_chat(data):
    chat_id = data.get('chatId')
    user_id = data.get('userId')
    if chat_id and user_id:
        join_room(chat_id)
        emit('status', {'msg': f'User {user_id} joined chat {chat_id}'}, room=chat_id)

# WebSocket: ออกจากห้องแชท
@socketio.on('leave_chat')
def leave_chat(data):
    chat_id = data.get('chatId')
    user_id = data.get('userId')
    if chat_id and user_id:
        leave_room(chat_id)
        emit('status', {'msg': f'User {user_id} left chat {chat_id}'}, room=chat_id)

# WebSocket: ส่งข้อความในแชท
@socketio.on('send_message')
def send_message(data):
    chat_id = data.get('chatId')
    sender_id = data.get('senderId')
    text = data.get('text')

    if not all([chat_id, sender_id, text]):
        return

    now = datetime.datetime.utcnow()

    # บันทึกข้อความใน MongoDB
    message = {
        "chatId": ObjectId(chat_id),
        "senderId": sender_id,  # ใช้ userId ที่เป็น int หรือ string ตามที่เก็บจริง
        "text": text,
        "sentAt": now,
        "readBy": [],
        "type": "text"
    }
    result = messages_col.insert_one(message)

    # อัปเดต lastMessage ใน chats
    chats_col.update_one(
        {"_id": ObjectId(chat_id)},
        {"$set": {"lastMessage": {
            "text": text,
            "sentAt": now,
            "senderId": sender_id
        }}}
    )

    # ส่งข้อความไปยังผู้ที่อยู่ในห้อง chat_id ทุกคน (Realtime)
    emit('new_message', serialize_message(message), room=chat_id)


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)
