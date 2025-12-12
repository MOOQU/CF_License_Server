# CF AutoText License Server (SERVER — userslist returns live total_usage_sec + session logs)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
import uuid
import time
from typing import Optional, Dict, Any, List

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
    version: Optional[str] = None

class UsernameModel(BaseModel):
    username: str

class HWIDModel(BaseModel):
    hwid: str

class TrialRequestModel(BaseModel):
    hwid: str
    version: Optional[str] = None

class HeartbeatModel(BaseModel):
    hwid: str
    username: Optional[str] = None
    mode: Optional[str] = None   # "trial" / "licensed"

class DaysModel(BaseModel):
    days: int

# -------------------------
# Settings
# -------------------------
TRIAL_LIMIT_SEC = 7200          # 2 hours
ONLINE_THRESHOLD = 160          # admin GUI uses 160 sec
SESSION_HISTORY_LIMIT = 50      # how many session entries to keep per user (to avoid unbounded growth)
SESSION_HISTORY_RETENTION_DAYS = 7  # retention window for session_history (7 days)

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

def now_ts() -> int:
    return int(time.time())

def append_session_history(user_doc: Dict[str, Any], start: int, end: int):
    """Append a session dict to session_history (server-side). Keep size bounded."""
    hist = user_doc.get("session_history") or []
    hist.append({"start": int(start), "end": int(end)})
    # keep only the latest SESSION_HISTORY_LIMIT entries
    hist = hist[-SESSION_HISTORY_LIMIT:]
    return hist

def clean_old_sessions_for_user(username: str, days: int = SESSION_HISTORY_RETENTION_DAYS):
    """
    Remove session_history entries for a user older than `days`.
    This enforces the time-based retention (e.g. 7 days).
    """
    cutoff = now_ts() - (days * 24 * 60 * 60)
    u = collection.find_one({"username": username})
    if not u:
        return
    hist = u.get("session_history", []) or []
    new_hist = [s for s in hist if s.get("end", 0) >= cutoff]
    # Also trim to SESSION_HISTORY_LIMIT for safety
    new_hist = new_hist[-SESSION_HISTORY_LIMIT:]
    if len(new_hist) != len(hist):
        collection.update_one({"username": username}, {"$set": {"session_history": new_hist}})

def clean_old_sessions_global(days: int = SESSION_HISTORY_RETENTION_DAYS):
    """
    Iterate all users and remove session_history entries older than `days`.
    Use cautiously (it's a DB operation over all users).
    """
    cutoff = now_ts() - (days * 24 * 60 * 60)
    for u in collection.find({}, {"username": 1, "session_history": 1}):
        hist = u.get("session_history", []) or []
        new_hist = [s for s in hist if s.get("end", 0) >= cutoff]
        new_hist = new_hist[-SESSION_HISTORY_LIMIT:]
        if len(new_hist) != len(hist):
            collection.update_one({"username": u["username"]}, {"$set": {"session_history": new_hist}})

def ensure_timestamps_for_existing_users():
    """Backfill created_at / license_activated_at / trial_started_at for existing users without them.
       This is safe: it will set timestamps to 'now' only if the fields don't exist.
    """
    now = now_ts()
    users = collection.find({})
    ops = []
    for u in users:
        updates = {}
        if "created_at" not in u:
            updates["created_at"] = now
        if u.get("user_type") == "licensed" and "license_activated_at" not in u:
            # If there's any existing 'last_start_time' or total_usage > 0, we consider this "activated" at now (can't know real past).
            updates["license_activated_at"] = now
        if u.get("user_type") == "trial" and "trial_started_at" not in u:
            updates["trial_started_at"] = now
        # ensure session_history field exists
        if "session_history" not in u:
            updates["session_history"] = u.get("session_history", [])
        # ensure opened_at/closed_at keys exist (may be None)
        if "opened_at" not in u:
            updates["opened_at"] = u.get("opened_at", None)
        if "closed_at" not in u:
            updates["closed_at"] = u.get("closed_at", None)
        if updates:
            collection.update_one({"_id": u["_id"]}, {"$set": updates})

