from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from pymongo import MongoClient
from datetime import datetime, timezone
from flask_cors import CORS
from config import MONGO_URI, SECRET_KEY
from bson.objectid import ObjectId
import logging

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app, resources={r"/*": {"origins": "*"}}) # Allow all origins for development

socketio = SocketIO(app, cors_allowed_origins="*", logger=True, engineio_logger=True) # Enable logging for Socket.IO

client = MongoClient(MONGO_URI)
db = client["Movemax"]

users_col = db["users"]
admins_col = db["admins"]
messages_col = db["messages"]
rooms_col = db["rooms"]
reads_col = db["user_room_reads"]
user_status_col = db["user_status"]

# Global mappings for efficient Socket.IO session management
# Stores which SIDs belong to a user_id (a user might have multiple connected devices/tabs)
connected_users_sessions = {} # {user_id: {sid1, sid2, ...}}
# Stores which user_id corresponds to a SID (for quick lookup on disconnect)
sid_to_user_id = {} # {sid: user_id}

# Helper function to convert ObjectId and datetime objects to string for JSON serialization
# This ensures that PyMongo's BSON types are converted to JSON-compatible strings.
def serialize_doc_for_json(doc):
    if doc is None:
        return None
    # Create a copy to avoid modifying the original document from PyMongo result
    serialized_doc = doc.copy()
    
    # Convert _id to string
    if '_id' in serialized_doc and isinstance(serialized_doc['_id'], ObjectId):
        serialized_doc['_id'] = str(serialized_doc['_id'])
    
    # Handle nested ObjectIds in lists (e.g., 'members' in room document)
    if 'members' in serialized_doc and isinstance(serialized_doc['members'], list):
        for i, member in enumerate(serialized_doc['members']):
            if 'id' in member and isinstance(member['id'], ObjectId):
                serialized_doc['members'][i]['id'] = str(member['id'])

    # Convert datetime objects to ISO format string (e.g., for 'timestamp', 'created_at', 'updated_at', 'last_active')
    for key, value in serialized_doc.items():
        if isinstance(value, datetime):
            serialized_doc[key] = value.isoformat() + 'Z' # 'Z' indicates UTC time
        elif isinstance(value, ObjectId): # Catch any remaining ObjectIds not explicitly handled above
            serialized_doc[key] = str(value)
            
    return serialized_doc

# Configure logging for Flask app
app.logger.setLevel(logging.INFO) # Set desired logging level for Flask app
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)

# --- MongoDB Indexing ---
# This function creates necessary indexes on application startup.
# Indexes are crucial for database query performance, especially with a large number of users/messages.
def create_mongo_indexes():
    app.logger.info("MongoDB: Checking and creating indexes...")
    try:
        # Index for faster user lookups by _id and username
        users_col.create_index([("_id", 1)], unique=True)
        users_col.create_index([("username", 1)], unique=True)

        # Index for faster admin lookups
        admins_col.create_index([("_id", 1)], unique=True)
        admins_col.create_index([("username", 1)], unique=True)

        # Index for fast room lookup by room_key
        rooms_col.create_index([("room_key", 1)], unique=True)
        # Index for efficient lookup of rooms a user is a member of
        rooms_col.create_index([("members.id", 1)])
        # Index for sorting rooms by last updated (useful for chat list)
        rooms_col.create_index([("updated_at", -1)])

        # Indexes for message history and unread counts
        messages_col.create_index([("room_id", 1)]) # Efficiently find messages for a room
        messages_col.create_index([("timestamp", 1)]) # Efficiently sort messages by time
        messages_col.create_index([("room_id", 1), ("timestamp", 1)]) # Compound index for chat history query

        # Indexes for unread reads collection
        reads_col.create_index([("user_id", 1), ("room_id", 1)], unique=True)
        # Index to sort by last_read_timestamp
        reads_col.create_index([("last_read_timestamp", 1)])


        # Index for user status lookups
        user_status_col.create_index([("user_id", 1)], unique=True)
        user_status_col.create_index([("is_online", 1)]) # For quick lookup of online users

        app.logger.info("MongoDB: All indexes checked/created successfully.")
    except Exception as e:
        app.logger.error(f"MongoDB Error: Failed to create indexes: {e}", exc_info=True)

# Call index creation on application startup
with app.app_context():
    create_mongo_indexes()


@app.route('/')
def index():
    return "Chat Server Running!"

@app.route('/api/users', methods=['GET'])
def get_users():
    app.logger.info("API: Fetching all users.")
    users = list(users_col.find({}, {"password": 0}))
    return jsonify([serialize_doc_for_json(u) for u in users]), 200

