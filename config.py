import os
from dotenv import load_dotenv

# โหลดค่าจากไฟล์ .env
load_dotenv()

# ใช้งานค่าที่ต้องใช้
MONGO_URI = os.getenv("MONGO_URI")
SECRET_KEY = os.getenv("SECRET_KEY")

# ตรวจสอบว่าโหลดค่ามาครบหรือยัง
if not MONGO_URI or not SECRET_KEY:
    raise ValueError("Missing required environment variables (MONGO_URI or SECRET_KEY)")
