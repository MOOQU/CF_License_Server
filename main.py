from fastapi import FastAPI
from pymongo import MongoClient
import os

app = FastAPI()

# ใช้ Environment Variable สำหรับ MongoDB
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(MONGO_URL)
db = client["mydatabase"]

@app.get("/")
def read_root():
    return {"message": "Server is running!"}

@app.get("/testdb")
def test_db():
    try:
        server_info = client.server_info()  # ตรวจสอบการเชื่อมต่อ
        return {"mongodb_status": "connected", "server_info": str(server_info)}
    except Exception as e:
        return {"mongodb_status": "failed", "error": str(e)}