# Run one-time backfill on startup
try:
    ensure_timestamps_for_existing_users()
except Exception:
    # If Mongo can't be reached at import time, this will be retried on actual endpoint calls; keep server running.
    pass

# ============================================================
# ROOT
# ============================================================
@app.get("/")
def root():
    return {"message": "Server is running!"}

# ============================================================
# USERS LIST (Admin)
#  - compute live total_usage_sec if last_start_time exists (do NOT persist computed value)
#  - compute last_opened_at / last_closed_at from session info or heuristic (last_heartbeat + threshold)
#  - include 'remaining' (seconds) for trials to help GUI
#  - include session_history (trimmed) and last session info
# ============================================================
@app.get("/userslist")
def userslist():
    users = list(collection.find({}, {"_id": 0}))  # do not return Mongo _id
    now = now_ts()

    out_users = []
    for u in users:
        # defensive defaults
        u = dict(u)  # copy to avoid mutating original doc
        last_seen = int(u.get("last_seen", 0) or 0)
        if "last_heartbeat" not in u:
            u["last_heartbeat"] = u.get("last_seen", last_seen)

        # compute online boolean
        u["online"] = (now - last_seen <= ONLINE_THRESHOLD)

        # compute live total usage: DB stores total_usage_sec (accumulated) and last_start_time (if running session)
        base_total = int(u.get("total_usage_sec", 0) or 0)
        last_start = u.get("last_start_time")
        if last_start:
            try:
                last_start_int = int(last_start)
                extra = max(0, now - last_start_int)
            except Exception:
                extra = 0
            computed_total = base_total + extra
        else:
            computed_total = base_total

        # expose computed value under the same key so admin GUI can read total_usage_sec directly
        u["total_usage_sec"] = computed_total

        # trial remaining
        if u.get("trial"):
            elapsed = computed_total
            remaining = max(0, TRIAL_LIMIT_SEC - elapsed)
            u["remaining"] = remaining
            u["trial_remaining_minutes"] = max(0, remaining // 60)
        else:
            u["remaining"] = None
            u["trial_remaining_minutes"] = "-"

        # session info:
        # prefer the explicit session_history/last session fields if present
        session_history = u.get("session_history", []) or []
        # trim to SESSION_HISTORY_LIMIT for safety, client will request history endpoints for time-range filtering
        session_history = session_history[-SESSION_HISTORY_LIMIT:]
        u["session_history"] = session_history

        # determine last_opened_at and last_closed_at
        # If user has explicit opened_at/closed_at fields, use them. Otherwise fallback to heuristics.
        last_opened = u.get("opened_at")
        last_closed = u.get("closed_at")

        # If currently online, we consider closed_at = None (not closed yet).
        if u["online"]:
            last_closed = None
        else:
            # if closed_at not present or None, compute heuristic from last_heartbeat + threshold
            if not last_closed:
                last_heartbeat = int(u.get("last_heartbeat", 0) or 0)
                if last_heartbeat:
                    last_closed = last_heartbeat + ONLINE_THRESHOLD
                else:
                    last_closed = None

        u["last_opened_at"] = last_opened
        u["last_closed_at"] = last_closed

        u["opened_at"] = last_opened
        u["closed_at"] = last_closed

        # ensure created_at/license/trial timestamps present in returned object
        u["created_at"] = u.get("created_at")
        u["license_activated_at"] = u.get("license_activated_at")
        u["trial_started_at"] = u.get("trial_started_at")

        # ensure basic fields exist
        u["username"] = u.get("username", "")
        u["license"] = u.get("license", "")
        u["hwid"] = u.get("hwid", "")
        u["banned"] = bool(u.get("banned", False))
        u["user_type"] = u.get("user_type")

        out_users.append(u)

    return {"users": out_users}

# ============================================================
# GENERATE LICENSE
# ============================================================
@app.post("/usersgen_license")
def gen_license(user: UsernameModel):
    if collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="User มีอยู่แล้ว")

    license_key = gen_license_key()
    now = now_ts()

    doc = {
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
        "user_type": "licensed",
        # new timestamp fields
        "created_at": now,
        "license_activated_at": now,
        # session tracking
        "opened_at": None,
        "closed_at": None,
        "session_history": []
    }

    collection.insert_one(doc)
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
#  - when starting session, set opened_at if not set
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

    now = now_ts()
    # bind hwid if empty
    if not u.get("hwid"):
        collection.update_one(
            {"username": req.username},
            {"$set": {
                "hwid": req.hwid,
                "last_seen": now,
                "last_heartbeat": now,
                "last_start_time": now,
                "opened_at": now,
                "closed_at": None,
                "created_at": u.get("created_at", now),
                "license_activated_at": u.get("license_activated_at", now)
            }}
        )
    else:
        # check mismatch
        if u["hwid"] != req.hwid:
            return {"status": "invalid", "message": "HWID ไม่ตรง"}
        # update online and ensure last_start_time exists
        upd = {"last_seen": now, "last_heartbeat": now}
        if not u.get("last_start_time"):
            upd["last_start_time"] = now
            upd["opened_at"] = now
            upd["closed_at"] = None
        collection.update_one({"username": req.username}, {"$set": upd})

    return {"status": "valid"}

