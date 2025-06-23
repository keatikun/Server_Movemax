from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room # Added leave_room for completeness
from pymongo import MongoClient
from datetime import datetime, timezone
from flask_cors import CORS
from config import MONGO_URI, SECRET_KEY # Assuming config.py exists and works
from bson.objectid import ObjectId
import logging

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app, resources={r"/*": {"origins": "*"}}) # Ensure CORS allows all origins for development

socketio = SocketIO(app, cors_allowed_origins="*", logger=True, engineio_logger=True) # Enable logging for Socket.IO

client = MongoClient(MONGO_URI)
db = client["Movemax"]

users_col = db["users"]
admins_col = db["admins"]
messages_col = db["messages"]
rooms_col = db["rooms"]
reads_col = db["user_room_reads"]
user_status_col = db["user_status"]

# Helper function to convert ObjectId and datetime objects to string for JSON serialization
def serialize_doc_for_json(doc):
    if doc is None:
        return None
    # Create a copy to avoid modifying the original document from PyMongo result
    serialized_doc = doc.copy()
    
    # Convert _id to string
    if '_id' in serialized_doc and isinstance(serialized_doc['_id'], ObjectId):
        serialized_doc['_id'] = str(serialized_doc['_id'])
    
    # Recursively handle nested ObjectIds if any (e.g., in members list of room)
    if 'members' in serialized_doc and isinstance(serialized_doc['members'], list):
        for i, member in enumerate(serialized_doc['members']):
            if 'id' in member and isinstance(member['id'], ObjectId):
                serialized_doc['members'][i]['id'] = str(member['id'])

    # Convert datetime objects to ISO format string
    for key, value in serialized_doc.items():
        if isinstance(value, datetime):
            serialized_doc[key] = value.isoformat() + 'Z' # 'Z' indicates UTC time
        elif isinstance(value, ObjectId): # Handle any remaining ObjectIds
            serialized_doc[key] = str(value)
            
    return serialized_doc

# Configure logging for Flask app
app.logger.setLevel(logging.INFO) # Set desired logging level
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)


@app.route('/')
def index():
    return "Chat Server Running!"

@app.route('/api/users', methods=['GET'])
def get_users():
    app.logger.info("Fetching all users.")
    users = list(users_col.find({}, {"password": 0}))
    return jsonify([serialize_doc_for_json(u) for u in users]), 200

@app.route('/api/admins', methods=['GET'])
def get_admins():
    app.logger.info("Fetching all admins.")
    admins = list(admins_col.find({}, {"password": 0}))
    return jsonify([serialize_doc_for_json(a) for a in admins]), 200

