from flask import Flask, jsonify
from flask_socketio import SocketIO, emit
from pymongo import MongoClient
import threading
import os
import json

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# MongoDB connection string (ใส่จริงตอน deploy)
mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

@app.route('/')
def index():
    return "✅ Flask + MongoDB Change Streams + WebSocket is running"

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

# WebSocket events

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
        # ไม่ต้อง emit ที่นี่ เพราะจะใช้ Change Stream แจ้ง client แทน
        emit('server_response', {'status': '✅ Message saved'})
    else:
        emit('server_response', {'error': '❌ Invalid message format'})

def watch_messages_changes():
    try:
        with messages_col.watch() as stream:
            for change in stream:
                print("🔔 Change detected:", change)
                if change['operationType'] in ['insert', 'update', 'replace']:
                    # ดึง document ล่าสุดมา ส่งให้ client
                    doc_id = change['documentKey']['_id']
                    doc = messages_col.find_one({'_id': doc_id})
                    if doc:
                        doc['_id'] = str(doc['_id'])
                        # ส่งข้อมูลผ่าน WebSocket ไปทุก client ที่เชื่อมต่อ
                        socketio.emit('message_broadcast', doc)
    except Exception as e:
        print("Error in watch_messages_changes:", e)

# รัน Change Stream listener ใน background thread
def start_change_stream_listener():
    thread = threading.Thread(target=watch_messages_changes)
    thread.daemon = True
    thread.start()

if __name__ == "__main__":
    start_change_stream_listener()
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)
