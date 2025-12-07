# CF AutoText License Server
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
import uuid
import time

app = FastAPI(title="CF AutoText License Server")

# -------------------------
# MongoDB Setup
# -------------------------
MONGO_URI = "mongodb+srv://MOOQU:SIRIMEEMAK@cluster0.crufku8.mongodb.net/cf_license_db?retryWrites=true&w=majority"
DB_NAME = "cf_license_db"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
collection = db["licenses"]
meta = db["meta"]

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

class HWIDModel(BaseModel):
    hwid: str

class TrialRequestModel(BaseModel):
    hwid: str
    version: str

class HeartbeatModel(BaseModel):
    hwid: str
    username: str = None
    mode: str = None

# -------------------------
# Settings
# -------------------------
TRIAL_LIMIT_SEC = 7200  # 2 hours
ONLINE_THRESHOLD = 100  # 100 seconds

# -------------------------
# Helpers
# -------------------------
def gen_license_key():
    raw = uuid.uuid4().hex[:16].upper()
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"

def get_next_trial_id():
    counter = meta.find_one({"_id": "trial_counter"})
    if not counter:
        meta.insert_one({"_id": "trial_counter", "value": 1})
        return 1

    new_val = counter["value"] + 1
    meta.update_one(
        {"_id": "trial_counter"},
        {"$set": {"value": new_val}}
    )
    return new_val

# ============================================================
# ROOT
# ============================================================
@app.get("/")
def root():
    return {"message": "Server is running!"}

# ============================================================
# USERS LIST (Admin)
# ============================================================
@app.get("/userslist")
def userslist():
    users = list(collection.find({}, {"_id": 0}))
    now = int(time.time())

    for u in users:
        last_seen = u.get("last_seen", 0)
        u["online"] = (now - last_seen <= ONLINE_THRESHOLD)

        # trial remaining
        if u.get("trial"):
            elapsed = u.get("total_usage_sec", 0)
            if u.get("last_start_time"):
                elapsed += now - u["last_start_time"]
            u["trial_remaining_minutes"] = max(0, (TRIAL_LIMIT_SEC - elapsed) // 60)
        else:
            u["trial_remaining_minutes"] = "-"

    return {"users": users}

# ============================================================
# GENERATE LICENSE
# ============================================================
@app.post("/usersgen_license")
def gen_license(user: UsernameModel):
    if collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="User มีอยู่แล้ว")

    license_key = gen_license_key()
    now = int(time.time())

    collection.insert_one({
        "username": user.username,
        "license": license_key,
        "hwid": "",
        "banned": False,
        "trial": False,
        "trial_start": None,
        "last_start_time": None,
        "total_usage_sec": 0,
        "status": "active",
        "last_seen": now,
        "last_heartbeat": now,
        "user_type": "licensed"
    })

    return {"status": "success", "username": user.username, "license": license_key}

# ============================================================
# DELETE USER
# ============================================================
@app.post("/usersdelete")
def delete_user(user: UsernameModel):
    result = collection.delete_one({"username": user.username})
    if result.deleted_count == 0:
        raise HTTPException(status_code=400, detail="User ไม่พบ")
    return {"status": "success"}

# ============================================================
# CHECK LICENSE (Normal Login)
# ============================================================
@app.post("/check_license")
def check_license(req: LicenseCheck):
    u = collection.find_one({"username": req.username, "license": req.license})
    if not u:
        return {"status": "invalid", "message": "License ไม่ถูกต้อง"}

    if u.get("banned"):
        return {"status": "invalid", "message": "บัญชีถูกแบน"}

    # first binding HWID
    if not u.get("hwid"):
        collection.update_one(
            {"username": req.username},
            {"$set": {"hwid": req.hwid}}
        )

    # wrong hwid
    elif u["hwid"] != req.hwid:
        return {"status": "invalid", "message": "HWID ไม่ตรง"}

    now = int(time.time())
    collection.update_one(
        {"username": req.username},
        {"$set": {"last_seen": now}}
    )

    return {"status": "valid"}

