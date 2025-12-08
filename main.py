# CF AutoText License Server (SERVER — userslist returns live total_usage_sec)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
import uuid
import time

app = FastAPI(title="CF AutoText License Server")

# -------------------------
# MongoDB Setup
# -------------------------
# ระวัง: คุณให้ URI มาโดยตรงแล้ว ถ้าจะแชร์ที่สาธารณะให้เปลี่ยนก่อน
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
    username: str | None = None
    mode: str | None = None   # "trial" / "licensed"

# -------------------------
# Settings
# -------------------------
TRIAL_LIMIT_SEC = 7200          # 2 hours
ONLINE_THRESHOLD = 160          # admin GUI uses 160 sec

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
    meta.update_one({"_id": "trial_counter"}, {"$set": {"value": new_val}})
    return new_val

# ============================================================
# ROOT
# ============================================================
@app.get("/")
def root():
    return {"message": "Server is running!"}

# ============================================================
# USERS LIST (Admin)
#  - compute live total_usage_sec if last_start_time exists (do NOT persist)
#  - include 'remaining' (seconds) for trials to help GUI
# ============================================================
@app.get("/userslist")
def userslist():
    users = list(collection.find({}, {"_id": 0}))
    now = int(time.time())

    for u in users:
        # ensure last_seen/last_heartbeat present
        last_seen = int(u.get("last_seen", 0) or 0)
        if "last_heartbeat" not in u:
            u["last_heartbeat"] = u.get("last_seen", last_seen)

        # online boolean
        u["online"] = (now - last_seen <= ONLINE_THRESHOLD)

        # compute live total usage: DB stores total_usage_sec (accumulated) and last_start_time (if running session)
        base_total = int(u.get("total_usage_sec", 0) or 0)
        last_start = u.get("last_start_time")
        if last_start:
            try:
                # last_start might be stored as int or str; be defensive
                last_start_int = int(last_start)
                extra = max(0, now - last_start_int)
            except Exception:
                extra = 0
            computed_total = base_total + extra
        else:
            computed_total = base_total

        # expose computed value under the same key so admin GUI can read total_usage_sec directly
        u["total_usage_sec"] = computed_total

        # if trial: compute remaining seconds (and minutes for backward compatibility)
        if u.get("trial"):
            elapsed = computed_total
            remaining = max(0, TRIAL_LIMIT_SEC - elapsed)
            u["remaining"] = remaining
            u["trial_remaining_minutes"] = max(0, remaining // 60)
        else:
            u["remaining"] = None
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
# CHECK LICENSE
#  - remove trial record with same HWID (if any)
#  - bind hwid if empty
#  - update last_seen/last_heartbeat and set last_start_time if missing so licensed time is tracked
# ============================================================
@app.post("/check_license")
def check_license(req: LicenseCheck):
    u = collection.find_one({"username": req.username, "license": req.license})
    if not u:
        return {"status": "invalid", "message": "License ไม่ถูกต้อง"}

    if u.get("banned"):
        return {"status": "invalid", "message": "บัญชีถูกแบน"}

    # remove trial record for same hwid to avoid duplicate listing
    trial_user = collection.find_one({"hwid": req.hwid, "trial": True})
    if trial_user:
        collection.delete_one({"hwid": req.hwid})

    now = int(time.time())
    # bind hwid if empty
    if not u.get("hwid"):
        collection.update_one(
            {"username": req.username},
            {"$set": {"hwid": req.hwid, "last_seen": now, "last_heartbeat": now, "last_start_time": now}}
        )
    else:
        # check mismatch
        if u["hwid"] != req.hwid:
            return {"status": "invalid", "message": "HWID ไม่ตรง"}
        # update online and ensure last_start_time exists
        upd = {"last_seen": now, "last_heartbeat": now}
        if not u.get("last_start_time"):
            upd["last_start_time"] = now
        collection.update_one({"username": req.username}, {"$set": upd})

    return {"status": "valid"}

# ============================================================
# TRIAL REQUEST
# ============================================================
@app.post("/request_trial")
def request_trial(data: TrialRequestModel):
    hwid = data.hwid
    now = int(time.time())
    u = collection.find_one({"hwid": hwid})

    # create new trial record if none exists
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
        elapsed = int(u.get("total_usage_sec", 0) or 0)
        if u.get("last_start_time"):
            try:
                elapsed += int(time.time()) - int(u.get("last_start_time"))
            except Exception:
                pass

        remaining = max(0, TRIAL_LIMIT_SEC - elapsed)
        collection.update_one({"hwid": hwid}, {"$set": {"last_seen": now}})

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

    elapsed = int(u.get("total_usage_sec", 0) or 0)
    if u.get("last_start_time"):
        try:
            elapsed += now - int(u.get("last_start_time"))
        except Exception:
            pass

    remaining = max(0, TRIAL_LIMIT_SEC - elapsed)

    # online update (so admin shows online)
    collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"last_seen": now, "last_heartbeat": now}}
    )

    if remaining <= 0:
        return {"status": "expired", "remaining": 0}
    return {"status": "active", "remaining": remaining, "username": u["username"]}

