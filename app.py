from flask import Flask, jsonify
from flask_socketio import SocketIO
from pymongo import MongoClient
from threading import Thread
import os

app = Flask(__name__)
# เปิด CORS ให้กับ REST API
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
messages_col = db["Messages"]

@app.route('/messages')
def get_messages():
    messages = list(messages_col.find())
    for msg in messages:
        msg['_id'] = str(msg['_id'])
    return jsonify(messages)

def watch_changes():
    with messages_col.watch() as stream:
        for change in stream:
            full_doc = change.get("fullDocument")
            if full_doc:
                full_doc['_id'] = str(full_doc['_id'])
                socketio.emit('message_update', full_doc)

@socketio.on('connect')
def on_connect():
    print("Client connected")

if __name__ == "__main__":
    watcher = Thread(target=watch_changes)
    watcher.daemon = True
    watcher.start()
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)
