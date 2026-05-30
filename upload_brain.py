import os
import glob
import requests
from dotenv import load_dotenv

load_dotenv()

AW_ENDPOINT = os.getenv("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1")
AW_PROJECT = os.getenv("APPWRITE_PROJECT_ID", "")
AW_KEY = os.getenv("APPWRITE_API_KEY", "")
DB_ID = os.getenv("APPWRITE_DATABASE_ID", "")
COL_ID = os.getenv("APPWRITE_COLLECTION_ID", "")
AW_BASE = f"{AW_ENDPOINT}/databases/{DB_ID}/collections/{COL_ID}/documents"
AW_HEADERS = {"X-Appwrite-Project": AW_PROJECT, "X-Appwrite-Key": AW_KEY}

BRAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain")

r = requests.get(AW_BASE, headers=AW_HEADERS)
r.raise_for_status()
existing = {d["filename"]: d["$id"] for d in r.json()["documents"]}

for path in sorted(glob.glob(os.path.join(BRAIN_DIR, "*.md"))):
    fname = os.path.basename(path)
    if fname == "README.md":
        continue
    with open(path) as f:
        content = f.read()
    if fname in existing:
        requests.patch(f"{AW_BASE}/{existing[fname]}",
                       headers={**AW_HEADERS, "Content-Type": "application/json"},
                       json={"data": {"content": content}}).raise_for_status()
        print(f"updated {fname}")
    else:
        requests.post(AW_BASE,
                      headers={**AW_HEADERS, "Content-Type": "application/json"},
                      json={"documentId": "unique()", "data": {"filename": fname, "content": content}}).raise_for_status()
        print(f"created {fname}")

print("done")
