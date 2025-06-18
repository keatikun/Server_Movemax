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

@app.route('/api/users', methods=['GET'])
def get_users():
    users = list(users_col.find({}, {"password": 0}))
    for u in users:
        u["_id"] = str(u["_id"])
    return jsonify(users), 200

@app.route('/api/admins', methods=['GET'])
def get_admins():
    admins = list(admins_col.find({}, {"password": 0}))
    for a in admins:
        a["_id"] = str(a["_id"])
    return jsonify(admins), 200

@app.route('/api/unread_counts/<user_id>', methods=['GET'])
def get_unread_counts(user_id):
    try:
        user_obj_id = ObjectId(user_id)
    except:
        return jsonify({"error": "Invalid user_id"}), 400

    reads = list(reads_col.find({"user_id": user_obj_id}))
    unread_counts = {}

    for read in reads:
        room_id = read["room_id"]
        last_read = read.get("last_read_timestamp", datetime.min.replace(tzinfo=timezone.utc))
        count = messages_col.count_documents({
            "room_id": room_id,
            "timestamp": {"$gt": last_read}
        })
        unread_counts[str(room_id)] = count

    return jsonify(unread_counts), 200

@app.route('/api/user_status/<user_id>', methods=['GET'])
def get_user_status(user_id):
    try:
        user_obj_id = ObjectId(user_id)
    except:
        return jsonify({"error": "Invalid user_id"}), 400

    status = user_status_col.find_one({"user_id": user_obj_id})
    if not status:
        return jsonify({"user_id": user_id, "is_online": False}), 200

    return jsonify({
        "user_id": user_id,
        "is_online": status.get("is_online", False),
        "last_active": status.get("last_active").isoformat() if status.get("last_active") else None
    }), 200

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
    try:
        room_id = ObjectId(data.get("room_id"))
        sender_id = ObjectId(data.get("sender_id"))
    except:
        return

    message_doc = {
        "room_id": room_id,
        "sender_id": sender_id,
        "sender_type": data.get("sender_type"),
        "message": data.get("message"),
        "type": data.get("type", "text"),
        "timestamp": datetime.now(timezone.utc)
    }
    result = messages_col.insert_one(message_doc)
    message_doc["_id"] = str(result.inserted_id)
    message_doc["timestamp"] = message_doc["timestamp"].isoformat()
    emit('receive_message', message_doc, room=str(room_id))
    socketio.emit('new_message', message_doc, room=str(room_id))

@socketio.on('typing')
def on_typing(data):
    room_id = data.get("room_id")
    emit('typing', {'user_id': data.get('user_id')}, room=room_id)

@socketio.on('stop_typing')
def on_stop_typing(data):
    room_id = data.get("room_id")
    emit('stop_typing', {'user_id': data.get('user_id')}, room=room_id)
    
@app.route('/chat_room')
def get_or_create_room():
    try:
        user1 = ObjectId(request.args.get("user1"))
        user2 = ObjectId(request.args.get("user2"))
        role1 = request.args.get("role1")
        role2 = request.args.get("role2")
    except:
        return jsonify({"error": "Invalid parameters"}), 400

    members = [
        {"id": user1, "type": role1},
        {"id": user2, "type": role2}
    ]

    # 🔐 จัดเรียงสมาชิกให้ลำดับคงที่ (เรียงตาม string id)
    members.sort(key=lambda m: str(m["id"]))

    room = rooms_col.find_one({
        "members": {"$all": members}
    })

    if room:
        return jsonify({"room_id": str(room["_id"])})

    result = rooms_col.insert_one({
        "type": "private",
        "members": members,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    })
    return jsonify({"room_id": str(result.inserted_id)})



@app.route('/chat/<room_id>', methods=['GET'])
def get_chat_history(room_id):
    try:
        room_obj_id = ObjectId(room_id)
    except:
        return jsonify({"error": "Invalid room_id"}), 400

    messages = list(messages_col.find({"room_id": room_obj_id}).sort("timestamp", 1))
    for msg in messages:
        msg["_id"] = str(msg["_id"])
        msg["timestamp"] = msg["timestamp"].isoformat()
    return jsonify(messages), 200

@app.route('/chat/mark_read', methods=['POST'])
def mark_as_read():
    data = request.json
    try:
        user_id = ObjectId(data["user_id"])
        room_id = ObjectId(data["room_id"])
    except:
        return jsonify({"error": "Invalid IDs"}), 400

    reads_col.update_one(
        {"user_id": user_id, "room_id": room_id},
        {"$set": {"last_read_timestamp": datetime.now(timezone.utc)}}, upsert=True
    )
    return jsonify({"status": "read"}), 200

if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    logging.getLogger('socketio').setLevel(logging.ERROR)
    logging.getLogger('engineio').setLevel(logging.ERROR)
    print("Starting production server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080)