# ============================================================
# TRIAL REQUEST
# ============================================================
@app.post("/request_trial")
def request_trial(data: TrialRequestModel):
    hwid = data.hwid
    now = now_ts()
    u = collection.find_one({"hwid": hwid})

    # create new trial record if none exists
    if not u:
        tid = get_next_trial_id()
        username = f"TRIAL USER {tid}"

        doc = {
            "username": username,
            "license": "",
            "hwid": hwid,
            "trial": True,
            "trial_start": now,
            "trial_started_at": now,
            "last_start_time": now,
            "total_usage_sec": 0,
            "status": "active",
            "banned": False,
            "last_seen": now,
            "last_heartbeat": now,
            "user_type": "trial",
            "created_at": now,
            "opened_at": now,
            "closed_at": None,
            "session_history": []
        }
        collection.insert_one(doc)
        return {"status": "active", "username": username, "remaining": TRIAL_LIMIT_SEC}

    # banned
    if u.get("banned"):
        return {"status": "banned", "remaining": 0}

    # still in trial
    if u.get("trial"):
        # compute elapsed including running session if any
        elapsed = int(u.get("total_usage_sec", 0) or 0)
        if u.get("last_start_time"):
            try:
                elapsed += int(time.time()) - int(u.get("last_start_time"))
            except Exception:
                pass

        remaining = max(0, TRIAL_LIMIT_SEC - elapsed)
        collection.update_one({"hwid": hwid}, {"$set": {"last_seen": now, "last_heartbeat": now}})
        if remaining <= 0:
            return {"status": "expired", "remaining": 0}
        return {"status": "active", "username": u["username"], "remaining": remaining}

    return {"status": "expired", "remaining": 0}

# ============================================================
# CHECK TRIAL STATUS
# ============================================================
@app.post("/check_trial")
def check_trial(data: HWIDModel):
    now = now_ts()
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
#  - update last_seen/last_heartbeat immediately
#  - if last_start_time exists we compute elapsed and add to total_usage_sec and reset last_start_time to now
#  - keep opened_at/closed_at semantics (opened_at set when session starts, closed_at cleared while running)
# ============================================================
@app.post("/heartbeat")
def heartbeat(data: HeartbeatModel):
    now = now_ts()

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

        # update total_usage_sec and bump last_start_time to now (continue running session)
        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {"total_usage_sec": total_usage, "last_start_time": now, "opened_at": u.get("opened_at", now), "closed_at": None}}
        )

    # trial logic response
    u = collection.find_one({"hwid": data.hwid})
    if u and u.get("trial"):
        used = int(u.get("total_usage_sec", 0) or 0)
        # if there's a running session, elapsed already added above; computed remaining:
        remaining = max(0, TRIAL_LIMIT_SEC - used)
        if remaining <= 0:
            return {"status": "expired", "remaining": 0}
        return {"status": "active", "remaining": remaining}

    return {"status": "ok", "user_type": "licensed"}

