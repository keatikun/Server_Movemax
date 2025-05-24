from flask import Flask, jsonify
from flask_socketio import SocketIO
from flask_cors import CORS
from pymongo import MongoClient
from threading import Thread
import os

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

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

def watch_changes():
    # เราจะดูทั้ง users และ messages collection
    pipeline = [{'$match': {'operationType': {'$in': ['insert', 'update', 'replace', 'delete']}}}]

    with users_col.watch(pipeline, full_document='updateLookup') as users_stream, \
         messages_col.watch(pipeline, full_document='updateLookup') as messages_stream:
        
        import select
        streams = [users_stream, messages_stream]

        while True:
            # ใช้ select เพื่อตรวจสอบ stream ที่มีข้อมูล
            ready = select.select(streams, [], [])[0]
            for stream in ready:
                change = next(stream)
                print("Change detected:", change)

                # ส่ง event แจ้ง client ตาม collection ที่เปลี่ยน
                if stream == users_stream:
                    doc = change.get('fullDocument')
                    if doc:
                        doc['_id'] = str(doc['_id'])
                    socketio.emit('user_update', doc or {})
                else:  # messages_stream
                    doc = change.get('fullDocument')
                    if doc:
                        doc['_id'] = str(doc['_id'])
                    socketio.emit('messages_update', doc or {})

@socketio.on('connect')
def on_connect():
    print("Client connected")

if __name__ == '__main__':
    watcher_thread = Thread(target=watch_changes)
    watcher_thread.daemon = True
    watcher_thread.start()

    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
