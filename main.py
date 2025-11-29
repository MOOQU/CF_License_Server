from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
import uuid
import os

app = FastAPI(title="CF AutoText License Server")

# -------------------------
# Connect MongoDB
# -------------------------
MONGO_URL = os.getenv("MONGO_URL")

client = MongoClient(MONGO_URL)
db = client["license_db"]
users = db["users"]

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
# Helpers
# -------------------------
def gen_license_key():
    return uuid.uuid4().hex[:16].upper()


# -------------------------
# API Endpoints
# -------------------------
@app.get("/users/list")
def list_users():
    result = list(users.find({}, {"_id": 0}))
    return {"users": result}


@app.post("/users/gen_license")
def gen_license(user: UsernameModel):
    if users.find_one({"username": user.username}):
        raise HTTPException(400, "User มีอยู่แล้ว")

    license_key = gen_license_key()
    users.insert_one({
        "username": user.username,
        "license": license_key,
        "status": "active",
        "hwid": ""
    })

    return {"status": "success", "username": user.username, "license": license_key}


@app.post("/users/delete")
def delete_user(user: UsernameModel):
    result = users.delete_one({"username": user.username})
    if result.deleted_count == 0:
        raise HTTPException(400, "User ไม่พบ")

    return {"status": "success", "message": f"User {user.username} ลบแล้ว"}


@app.post("/check_license")
def check_license(req: LicenseCheck):
    u = users.find_one({"username": req.username, "license": req.license})

    if not u:
        return {"status": "invalid", "message": "License ไม่ถูกต้อง"}

    if u["status"] != "active":
        return {"status": "invalid", "message": f"License {u['status']}"}

    # HWID binding
    if u["hwid"] == "":
        users.update_one({"username": req.username}, {"$set": {"hwid": req.hwid}})
    elif u["hwid"] != req.hwid:
        return {"status": "invalid", "message": "HWID ไม่ตรง"}

    return {"status": "valid", "message": "License ถูกต้อง"}
