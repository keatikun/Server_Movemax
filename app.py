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

try:
    import redis
    socketio = SocketIO(app, cors_allowed_origins="*", message_queue='redis://localhost:6379')
except ImportError:
    socketio = SocketIO(app, cors_allowed_origins="*")

client = MongoClient(MONGO_URI)
db = client["Movemax"]

admins_col = db["admins"]
users_col = db["users"]
chats_col = db["chats"]
messages_col = db["messages"]

connected_users = {}

@app.route('/')
def index():
    return "Production Chat Server Running!"

def get_unread_counts_for_user(user_id):
    pipeline = [
        {"$match": {"to": user_id, "is_read": False}},
        {"$group": {"_id": "$from", "count": {"$sum": 1}}}
    ]
    result = list(messages_col.aggregate(pipeline))
    return {entry["_id"]: entry["count"] for entry in result}

@app.route('/api/users', methods=['GET'])
def get_users():
    current_user = request.args.get("user_id")
    users = list(users_col.find({}, {"_id": 0, "username": 1, "name": 1, "is_online": 1}))
    if current_user:
        unread_map = get_unread_counts_for_user(current_user)
        for user in users:
            user['unread_count'] = unread_map.get(user["username"], 0)
    else:
        for user in users:
            user['unread_count'] = 0
    return jsonify(users), 200

@app.route('/api/admins', methods=['GET'])
def get_admins():
    current_user = request.args.get("user_id")
    admins = list(admins_col.find({}, {"_id": 0, "username": 1, "name": 1, "is_online": 1}))
    if current_user:
        unread_map = get_unread_counts_for_user(current_user)
        for admin in admins:
            admin['unread_count'] = unread_map.get(admin["username"], 0)
    else:
        for admin in admins:
            admin['unread_count'] = 0
    return jsonify(admins), 200

# เพิ่ม API นี้ เพื่อแก้ปัญหา 404
@app.route('/api/admins_with_unread', methods=['GET'])
def get_admins_with_unread():
    current_user = request.args.get("user_id")
    admins = list(admins_col.find({}, {"_id": 0, "username": 1, "name": 1, "is_online": 1}))
    if current_user:
        unread_map = get_unread_counts_for_user(current_user)
        for admin in admins:
            admin['unread_count'] = unread_map.get(admin["username"], 0)
    else:
        for admin in admins:
            admin['unread_count'] = 0
    return jsonify(admins), 200

@app.route('/chat/<user1>/<user2>', methods=['GET'])
def get_chat_history(user1, user2):
    messages = list(messages_col.find({
        "$or": [
            {"from": user1, "to": user2},
            {"from": user2, "to": user1}
        ]
    }).sort("timestamp", 1))

    for msg in messages:
        msg["_id"] = str(msg["_id"])
        if isinstance(msg.get("timestamp"), datetime):
            msg["timestamp"] = msg["timestamp"].isoformat()

    return jsonify({"messages": messages}), 200

@socketio.on('join_user_room')
def join_user_room(data):
    user_id = data.get('userId')
    if user_id:
        join_room(user_id)
        connected_users.setdefault(user_id, set()).add(request.sid)
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
        "is_read": False,
        "is_pinned": False,
        "notifications_enabled": True
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
    socketio.emit('update_unread', to=receiver)

@socketio.on('typing')
def on_typing(data):
    room = "_".join(sorted([data['from'], data['to']]))
    emit('typing', {'from': data['from']}, room=room)

@socketio.on('stop_typing')
def on_stop_typing(data):
    room = "_".join(sorted([data['from'], data['to']]))
    emit('stop_typing', {'from': data['from']}, room=room)

@app.route('/chat/mark_read/<user1>/<user2>', methods=['POST'])
def mark_as_read(user1, user2):
    result = messages_col.update_many(
        {"from": user2, "to": user1, "is_read": False},
        {"$set": {"is_read": True}}
    )
    socketio.emit('update_unread', to=user1)
    socketio.emit('update_unread', to=user2)
    return jsonify({"marked_as_read": result.modified_count}), 200

@app.route('/chat/unread_count/<user_id>', methods=['GET'])
def get_unread_count(user_id):
    pipeline = [
        {"$match": {"to": user_id, "is_read": False}},
        {"$group": {"_id": "$from", "count": {"$sum": 1}}}
    ]
    result = list(messages_col.aggregate(pipeline))
    return jsonify({"unread_counts": result}), 200

@app.route('/chat/last_messages/<user_id>', methods=['GET'])
def get_last_messages(user_id):
    messages = list(messages_col.find({"$or": [{"from": user_id}, {"to": user_id}]}).sort("timestamp", -1))
    last_msgs = {}
    for msg in messages:
        other = msg['to'] if msg['from'] == user_id else msg['from']
        if other not in last_msgs:
            msg['_id'] = str(msg['_id'])
            last_msgs[other] = msg
    return jsonify({"last_messages": list(last_msgs.values())}), 200

@app.route('/chat/pin_message/<message_id>', methods=['POST'])
def pin_message(message_id):
    try:
        messages_col.update_one({"_id": ObjectId(message_id)}, {"$set": {"is_pinned": True}})
        return jsonify({"status": "pinned"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/chat/unpin_message/<message_id>', methods=['POST'])
def unpin_message(message_id):
    try:
        messages_col.update_one({"_id": ObjectId(message_id)}, {"$set": {"is_pinned": False}})
        return jsonify({"status": "unpinned"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/chat/delete_chat/<user1>/<user2>', methods=['DELETE'])
def delete_chat(user1, user2):
    result = messages_col.delete_many({
        "$or": [
            {"from": user1, "to": user2},
            {"from": user2, "to": user1}
        ]
    })
    return jsonify({"deleted": result.deleted_count}), 200

@app.route('/chat/set_notifications', methods=['POST'])
def set_notifications():
    data = request.json
    sender = data['from']
    receiver = data['to']
    enabled = data['enabled']
    messages_col.update_many({"from": sender, "to": receiver}, {"$set": {"notifications_enabled": enabled}})
    return jsonify({"status": "updated"}), 200

if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    logging.getLogger('socketio').setLevel(logging.ERROR)
    logging.getLogger('engineio').setLevel(logging.ERROR)
    print("Starting production server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080)