@app.route('/api/admins', methods=['GET'])
def get_admins():
    app.logger.info("API: Fetching all admins.")
    admins = list(admins_col.find({}, {"password": 0}))
    return jsonify([serialize_doc_for_json(a) for a in admins]), 200

@app.route('/api/unread_counts/<user_id>', methods=['GET'])
def get_unread_counts(user_id):
    app.logger.info(f"API: Fetching unread counts for user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid user_id format for unread counts: {user_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid user_id format"}), 400

    unread_counts = {}
    
    # --- OPTIMIZATION: Using MongoDB Aggregation Pipeline for efficient unread count calculation ---
    # This pipeline directly calculates unread counts for all rooms a user is a member of.
    pipeline = [
        # Stage 1: Match documents in user_room_reads for the specific user
        {"$match": {"user_id": user_obj_id}},
        # Stage 2: Join with 'rooms' collection to get room details
        {"$lookup": {
            "from": "rooms",
            "localField": "room_id",
            "foreignField": "_id",
            "as": "room_info"
        }},
        # Stage 3: Deconstruct the room_info array (should only have one element)
        {"$unwind": "$room_info"}, 
        # Stage 4: Lookup messages for each room, considering the last_read_timestamp
        {"$lookup": {
            "from": "messages",
            "let": {"roomId": "$room_id", "lastRead": "$last_read_timestamp"},
            "pipeline": [
                {"$match": {
                    "$expr": { # Use $expr for aggregation pipeline expressions
                        "$and": [
                            {"$eq": ["$room_id", "$$roomId"]}, # Match messages for the room
                            {"$gt": ["$timestamp", "$$lastRead"]} # Only count messages newer than last_read
                        ]
                    }
                }},
                {"$count": "unreadMessages"} # Count the matched unread messages
            ],
            "as": "unread_messages_count_result" # Name the output field for this lookup
        }},
        # Stage 5: Deconstruct unread_messages_count_result, preserving rooms with no unread messages
        {"$unwind": {"path": "$unread_messages_count_result", "preserveNullAndEmptyArrays": True}},
        # Stage 6: Project the final desired fields
        {"$project": {
            "_id": 0, # Exclude default _id
            "room_id": {"$toString": "$room_id"}, # Convert ObjectId to string for the key
            "unread_count": {"$ifNull": ["$unread_messages_count_result.unreadMessages", 0]} # Default to 0 if no unread messages
        }}
    ]

    try:
        results = list(reads_col.aggregate(pipeline))
        for res in results:
            unread_counts[res["room_id"]] = res["unread_count"]
        
        # Additional step for rooms that don't have an entry in 'user_room_reads' yet.
        # This assumes all messages in such rooms are unread for that user.
        # This part still involves multiple queries, but should be less frequent than the main loop.
        user_rooms_not_in_reads = rooms_col.find({
            "members.id": user_obj_id,
            "_id": {"$nin": [ObjectId(rid) for rid in unread_counts.keys()]} # Exclude rooms already processed
        })
        for room in user_rooms_not_in_reads:
            total_messages_in_room = messages_col.count_documents({"room_id": room["_id"]})
            unread_counts[str(room["_id"])] = total_messages_in_room

        app.logger.info(f"API Success: Fetched unread counts for user {user_id}: {unread_counts}")
        return jsonify(unread_counts), 200

    except Exception as e:
        app.logger.error(f"API Error: Failed to fetch unread counts for user {user_id}: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500


@app.route('/api/user_status/<user_id>', methods=['GET'])
def get_user_status(user_id):
    app.logger.info(f"API: Fetching status for user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid user_id format for status: {user_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid user_id format"}), 400

    status = user_status_col.find_one({"user_id": user_obj_id})
    if not status:
        app.logger.info(f"API Info: No status found for user {user_id}, returning default offline.")
        return jsonify({"user_id": user_id, "is_online": False, "last_active": None}), 200
    
    serialized_status = serialize_doc_for_json(status)
    app.logger.info(f"API Success: Fetched status for user {user_id}: {serialized_status}")
    return jsonify(serialized_status), 200

# Endpoint to get or create a chat room between two users
@app.route('/chat_room', methods=['GET'])
def get_or_create_room():
    app.logger.info("API: Attempting to get or create chat room.")
    try:
        user1_id = request.args.get("user1")
        user2_id = request.args.get("user2")
        role1 = request.args.get("role1")
        role2 = request.args.get("role2")

        if not all([user1_id, user2_id, role1, role2]):
            raise ValueError("Missing parameters")

        # Convert user IDs to ObjectId for member list consistency in DB
        user1_obj_id = ObjectId(user1_id)
        user2_obj_id = ObjectId(user2_id)

        # Create room_key based on sorted user IDs (important for consistent room lookup)
        # Using string IDs for room_key to match current Flutter logic and simplify key creation.
        ids = sorted([
            f"{user1_id}_{role1}",
            f"{user2_id}_{role2}"
        ])
        room_key = "|".join(ids)

        room = rooms_col.find_one({"room_key": room_key})
        if room:
            app.logger.info(f"API Success: Found existing room: {str(room['_id'])}")
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
        app.logger.info(f"API Success: Created new room: {str(result.inserted_id)}")
        return jsonify({"room_id": str(result.inserted_id)})

    except Exception as e:
        app.logger.error(f"API Error: Failed to get/create room: {e}", exc_info=True) # Log full traceback
        return jsonify({"error": f"Failed to get/create room: {str(e)}"}), 500


