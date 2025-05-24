from flask import Flask, jsonify
from flask_socketio import SocketIO
from pymongo import MongoClient
import threading
import time
import os

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# MongoDB connection string (แก้ให้ตรงของคุณ)
mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
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

def watch_collection_changes(collection, event_name):
    while True:
        try:
            with collection.watch() as stream:
                print(f"🔍 Watching MongoDB {collection.name} changes...")
                for change in stream:
                    print(f"🔔 {collection.name} Change detected:", change)
                    if change['operationType'] == 'insert':
                        full_doc = change['fullDocument']
                        full_doc['_id'] = str(full_doc['_id'])
                        socketio.emit(event_name, full_doc)
        except Exception as e:
            print(f"Error watching {collection.name} change stream:", e)
            time.sleep(5)

def start_watchers():
    t_users = threading.Thread(target=watch_collection_changes, args=(users_col, 'new_user'))
    t_users.daemon = True
    t_users.start()

    t_messages = threading.Thread(target=watch_collection_changes, args=(messages_col, 'new_message'))
    t_messages.daemon = True
    t_messages.start()

@socketio.on('connect')
def on_connect():
    print("🟢 Client connected")

@socketio.on('disconnect')
def on_disconnect():
    print("🔴 Client disconnected")

if __name__ == "__main__":
    start_watchers()
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)
