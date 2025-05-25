import eventlet
eventlet.monkey_patch()  # ต้องอยู่บรรทัดบนสุดก่อน import อื่นๆ

from flask import Flask, jsonify, request
from flask_socketio import SocketIO
from flask_cors import CORS
from pymongo import MongoClient
from threading import Thread
import os
import time
from bson.objectid import ObjectId
import json  # ✅ เพิ่มสำหรับ safe_emit

# ====== Flask app =======
app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# MongoDB config
mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

# ✅ Emit อย่างปลอดภัย (แก้ให้ข้อมูล clean)
def safe_emit(event, data):
    try:
        clean_data = json.loads(json.dumps(data, ensure_ascii=False))
        socketio.emit(event, clean_data)
        print(f"[Emit] {event} -> {json.dumps(clean_data, ensure_ascii=False)}")
    except Exception as e:
        print(f"[Emit Error] {event}: {e}")

# --- API ดึง users ทั้งหมด ---
@app.route('/users')
def get_users():
    users = list(users_col.find())
    for u in users:
        u['_id'] = str(u['_id'])
    return jsonify(users)

# --- API ดึง messages ทั้งหมด ---
@app.route('/messages')
def get_messages():
    messages = list(messages_col.find())
    for m in messages:
        m['_id'] = str(m['_id'])
    return jsonify(messages)

# --- API ดึง Chat 1:1 ---
@app.route('/chat-messages')
def get_chat_messages():
    try:
        user_id = int(request.args.get('userId'))
        contact_id = int(request.args.get('contactId'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid or missing userId/contactId'}), 400

    user_doc = messages_col.find_one({"userId": user_id})
    if not user_doc or 'chats' not in user_doc:
        return jsonify([])

    chat = next((c for c in user_doc['chats'] if c['contactId'] == contact_id), None)
    if not chat:
        return jsonify([])

    return jsonify(chat)

# ---  chat list ของ userId ที่ส่งมา --- #
@app.route('/chat-list')
def get_chat_list():
    try:
        user_id = int(request.args.get('userId'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid or missing userId'}), 400

    user_doc = messages_col.find_one({"userId": user_id})
    if not user_doc or 'chats' not in user_doc:
        return jsonify([])  # ไม่มี chat list

    # chats คือ list ของ chat ทั้งหมดของ user นี้
    chat_list = user_doc['chats']

    # แปลง ObjectId ให้เป็น string ถ้ามี (ในกรณีเก็บ ObjectId)
    for chat in chat_list:
        if '_id' in chat:
            chat['_id'] = str(chat['_id'])

    return jsonify(chat_list)


# --- เพิ่มข้อความใหม่ ---
@app.route('/messages', methods=['POST'])
def add_message():
    data = request.json
    if not data or not all(k in data for k in ('senderId', 'receiverId', 'text')):
        return jsonify({'error': 'Missing fields'}), 400

    new_msg = {
        'senderId': int(data['senderId']),
        'receiverId': int(data['receiverId']),
        'text': data['text'],
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'isRead': False
    }
    result = messages_col.insert_one(new_msg)
    new_msg['_id'] = str(result.inserted_id)

    safe_emit('messages_update', new_msg)
    return jsonify(new_msg), 201

# --- อัปเดตข้อความ ---
@app.route('/messages/<msg_id>', methods=['PUT'])
def update_message(msg_id):
    data = request.json
    if not data:
        return jsonify({'error': 'No data to update'}), 400

    try:
        obj_id = ObjectId(msg_id)
    except Exception:
        return jsonify({'error': 'Invalid message ID'}), 400

    update_data = {}
    if 'text' in data:
        update_data['text'] = data['text']
    if 'isRead' in data:
        update_data['isRead'] = data['isRead']

    if not update_data:
        return jsonify({'error': 'No valid fields to update'}), 400

    result = messages_col.find_one_and_update(
        {'_id': obj_id},
        {'$set': update_data},
        return_document=True
    )

    if not result:
        return jsonify({'error': 'Message not found'}), 404

    result['_id'] = str(result['_id'])
    safe_emit('messages_update', result)
    return jsonify(result)

# --- ลบข้อความ ---
@app.route('/messages/<msg_id>', methods=['DELETE'])
def delete_message(msg_id):
    try:
        obj_id = ObjectId(msg_id)
    except Exception:
        return jsonify({'error': 'Invalid message ID'}), 400

    result = messages_col.delete_one({'_id': obj_id})
    if result.deleted_count == 0:
        return jsonify({'error': 'Message not found'}), 404

    safe_emit('messages_delete', {'_id': msg_id})
    return jsonify({'result': 'Message deleted'}), 200

# --- MongoDB Change Stream watcher for users ---
def watch_users_changes():
    with app.app_context():
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
            watch_users_changes()

# --- MongoDB Change Stream watcher for messages ---
def watch_messages_changes():
    with app.app_context():
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
            watch_messages_changes()

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
