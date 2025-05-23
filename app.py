from flask import Flask, jsonify
from pymongo import MongoClient
import os

app = Flask(__name__)

# เชื่อมต่อ MongoDB Atlas
mongo_uri = os.getenv("MONGO_URI") 
if not mongo_uri:
    mongo_uri = "mongodb+srv://Keatikun:Ong100647@movemax.szryalr.mongodb.net/"  # ใช้ ENV จริงในการ deploy

client = MongoClient(mongo_uri)
db = client["Movemax"]  # Database name
users_col = db["User"]  # Collection name
messages_col = db["messages"]

@app.route('/')
def index():
    return "✅ Flask + MongoDB is running"

@app.route('/users')
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
