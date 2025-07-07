from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
from flask_cors import CORS
from config import MONGO_URI, SECRET_KEY 
from bson.objectid import ObjectId
import logging
from collections import deque 


app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
CORS(app, resources={r"/*": {"origins": "*"}}) 

# Configure logging for the Flask app
app.logger.setLevel(logging.INFO) 
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)

# Configure logging for Socket.IO and Engine.IO
logging.getLogger('socketio').setLevel(logging.INFO)
logging.getLogger('engineio').setLevel(logging.INFO)


# Initialize SocketIO with async_mode='eventlet' for better production compatibility
socketio = SocketIO(app, cors_allowed_origins="*", logger=True, engineio_logger=True, async_mode='eventlet')

client = MongoClient(MONGO_URI)
db = client["Movemax"]

users_col = db["users"]
admins_col = db["admins"]
messages_col = db["messages"]
rooms_col = db["rooms"]
reads_col = db["user_room_reads"]
user_status_col = db["user_status"]

# Global mappings for efficient Socket.IO session management
connected_users_sessions = {} 
sid_to_user_id = {} 

# --- Spam Prevention Configuration and Data Structures ---
# Rate Limiting: Max 3 messages every 5 seconds per user
MESSAGE_RATE_LIMIT_INTERVAL_SECONDS = 5
MESSAGE_RATE_LIMIT_MAX_MESSAGES = 3
user_message_timestamps = {} # {user_id: deque([timestamp1, timestamp2, ...])}

# Duplicate Message Prevention: Prevent same message within 2 seconds
DUPLICATE_MESSAGE_COOLDOWN_SECONDS = 2
user_last_message_content = {} # {user_id: {'content': 'last_msg', 'timestamp': datetime}}

# Message Length Limit: Max 500 characters
MESSAGE_MAX_LENGTH = 500
# --- End Spam Prevention Configuration ---


