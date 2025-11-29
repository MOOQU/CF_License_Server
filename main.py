from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
import os
import uuid
from dotenv import load_dotenv

# โหลด environment variables
load_dotenv()

app = FastAPI(title="CF AutoText License Server")

# -------------------------
# MongoDB Setup
# -------------------------
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "cf_license_db")

if not MONGO_URI:
    raise Exception("MONGO_URI not set in environment variables")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
collection = db["licenses"]

# -------------------------
# Models
# -------------------------
class LicenseCheck(BaseModel):
    username: str
    license: str
    hwid: str
    version: str

class UsernameModel(BaseModel):
    username: str

# -------------------------
# License helpers
# -------------------------
def gen_license_key():
    return uuid.uuid4().hex[:16].upper()

# -------------------------
# API Endpoints
# -------------------------
@app.get("/")
def root():
    return {"message": "Server is running!"}

@app.get("/users/list")
def list_users():
    try:
        users = list(collection.find({}, {"_id": 0}))
        return {"users": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/users/gen_license")
def gen_license(user: UsernameModel):
    if collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="User มีอยู่แล้ว")
    license_key = gen_license_key()
    collection.insert_one({
        "username": user.username,
        "license": license_key,
        "status": "active",
        "hwid": ""
    })
    return {"status": "success", "username": user.username, "license": license_key}

@app.post("/users/delete")
def delete_user(user: UsernameModel):
    result = collection.delete_one({"username": user.username})
    if result.deleted_count == 0:
        raise HTTPException(status_code=400, detail="User ไม่พบ")
    return {"status": "success", "message": f"User {user.username} ลบแล้ว"}

@app.post("/check_license")
def check_license(req: LicenseCheck):
    u = collection.find_one({"username": req.username, "license": req.license})

    if not u:
        return {"status": "invalid", "message": "License ไม่ถูกต้อง"}

    if u.get("status") != "active":
        return {"status": "invalid", "message": f"License {u['status']}"}

    # HWID binding
    if not u.get("hwid"):
        collection.update_one(
            {"username": req.username},
            {"$set": {"hwid": req.hwid}}
        )
    elif u.get("hwid") != req.hwid:
        return {"status": "invalid", "message": "HWID ไม่ตรง"}

    return {"status": "valid", "message": "License ถูกต้อง"}