@app.route('/api/unread_counts/<user_id>', methods=['GET'])
def get_unread_counts(user_id):
    app.logger.info(f"Fetching unread counts for user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e: # Catch specific exception for better error message
        app.logger.error(f"Invalid user_id format for unread counts: {user_id}, Error: {e}")
        return jsonify({"error": "Invalid user_id format"}), 400

    unread_counts = {}
    
    # Find rooms where this user is a member
    user_rooms = rooms_col.find({
        "members.id": user_obj_id # Query directly by ObjectId in members array
    })

    for room in user_rooms:
        room_id_str = str(room["_id"])
        
        # Get last_read_timestamp for this user in this room
        read_doc = reads_col.find_one(
            {"user_id": user_obj_id, "room_id": room["_id"]}
        )
        
        last_read = read_doc.get("last_read_timestamp", datetime.min.replace(tzinfo=timezone.utc)) if read_doc else datetime.min.replace(tzinfo=timezone.utc)
        
        # Count messages in this room since last_read
        count = messages_col.count_documents({
            "room_id": room["_id"],
            "timestamp": {"$gt": last_read}
        })
        unread_counts[room_id_str] = count

    return jsonify(unread_counts), 200

@app.route('/api/user_status/<user_id>', methods=['GET'])
def get_user_status(user_id):
    app.logger.info(f"Fetching status for user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"Invalid user_id format for status: {user_id}, Error: {e}")
        return jsonify({"error": "Invalid user_id format"}), 400

    status = user_status_col.find_one({"user_id": user_obj_id})
    if not status:
        app.logger.info(f"No status found for user {user_id}, returning default offline.")
        return jsonify({"user_id": user_id, "is_online": False, "last_active": None}), 200
    
    # Serialize the status document
    serialized_status = serialize_doc_for_json(status)
    return jsonify(serialized_status), 200

# Endpoint to get or create a chat room between two users
@app.route('/chat_room', methods=['GET'])
def get_or_create_room():
    app.logger.info("Attempting to get or create chat room.")
    try:
        user1_id = request.args.get("user1")
        user2_id = request.args.get("user2")
        role1 = request.args.get("role1")
        role2 = request.args.get("role2")

        if not all([user1_id, user2_id, role1, role2]):
            raise ValueError("Missing parameters")

        # Convert user IDs to ObjectId for member list consistency
        user1_obj_id = ObjectId(user1_id)
        user2_obj_id = ObjectId(user2_id)

        # Create room_key based on sorted user IDs (important for consistency)
        # Use ObjectId directly in the key or ensure consistent string representation
        # For simplicity, let's use the provided string IDs for room_key to match current Flutter logic.
        ids = sorted([
            f"{user1_id}_{role1}",
            f"{user2_id}_{role2}"
        ])
        room_key = "|".join(ids)

        room = rooms_col.find_one({"room_key": room_key})
        if room:
            app.logger.info(f"Found existing room: {str(room['_id'])}")
            return jsonify({"room_id": str(room["_id"])})

        # If room doesn't exist, create a new one
        members = [
            {"id": user1_obj_id, "type": role1}, # Store ObjectId in members
            {"id": user2_obj_id, "type": role2}
        ]

        new_room = {
            "type": "private",
            "room_key": room_key,
            "members": members,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)
        }
        result = rooms_col.insert_one(new_room)
        app.logger.info(f"Created new room: {str(result.inserted_id)}")
        return jsonify({"room_id": str(result.inserted_id)})

    except Exception as e:
        app.logger.error(f"Error in get_or_create_room: {e}", exc_info=True) # Log full traceback
        return jsonify({"error": f"Failed to get/create room: {str(e)}"}), 500


# Endpoint to get chat history for a specific room
@app.route('/chat/<room_id>', methods=['GET'])
def get_chat_history(room_id):
    app.logger.info(f"Fetching chat history for room_id: {room_id}")
    try:
        room_obj_id = ObjectId(room_id)
    except Exception as e:
        app.logger.error(f"Invalid room_id format for chat history: {room_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid room_id format"}), 400

    try:
        # Find messages for the given room_id, sorted by timestamp
        messages = list(messages_col.find({"room_id": room_obj_id}).sort("timestamp", 1))
        
        # Serialize each message document to convert ObjectIds and datetimes to strings
        serialized_messages = [serialize_doc_for_json(msg) for msg in messages]
        
        app.logger.info(f"Successfully fetched {len(serialized_messages)} messages for room {room_id}.")
        return jsonify(serialized_messages), 200
    except Exception as e:
        app.logger.error(f"Error fetching chat history for room {room_id}: {e}", exc_info=True) # Log full traceback
        return jsonify({"error": f"Internal Server Error: {e}"}), 500