# ============================================================
# BAN / UNBAN
# ============================================================
@app.post("/ban")
def ban(data: HWIDModel):
    r = collection.update_one({"hwid": data.hwid}, {"$set": {"banned": True}})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="HWID ไม่พบ")
    return {"status": "success"}

@app.post("/unban")
def unban(data: HWIDModel):
    r = collection.update_one({"hwid": data.hwid}, {"$set": {"banned": False}})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="HWID ไม่พบ")
    return {"status": "success"}

# ============================================================
# HEARTBEAT (counts time for active session if last_start_time present)
# ============================================================
@app.post("/heartbeat")
def heartbeat(data: HeartbeatModel):
    now = int(time.time())

    u = collection.find_one({"hwid": data.hwid})
    if not u:
        return {"status": "fail", "reason": "user_not_found"}

    # update online timestamps immediately
    collection.update_one(
        {"hwid": data.hwid},
        {"$set": {"last_seen": now, "last_heartbeat": now}}
    )

    # re-fetch to read last_start_time and total_usage_sec
    u = collection.find_one({"hwid": data.hwid})

    last_start = u.get("last_start_time")
    if last_start:
        try:
            elapsed = now - int(last_start)
            if elapsed < 0:
                elapsed = 0
        except Exception:
            elapsed = 0
        total_usage = int(u.get("total_usage_sec", 0) or 0) + elapsed
        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {"total_usage_sec": total_usage, "last_start_time": now}}
        )

    # trial logic response
    if u.get("trial"):
        used = int(u.get("total_usage_sec", 0) or 0)
        remaining = max(0, TRIAL_LIMIT_SEC - used)
        if remaining <= 0:
            return {"status": "expired", "remaining": 0}
        return {"status": "active", "remaining": remaining}

    # licensed
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

    collection.update_one({"hwid": data.hwid}, {"$set": {"last_start_time": now}})
    return {"status": "started"}

@app.post("/stop_trial_session")
def stop_trial_session(data: HWIDModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})
    if not u or not u.get("trial"):
        raise HTTPException(status_code=404, detail="Trial ไม่พบ")

    last_start = u.get("last_start_time")
    if last_start:
        try:
            elapsed = now - int(last_start)
            if elapsed < 0:
                elapsed = 0
        except Exception:
            elapsed = 0
        total_usage = int(u.get("total_usage_sec", 0) or 0) + elapsed
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

    collection.update_one({"hwid": data.hwid}, {"$set": {"last_start_time": now}})
    return {"status": "started"}

@app.post("/stop_session")
def stop_session(data: HWIDModel):
    now = int(time.time())
    u = collection.find_one({"hwid": data.hwid})
    if not u:
        raise HTTPException(status_code=404, detail="User ไม่พบ")

    last_start = u.get("last_start_time")
    if last_start:
        try:
            elapsed = now - int(last_start)
            if elapsed < 0:
                elapsed = 0
        except Exception:
            elapsed = 0
        total_usage = int(u.get("total_usage_sec", 0) or 0) + elapsed
        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {"total_usage_sec": total_usage, "last_start_time": None}}
        )
    return {"status": "stopped"}
