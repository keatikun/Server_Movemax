from flask import Flask, jsonify, request
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

@app.route('/messages', methods=['GET'])
def get_messages():
    messages = list(messages_col.find())
    for msg in messages:
        msg['_id'] = str(msg['_id'])
    return jsonify(messages)

@app.route('/messages', methods=['POST'])
def add_message():
    data = request.json
    try:
        result = messages_col.insert_one(data)
        return jsonify({'success': True, 'inserted_id': str(result.inserted_id)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/messages/<message_id>', methods=['PUT'])
def update_message(message_id):
    data = request.json
    try:
        result = messages_col.update_one(
            {'_id': ObjectId(message_id)},
            {'$set': data}
        )
        return jsonify({'success': result.modified_count > 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/messages/<message_id>', methods=['DELETE'])
def delete_message(message_id):
    try:
        result = messages_col.delete_one({'_id': ObjectId(message_id)})
        return jsonify({'success': result.deleted_count > 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

def watch_changes():
    with messages_col.watch() as stream:
        for change in stream:
            full_doc = change.get('fullDocument')
            if full_doc:
                full_doc['_id'] = str(full_doc['_id'])
                socketio.emit('message_update', full_doc)

@socketio.on('connect')
def on_connect():
    print("Client connected")

if __name__ == "__main__":
    watcher_thread = Thread(target=watch_changes)
    watcher_thread.daemon = True
    watcher_thread.start()
    socketio.run(app, host="0.0.0.0", port=8080)