@app.route('/chat/mark_read', methods=['POST'])
def mark_as_read():
    app.logger.info("Marking messages as read.")
    data = request.json
    try:
        user_id = ObjectId(data["user_id"])
        room_id = ObjectId(data["room_id"])
    except Exception as e:
        app.logger.error(f"Invalid IDs for mark_read: {data}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid IDs"}), 400

    try:
        reads_col.update_one(
            {"user_id": user_id, "room_id": room_id},
            {"$set": {"last_read_timestamp": datetime.now(timezone.utc)}}, upsert=True
        )
        app.logger.info(f"User {user_id} marked room {room_id} as read.")
        return jsonify({"status": "read"}), 200
    except Exception as e:
        app.logger.error(f"Error marking messages as read: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500

# Socket.IO event handlers
@socketio.on('connect')
def handle_connect():
    app.logger.info(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def on_disconnect():
    app.logger.info(f"Client disconnected: {request.sid}")
    # Consider updating user status to offline here (more robust logic needed)
    # For now, relying on explicit 'join_user_room' and the Flutter app's _fetchData to update status
    
@socketio.on('join_user_room')
def join_user_room(data):
    user_id_str = data.get('userId')
    if user_id_str:
        # Each user joins a room named after their string user ID
        # This allows sending specific messages to individual users (e.g., status updates, new messages)
        join_room(user_id_str) 
        app.logger.info(f"User {user_id_str} joined their personal Socket.IO room.")
        
        try:
            user_obj_id = ObjectId(user_id_str)
            user_status_col.update_one(
                {"user_id": user_obj_id},
                {"$set": {"is_online": True, "last_active": datetime.now(timezone.utc)}},
                upsert=True
            )
            
            # Broadcast status change to ALL connected clients (so everyone can see who is online/offline)
            # Fetch the updated status document to ensure all fields are correct
            updated_status = user_status_col.find_one({"user_id": user_obj_id})
            if updated_status:
                serialized_status = serialize_doc_for_json(updated_status)
                socketio.emit('user_status_changed', serialized_status, broadcast=True)
                app.logger.info(f"Broadcasted user {user_id_str} online status: {serialized_status}")
            
        except Exception as e:
            app.logger.error(f"Error updating user status or broadcasting: {e}", exc_info=True)

@socketio.on('send_message')
def on_send_message(data):
    app.logger.info(f"Received send_message: {data}")
    room_id_str = data.get("room_id")
    sender_id_str = data.get("sender_id")
    sender_type = data.get("sender_type")
    message_content = data.get("message")
    message_type = data.get("type", "text")

    if not all([room_id_str, sender_id_str, message_content, sender_type]):
        app.logger.error("Missing data for send_message")
        return

    try:
        room_obj_id = ObjectId(room_id_str)
        sender_obj_id = ObjectId(sender_id_str)
    except Exception as e:
        app.logger.error(f"Invalid ObjectId in send_message data: {e}", exc_info=True)
        return

    new_message = {
        "room_id": room_obj_id,
        "sender_id": sender_obj_id,
        "sender_type": sender_type,
        "message": message_content,
        "type": message_type,
        "timestamp": datetime.now(timezone.utc)
    }
    
    try:
        result = messages_col.insert_one(new_message)
        # Fetch the inserted message to ensure all fields (like _id) are populated
        inserted_message = messages_col.find_one({"_id": result.inserted_id})
        
        # Update room's updated_at timestamp (good practice for ordering rooms)
        rooms_col.update_one(
            {"_id": room_obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc)}}
        )

        # Serialize the message for sending via socket (convert ObjectIds and datetimes)
        serialized_msg = serialize_doc_for_json(inserted_message)
        
        # Get room members to send messages to their individual rooms
        room_doc = rooms_col.find_one({"_id": room_obj_id})
        if room_doc and 'members' in room_doc:
            for member in room_doc['members']:
                member_id_obj = member['id'] # This is an ObjectId from the DB
                member_id_str = str(member_id_obj) # Convert to string for Socket.IO room name

                # Send 'receive_message' to the target user's personal room
                # This is for clients currently in the specific chat room
                socketio.emit('receive_message', serialized_msg, room=member_id_str)
                app.logger.info(f"Emitted 'receive_message' for msg {serialized_msg['_id']} to room {member_id_str}")

                # Send 'new_message' for updating unread counts or chat list previews
                # Only send if the receiver is not the sender
                if member_id_str != sender_id_str:
                    socketio.emit('new_message', serialized_msg, room=member_id_str)
                    app.logger.info(f"Emitted 'new_message' for msg {serialized_msg['_id']} to room {member_id_str} (unread for {member_id_str})")

                    # Increment unread count for the receiver
                    reads_col.update_one(
                        {"user_id": member_id_obj, "room_id": room_obj_id},
                        {"$inc": {"unread_count": 1}, "$set": {"last_read_timestamp": datetime.now(timezone.utc)}}, # Update last_read also for sender
                        upsert=True
                    )
                    app.logger.info(f"Incremented unread count for user {member_id_str} in room {room_id_str}")
        else:
            app.logger.warning(f"Room {room_id_str} not found or no members for message broadcast.")

    except Exception as e:
        app.logger.error(f"Error sending message or broadcasting: {e}", exc_info=True)


@socketio.on('typing')
def on_typing(data):
    room_id_str = data.get("room_id")
    user_id_str = data.get("user_id")
    if room_id_str and user_id_str:
        try:
            room_obj_id = ObjectId(room_id_str)
            room_doc = rooms_col.find_one({"_id": room_obj_id})
            if room_doc and 'members' in room_doc:
                for member in room_doc['members']:
                    member_id_str = str(member['id']) # Convert ObjectId to string for room name
                    if member_id_str != user_id_str: # Emit to the other member in the room
                        emit('typing', {'user_id': user_id_str}, room=member_id_str)
                        app.logger.info(f"Typing: Emitted from {user_id_str} to {member_id_str} in room {room_id_str}")
        except Exception as e:
            app.logger.error(f"Error handling typing event: {e}", exc_info=True)


@socketio.on('stop_typing')
def on_stop_typing(data):
    room_id_str = data.get("room_id")
    user_id_str = data.get("user_id")
    if room_id_str and user_id_str:
        try:
            room_obj_id = ObjectId(room_id_str)
            room_doc = rooms_col.find_one({"_id": room_obj_id})
            if room_doc and 'members' in room_doc:
                for member in room_doc['members']:
                    member_id_str = str(member['id']) # Convert ObjectId to string for room name
                    if member_id_str != user_id_str: # Emit to the other member in the room
                        emit('stop_typing', {'user_id': user_id_str}, room=member_id_str)
                        app.logger.info(f"Stop Typing: Emitted from {user_id_str} to {member_id_str} in room {room_id_str}")
        except Exception as e:
            app.logger.error(f"Error handling stop_typing event: {e}", exc_info=True)

if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    logging.getLogger('socketio').setLevel(logging.INFO) # Set to INFO to see more socket logs
    logging.getLogger('engineio').setLevel(logging.INFO) # Set to INFO to see more engineio logs
    print("Starting production server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080)