def serialize_doc_for_json(doc):
    if doc is None:
        return None
    serialized_doc = doc.copy()
    
    if '_id' in serialized_doc and isinstance(serialized_doc['_id'], ObjectId):
        serialized_doc['_id'] = str(serialized_doc['_id'])
    
    if 'members' in serialized_doc and isinstance(serialized_doc['members'], list):
        for i, member in enumerate(serialized_doc['members']):
            if 'id' in member and isinstance(member['id'], ObjectId):
                serialized_doc['members'][i]['id'] = str(member['id'])

    for key, value in serialized_doc.items():
        if isinstance(value, datetime):
            serialized_doc[key] = value.replace(tzinfo=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        elif isinstance(value, ObjectId):
            serialized_doc[key] = str(value)
            
    return serialized_doc


def create_mongo_indexes():
    app.logger.info("MongoDB: Checking and creating indexes...")
    try:
        users_col.create_index([("_id", 1)]) 
        users_col.create_index([("username", 1)], unique=True)

        admins_col.create_index([("_id", 1)])
        admins_col.create_index([("username", 1)], unique=True)

        rooms_col.create_index([("room_key", 1)], unique=True)
        rooms_col.create_index([("members.id", 1)])
        rooms_col.create_index([("updated_at", -1)])

        messages_col.create_index([("room_id", 1)])
        messages_col.create_index([("timestamp", 1)])
        messages_col.create_index([("room_id", 1), ("timestamp", 1)])
        messages_col.create_index([("room_id", 1), ("sender_id", 1), ("is_read", 1)]) 

        reads_col.create_index([("user_id", 1), ("room_id", 1)], unique=True)
        reads_col.create_index([("last_read_timestamp", 1)])
        reads_col.create_index([("user_id", 1), ("is_muted", 1)])
        reads_col.create_index([("user_id", 1), ("is_pinned", 1)])

        user_status_col.create_index([("user_id", 1)], unique=True)
        user_status_col.create_index([("is_online", 1)])

        app.logger.info("MongoDB: All indexes checked/created successfully.")
    except Exception as e:
        app.logger.error(f"MongoDB Error: Failed to create indexes: {e}", exc_info=True)

with app.app_context():
    create_mongo_indexes()


def _notify_related_users_of_status_change(user_id_str, new_status_doc):
    try:
        user_obj_id = ObjectId(user_id_str)
        serialized_status = serialize_doc_for_json(new_status_doc)
        
        rooms_cursor = rooms_col.find({"members.id": user_obj_id})
        
        notified_users = set() 
        notified_users.add(user_id_str) 

        for room_doc in rooms_cursor:
            app.logger.debug(f"Socket: Processing room {room_doc['_id']} for status change of {user_id_str}")
            if 'members' in room_doc:
                for member in room_doc['members']:
                    member_id_str = str(member['id'])
                    if member_id_str != user_id_str and member_id_str not in notified_users:
                        socketio.emit('user_status_changed', serialized_status, room=member_id_str)
                        app.logger.info(f"Socket: Emitted user_status_changed for {user_id_str} to room {member_id_str}")
                        notified_users.add(member_id_str)
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

@app.route('/api/last_messages_for_user/<user_id>', methods=['GET'])
def get_last_messages_for_user(user_id):
    app.logger.info(f"API: Fetching last messages for all rooms of user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid user_id format for last messages: {user_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid user_id format"}), 400

    pipeline = [
        {"$match": {"members.id": user_obj_id}},
        {"$lookup": {
            "from": "messages",
            "localField": "_id",
            "foreignField": "room_id",
            "as": "room_messages"
        }},
        {"$unwind": {"path": "$room_messages", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"_id": 1, "room_messages.timestamp": -1}},
        {"$group": {
            "_id": "$_id",
            "members": {"$first": "$members"},
            "last_message": {"$first": "$room_messages"}
        }},
        {"$project": {
            "_id": 0,
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
        {"$project": {
            "chat_partner_id": {"$toString": "$chat_partner_id.id"},
            "last_message": "$last_message"
        }}
    ]

    try:
        results = list(rooms_col.aggregate(pipeline))
        
        response_data = {}
        for res in results:
            chat_partner_id = res.get('chat_partner_id')
            last_message_doc = res.get('last_message')
            if chat_partner_id: 
                response_data[chat_partner_id] = serialize_doc_for_json(last_message_doc)
        
        app.logger.info(f"API Success: Fetched last messages for user {user_id}. Count: {len(response_data)}")
        return jsonify(response_data), 200

    except Exception as e:
        app.logger.error(f"API Error: Failed to fetch last messages for user {user_id}: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500

@app.route('/api/all_user_statuses', methods=['GET'])
def get_all_user_statuses():
    app.logger.info("API: Fetching all user statuses.")
    try:
        statuses = list(user_status_col.find({}))
        serialized_statuses = {}
        for status in statuses:
            serialized_statuses[str(status['user_id'])] = serialize_doc_for_json(status)
        app.logger.info(f"API Success: Fetched {len(serialized_statuses)} user statuses.")
        return jsonify(serialized_statuses), 200
    except Exception as e:
        app.logger.error(f"API Error: Failed to fetch all user statuses: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500

@app.route('/api/last_messages_for_admin_conversations/<admin_id>', methods=['GET'])
def get_last_messages_for_admin_conversations(admin_id):
    app.logger.info(f"API: Fetching last messages for admin's conversations: {admin_id}")
    try:
        admin_obj_id = ObjectId(admin_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid admin_id format for last messages: {admin_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid admin_id format"}), 400

    pipeline = [
        {"$match": {"members.id": admin_obj_id}},
        {"$lookup": {
            "from": "messages",
            "localField": "_id",
            "foreignField": "room_id",
            "as": "room_messages"
        }},
        {"$unwind": {"path": "$room_messages", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"_id": 1, "room_messages.timestamp": -1}},
        {"$group": {
            "_id": "$_id",
            "members": {"$first": "$members"},
            "last_message": {"$first": "$room_messages"}
        }},
        {"$project": {
            "_id": 0,
            "room_id": {"$toString": "$_id"},
            "last_message": "$last_message",
            "chat_partner_id": {
                "$arrayElemAt": [
                    {"$filter": {
                        "input": "$members",
                        "as": "member",
                        "cond": {"$ne": ["$$member.id", admin_obj_id]}
                    }},
                    0
                ]
            }
        }},
        {"$project": {
            "chat_partner_id": {"$toString": "$chat_partner_id.id"},
            "last_message": "$last_message"
        }}
    ]

    try:
        results = list(rooms_col.aggregate(pipeline))
        
        response_data = {}
        for res in results:
            chat_partner_id = res.get('chat_partner_id')
            last_message_doc = res.get('last_message')
            if chat_partner_id: 
                response_data[chat_partner_id] = serialize_doc_for_json(last_message_doc)
        
        app.logger.info(f"API Success: Fetched last messages for admin {admin_id}. Count: {len(response_data)}")
        return jsonify(response_data), 200

    except Exception as e:
        app.logger.error(f"API Error: Failed to fetch last messages for admin {admin_id}: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500


@app.route('/api/user_room_mappings/<user_id>', methods=['GET'])
def get_user_room_mappings(user_id):
    app.logger.info(f"API: Fetching room mappings for user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid user_id format for room mappings: {user_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid user_id format"}), 400

    pipeline = [
        {"$match": {"members.id": user_obj_id}},
        {"$project": {
            "_id": 0,
            "room_id": {"$toString": "$_id"},
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
        {"$project": {
            "room_id": 1,
            "chat_partner_id": {"$toString": "$chat_partner_id.id"}
        }}
    ]

    try:
        results = list(rooms_col.aggregate(pipeline))
        room_mappings = {}
        for res in results:
            if res.get('chat_partner_id') and res.get('room_id'):
                room_mappings[res['chat_partner_id']] = res['room_id']
        app.logger.info(f"API Success: Fetched room mappings for user {user_id}. Count: {len(room_mappings)}")
        return jsonify(room_mappings), 200
    except Exception as e:
        app.logger.error(f"API Error: Failed to fetch room mappings for user {user_id}: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500


@app.route('/api/unread_counts/<user_id>', methods=['GET'])
def get_unread_counts(user_id):
    app.logger.info(f"API: Fetching unread counts for user: {user_id}")
    try:
        user_obj_id = ObjectId(user_id)
    except Exception as e:
        app.logger.error(f"API Error: Invalid user_id format for unread counts: {user_id}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid user_id format"}), 400

    unread_counts_and_settings = {} 
    
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
            "room_id": {"$toString": "$room_info._id"},
            "unread_count": {"$ifNull": ["$unread_messages_count_result.unreadMessages", 0]},
            "is_muted": {"$ifNull": ["$is_muted", False]}, 
            "is_pinned": {"$ifNull": ["$is_pinned", False]} 
        }}
    ]

    try:
        results = list(reads_col.aggregate(pipeline))
        for res in results:
            room_id = res["room_id"]
            unread_counts_and_settings[room_id] = {
                "unread_count": res["unread_count"],
                "is_muted": res["is_muted"],
                "is_pinned": res["is_pinned"]
            }
        
        user_rooms_not_in_reads = rooms_col.find({
            "members.id": user_obj_id,
            "_id": {"$nin": [ObjectId(rid) for rid in unread_counts_and_settings.keys()]} 
        })
        for room in user_rooms_not_in_reads:
            total_messages_in_room = messages_col.count_documents({"room_id": room["_id"]})
            room_id_str = str(room["_id"])
            unread_counts_and_settings[room_id_str] = {
                "unread_count": total_messages_in_room,
                "is_muted": False, 
                "is_pinned": False 
            }

        app.logger.info(f"API Success: Fetched unread counts and chat settings for user {user_id}: {unread_counts_and_settings}")
        return jsonify(unread_counts_and_settings), 200

    except Exception as e:
        app.logger.error(f"API Error: Failed to fetch unread counts and chat settings for user {user_id}: {e}", exc_info=True)
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
        app.logger.error(f"API Error: Failed to get/create room: {str(e)}" , exc_info=True)
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
        # Fetch messages and include sender's profile image URL
        pipeline = [
            {"$match": {"room_id": room_obj_id}},
            {"$sort": {"timestamp": 1}},
            {"$lookup": {
                "from": "users", # Assuming users collection for profile images
                "localField": "sender_id",
                "foreignField": "_id",
                "as": "sender_user_info"
            }},
            {"$lookup": {
                "from": "admins", # Assuming admins collection for profile images
                "localField": "sender_id",
                "foreignField": "_id",
                "as": "sender_admin_info"
            }},
            {"$addFields": {
                "sender_profile_image_url": {
                    "$cond": {
                        "if": {"$ne": [{"$size": "$sender_user_info"}, 0]},
                        "then": {"$arrayElemAt": ["$sender_user_info.profile_image_url", 0]},
                        "else": {"$arrayElemAt": ["$sender_admin_info.profile_image_url", 0]}
                    }
                }
            }},
            {"$project": {
                "sender_user_info": 0,
                "sender_admin_info": 0
            }}
        ]
        messages = list(messages_col.aggregate(pipeline))
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
        # Get the current last_read_timestamp for the user in this room
        read_status_doc = reads_col.find_one({"user_id": user_id, "room_id": room_id})
        last_read_timestamp = read_status_doc.get("last_read_timestamp", datetime.min.replace(tzinfo=timezone.utc)) if read_status_doc else datetime.min.replace(tzinfo=timezone.utc)

        # Find the other member in the room to know whose messages to mark as read
        room_doc = rooms_col.find_one({"_id": room_id})
        if room_doc:
            other_member_id = None
            for member in room_doc['members']:
                if member['id'] != user_id: # This is the other person in the 1-on-1 chat
                    other_member_id = member['id']
                    break
            
            if other_member_id:
                app.logger.info(f"API: Marking messages from {other_member_id} in room {room_id} as read by {user_id}.")
                # Update messages in this room sent by the OTHER member to 'is_read: True'
                # Only mark messages sent AFTER the last_read_timestamp of the current user
                # and are currently not read.
                update_result = messages_col.update_many(
                    {
                        "room_id": room_id,
                        "sender_id": other_member_id,
                        "is_read": False, # Only update if not already read
                        "timestamp": {"$gt": last_read_timestamp} # Only mark messages sent after user's last read
                    },
                    {"$set": {"is_read": True}}
                )
                app.logger.info(f"API: Marked {update_result.modified_count} messages as read.")

                # Emit message_read event to the sender of the messages that were just read
                if update_result.modified_count > 0:
                    # Fetch IDs of messages that were just marked as read to send to the client
                    read_message_ids = [str(msg['_id']) for msg in messages_col.find({
                        "room_id": room_id,
                        "sender_id": other_member_id,
                        "is_read": True,
                        "timestamp": {"$gt": last_read_timestamp} # Ensure we only pick the ones just read
                    }, {"_id": 1})] 
                    
                    # Check if the other_member is connected via Socket.IO
                    if other_member_id in connected_users_sessions:
                        app.logger.info(f"Socket: Emitting 'messages_read' to {other_member_id} for room {room_id} and messages {read_message_ids}")
                        socketio.emit('messages_read', {
                            'room_id': str(room_id),
                            'reader_id': str(user_id), # The ID of the user who just read the messages
                            'message_ids': read_message_ids
                        }, room=str(other_member_id)) # Emit to the sender's personal room
            else:
                app.logger.warning(f"API Warning: Could not find other member in room {room_id} for read receipt.")
        else:
            app.logger.warning(f"API Warning: Room {room_id} not found when trying to mark messages as read.")


        # Update the user's last_read_timestamp in the reads_col
        reads_col.update_one(
            {"user_id": user_id, "room_id": room_id},
            {"$set": {"last_read_timestamp": datetime.now(timezone.utc), "unread_count": 0}},
            upsert=True
        )
        app.logger.info(f"API Success: User {user_id} marked room {room_id} as read and updated messages.")
        return jsonify({"status": "read"}), 200
    except Exception as e:
        app.logger.error(f"API Error: Error marking messages as read: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500

@app.route('/api/toggle_mute', methods=['POST'])
def toggle_mute():
    app.logger.info("API: Toggling mute status.")
    data = request.json
    try:
        user_id = ObjectId(data["user_id"])
        room_id = ObjectId(data["room_id"])
        is_muted = bool(data["is_muted"]) 
    except Exception as e:
        app.logger.error(f"API Error: Invalid data for toggle_mute: {data}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid data"}), 400

    try:
        reads_col.update_one(
            {"user_id": user_id, "room_id": room_id},
            {"$set": {"is_muted": is_muted}},
            upsert=True
        )
        app.logger.info(f"API Success: User {user_id} mute status for room {room_id} set to {is_muted}.")
        return jsonify({"status": "success", "is_muted": is_muted}), 200
    except Exception as e:
        app.logger.error(f"API Error: Error toggling mute status: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500

@app.route('/api/toggle_pin', methods=['POST'])
def toggle_pin():
    app.logger.info("API: Toggling pin status.")
    data = request.json
    try:
        user_id = ObjectId(data["user_id"])
        room_id = ObjectId(data["room_id"])
        is_pinned = bool(data["is_pinned"]) 
    except Exception as e:
        app.logger.error(f"API Error: Invalid data for toggle_pin: {data}, Error: {e}", exc_info=True)
        return jsonify({"error": "Invalid data"}), 400

    try:
        reads_col.update_one(
            {"user_id": user_id, "room_id": room_id},
            {"$set": {"is_pinned": is_pinned}},
            upsert=True
        )
        app.logger.info(f"API Success: User {user_id} pin status for room {room_id} set to {is_pinned}.")
        return jsonify({"status": "success", "is_pinned": is_pinned}), 200
    except Exception as e:
        app.logger.error(f"API Error: Error toggling pin status: {e}", exc_info=True)
        return jsonify({"error": f"Internal Server Error: {e}"}), 500


# Socket.IO event handlers
@socketio.on('connect')
def handle_connect(*args, **kwargs):
    app.logger.info(f"Socket: Client connected: {request.sid}")

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    app.logger.info(f"Socket: Client disconnected: {sid}")

    user_id_str = sid_to_user_id.get(sid)
    if user_id_str:
        connected_users_sessions.get(user_id_str, set()).discard(sid)
        if sid in sid_to_user_id:
            del sid_to_user_id[sid]

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
                    _notify_related_users_of_status_change(user_id_str, updated_status)
            except Exception as e:
                app.logger.error(f"Socket Error: Error updating user status on disconnect or notifying: {e}", exc_info=True)
    else:
        app.logger.info(f"Socket: Disconnected client {sid} was not a known user session.")


@socketio.on('join_user_room')
def join_user_room(data):
    user_id_str = data.get('userId')
    if user_id_str:
        join_room(user_id_str)
        app.logger.info(f"Socket: User {user_id_str} joined their personal Socket.IO room.")
        
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
                _notify_related_users_of_status_change(user_id_str, updated_status)
            
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
    # --- START OF CHANGE 1: Renamed temp_id to client_temp_id for clarity ---
    client_temp_id = data.get("temp_id") 
    # --- END OF CHANGE 1 ---

    if not all([room_id_str, sender_id_str, message_content, sender_type]):
        app.logger.error("Socket Error: Missing data for send_message")
        return

    current_time = datetime.now(timezone.utc)

    # --- Spam Prevention Checks (NO CHANGES HERE) ---
    # 1. Message Length Limit
    if len(message_content) > MESSAGE_MAX_LENGTH:
        app.logger.warning(f"Spam Prevention: Message from {sender_id_str} blocked due to excessive length ({len(message_content)} > {MESSAGE_MAX_LENGTH}).")
        emit('message_blocked', {'reason': 'Message too long'}, room=request.sid)
        return

    # 2. Duplicate Message Prevention
    last_msg_info = user_last_message_content.get(sender_id_str)
    if last_msg_info:
        last_content = last_msg_info['content']
        last_timestamp = last_msg_info['timestamp']
        if last_content == message_content and \
           (current_time - last_timestamp).total_seconds() < DUPLICATE_MESSAGE_COOLDOWN_SECONDS:
            app.logger.warning(f"Spam Prevention: Message from {sender_id_str} blocked due to being a duplicate within {DUPLICATE_MESSAGE_COOLDOWN_SECONDS} seconds.")
            emit('message_blocked', {'reason': 'Duplicate message too soon'}, room=request.sid)
            return
    user_last_message_content[sender_id_str] = {'content': message_content, 'timestamp': current_time}

    # 3. Rate Limiting
    if sender_id_str not in user_message_timestamps:
        user_message_timestamps[sender_id_str] = deque()
    
    # Remove timestamps older than the interval
    while user_message_timestamps[sender_id_str] and \
          (current_time - user_message_timestamps[sender_id_str][0]).total_seconds() > MESSAGE_RATE_LIMIT_INTERVAL_SECONDS:
        user_message_timestamps[sender_id_str].popleft()

    # Check if adding the current message exceeds the limit
    if len(user_message_timestamps[sender_id_str]) >= MESSAGE_RATE_LIMIT_MAX_MESSAGES:
        app.logger.warning(f"Spam Prevention: Message from {sender_id_str} blocked due to rate limit ({MESSAGE_RATE_LIMIT_MAX_MESSAGES} messages in {MESSAGE_RATE_LIMIT_INTERVAL_SECONDS} seconds).")
        emit('message_blocked', {'reason': 'Rate limit exceeded'}, room=request.sid)
        return
    
    # Add current message timestamp
    user_message_timestamps[sender_id_str].append(current_time)
    # --- End Spam Prevention Checks ---

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
        "timestamp": datetime.now(timezone.utc),
        "is_read": False # Initialize is_read to False
    }
    
    # Get sender's profile image URL (NO CHANGES HERE)
    sender_profile_image_url = None
    if sender_type == 'user':
        sender_doc = users_col.find_one({"_id": sender_obj_id}, {"profile_image_url": 1})
        if sender_doc:
            sender_profile_image_url = sender_doc.get("profile_image_url")
    elif sender_type == 'admin':
        sender_doc = admins_col.find_one({"_id": sender_obj_id}, {"profile_image_url": 1})
        if sender_doc:
            sender_profile_image_url = sender_doc.get("profile_image_url")
    if sender_profile_image_url:
        new_message['sender_profile_image_url'] = sender_profile_image_url

    try:
        result = messages_col.insert_one(new_message)
        # Fetch the inserted message document to get all fields, especially the actual _id
        inserted_message_doc = messages_col.find_one({"_id": result.inserted_id})
        
        rooms_col.update_one(
            {"_id": room_obj_id},
            {"$set": {"updated_at": datetime.now(timezone.utc)}}
        )

        # --- START OF CRITICAL CHANGES FOR REAL-TIME READ RECEIPTS ---
        # Serialize the message with the actual MongoDB _id for general use
        serialized_msg_with_actual_id = serialize_doc_for_json(inserted_message_doc)
        app.logger.info(f"Socket: Message inserted with actual MongoDB ID: {serialized_msg_with_actual_id['_id']}")

        # Create a copy for the sender that includes the client_temp_id for matching
        # The _id in this copy will be the actual MongoDB _id, not the temp_id.
        # The client will use client_temp_id to find the optimistic message,
        # then update its _id to the actual MongoDB _id received here.
        serialized_msg_for_sender = serialized_msg_with_actual_id.copy()
        if client_temp_id:
            serialized_msg_for_sender['client_temp_id'] = client_temp_id # Add client's temp ID as a separate field
            app.logger.info(f"Socket: Added client_temp_id {client_temp_id} to serialized message for sender.")

        app.logger.info(f"Socket: Emitting receive_message to sender {request.sid} with data: {serialized_msg_for_sender}")
        emit('receive_message', serialized_msg_for_sender, room=request.sid)

        room_doc = rooms_col.find_one({"_id": room_obj_id})
        if room_doc and 'members' in room_doc:
            for member in room_doc['members']:
                member_id_obj = member['id']
                member_id_str = str(member_id_obj)

                # Emit 'new_message' specifically for unread count increment for the recipient
                # This message should NOT contain client_temp_id as it's not relevant to other clients
                if member_id_str != sender_id_str: # Only for the recipient, not the sender
                    read_status = reads_col.find_one({"user_id": member_id_obj, "room_id": room_obj_id})
                    is_muted_for_recipient = read_status.get('is_muted', False) if read_status else False

                    if not is_muted_for_recipient:
                        # Emit the message with the actual MongoDB _id (no client_temp_id)
                        socketio.emit('new_message', serialized_msg_with_actual_id, room=member_id_str)
                        app.logger.info(f"Socket: Emitted 'new_message' for msg {serialized_msg_with_actual_id['_id']} to room {member_id_str} (unread for {member_id_str})")
                        reads_col.update_one(
                            {"user_id": member_id_obj, "room_id": room_obj_id},
                            {"$inc": {"unread_count": 1}},
                            upsert=True
                        )
                        app.logger.info(f"Socket: Incremented unread count for user {member_id_str} in room {room_id_str}")
                    else:
                        app.logger.info(f"Socket: New message for user {member_id_str} in room {room_id_str} but chat is muted. Not incrementing unread count or emitting 'new_message'.")
        else:
            app.logger.warning(f"Socket Warning: Room {room_id_str} not found or no members for message broadcast.")
        # --- END OF CRITICAL CHANGES FOR REAL-TIME READ RECEIPTS ---

    except Exception as e:
        app.logger.error(f"Socket Error: Error sending message or broadcasting: {e}", exc_info=True)
        # Emit an error back to the sender if message sending failed
        # --- START OF CHANGE 2: Use client_temp_id here ---
        emit('message_error', {'temp_id': client_temp_id, 'error': str(e)}, room=request.sid) 
        # --- END OF CHANGE 2 ---

if __name__ == '__main__':
    import eventlet
    import eventlet.wsgi
    app.logger.info("Starting production server on http://0.0.0.0:8080")
    socketio.run(app, host='0.0.0.0', port=8080)