# ============================================================
# START/STOP Trial Session
#  - set opened_at when session starts; clear closed_at
#  - on stop: compute elapsed, update total_usage_sec, set closed_at, append to session_history
# ============================================================
@app.post("/start_trial_session")
def start_trial_session(data: HWIDModel):
    now = now_ts()
    u = collection.find_one({"hwid": data.hwid})
    if not u or not u.get("trial"):
        raise HTTPException(status_code=404, detail="Trial ไม่พบ")

    # If already running, do not reset last_start_time — just ensure opened_at set
    if u.get("last_start_time"):
        collection.update_one({"hwid": data.hwid}, {"$set": {"opened_at": u.get("opened_at", now), "closed_at": None}})
        return {"status": "already_running"}

    collection.update_one({"hwid": data.hwid}, {"$set": {"last_start_time": now, "opened_at": now, "closed_at": None}})
    return {"status": "started"}

@app.post("/stop_trial_session")
def stop_trial_session(data: HWIDModel):
    now = now_ts()
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
        # append session_history (use original u doc to avoid races)
        hist = u.get("session_history", []) or []
        hist = append_session_history(u, int(last_start), now)
        # update accumulated usage and clear running flag
        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {"total_usage_sec": total_usage, "last_start_time": None, "closed_at": now, "opened_at": u.get("opened_at")}}
        )
        # update session_history separately to avoid race overwrite issues
        collection.update_one({"hwid": data.hwid}, {"$set": {"session_history": hist}})
        # clean sessions older than retention window (e.g. 7 days)
        if u.get("username"):
            clean_old_sessions_for_user(u["username"], days=SESSION_HISTORY_RETENTION_DAYS)
    else:
        # No running session — still ensure closed_at is set to now (idempotent)
        collection.update_one({"hwid": data.hwid}, {"$set": {"closed_at": now, "last_start_time": None}})
    return {"status": "stopped"}

# ============================================================
# START/STOP Licensed Session
#  - same as trial but for licensed users
#  - when starting, set opened_at if none; clear closed_at
#  - when stopping, compute elapsed, append session, set closed_at
# ============================================================
@app.post("/start_session")
def start_session(data: HWIDModel):
    now = now_ts()
    u = collection.find_one({"hwid": data.hwid})
    if not u:
        raise HTTPException(status_code=404, detail="User ไม่พบ")

    # If session already running don't reset last_start_time (avoid erasing original start)
    if u.get("last_start_time"):
        # ensure opened_at exists and closed_at cleared
        collection.update_one({"hwid": data.hwid}, {"$set": {"opened_at": u.get("opened_at", now), "closed_at": None}})
        return {"status": "already_running"}

    # start new session
    collection.update_one({"hwid": data.hwid}, {"$set": {"last_start_time": now, "opened_at": now, "closed_at": None}})
    return {"status": "started"}

@app.post("/stop_session")
def stop_session(data: HWIDModel):
    now = now_ts()
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
        # Build new history entry from the last_start we fetched from DB
        hist = u.get("session_history", []) or []
        hist = append_session_history(u, int(last_start), now)
        # apply updates: accumulate usage, clear running flag, set closed_at
        collection.update_one(
            {"hwid": data.hwid},
            {"$set": {"total_usage_sec": total_usage, "last_start_time": None, "closed_at": now}}
        )
        # store session_history separately to avoid overwrite races
        collection.update_one({"hwid": data.hwid}, {"$set": {"session_history": hist}})
        # clean sessions older than retention window (e.g. 7 days)
        if u.get("username"):
            clean_old_sessions_for_user(u["username"], days=SESSION_HISTORY_RETENTION_DAYS)
    else:
        # No running session found — idempotent set closed_at
        collection.update_one({"hwid": data.hwid}, {"$set": {"closed_at": now, "last_start_time": None}})
    return {"status": "stopped"}