# ============================================================
# TRIAL REQUEST
# ============================================================
@app.post("/request_trial")
def request_trial(data: TrialRequestModel):
    hwid = data.hwid
    now = int(time.time())

    u = collection.find_one({"hwid": hwid})

    # create new trial
    if not u:
        tid = get_next_trial_id()
        username = f"TRIAL USER {tid}"

        collection.insert_one({
            "username": username,
            "license": "",
            "hwid": hwid,
            "trial": True,
            "trial_start": now,
            "last_start_time": now,
            "total_usage_sec": 0,
            "status": "active",
            "banned": False,
            "last_seen": now,
            "last_heartbeat": now,
            "user_type": "trial"
        })

        return {"status": "active", "username": username, "remaining": TRIAL_LIMIT_SEC}

    # banned
    if u.get("banned"):
        return {"status": "banned", "remaining": 0}

    # still in trial
    if u.get("trial"):
        elapsed = u.get("total_usage_sec", 0)
        if u.get("last_start_time"):
            elapsed += now - u["last_start_time"]

        remaining = max(0, TRIAL_LIMIT_SEC - elapsed)

        collection.update_one(
            {"hwid": hwid},
            {"$set": {"last_seen": now}}
        )

        if remaining <= 0:
            return {"status": "expired", "remaining": 0}

        return {"status": "active", "username": u["username"], "remaining": remaining}

    return {"status": "expired", "remaining": 0}

# ============================================================
# CHECK TRIAL STATUS
# ============================================================
@app.post("/check_trial")
def check_trial(data: HWIDModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})

    if not u:
        return {"status": "no_user"}

    if u.get("banned"):
        return {"status": "banned", "remaining": 0}

    if not u.get("trial"):
        return {"status": "licensed", "remaining": None}

    elapsed = u.get("total_usage_sec", 0)
    if u.get("last_start_time"):
        elapsed += now - u["last_start_time"]

    remaining = max(0, TRIAL_LIMIT_SEC - elapsed)

    collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"last_seen": now}}
    )

    if remaining <= 0:
        return {"status": "expired", "remaining": 0}

    return {
        "status": "active",
        "remaining": remaining,
        "username": u.get("username")
    }

# ============================================================
# BAN / UNBAN
# ============================================================
@app.post("/ban")
def ban(data: HWIDModel):
    result = collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"banned": True}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="HWID ไม่พบ")
    return {"status": "success"}

@app.post("/unban")
def unban(data: HWIDModel):
    result = collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"banned": False}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="HWID ไม่พบ")
    return {"status": "success"}

# ============================================================
# HEARTBEAT  (ลดเวลา trial & licensed)
# ============================================================
@app.post("/heartbeat")
def heartbeat(data: HeartbeatModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})

    if not u:
        return {"status": "fail", "reason": "user_not_found"}

    # update online
    collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"last_seen": now, "last_heartbeat": now}}
    )

    # time usage
    last_start = u.get("last_start_time")
    if last_start:
        elapsed = now - last_start
        total_usage = u.get("total_usage_sec", 0) + elapsed

        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {
                "total_usage_sec": total_usage,
                "last_start_time": now
            }}
        )

    # Trial expire check
    if u.get("trial"):
        used = u.get("total_usage_sec", 0)
        remaining = max(0, TRIAL_LIMIT_SEC - used)

        if remaining <= 0:
            return {"status": "expired", "remaining": 0}

        return {"status": "active", "remaining": remaining}

    return {"status": "ok", "user_type": "licensed"}

# ============================================================
# START/STOP Trial Session
# ============================================================
@app.post("/start_trial_session")
def start_trial_session(data: HWIDModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})

    if not u or not u.get("trial"):
        raise HTTPException(status_code=404, detail="Trial ไม่พบ")

    collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"last_start_time": now}}
    )
    return {"status": "started"}

@app.post("/stop_trial_session")
def stop_trial_session(data: HWIDModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})

    if not u or not u.get("trial"):
        raise HTTPException(status_code=404, detail="Trial ไม่พบ")

    last_start = u.get("last_start_time")
    if last_start:
        elapsed = now - last_start
        total_usage = u.get("total_usage_sec", 0) + elapsed

        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {"total_usage_sec": total_usage, "last_start_time": None}}
        )

    return {"status": "stopped"}

# ============================================================
# START/STOP Licensed Session
# ============================================================
@app.post("/start_session")
def start_session(data: HWIDModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})

    if not u:
        raise HTTPException(status_code=404, detail="User ไม่พบ")

    collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"last_start_time": now}}
    )
    return {"status": "started"}

@app.post("/stop_session")
def stop_session(data: HWIDModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})

    if not u:
        raise HTTPException(status_code=404, detail="User ไม่พบ")

    last_start = u.get("last_start_time")
    if last_start:
        elapsed = now - last_start
        total_usage = u.get("total_usage_sec", 0) + elapsed

        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {"total_usage_sec": total_usage, "last_start_time": None}}
        )

    return {"status": "stopped"}
