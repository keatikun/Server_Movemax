from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from pymongo import MongoClient
from datetime import datetime, timezone
from flask_cors import CORS
from config import MONGO_URI, SECRET_KEY # Assuming config.py exists and works
from bson.objectid import ObjectId
import logging

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app, resources={r"/*": {"origins": "*"}}) # Allow all origins for development

# Set logging levels for Flask app, Socket.IO, and Engine.IO to INFO for production.
app.logger.setLevel(logging.INFO) 
logging.getLogger('socketio').setLevel(logging.INFO)
logging.getLogger('engineio').setLevel(logging.INFO)

handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)

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
            # Ensure timestamp is in UTC ISO format (e.g., '2023-10-27T10:00:00.000Z')
            serialized_doc[key] = value.replace(tzinfo=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        elif isinstance(value, ObjectId): # Catch any remaining ObjectIds not explicitly handled above
            serialized_doc[key] = str(value)
            
    return serialized_doc


# --- MongoDB Indexing ---
# This function creates necessary indexes on application startup.
# Indexes are crucial for database query performance, especially with a large number of users/messages.
def create_mongo_indexes():
    app.logger.info("MongoDB: Checking and creating indexes...")
    try:
        # Index for faster user lookups by _id and username
        users_col.create_index([("_id", 1)]) 
        users_col.create_index([("username", 1)], unique=True)

        # Index for faster admin lookups
        admins_col.create_index([("_id", 1)])
        admins_col.create_index([("username", 1)], unique=True)

        # Index for fast room lookup by room_key
        rooms_col.create_index([("room_key", 1)], unique=True)
        # Index for efficient lookup of rooms a user is a member of
        rooms_col.create_index([("members.id", 1)])
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


# --- Helper function for Optimized Broadcasts of User Status ---
def _notify_related_users_of_status_change(user_id_str, new_status_doc):
    """
    Notifies other users in shared chat rooms about a user's status change.
    Instead of broadcasting to all connected clients, this function
    finds users who share at least one chat room with the changing user
    and emits the status update only to their personal Socket.IO rooms.
    This significantly reduces server load and network traffic for status updates.
    """
    try:
        user_obj_id = ObjectId(user_id_str)
        serialized_status = serialize_doc_for_json(new_status_doc)
        
        # Find all rooms where this user is a member
        rooms_cursor = rooms_col.find({"members.id": user_obj_id})
        
        notified_users = set() # To track which users have already been notified
        notified_users.add(user_id_str) # Do not notify the user themselves

        for room_doc in rooms_cursor:
            app.logger.debug(f"Socket: Processing room {room_doc['_id']} for status change of {user_id_str}")
            if 'members' in room_doc:
                for member in room_doc['members']:
                    member_id_str = str(member['id'])
                    # If the member is not the current user and hasn't been notified yet
                    if member_id_str != user_id_str and member_id_str not in notified_users:
                        # Emit status change to the other member's personal Socket.IO room
                        # Each user client joins their own room (identified by their userId)
                        socketio.emit('user_status_changed', serialized_status, room=member_id_str)
                        app.logger.info(f"Socket: Emitted user_status_changed for {user_id_str} to room {member_id_str}")
                        notified_users.add(member_id_str) # Mark as notified
    except Exception as e:
        app.logger.error(f"Socket Error: Failed to notify related users of status change for {user_id_str}: {e}", exc_info=True)


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

# --- NEW API ENDPOINT FOR LAST MESSAGE OF ALL CONVERSATIONS FOR A GIVEN USER ---
@app.route('/api/last_messages_for_user/<user_id>', methods=['GET'])
def get_last_messages_for_user(user_id):
    app.logger.info(f"API: Fetching last messages for all rooms of user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid user_id format for last messages: {user_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid user_id format"}), 400

    pipeline = [
        # Match rooms where the current user is a member
        {"$match": {"members.id": user_obj_id}},
        # Lookup messages for these rooms
        {"$lookup": {
            "from": "messages",
            "localField": "_id",
            "foreignField": "room_id",
            "as": "room_messages"
        }},
        # Unwind messages to work with each message individually
        {"$unwind": {"path": "$room_messages", "preserveNullAndEmptyArrays": True}},
        # Sort messages by timestamp descending to get the latest first
        {"$sort": {"room_messages.timestamp": -1}},
        # Group by room_id to get the latest message for each room
        {"$group": {
            "_id": "$_id", # Room ID
            "members": {"$first": "$members"}, # Keep members of the room
            "last_message": {"$first": "$room_messages"} # Get the latest message
        }},
        # Project to format the output and find the chat partner
        {"$project": {
            "_id": 0, # Exclude default _id
            "room_id": {"$toString": "$_id"},
            "last_message": "$last_message",
            "chat_partner_id": {
                "$arrayElemAt": [
                    {"$filter": {
                        "input": "$members",
                        "as": "member",
                        "cond": {"$ne": ["$$member.id", user_obj_id]}
                    }},
                    0
                ]
            }
        }},
        # Final projection to get the desired structure: {chat_partner_id: last_message}
        {"$project": {
            "chat_partner_id": {"$toString": "$chat_partner_id.id"}, # Convert partner ObjectId to string
            "last_message": "$last_message"
        }}
    ]

    try:
        results = list(rooms_col.aggregate(pipeline))
        
        # Format the results into a dictionary {chat_partner_id: last_message_data}
        response_data = {}
        for res in results:
            chat_partner_id = res.get('chat_partner_id')
            last_message_doc = res.get('last_message')
            if chat_partner_id: # Only add if chat_partner_id is found (should always be for 1-1 chats)
                response_data[chat_partner_id] = serialize_doc_for_json(last_message_doc)
        
        app.logger.info(f"API Success: Fetched last messages for user {user_id}. Count: {len(response_data)}")
        return jsonify(response_data), 200

    except Exception as e:
        app.logger.error(f"API Error: Failed to fetch last messages for user {user_id}: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500


@app.route('/api/unread_counts/<user_id>', methods=['GET'])
def get_unread_counts(user_id):
    app.logger.info(f"API: Fetching unread counts for user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid user_id format for unread counts: {user_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid user_id format"}), 400

    unread_counts = {}
    
    # Using MongoDB Aggregation Pipeline for efficient unread count calculation
    pipeline = [
        {"$match": {"user_id": user_obj_id}},
        {"$lookup": {
            "from": "rooms",
            "localField": "room_id",
            "foreignField": "_id",
            "as": "room_info"
        }},
        {"$unwind": "$room_info"},
        {"$lookup": {
            "from": "messages",
            "let": {"roomId": "$room_id", "lastRead": "$last_read_timestamp"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$room_id", "$$roomId"]},
                            {"$gt": ["$timestamp", "$$lastRead"]}
                        ]
                    }
                }},
                {"$count": "unreadMessages"}
            ],
            "as": "unread_messages_count_result"
        }},
        {"$unwind": {"path": "$unread_messages_count_result", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": 0,
            "room_id": {"$toString": "$room_info._id"}, # Use room_info._id to map back to the room's actual ID
            "unread_count": {"$ifNull": ["$unread_messages_count_result.unreadMessages", 0]}
        }}
    ]

    try:
        results = list(reads_col.aggregate(pipeline))
        for res in results:
            unread_counts[res["room_id"]] = res["unread_count"]
        
        # Also include rooms where the user is a member but has no read record yet
        # For these rooms, all messages are considered unread if no read record exists
        user_rooms_not_in_reads = rooms_col.find({
            "members.id": user_obj_id,
            "_id": {"$nin": [ObjectId(rid) for rid in unread_counts.keys()]} # Exclude rooms already counted
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

        user1_obj_id = ObjectId(user1_id)
        user2_obj_id = ObjectId(user2_id)

        ids = sorted([
            f"{user1_id}_{role1}",
            f"{user2_id}_{role2}"
        ])
        room_key = "|".join(ids)

        room = rooms_col.find_one({"room_key": room_key})
        if room:
            app.logger.info(f"API Success: Found existing room: {str(room['_id'])}")
            return jsonify({"room_id": str(room["_id"])})

        members = [
            {"id": user1_obj_id, "type": role1},
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
        app.logger.error(f"API Error: Failed to get/create room: {e}", exc_info=True)
        return jsonify({"error": f"Failed to get/create room: {str(e)}"}), 500


@app.route('/chat/<room_id>', methods=['GET'])
def get_chat_history(room_id):
    app.logger.info(f"API: Fetching chat history for room_id: {room_id}")
    try:
        room_obj_id = ObjectId(room_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid room_id format for chat history: {room_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid room_id format"}), 400

    try:
        messages = list(messages_col.find({"room_id": room_obj_id}).sort("timestamp", 1))
        serialized_messages = [serialize_doc_for_json(msg) for msg in messages]
        
        app.logger.info(f"API Success: Fetched {len(serialized_messages)} messages for room {room_id}.")
        return jsonify(serialized_messages), 200
    except Exception as e:
        app.logger.error(f"API Error: Error fetching chat history for room {room_id}: {e}", exc_info=True)
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
        reads_col.update_one(
            {"user_id": user_id, "room_id": room_id},
            {"$set": {"last_read_timestamp": datetime.now(timezone.utc), "unread_count": 0}},
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

    user_id_str = sid_to_user_id.get(sid)
    if user_id_str:
        # Remove SID from the user's connected sessions set
        connected_users_sessions.get(user_id_str, set()).discard(sid)
        # Remove SID from the global SID to user_id mapping
        if sid in sid_to_user_id:
            del sid_to_user_id[sid]

        # If no more active sessions for this user, mark them as offline
        if not connected_users_sessions.get(user_id_str):
            try:
                user_obj_id = ObjectId(user_id_str)
                user_status_col.update_one(
                    {"user_id": user_obj_id},
                    {"$set": {"is_online": False, "last_active": datetime.now(timezone.utc)}}
                )
                app.logger.info(f"Socket: User {user_id_str} is now offline.")
                
                updated_status = user_status_col.find_one({"user_id": user_obj_id})
                if updated_status:
                    # --- Optimized: Call helper to notify only related users instead of broadcasting to all ---
                    _notify_related_users_of_status_change(user_id_str, updated_status)
                    # --- End Optimized ---
            except Exception as e:
                app.logger.error(f"Socket Error: Error updating user status on disconnect or notifying: {e}", exc_info=True)
    else:
        app.logger.info(f"Socket: Disconnected client {sid} was not a known user session.")


@socketio.on('join_user_room')
def join_user_room(data):
    user_id_str = data.get('userId')
    if user_id_str:
        # Each client joins a unique room identified by their user_id.
        # This allows targeted emissions to specific users (e.g., status updates).
        join_room(user_id_str)
        app.logger.info(f"Socket: User {user_id_str} joined their personal Socket.IO room.")
        
        # Track connected SIDs for each user
        connected_users_sessions.setdefault(user_id_str, set()).add(request.sid)
        sid_to_user_id[request.sid] = user_id_str

        try:
            user_obj_id = ObjectId(user_id_str)
            user_status_col.update_one(
                {"user_id": user_obj_id},
                {"$set": {"is_online": True, "last_active": datetime.now(timezone.utc)}},
                upsert=True
            )
            
            updated_status = user_status_col.find_one({"user_id": user_obj_id})
            if updated_status:
                # --- Optimized: Call helper to notify only related users instead of broadcasting to all ---
                _notify_related_users_of_status_change(user_id_str, updated_status)
                # --- End Optimized ---
            
        except Exception as e:
            app.logger.error(f"Socket Error: Error updating user status on connect or notifying: {e}", exc_info=True)

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
        # Insert message into MongoDB
        result = messages_col.insert_one(new_message)
        inserted_message = messages_col.find_one({"_id": result.inserted_id})
        
        # Update room's last updated timestamp
        rooms_col.update_one(
            {"_id": room_obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc)}}
        )

        serialized_msg = serialize_doc_for_json(inserted_message)
        
        # Retrieve room members to send targeted messages
        room_doc = rooms_col.find_one({"_id": room_obj_id})
        if room_doc and 'members' in room_doc:
            for member in room_doc['members']:
                member_id_obj = member['id']
                member_id_str = str(member_id_obj)

                # Emit 'receive_message' to all members of the room
                # Each client listens to their own user_id room, so we emit to that room.
                socketio.emit('receive_message', serialized_msg, room=member_id_str)
                app.logger.info(f"Socket: Emitted 'receive_message' for msg {serialized_msg['_id']} to room {member_id_str}")

                # If the recipient is not the sender, also send 'new_message' for unread notification
                if member_id_str != sender_id_str:
                    socketio.emit('new_message', serialized_msg, room=member_id_str)
                    app.logger.info(f"Socket: Emitted 'new_message' for msg {serialized_msg['_id']} to room {member_id_str} (unread for {member_id_str})")

                    # Increment unread count for the recipient
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
    # This event is already optimized as it emits to specific room members.
    room_id_str = data.get("room_id")
    user_id_str = data.get("user_id")
    if room_id_str and user_id_str:
        try:
            room_obj_id = ObjectId(room_id_str)
            room_doc = rooms_col.find_one({"_id": room_obj_id})
            if room_doc and 'members' in room_doc:
                for member in room_doc['members']:
                    member_id_str = str(member['id'])
                    # Send typing event only to the OTHER member in the room
                    if member_id_str != user_id_str:
                        emit('typing', {'user_id': user_id_str}, room=member_id_str)
                        app.logger.info(f"Socket: Typing event from {user_id_str} to {member_id_str} in room {room_id_str}")
        except Exception as e:
            app.logger.error(f"Socket Error: Error handling typing event: {e}", exc_info=True)


@socketio.on('stop_typing')
def on_stop_typing(data):
    # This event is already optimized as it emits to specific room members.
    room_id_str = data.get("room_id")
    user_id_str = data.get("user_id")
    if room_id_str and user_id_str:
        try:
            room_obj_id = ObjectId(room_id_str)
            room_doc = rooms_col.find_one({"_id": room_obj_id})
            if room_doc and 'members' in room_doc:
                for member in room_doc['members']:
                    member_id_str = str(member['id'])
                    # Send stop_typing event only to the OTHER member in the room
                    if member_id_str != user_id_str:
                        emit('stop_typing', {'user_id': user_id_str}, room=member_id_str)
                        app.logger.info(f"Socket: Stop Typing event from {user_id_str} to {member_id_str} in room {room_id_str}")
        except Exception as e:
            app.logger.error(f"Socket Error: Error handling stop_typing event: {e}", exc_info=True)


if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    app.logger.info("Starting production server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080)

