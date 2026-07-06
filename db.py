import os
from functools import lru_cache
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "cortex")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "memories")


@lru_cache(maxsize=1)
def _col():
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI not set — add it to .env")
    client = MongoClient(MONGODB_URI, appname="cortex", serverSelectionTimeoutMS=8000)
    col = client[MONGODB_DB][MONGODB_COLLECTION]
    col.create_index("filename", unique=True)
    return col


def _shape(d):
    return {"$id": str(d["_id"]), "filename": d["filename"], "content": d.get("content", "")}


def db_list(include_internal=False):
    docs = [_shape(d) for d in _col().find({}, {"filename": 1, "content": 1})]
    if not include_internal:
        docs = [d for d in docs if not d["filename"].startswith("__")]
    return docs


def db_find(filename):
    d = _col().find_one({"filename": filename})
    return _shape(d) if d else None


def db_names():
    return sorted(d["filename"] for d in _col().find({}, {"filename": 1})
                  if not d["filename"].startswith("__"))


def db_create(filename, content):
    res = _col().insert_one({"filename": filename, "content": content})
    return {"$id": str(res.inserted_id), "filename": filename, "content": content}


def db_update(doc_id, content):
    _col().update_one({"_id": ObjectId(doc_id)}, {"$set": {"content": content}})


def db_update_cas(doc_id, old_content, new_content):
    """Compare-and-swap: only write if the doc still holds old_content."""
    res = _col().update_one({"_id": ObjectId(doc_id), "content": old_content},
                            {"$set": {"content": new_content}})
    return res.modified_count == 1


def db_rename(doc_id, filename):
    _col().update_one({"_id": ObjectId(doc_id)}, {"$set": {"filename": filename}})


def db_delete(doc_id):
    _col().delete_one({"_id": ObjectId(doc_id)})


def db_log_activity(event):
    """Append one activity event in a single round trip (capped ring buffer)."""
    _col().update_one({"filename": "__activity__"},
                      {"$push": {"events": {"$each": [event], "$slice": -30}}},
                      upsert=True)


def db_get_activity():
    d = _col().find_one({"filename": "__activity__"}, {"events": 1, "content": 1})
    if not d:
        return []
    events = d.get("events", [])
    if not events and d.get("content", "").strip():
        # legacy newline-JSON format
        import json
        for line in d["content"].strip().split("\n"):
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events
