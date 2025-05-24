from flask import Flask, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from pymongo import MongoClient
import os

app = Flask(__name__)
CORS(app)  # อนุญาต CORS ทุก origin
socketio = SocketIO(app, cors_allowed_origins="*")

# MongoDB Connection String (ใส่ของจริงตอน deploy)
mongo_uri = os.getenv("MONGO_URI") or "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"

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

@socketio.on('connect')
def handle_connect():
    print("🟢 Client connected")
    emit('server_response', {'message': 'Connected to WebSocket server'})

@socketio.on('disconnect')
def handle_disconnect():
    print("🔴 Client disconnected")

@socketio.on('new_message')
def handle_new_message(data):
    print("📩 New message received:", data)
    if 'sender' in data and 'message' in data:
        messages_col.insert_one(data)
        emit('message_broadcast', data, broadcast=True)
        emit('server_response', {'status': '✅ Message saved'})
    else:
        emit('server_response', {'error': '❌ Invalid message format'})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)
