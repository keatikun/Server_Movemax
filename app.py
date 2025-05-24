from flask import Flask, request, jsonify
from flask_socketio import SocketIO
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
from threading import Thread

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(mongo_uri)
db = client["Movemax"]
users_col = db["User"]
messages_col = db["Messages"]

@app.route('/users', methods=['GET'])
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

@app.route('/add_chat', methods=['POST'])
def add_chat():
    data = request.json
    user_id = data.get('userId')
    new_chat = data.get('chat')
    try:
        users_col.update_one(
            {'_id': ObjectId(user_id)},
            {'$push': {'chats': new_chat}}
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/update_chat', methods=['POST'])
def update_chat():
    data = request.json
    user_id = data.get('userId')
    index = data.get('chatIndex')
    new_text = data.get('newText')
    is_typing = data.get('isTyping')
    try:
        users_col.update_one(
            {'_id': ObjectId(user_id)},
            {
                '$set': {
                    f'chats.{index}.lastMessage.text': new_text,
                    f'chats.{index}.isTyping': is_typing
                }
            }
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/delete_chat', methods=['POST'])
def delete_chat():
    data = request.json
    user_id = data.get('userId')
    index = data.get('chatIndex')
    try:
        user = users_col.find_one({'_id': ObjectId(user_id)})
        if user and 'chats' in user and len(user['chats']) > index:
            user['chats'].pop(index)
            users_col.update_one(
                {'_id': ObjectId(user_id)},
                {'$set': {'chats': user['chats']}}
            )
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Invalid index'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# Change Stream to broadcast real-time updates
def watch_changes():
    with users_col.watch() as stream:
        for change in stream:
            full_doc = change.get("fullDocument")
            if full_doc and "userId" in full_doc:
                full_doc['_id'] = str(full_doc['_id'])
                socketio.emit('chat_update', full_doc)

@socketio.on('connect')
def on_connect():
    print("🟢 WebSocket client connected")

if __name__ == '__main__':
    watcher_thread = Thread(target=watch_changes)
    watcher_thread.daemon = True
    watcher_thread.start()
    socketio.run(app, host='0.0.0.0', port=8080)
