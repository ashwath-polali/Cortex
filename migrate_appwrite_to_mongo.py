import os
import sys
import requests
from dotenv import load_dotenv
from db import _col

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

AW_ENDPOINT = os.getenv("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1")
AW_PROJECT = os.getenv("APPWRITE_PROJECT_ID", "")
AW_KEY = os.getenv("APPWRITE_API_KEY", "")
DB_ID = os.getenv("APPWRITE_DATABASE_ID", "")
COL_ID = os.getenv("APPWRITE_COLLECTION_ID", "")
AW_BASE = f"{AW_ENDPOINT}/databases/{DB_ID}/collections/{COL_ID}/documents"
AW_HEADERS = {"X-Appwrite-Project": AW_PROJECT, "X-Appwrite-Key": AW_KEY}


def fetch_all():
    docs = []
    offset = 0
    while True:
        r = requests.get(AW_BASE, headers=AW_HEADERS, params={"limit": 100, "offset": offset}, timeout=30)
        if r.status_code == 402:
            print("Appwrite reads still capped (402). Wait for the monthly reset or upgrade, then re-run.")
            sys.exit(1)
        r.raise_for_status()
        page = r.json()["documents"]
        docs.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return docs


def main():
    docs = fetch_all()
    col = _col()
    migrated = 0
    for d in docs:
        col.update_one(
            {"filename": d["filename"]},
            {"$set": {"filename": d["filename"], "content": d.get("content", "")}},
            upsert=True,
        )
        migrated += 1
        print(f"  {d['filename']} ({len(d.get('content', ''))} chars)")
    print(f"\nmigrated {migrated} documents from Appwrite -> MongoDB ({col.database.name}.{col.name})")


if __name__ == "__main__":
    main()
