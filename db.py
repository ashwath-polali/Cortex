import os
import time
import hashlib
from functools import lru_cache
from pymongo import MongoClient, ASCENDING
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "cortex")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "memories")


@lru_cache(maxsize=1)
def _client():
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI not set — add it to .env")
    return MongoClient(MONGODB_URI, appname="cortex", serverSelectionTimeoutMS=8000)


@lru_cache(maxsize=1)
def _col():
    col = _client()[MONGODB_DB][MONGODB_COLLECTION]
    col.create_index([("user_id", ASCENDING), ("filename", ASCENDING)], unique=True)
    return col


@lru_cache(maxsize=1)
def _users():
    col = _client()[MONGODB_DB]["users"]
    col.create_index("email", unique=True)
    col.create_index("mcp_key_hash", sparse=True)
    return col


@lru_cache(maxsize=1)
def _ratelimits():
    return _client()[MONGODB_DB]["ratelimits"]


def _shape(d):
    return {"$id": str(d["_id"]), "filename": d["filename"], "content": d.get("content", "")}


# ---- brain docs (every query is tenant-scoped; user_id is never optional) ----

def db_list(user_id, include_internal=False):
    docs = [_shape(d) for d in _col().find({"user_id": user_id}, {"filename": 1, "content": 1})]
    if not include_internal:
        docs = [d for d in docs if not d["filename"].startswith("__")]
    return docs


def db_find(user_id, filename):
    d = _col().find_one({"user_id": user_id, "filename": filename})
    return _shape(d) if d else None


def db_names(user_id):
    return sorted(d["filename"] for d in _col().find({"user_id": user_id}, {"filename": 1})
                  if not d["filename"].startswith("__"))


def db_create(user_id, filename, content):
    res = _col().insert_one({"user_id": user_id, "filename": filename, "content": content})
    return {"$id": str(res.inserted_id), "filename": filename, "content": content}


def db_update(user_id, doc_id, content):
    _col().update_one({"_id": ObjectId(doc_id), "user_id": user_id}, {"$set": {"content": content}})


def db_update_cas(user_id, doc_id, old_content, new_content):
    res = _col().update_one({"_id": ObjectId(doc_id), "user_id": user_id, "content": old_content},
                            {"$set": {"content": new_content}})
    return res.modified_count == 1


def db_rename(user_id, doc_id, filename):
    _col().update_one({"_id": ObjectId(doc_id), "user_id": user_id}, {"$set": {"filename": filename}})


def db_delete(user_id, doc_id):
    _col().delete_one({"_id": ObjectId(doc_id), "user_id": user_id})


def db_brain_bytes(user_id):
    agg = _col().aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": None, "n": {"$sum": {"$strLenBytes": {"$ifNull": ["$content", ""]}}}}},
    ])
    for row in agg:
        return row["n"]
    return 0


def adopt_orphan_docs(user_id):
    """Assign pre-multiuser docs (no user_id) to the given user. Bootstrap only."""
    res = _col().update_many({"user_id": {"$exists": False}}, {"$set": {"user_id": user_id}})
    return res.modified_count


def db_all_users_docs():
    """Maintenance only (expiry auto-decay). Returns (user_id, doc) pairs."""
    return [(d["user_id"], _shape(d)) for d in _col().find({}, {"user_id": 1, "filename": 1, "content": 1})]


# ---- activity (per user) ------------------------------------------------------

def db_log_activity(user_id, event):
    _col().update_one({"user_id": user_id, "filename": "__activity__"},
                      {"$push": {"events": {"$each": [event], "$slice": -30}}},
                      upsert=True)


def db_get_activity(user_id):
    d = _col().find_one({"user_id": user_id, "filename": "__activity__"}, {"events": 1})
    return d.get("events", []) if d else []


# ---- users ---------------------------------------------------------------------

def _shape_user(u):
    if not u:
        return None
    return {"id": str(u["_id"]), "email": u["email"], "pw_hash": u["pw_hash"],
            "role": u.get("role", "user"), "created": u.get("created")}


def user_create(email, pw_hash, role="user"):
    res = _users().insert_one({"email": email.lower().strip(), "pw_hash": pw_hash,
                               "role": role, "created": time.time()})
    return str(res.inserted_id)


def user_by_email(email):
    return _shape_user(_users().find_one({"email": email.lower().strip()}))


def user_by_id(user_id):
    try:
        return _shape_user(_users().find_one({"_id": ObjectId(user_id)}))
    except Exception:
        return None


def user_count():
    return _users().count_documents({})


def user_owner():
    return _shape_user(_users().find_one({"role": "owner"}))


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def user_set_key_hash(user_id, key_hash):
    _users().update_one({"_id": ObjectId(user_id)}, {"$set": {"mcp_key_hash": key_hash}})


def user_by_key_hash(key_hash):
    return _shape_user(_users().find_one({"mcp_key_hash": key_hash}))


def user_set_password(user_id, pw_hash):
    _users().update_one({"_id": ObjectId(user_id)}, {"$set": {"pw_hash": pw_hash}})


def user_ai_spend(user_id, day, limit):
    """Atomically count one AI call against today's quota. True if allowed."""
    res = _users().update_one(
        {"_id": ObjectId(user_id), "$or": [{"ai_day": {"$ne": day}}, {"ai_count": {"$lt": limit}}]},
        [{"$set": {
            "ai_count": {"$cond": [{"$eq": ["$ai_day", day]}, {"$add": [{"$ifNull": ["$ai_count", 0]}, 1]}, 1]},
            "ai_day": day,
        }}],
    )
    return res.modified_count == 1


def user_ai_usage(user_id, day):
    u = _users().find_one({"_id": ObjectId(user_id)}, {"ai_day": 1, "ai_count": 1})
    if not u or u.get("ai_day") != day:
        return 0
    return u.get("ai_count", 0)


# ---- rate limiting (DB-backed: survives serverless instance churn) --------------

def rate_check(bucket, limit, window_sec):
    """True if this event is allowed; records it. Sliding window per bucket key."""
    now = time.time()
    _ratelimits().update_one({"_id": bucket}, {"$pull": {"ts": {"$lt": now - window_sec}}}, upsert=True)
    d = _ratelimits().find_one({"_id": bucket}, {"ts": 1})
    if d and len(d.get("ts", [])) >= limit:
        return False
    _ratelimits().update_one({"_id": bucket}, {"$push": {"ts": now}}, upsert=True)
    return True
