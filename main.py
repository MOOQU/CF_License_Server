# ==========================
#  CF LICENSE SERVER (FIXED)
#  รองรับ Trial + Licensed เต็มระบบ
#  แก้ last_seen / offline ครบแล้ว
# ==========================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from pymongo import MongoClient
import time

# ==========================
# CONFIG
# ==========================
MONGO_URI = "mongodb+srv://MOOQU:SIRIMEEMAK@cluster0.crufku8.mongodb.net/cf_license_db?retryWrites=true&w=majority"
DB_NAME = "cf_license_db"

TRIAL_DURATION_SECONDS = 30 * 60   # trial 30 นาที
HEARTBEAT_INTERVAL = 60           # client ส่งทุก 60 วิ

# ==========================
# DATABASE
# ==========================
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users = db["users"]

# ==========================
# APP
# ==========================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================
# MODELS
# ==========================
class HBModel(BaseModel):
    hwid: str
    username: str
    mode: str  # trial / licensed
    version: str = "1.0"


class LicenseCheckModel(BaseModel):
    username: str
    license: str
    hwid: str


class TrialRequestModel(BaseModel):
    hwid: str


class TrialCheckModel(BaseModel):
    hwid: str


class BanModel(BaseModel):
    hwid: str


# ==========================
# ENDPOINTS
# ==========================

# --------------------------
# Heartbeat (Trial + Licensed)
# --------------------------
@app.post("/heartbeat")
async def heartbeat(data: HBModel):
    now = int(time.time())

    users.update_one(
        {"hwid": data.hwid},
        {
            "$set": {
                "last_seen": now,
                "username": data.username,
                "mode": data.mode,
            }
        },
        upsert=True
    )

    return {"status": "ok", "last_seen": now}


# --------------------------
# Request Trial
# --------------------------
@app.post("/request_trial")
async def request_trial(data: TrialRequestModel):
    hwid = data.hwid
    now = int(time.time())

    u = users.find_one({"hwid": hwid})

    # เคยมี trial แล้ว → ใช้ต่อ
    if u:
        remaining = u.get("trial_remaining", 0)
        if remaining <= 0:
            return {"status": "expired"}

        users.update_one(
            {"hwid": hwid},
            {"$set": {"last_seen": now}}
        )

        return {"status": "active", "remaining": remaining}

    # ยังไม่เคย → สร้างใหม่
    users.insert_one({
        "hwid": hwid,
        "user_type": "trial",
        "trial_remaining": TRIAL_DURATION_SECONDS,
        "last_seen": now,
        "banned": False,
        "username": f"trial_{hwid[:6]}",
        "license": "",
        "total_usage": 0
    })

    return {"status": "active", "remaining": TRIAL_DURATION_SECONDS}


# --------------------------
# Check Trial Remaining
# --------------------------
@app.post("/check_trial")
async def check_trial(data: TrialCheckModel):
    hwid = data.hwid
    now = int(time.time())

    u = users.find_one({"hwid": hwid})
    if not u:
        return {"status": "not_found"}

    remaining = u.get("trial_remaining", 0)

    # หมดเวลาแล้ว
    if remaining <= 0:
        return {"status": "expired"}

    # อัปเดต last_seen
    users.update_one(
        {"hwid": hwid},
        {"$set": {"last_seen": now}}
    )

    return {"status": "active", "remaining": remaining}


# --------------------------
# Check License (Licensed Users)
# --------------------------
@app.post("/check_license")
async def check_license(data: LicenseCheckModel):
    now = int(time.time())

    u = users.find_one({"username": data.username})

    if not u:
        return {"status": "not_found"}

    if u.get("banned"):
        return {"status": "banned"}

    if u.get("license") != data.license:
        return {"status": "invalid"}

    # Fix HWID
    if u.get("hwid") == "":
        users.update_one(
            {"username": data.username},
            {"$set": {"hwid": data.hwid}}
        )

    # update last_seen
    users.update_one(
        {"username": data.username},
        {"$set": {"last_seen": now}}
    )

    return {"status": "ok", "type": u.get("user_type", "license")}


# --------------------------
# Ban Device
# --------------------------
@app.post("/ban")
async def ban(data: BanModel):
    users.update_one(
        {"hwid": data.hwid},
        {"$set": {"banned": True}}
    )
    return {"status": "success"}


# --------------------------
# Unban Device
# --------------------------
@app.post("/unban")
async def unban(data: BanModel):
    users.update_one(
        {"hwid": data.hwid},
        {"$set": {"banned": False}}
    )
    return {"status": "success"}


# --------------------------
# Users List (Admin GUI)
# --------------------------
@app.get("/userslist")
async def users_list():
    arr = list(users.find({}, {"_id": 0}))
    return {"users": arr}


# --------------------------
# Generate License (Admin GUI)
# --------------------------
class GenModel(BaseModel):
    username: str


@app.post("/usersgen_license")
async def generate_license(data: GenModel):
    username = data.username
    key = f"LIC-{int(time.time())}"

    users.update_one(
        {"username": username},
        {
            "$set": {
                "license": key,
                "user_type": "licensed",
                "last_seen": 0,
                "total_usage": 0,
                "hwid": ""
            }
        },
        upsert=True
    )

    return {"status": "success", "license": key}


# --------------------------
# Delete User
# --------------------------
class DeleteModel(BaseModel):
    username: str | None = None
    hwid: str | None = None


@app.post("/usersdelete")
async def delete_user(data: DeleteModel):
    if data.username:
        users.delete_one({"username": data.username})
        return {"status": "success"}

    if data.hwid:
        users.delete_one({"hwid": data.hwid})
        return {"status": "success"}

    return {"status": "error"}


# --------------------------
# Auto-decrease Trial (every req)
# --------------------------
@app.on_event("startup")
async def start_background():
    import threading

    def reduce_trial_loop():
        while True:
            time.sleep(60)

            now = int(time.time())
            all_trial = users.find({"user_type": "trial"})

            for u in all_trial:
                last = u.get("last_seen", 0)
                diff = now - last

                if diff > 120:
                    continue  # offline ไม่มีการใช้งาน

                new_remaining = max(0, u.get("trial_remaining", 0) - diff)

                users.update_one(
                    {"hwid": u["hwid"]},
                    {"$set": {"trial_remaining": new_remaining}}
                )

    t = threading.Thread(target=reduce_trial_loop, daemon=True)
    t.start()
