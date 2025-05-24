from flask import Flask, jsonify
from flask_socketio import SocketIO
from flask_cors import CORS
from pymongo import MongoClient
from threading import Thread
import os
import time

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")  # async_mode ระบุชัด

mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

# -- Helper สำหรับ emit แบบปลอดภัย --
def safe_emit(event, data):
    try:
        socketio.emit(event, data)
        print(f"[Emit] {event} -> {data}")
    except Exception as e:
        print(f"[Emit Error] {event}: {e}")

@app.route('/users')
def get_users():
    users = list(users_col.find())
    for u in users:
        u['_id'] = str(u['_id'])
    return jsonify(users)

@app.route('/messages')
def get_messages():
    messages = list(messages_col.find())
    for m in messages:
        m['_id'] = str(m['_id'])
    return jsonify(messages)

# -- Watch Function ปรับปรุงแล้ว --
def watch_users_changes():
    pipeline = [{'$match': {'operationType': {'$in': ['insert', 'update', 'replace', 'delete']}}}]
    try:
        with users_col.watch(pipeline, full_document='updateLookup') as stream:
            for change in stream:
                print("User change detected:", change)
                doc = change.get('fullDocument')
                if doc:
                    doc['_id'] = str(doc['_id'])
                safe_emit('user_update', doc or {})
    except Exception as e:
        print("Error in watch_users_changes:", e)
        time.sleep(2)
        watch_users_changes()  # รีสตาร์ท watcher

def watch_messages_changes():
    pipeline = [{'$match': {'operationType': {'$in': ['insert', 'update', 'replace', 'delete']}}}]
    try:
        with messages_col.watch(pipeline, full_document='updateLookup') as stream:
            for change in stream:
                print("Message change detected:", change)
                doc = change.get('fullDocument')
                if doc:
                    doc['_id'] = str(doc['_id'])
                safe_emit('messages_update', doc or {})
    except Exception as e:
        print("Error in watch_messages_changes:", e)
        time.sleep(2)
        watch_messages_changes()  # รีสตาร์ท watcher

@socketio.on('connect')
def on_connect():
    print("Client connected")

if __name__ == '__main__':
    user_thread = Thread(target=watch_users_changes)
    user_thread.daemon = True
    user_thread.start()

    message_thread = Thread(target=watch_messages_changes)
    message_thread.daemon = True
    message_thread.start()

    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
