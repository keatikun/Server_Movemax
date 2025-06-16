import os
from fastapi import FastAPI
from socketio import AsyncServer
from socketio.asgi import ASGIApp
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from dotenv import load_dotenv

# โหลดตัวแปรจาก .env
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "chat_db")

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

sio = AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
socket_app = ASGIApp(sio, app)

connected_users = {}  # user_id -> sid
online_users = set()
user_typing = {}  # room_id -> set(user_id)

# --- Helper functions ---
async def set_online(user_id: str):
    online_users.add(user_id)

async def set_offline(user_id: str):
    online_users.discard(user_id)

async def save_message(data: dict):
    await db.messages.insert_one(data)

async def update_read_timestamp(user_id: str, room_id: str, timestamp: datetime):
    await db.user_room_reads.update_one(
        {"user_id": user_id, "room_id": room_id},
        {"$set": {"last_read_timestamp": timestamp}},
        upsert=True
    )

async def get_unread_count(user_id: str, room_id: str):
    read_doc = await db.user_room_reads.find_one({"user_id": user_id, "room_id": room_id})
    last_read = read_doc["last_read_timestamp"] if read_doc else datetime.fromtimestamp(0)
    count = await db.messages.count_documents({
        "room_id": room_id,
        "timestamp": {"$gt": last_read}
    })
    return count

# --- Socket.IO events ---
@sio.event
async def connect(sid, environ):
    print(f"[CONNECT] sid={sid}")

@sio.event
async def join(sid, data):
    user_id = data.get("user_id")
    room_id = data.get("room_id")
    if user_id and room_id:
        connected_users[user_id] = sid
        await set_online(user_id)
        await sio.save_session(sid, {"user_id": user_id, "room_id": room_id})
        await sio.enter_room(sid, room_id)
        print(f"User {user_id} joined room {room_id}")

@sio.event
async def leave(sid, data):
    room_id = data.get("room_id")
    if room_id:
        await sio.leave_room(sid, room_id)
        user_id = (await sio.get_session(sid)).get("user_id")
        if user_id and room_id in user_typing and user_id in user_typing[room_id]:
            user_typing[room_id].remove(user_id)
            await sio.emit("typing", {"room_id": room_id, "typing": list(user_typing[room_id])}, room=room_id)

@sio.event
async def send_message(sid, data):
    user_id = data.get("user_id")
    room_id = data.get("room_id")
    message_text = data.get("message")
    msg_type = data.get("type", "text")
    timestamp = datetime.utcnow()
    if user_id and room_id and message_text:
        message_doc = {
            "room_id": room_id,
            "sender_id": user_id,
            "message": message_text,
            "type": msg_type,
            "timestamp": timestamp,
        }
        await save_message(message_doc)
        await sio.emit("new_message", message_doc, room=room_id)
        await update_read_timestamp(user_id, room_id, timestamp)
        if room_id in user_typing and user_id in user_typing[room_id]:
            user_typing[room_id].remove(user_id)
            await sio.emit("typing", {"room_id": room_id, "typing": list(user_typing[room_id])}, room=room_id)

@sio.event
async def typing(sid, data):
    user_id = data.get("user_id")
    room_id = data.get("room_id")
    is_typing = data.get("typing", False)
    if user_id and room_id:
        if room_id not in user_typing:
            user_typing[room_id] = set()
        if is_typing:
            user_typing[room_id].add(user_id)
        else:
            user_typing[room_id].discard(user_id)
        await sio.emit("typing", {"room_id": room_id, "typing": list(user_typing[room_id])}, room=room_id)

@sio.event
async def read_messages(sid, data):
    user_id = data.get("user_id")
    room_id = data.get("room_id")
    timestamp = datetime.utcnow()
    if user_id and room_id:
        await update_read_timestamp(user_id, room_id, timestamp)
        count = await get_unread_count(user_id, room_id)
        await sio.emit("unread_count", {"room_id": room_id, "count": count}, room=sid)

@sio.event
async def disconnect(sid):
    session = await sio.get_session(sid)
    user_id = session.get("user_id") if session else None
    room_id = session.get("room_id") if session else None
    if user_id:
        await set_offline(user_id)
        connected_users.pop(user_id, None)
        print(f"User {user_id} disconnected")
    if room_id and user_id and room_id in user_typing and user_id in user_typing[room_id]:
        user_typing[room_id].remove(user_id)
        await sio.emit("typing", {"room_id": room_id, "typing": list(user_typing[room_id])}, room=room_id)

# --- FastAPI routes ---
@app.get("/")
async def root():
    return {"message": "Realtime Chat Server with typing and unread count"}

@app.get("/rooms/{user_id}")
async def get_rooms(user_id: str):
    cursor = db.rooms.find({"members.id": user_id})
    rooms = []
    async for room in cursor:
        room["_id"] = str(room["_id"])
        room["members"] = [{"id": str(m["id"]), "type": m["type"]} for m in room["members"]]
        rooms.append(room)
    return {"rooms": rooms}
