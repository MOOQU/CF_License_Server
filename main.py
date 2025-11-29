from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uuid
from pymongo import MongoClient

app = FastAPI(title="CF AutoText License Server")

# -------------------------
# MongoDB Config
# -------------------------
MONGO_URI = "mongodb://localhost:27017"  # เปลี่ยนตาม MongoDB ของคุณ
DB_NAME = "cf_autotext"
COLLECTION_NAME = "licenses"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

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
# Helper functions
# -------------------------
def gen_license_key():
    return uuid.uuid4().hex[:16].upper()

# -------------------------
# API Endpoints
# -------------------------
@app.get("/users/list")
def list_users():
    users = list(collection.find({}, {"_id": 0}))
    return {"users": users}

@app.post("/users/gen_license")
def gen_license(user: UsernameModel):
    existing = collection.find_one({"username": user.username})
    if existing:
        raise HTTPException(400, "User มีอยู่แล้ว")
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
        raise HTTPException(400, "User ไม่พบ")
    return {"status": "success", "message": f"User {user.username} ลบแล้ว"}

@app.post("/check_license")
def check_license(req: LicenseCheck):
    u = collection.find_one({"username": req.username, "license": req.license})
    if not u:
        return {"status": "invalid", "message": "License ไม่ถูกต้อง"}

    if u["status"] != "active":
        return {"status": "invalid", "message": f"License {u['status']}"}

    # HWID binding
    if u.get("hwid", "") == "":
        collection.update_one({"username": req.username}, {"$set": {"hwid": req.hwid}})
    elif u["hwid"] != req.hwid:
        return {"status": "invalid", "message": "HWID ไม่ตรง"}

    return {"status": "valid", "message": "License ถูกต้อง"}
