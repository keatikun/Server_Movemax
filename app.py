# production_server.py
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from datetime import datetime, timezone
from flask_cors import CORS
from config import MONGO_URI, SECRET_KEY  # your own config file

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app)

# Use Redis as message queue for scaling across multiple workers
socketio = SocketIO(app, cors_allowed_origins="*", message_queue='redis://localhost:6379')

client = MongoClient(MONGO_URI)
db = client["Movemax"]

admins_col = db["admins"]
users_col = db["users"]
chats_col = db["chats"]
messages_col = db["messages"]

connected_users = {}  # in-memory connection tracking

@app.route('/')
def index():
    return "Production Chat Server Running!"

@socketio.on('join_user_room')
def join_user_room(data):
    user_id = data.get('userId')
    if user_id:
        join_room(user_id)
        if user_id not in connected_users:
            connected_users[user_id] = set()
        connected_users[user_id].add(request.sid)
        users_col.update_one({"username": user_id}, {"$set": {"is_online": True}})
        admins_col.update_one({"username": user_id}, {"$set": {"is_online": True}})
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
                users_col.update_one({"username": user_id}, {"$set": {"is_online": False}})
                admins_col.update_one({"username": user_id}, {"$set": {"is_online": False}})
                socketio.emit('user_status_changed', {'userId': user_id, 'is_online': False})
            break
    for s in connected_users.values():
        s.discard(sid)

@socketio.on('join')
def on_join(data):
    room = "_".join(sorted([data['user1'], data['user2']]))
    join_room(room)

@socketio.on('send_message')
def on_send_message(data):
    sender = data.get("from")
    receiver = data.get("to")
    room = "_".join(sorted([sender, receiver]))
    message = {
        "from": sender,
        "to": receiver,
        "text": data.get("text"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "is_read": False
    }
    result = messages_col.insert_one(message)
    message["_id"] = str(result.inserted_id)

    if not chats_col.find_one({"room": room}):
        chats_col.insert_one({
            "room": room,
            "participants": [sender, receiver],
            "created_at": datetime.now(timezone.utc).isoformat()
        })

    emit('receive_message', message, room=room)
    socketio.emit('new_message', message, to=receiver)

@socketio.on('typing')
def on_typing(data):
    room = "_".join(sorted([data['from'], data['to']]))
    emit('typing', {'from': data['from']}, room=room)

@socketio.on('stop_typing')
def on_stop_typing(data):
    room = "_".join(sorted([data['from'], data['to']]))
    emit('stop_typing', {'from': data['from']}, room=room)

if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    import logging
    logging.getLogger('socketio').setLevel(logging.ERROR)
    logging.getLogger('engineio').setLevel(logging.ERROR)

    print("Starting production server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080)
