from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from datetime import datetime, timezone
from flask_cors import CORS
from config import MONGO_URI, SECRET_KEY
from bson.objectid import ObjectId
import logging

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*")

client = MongoClient(MONGO_URI)
db = client["Movemax"]

users_col = db["users"]
admins_col = db["admins"]
messages_col = db["messages"]
rooms_col = db["rooms"]
reads_col = db["user_room_reads"]
user_status_col = db["user_status"]

connected_users = {}

@app.route('/')
def index():
    return "Chat Server Running!"

@socketio.on('join_user_room')
def join_user_room(data):
    user_id = data.get('userId')
    if user_id:
        join_room(user_id)
        connected_users.setdefault(user_id, set()).add(request.sid)
        user_status_col.update_one(
            {"user_id": ObjectId(user_id)},
            {"$set": {"is_online": True, "last_active": datetime.now(timezone.utc)}},
            upsert=True
        )
        socketio.emit('user_status_changed', {'userId': user_id, 'is_online': True})

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    user_id = None
    for uid, sids in connected_users.items():
        if sid in sids:
            user_id = uid
            sids.remove(sid)
            if not sids:
                user_status_col.update_one(
                    {"user_id": ObjectId(user_id)},
                    {"$set": {"is_online": False, "last_active": datetime.now(timezone.utc)}}
                )
                socketio.emit('user_status_changed', {'userId': user_id, 'is_online': False})
            break
    for s in connected_users.values():
        s.discard(sid)

@socketio.on('send_message')
def on_send_message(data):
    room_id = data.get("room_id")
    sender_id = data.get("sender_id")
    message_doc = {
        "room_id": ObjectId(room_id),
        "sender_id": ObjectId(sender_id),
        "sender_type": data.get("sender_type"),
        "message": data.get("message"),
        "type": data.get("type", "text"),
        "timestamp": datetime.now(timezone.utc)
    }
    result = messages_col.insert_one(message_doc)
    message_doc["_id"] = str(result.inserted_id)
    message_doc["timestamp"] = message_doc["timestamp"].isoformat()
    emit('receive_message', message_doc, room=room_id)
    socketio.emit('new_message', message_doc, room=room_id)

@socketio.on('typing')
def on_typing(data):
    room_id = data.get("room_id")
    emit('typing', {'user_id': data.get('user_id')}, room=room_id)

@socketio.on('stop_typing')
def on_stop_typing(data):
    room_id = data.get("room_id")
    emit('stop_typing', {'user_id': data.get('user_id')}, room=room_id)

@app.route('/chat/<room_id>', methods=['GET'])
def get_chat_history(room_id):
    messages = list(messages_col.find({"room_id": ObjectId(room_id)}).sort("timestamp", 1))
    for msg in messages:
        msg["_id"] = str(msg["_id"])
        msg["timestamp"] = msg["timestamp"].isoformat()
    return jsonify(messages), 200

@app.route('/chat/mark_read', methods=['POST'])
def mark_as_read():
    data = request.json
    reads_col.update_one(
        {"user_id": ObjectId(data["user_id"]), "room_id": ObjectId(data["room_id"])}
        , {"$set": {"last_read_timestamp": datetime.now(timezone.utc)}}, upsert=True
    )
    return jsonify({"status": "read"}), 200

if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    logging.getLogger('socketio').setLevel(logging.ERROR)
    logging.getLogger('engineio').setLevel(logging.ERROR)
    print("Starting production server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080)