# Endpoint to get chat history for a specific room
@app.route('/chat/<room_id>', methods=['GET'])
def get_chat_history(room_id):
    app.logger.info(f"API: Fetching chat history for room_id: {room_id}")
    try:
        room_obj_id = ObjectId(room_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid room_id format for chat history: {room_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid room_id format"}), 400

    try:
        # Find messages for the given room_id, sorted by timestamp
        messages = list(messages_col.find({"room_id": room_obj_id}).sort("timestamp", 1))
        
        # Serialize each message document to convert ObjectIds and datetimes to strings
        serialized_messages = [serialize_doc_for_json(msg) for msg in messages]
        
        app.logger.info(f"API Success: Fetched {len(serialized_messages)} messages for room {room_id}.")
        return jsonify(serialized_messages), 200
    except Exception as e:
        app.logger.error(f"API Error: Error fetching chat history for room {room_id}: {e}", exc_info=True) # Log full traceback
        return jsonify({"error": f"Internal Server Error: {e}"}), 500


@app.route('/chat/mark_read', methods=['POST'])
def mark_as_read():
    app.logger.info("API: Marking messages as read.")
    data = request.json
    try:
        user_id = ObjectId(data["user_id"])
        room_id = ObjectId(data["room_id"])
    except Exception as e:
        app.logger.error(f"API Error: Invalid IDs for mark_read: {data}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid IDs"}), 400

    try:
        # Update last_read_timestamp for the user in this room
        # We ensure unread_count is set to 0 when marked as read, or handle it differently if needed.
        reads_col.update_one(
            {"user_id": user_id, "room_id": room_id},
            {"$set": {"last_read_timestamp": datetime.now(timezone.utc), "unread_count": 0}}, # Set unread_count to 0
            upsert=True
        )
        app.logger.info(f"API Success: User {user_id} marked room {room_id} as read.")
        return jsonify({"status": "read"}), 200
    except Exception as e:
        app.logger.error(f"API Error: Error marking messages as read: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500

# Socket.IO event handlers
@socketio.on('connect')
def handle_connect():
    app.logger.info(f"Socket: Client connected: {request.sid}")

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    app.logger.info(f"Socket: Client disconnected: {sid}")

    user_id_str = sid_to_user_id.get(sid) # Efficient lookup
    if user_id_str:
        # Remove sid from the user's session set
        connected_users_sessions.get(user_id_str, set()).discard(sid)
        # Remove sid from the global mapping
        if sid in sid_to_user_id: # Ensure it exists before deleting
            del sid_to_user_id[sid]

        # If no more active sessions for this user, mark as offline
        if not connected_users_sessions.get(user_id_str): # Check if the set is empty
            try:
                user_obj_id = ObjectId(user_id_str)
                user_status_col.update_one(
                    {"user_id": user_obj_id},
                    {"$set": {"is_online": False, "last_active": datetime.now(timezone.utc)}}
                )
                app.logger.info(f"Socket: User {user_id_str} is now offline.")
                
                # Broadcast status change to all clients
                updated_status = user_status_col.find_one({"user_id": user_obj_id})
                if updated_status:
                    serialized_status = serialize_doc_for_json(updated_status)
                    # For 1000+ users, broadcasting to ALL can be a bottleneck.
                    # Consider more targeted emits or client-side polling for status if this becomes a problem.
                    socketio.emit('user_status_changed', serialized_status, broadcast=True)
                    app.logger.info(f"Socket: Broadcasted user {user_id_str} offline status: {serialized_status}")
            except Exception as e:
                app.logger.error(f"Socket Error: Error updating user status on disconnect or broadcasting: {e}", exc_info=True)
    else:
        app.logger.info(f"Socket: Disconnected client {sid} was not a known user session.")


@socketio.on('join_user_room')
def join_user_room(data):
    user_id_str = data.get('userId')
    if user_id_str:
        # Each user joins a room named after their string user ID.
        # This allows sending specific messages to individual users (e.g., status updates, new messages).
        join_room(user_id_str) 
        app.logger.info(f"Socket: User {user_id_str} joined their personal Socket.IO room.")
        
        # Store mapping for quick lookup on disconnect and for multi-device support
        connected_users_sessions.setdefault(user_id_str, set()).add(request.sid)
        sid_to_user_id[request.sid] = user_id_str

        try:
            user_obj_id = ObjectId(user_id_str)
            user_status_col.update_one(
                {"user_id": user_obj_id},
                {"$set": {"is_online": True, "last_active": datetime.now(timezone.utc)}},
                upsert=True
            )
            
            # Broadcast status change to ALL connected clients.
            # This can be a bottleneck for 1000+ users if many users come online/offline frequently.
            # For higher scale, consider targeted emits (e.g., only to friends) or client-side polling.
            updated_status = user_status_col.find_one({"user_id": user_obj_id})
            if updated_status:
                serialized_status = serialize_doc_for_json(updated_status)
                socketio.emit('user_status_changed', serialized_status, broadcast=True)
                app.logger.info(f"Socket: Broadcasted user {user_id_str} online status: {serialized_status}")
            
        except Exception as e:
            app.logger.error(f"Socket Error: Error updating user status or broadcasting from join_user_room: {e}", exc_info=True)

@socketio.on('send_message')
def on_send_message(data):
    app.logger.info(f"Socket: Received send_message: {data}")
    room_id_str = data.get("room_id")
    sender_id_str = data.get("sender_id")
    sender_type = data.get("sender_type")
    message_content = data.get("message")
    message_type = data.get("type", "text")

    if not all([room_id_str, sender_id_str, message_content, sender_type]):
        app.logger.error("Socket Error: Missing data for send_message")
        return

    try:
        room_obj_id = ObjectId(room_id_str)
        sender_obj_id = ObjectId(sender_id_str)
    except Exception as e:
        app.logger.error(f"Socket Error: Invalid ObjectId in send_message data: {e}", exc_info=True)
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
        inserted_message = messages_col.find_one({"_id": result.inserted_id})
        
        # Update room's updated_at timestamp (useful for ordering rooms in chat list)
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

                # Emit 'receive_message' to the target user's personal room
                # This is for clients currently in the specific chat room
                socketio.emit('receive_message', serialized_msg, room=member_id_str)
                app.logger.info(f"Socket: Emitted 'receive_message' for msg {serialized_msg['_id']} to room {member_id_str}")

                # Send 'new_message' for updating unread counts or chat list previews
                # Only send if the receiver is not the sender
                if member_id_str != sender_id_str:
                    socketio.emit('new_message', serialized_msg, room=member_id_str)
                    app.logger.info(f"Socket: Emitted 'new_message' for msg {serialized_msg['_id']} to room {member_id_str} (unread for {member_id_str})")

                    # Increment unread count for the receiver
                    # We also update last_read_timestamp for the sender here (when they send a message)
                    reads_col.update_one(
                        {"user_id": member_id_obj, "room_id": room_obj_id},
                        {"$inc": {"unread_count": 1}},
                        upsert=True
                    )
                    app.logger.info(f"Socket: Incremented unread count for user {member_id_str} in room {room_id_str}")
        else:
            app.logger.warning(f"Socket Warning: Room {room_id_str} not found or no members for message broadcast.")

    except Exception as e:
        app.logger.error(f"Socket Error: Error sending message or broadcasting: {e}", exc_info=True)


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
                    member_id_str = str(member['id'])
                    if member_id_str != user_id_str: # Emit to the other member in the room
                        emit('typing', {'user_id': user_id_str}, room=member_id_str)
                        app.logger.info(f"Socket: Typing event from {user_id_str} to {member_id_str} in room {room_id_str}")
        except Exception as e:
            app.logger.error(f"Socket Error: Error handling typing event: {e}", exc_info=True)


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
                    member_id_str = str(member['id'])
                    if member_id_str != user_id_str: # Emit to the other member in the room
                        emit('stop_typing', {'user_id': user_id_str}, room=member_id_str)
                        app.logger.info(f"Socket: Stop Typing event from {user_id_str} to {member_id_str} in room {room_id_str}")
        except Exception as e:
            app.logger.error(f"Socket Error: Error handling stop_typing event: {e}", exc_info=True)


if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    # Set logging levels for Socket.IO and Engine.IO to INFO for better visibility during development/debugging.
    # For production, consider WARNING or ERROR to reduce log volume.
    logging.getLogger('socketio').setLevel(logging.INFO)
    logging.getLogger('engineio').setLevel(logging.INFO)
    print("Starting production server on http://0.0.0.0:8080")
    # Use Eventlet WSGI server for production deployment with Socket.IO
    # Ensure eventlet is installed (pip install eventlet)
    socketio.run(app, host='0.0.0.0', port=8080)
