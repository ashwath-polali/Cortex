# One-time multi-user migration: creates the owner account, stamps every
# existing doc with the owner's user_id, and swaps the unique index from
# (filename) to (user_id, filename). Prints a temp password once — change it
# at /account after first login.

import sys
import secrets
sys.stdout.reconfigure(encoding="utf-8")
from werkzeug.security import generate_password_hash
from db import _col, _users, user_owner, user_create, adopt_orphan_docs

if len(sys.argv) < 2:
    print("usage: python migrate_multiuser.py <owner-email>")
    sys.exit(1)
OWNER_EMAIL = sys.argv[1]

owner = user_owner()
if owner:
    print(f"owner already exists: {owner['email']}")
    uid = owner["id"]
else:
    temp = secrets.token_urlsafe(12)
    uid = user_create(OWNER_EMAIL, generate_password_hash(temp), role="owner")
    print(f"owner created: {OWNER_EMAIL}")
    print(f"TEMP PASSWORD (change at /account after login): {temp}")

n = adopt_orphan_docs(uid)
print(f"stamped {n} docs with owner user_id")

# drop the old single-field unique filename index if present
for idx in _col().list_indexes():
    if idx["name"] == "filename_1":
        _col().drop_index("filename_1")
        print("dropped legacy unique filename index")

names = [i["name"] for i in _col().list_indexes()]
print(f"indexes now: {names}")
docs = _col().count_documents({"user_id": {"$exists": False}})
print(f"docs without user_id remaining: {docs} (must be 0)")
print(f"users: {_users().count_documents({})}")
