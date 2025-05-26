import os
from flask import Flask, request, jsonify
from flask_pymongo import PyMongo
from flask_socketio import SocketIO, emit, join_room
from bson import ObjectId
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
app.config['MONGO_URI'] = 'mongodb://localhost:27017/chatdb'
app.config['UPLOAD_FOLDER'] = 'uploads'
mongo = PyMongo(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# --------------------------
# Upload File API
# --------------------------
@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files['file']
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    # Return file URL for message use
    return jsonify({
        "fileUrl": f"https://yourdomain.com/uploads/{filename}",
        "fileName": filename,
        "fileSize": os.path.getsize(file_path)
    })

# --------------------------
# Join Room
# --------------------------
@socketio.on('join')
def handle_join(data):
    user_id = data['userId']
    conversation_id = data['conversationId']
    join_room(conversation_id)
    emit('status', f'User {user_id} joined room {conversation_id}', to=conversation_id)

# --------------------------
# Send Message (text / file / image)
# --------------------------
@socketio.on('send_message')
def handle_send_message(data):
    conversation_id = data['conversationId']
    sender_id = data['senderId']
    message_type = data.get('messageType', 'text')
    text = data.get('text', '')
    file_url = data.get('fileUrl')
    file_name = data.get('fileName')
    file_size = data.get('fileSize')
    timestamp = datetime.utcnow()

    message = {
        "conversationId": ObjectId(conversation_id),
        "senderId": sender_id,
        "messageType": message_type,
        "text": text,
        "fileUrl": file_url,
        "fileName": file_name,
        "fileSize": file_size,
        "sentAt": timestamp,
        "isRead": False,
        "isDeleted": False
    }

    result = mongo.db.messages.insert_one(message)

    # อัปเดต last message
    mongo.db.conversations.update_one(
        {"_id": ObjectId(conversation_id)},
        {
            "$set": {
                "lastMessage": {
                    "text": text or file_name,
                    "senderId": sender_id,
                    "timestamp": timestamp,
                    "isRead": False
                },
                "updatedAt": timestamp
            }
        }
    )

    message["_id"] = str(result.inserted_id)
    message["sentAt"] = timestamp.isoformat()
    emit("new_message", message, to=conversation_id)

# --------------------------
# Get Message History
# --------------------------
@app.route("/messages/<conversation_id>")
def get_messages(conversation_id):
    messages = list(mongo.db.messages.find({
        "conversationId": ObjectId(conversation_id)
    }).sort("sentAt", 1))
    for m in messages:
        m["_id"] = str(m["_id"])
        m["conversationId"] = str(m["conversationId"])
    return jsonify(messages)

# --------------------------
# Start App
# --------------------------
if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    socketio.run(app, host="0.0.0.0", port=5000)