# ============================================================
# Optional: endpoint to fetch logs (simple)
#  - this endpoint can be used by Admin GUI to fetch human-readable logs
#  - it compiles recent sessions for all users (last N entries)
# ============================================================
@app.get("/logs")
def logs(limit: int = 200):
    """Return a simple logs view: recent session starts/stops and important events."""
    # We'll create a simple list of events from session_history and created_at/license events.
    users = list(collection.find({}, {"username": 1, "session_history": 1, "created_at": 1, "license_activated_at": 1, "trial_started_at": 1, "banned": 1, "hwid": 1, "_id": 0}))
    events = []
    for u in users:
        username = u.get("username")
        hwid = u.get("hwid")
        created_at = u.get("created_at")
        if created_at:
            events.append({"ts": int(created_at), "type": "created", "username": username, "hwid": hwid})
        if u.get("license_activated_at"):
            events.append({"ts": int(u.get("license_activated_at")), "type": "license_activated", "username": username, "hwid": hwid})
        if u.get("trial_started_at"):
            events.append({"ts": int(u.get("trial_started_at")), "type": "trial_started", "username": username, "hwid": hwid})
        for s in (u.get("session_history") or [])[-10:]:
            events.append({"ts": int(s.get("start")), "type": "session_start", "username": username, "hwid": hwid})
            events.append({"ts": int(s.get("end")), "type": "session_end", "username": username, "hwid": hwid})
    # sort events by ts desc
    events_sorted = sorted(events, key=lambda x: x["ts"], reverse=True)[:limit]
    # human readable mapping
    for e in events_sorted:
        e["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
    return {"events": events_sorted}

# ============================================================
# NEW: user session history endpoint (time-filtered)
# ============================================================
@app.get("/user_session_history")
def user_session_history(username: str, days: int = SESSION_HISTORY_RETENTION_DAYS):
    """
    Return session entries for a user filtered by a retention window (days).
    Default days uses SESSION_HISTORY_RETENTION_DAYS.
    """
    cutoff = now_ts() - (days * 24 * 60 * 60)
    u = collection.find_one({"username": username}, {"session_history": 1, "_id": 0})
    if not u:
        return {"history": []}
    hist = u.get("session_history", []) or []
    filtered = [
        {"start": s.get("start"), "end": s.get("end"), "length": (s.get("end", 0) - s.get("start", 0))}
        for s in hist
        if s.get("end", 0) >= cutoff
    ]
    # return newest first
    filtered_sorted = sorted(filtered, key=lambda x: x["start"], reverse=True)
    return {"history": filtered_sorted}

# ============================================================
# NEW: Clear logs endpoints
# ============================================================
@app.post("/clear_user_logs")
def clear_user_logs(payload: UsernameModel):
    username = payload.username
    collection.update_one({"username": username}, {"$set": {"session_history": []}})
    return {"status": "ok", "message": f"Logs cleared for {username}"}

@app.post("/clear_all_logs")
def clear_all_logs():
    collection.update_many({}, {"$set": {"session_history": []}})
    return {"status": "ok", "message": "All logs cleared"}

@app.post("/clear_logs_days")
def clear_logs_days(payload: DaysModel):
    days = int(payload.days)
    cutoff = now_ts() - (days * 24 * 60 * 60)
    for u in collection.find({}, {"username": 1, "session_history": 1}):
        hist = u.get("session_history", []) or []
        new_hist = [s for s in hist if s.get("end", 0) >= cutoff]
        new_hist = new_hist[-SESSION_HISTORY_LIMIT:]
        if len(new_hist) != len(hist):
            collection.update_one({"username": u["username"]}, {"$set": {"session_history": new_hist}})
    return {"status": "ok", "message": f"Cleared sessions older than {days} days"}

# ============================================================
# End of file
# ============================================================
