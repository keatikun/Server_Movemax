from flask import Flask, jsonify
from flask_socketio import SocketIO, emit
from pymongo import MongoClient
from threading import Thread
import os
import json

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

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

# Background thread for watching change streams
def watch_changes():
    with messages_col.watch() as stream:
        for change in stream:
            full_doc = change.get("fullDocument")
            # ส่งเฉพาะถ้าเป็น chat structure (มี userId และ chats)
            if full_doc and "userId" in full_doc and "chats" in full_doc:
                # แปลง _id ObjectId เป็น string ก่อนส่ง
                full_doc['_id'] = str(full_doc['_id'])
                socketio.emit('chat_update', full_doc)

@socketio.on('connect')
def on_connect():
    print("Client connected")

if __name__ == "__main__":
    # Start the background thread to watch MongoDB changes
    watcher_thread = Thread(target=watch_changes)
    watcher_thread.daemon = True
    watcher_thread.start()

    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)
