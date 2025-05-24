from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from pymongo import MongoClient
from threading import Thread
import os

app = Flask(__name__)
CORS(app)  # เปิด CORS ให้ REST API
socketio = SocketIO(app, cors_allowed_origins="*")  # เปิด CORS ให้ WebSocket

# เชื่อม MongoDB
mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

# REST API ดึงข้อมูล users
@app.route('/users', methods=['GET'])
def get_users():
    users = list(users_col.find())
    for user in users:
        user['_id'] = str(user['_id'])
    return jsonify(users)

# REST API ดึงข้อมูล messages
@app.route('/messages', methods=['GET'])
def get_messages():
    messages = list(messages_col.find())
    for msg in messages:
        msg['_id'] = str(msg['_id'])
    return jsonify(messages)

# ฟังก์ชันฟัง MongoDB Change Streams แบบ real-time
def watch_changes():
    with messages_col.watch() as stream:
        for change in stream:
            op_type = change['operationType']
            full_doc = change.get('fullDocument')
            doc_id = str(change['documentKey']['_id'])

            # ส่งข้อมูลผ่าน WebSocket event ตามประเภทการเปลี่ยนแปลง
            if op_type == 'insert':
                socketio.emit('message_insert', {'_id': doc_id, 'fullDocument': full_doc})
            elif op_type == 'update':
                # full_doc อาจไม่อยู่ถ้าเป็น update partial ต้อง query ใหม่
                if not full_doc:
                    full_doc = messages_col.find_one({'_id': change['documentKey']['_id']})
                socketio.emit('message_update', {'_id': doc_id, 'fullDocument': full_doc})
            elif op_type == 'delete':
                socketio.emit('message_delete', {'_id': doc_id})

@socketio.on('connect')
def on_connect():
    print("Client connected")

if __name__ == '__main__':
    # Start MongoDB watcher thread
    watcher_thread = Thread(target=watch_changes)
    watcher_thread.daemon = True
    watcher_thread.start()

    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host='0.0.0.0', port=port)
