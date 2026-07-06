# Multi-user isolation + auth tests. Fake db layer, real Flask routes.
# Run: .venv/Scripts/python.exe test_multiuser.py

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
os.environ["CORS_ORIGIN"] = "https://example.com"  # deployed mode: no dev bypass
os.environ.pop("CORTEX_PASSWORD", None)

import time
import app as A
from db import hash_key

# ---- fake stores --------------------------------------------------------------
_docs = {}
_users = {}
_seq = [1]


def _nid():
    _seq[0] += 1
    return str(_seq[0])


def f_db_list(uid, include_internal=False):
    out = [{"$id": k, "filename": v["filename"], "content": v["content"]}
           for k, v in _docs.items() if v["user_id"] == uid]
    if not include_internal:
        out = [d for d in out if not d["filename"].startswith("__")]
    return out


def f_db_create(uid, filename, content):
    i = _nid()
    _docs[i] = {"user_id": uid, "filename": filename, "content": content}
    return {"$id": i, "filename": filename, "content": content}


def f_db_update(uid, doc_id, content):
    d = _docs.get(doc_id)
    if d and d["user_id"] == uid:
        d["content"] = content


def f_db_delete(uid, doc_id):
    d = _docs.get(doc_id)
    if d and d["user_id"] == uid:
        del _docs[doc_id]


def f_brain_bytes(uid):
    return sum(len(v["content"]) for v in _docs.values() if v["user_id"] == uid)


def f_user_create(email, pw_hash, role="user"):
    i = _nid()
    _users[i] = {"id": i, "email": email, "pw_hash": pw_hash, "role": role,
                 "key_hash": None, "ai_day": None, "ai_count": 0}
    return i


def f_user_by_email(email):
    return next((u for u in _users.values() if u["email"] == email), None)


def f_user_by_id(uid):
    return _users.get(uid)


def f_user_count():
    return len(_users)


def f_user_owner():
    return next((u for u in _users.values() if u["role"] == "owner"), None)


def f_user_set_key_hash(uid, h):
    _users[uid]["key_hash"] = h


def f_user_by_key_hash(h):
    return next((u for u in _users.values() if u["key_hash"] == h), None)


def f_user_set_password(uid, pw_hash):
    _users[uid]["pw_hash"] = pw_hash


def f_user_ai_spend(uid, day, limit):
    u = _users[uid]
    if u["ai_day"] != day:
        u["ai_day"], u["ai_count"] = day, 0
    if u["ai_count"] >= limit:
        return False
    u["ai_count"] += 1
    return True


def f_user_ai_usage(uid, day):
    u = _users[uid]
    return u["ai_count"] if u["ai_day"] == day else 0


rate_allow = [True]


def f_rate_check(bucket, limit, window):
    return rate_allow[0]


def f_log_activity(uid, event):
    pass


def f_get_activity(uid):
    return [{"cluster": "core.md", "action": "read", "source": "t", "ts": time.time()}]


for name, fn in [
    ("db_list", f_db_list), ("db_create", f_db_create), ("db_update", f_db_update),
    ("db_delete", f_db_delete), ("db_brain_bytes", f_brain_bytes),
    ("db_log_activity", f_log_activity), ("db_get_activity", f_get_activity),
    ("user_create", f_user_create), ("user_by_email", f_user_by_email),
    ("user_by_id", f_user_by_id), ("user_count", f_user_count),
    ("user_owner", f_user_owner), ("user_set_key_hash", f_user_set_key_hash),
    ("user_by_key_hash", f_user_by_key_hash), ("user_set_password", f_user_set_password),
    ("user_ai_spend", f_user_ai_spend), ("user_ai_usage", f_user_ai_usage),
    ("rate_check", f_rate_check),
]:
    setattr(A, name, fn)


class FakeAnthropic:
    class messages:
        @staticmethod
        def create(**kw):
            class R:
                content = [type("T", (), {"text": '{"updates": []}'})()]
                stop_reason = "end_turn"
            return R()


A.client = FakeAnthropic()
A.AI_DAILY_QUOTA = 2
A.MAX_BRAIN_BYTES = 5000

passed, failed = [], []


def check(label, cond, detail=""):
    (passed if cond else failed).append(label)
    if not cond:
        print(f"FAIL: {label}\n      {detail}")


ca = A.app.test_client()
cb = A.app.test_client()

# ---- signup + login ------------------------------------------------------------
r = ca.post("/signup", data={"email": "a@test.com", "password": "password-aaa"})
check("signup A redirects", r.status_code == 302, r.status_code)
r = cb.post("/signup", data={"email": "b@test.com", "password": "password-bbb"})
check("signup B redirects", r.status_code == 302, r.status_code)
r = A.app.test_client().post("/signup", data={"email": "a@test.com", "password": "password-xyz"})
check("duplicate email rejected", b"already has an account" in r.data, r.status_code)
r = A.app.test_client().post("/signup", data={"email": "c@test.com", "password": "short"})
check("short password rejected", b"at least 8" in r.data, "")
r = A.app.test_client().post("/signup", data={"email": "d@test.com", "password": "password-ddd", "website": "bot"})
check("honeypot rejected", b"signup failed" in r.data, "")
rate_allow[0] = False
r = A.app.test_client().post("/signup", data={"email": "e@test.com", "password": "password-eee"})
check("signup rate limit", b"too many" in r.data, "")
rate_allow[0] = True

r = A.app.test_client().post("/login", data={"email": "a@test.com", "password": "wrong-password"})
check("wrong login rejected", b"wrong email or password" in r.data, "")

# ---- tenant isolation ------------------------------------------------------------
r = ca.post("/memory/new", json={"filename": "secret_a.md", "content": "## s\n- alpha secret {{2026-07-06|i:3|s:manual}}\n"})
check("A creates memory", r.status_code == 200, r.data)
a_doc_id = next(k for k, v in _docs.items() if v["filename"] == "secret_a.md")

r = ca.get("/brain")
check("A sees own memory", b"alpha secret" in r.data, "")
r = cb.get("/brain")
check("B does NOT see A's memory", b"alpha secret" not in r.data, r.data[:200])

r = cb.get("/memory?file=secret_a.md")
check("B cannot fetch A's file by name", r.status_code == 404, r.status_code)

r = cb.post("/memory", json={"id": a_doc_id, "content": "OVERWRITTEN BY B"})
check("B's cross-tenant write is a no-op", _docs[a_doc_id]["content"] != "OVERWRITTEN BY B",
      _docs[a_doc_id]["content"])

r = cb.delete("/memory", json={"filename": "secret_a.md"})
check("B cannot delete A's file", "secret_a.md" in [v["filename"] for v in _docs.values()], "")

r = cb.get("/search?q=alpha secret")
check("B's search misses A's data", b"alpha secret" not in r.data, "")

# ---- unauthenticated access -------------------------------------------------------
anon = A.app.test_client()
r = anon.get("/brain", headers={"Content-Type": "application/json"})
check("anon /brain blocked", r.status_code in (302, 401), r.status_code)
r = anon.post("/activity", json={"cluster": "x"})
check("anon activity blocked", r.status_code == 401, r.status_code)

# ---- AI quota (2/day for non-owner) -----------------------------------------------
r = cb.post("/chat", json={"message": "hi"})
r = cb.post("/chat", json={"message": "hi"})
check("quota allows first calls", r.status_code == 200, r.status_code)
r = cb.post("/chat", json={"message": "hi"})
check("quota blocks third call (429)", r.status_code == 429, r.status_code)

# ---- storage cap --------------------------------------------------------------------
r = cb.post("/memory/new", json={"filename": "big.md", "content": "x" * 6000})
check("storage cap blocks oversized brain (413)", r.status_code == 413, r.status_code)

# ---- owner unlimited -----------------------------------------------------------------
owner_id = f_user_create("owner@test.com", A.generate_password_hash("owner-password"), role="owner")
co = A.app.test_client()
r = co.post("/login", data={"email": "owner@test.com", "password": "owner-password"})
check("owner logs in", r.status_code == 302, r.status_code)
for _ in range(4):
    r = co.post("/chat", json={"message": "hi"})
check("owner bypasses AI quota", r.status_code == 200, r.status_code)

# ---- account page + key ----------------------------------------------------------------
r = ca.get("/account")
check("account page renders", b"a@test.com" in r.data, r.status_code)
r = ca.post("/account/key")
check("key generated + shown once", b"ctx_" in r.data, "")
a_uid = f_user_by_email("a@test.com")["id"]
check("key stored hashed (not plaintext)", _users[a_uid]["key_hash"] and not _users[a_uid]["key_hash"].startswith("ctx_"), "")

r = ca.post("/account/password", data={"current": "password-aaa", "new": "password-aa2"})
check("password change works", b"password changed" in r.data, r.data[:200])
r = A.app.test_client().post("/login", data={"email": "a@test.com", "password": "password-aa2"})
check("new password logs in", r.status_code == 302, r.status_code)

# ---- MCP-key activity mapping ------------------------------------------------------------
key = "ctx_testkey123"
f_user_set_key_hash(a_uid, hash_key(key))
r = A.app.test_client().post("/activity", json={"cluster": "x.md"}, headers={"X-Api-Key": key})
check("per-user MCP key authenticates activity", r.status_code == 200, r.status_code)
r = A.app.test_client().post("/activity", json={"cluster": "x.md"}, headers={"X-Api-Key": "ctx_invalid"})
check("invalid key rejected", r.status_code == 401, r.status_code)

# ---- mcp_server request-user scoping -------------------------------------------------------
import mcp_server as M

seen = []
M.db_brain_bytes = lambda uid: 0
M.user_owner = lambda: {"id": "OWNERID", "role": "owner"}
M._raw_find = lambda uid, fn: seen.append(uid) or None
M._raw_names = lambda uid: []
tokens = M.set_request_user("USER_B", "user")
try:
    getattr(M.get_cluster, "fn", M.get_cluster)("whatever.md")
finally:
    M.reset_request_user(tokens)
check("mcp tool call carries request user", seen == ["USER_B"], seen)

seen.clear()
M._stdio_uid = None
os.environ.pop("CORTEX_USER_ID", None)
getattr(M.get_cluster, "fn", M.get_cluster)("whatever.md")
check("stdio call resolves owner", seen == ["OWNERID"], seen)

print(f"\n{len(passed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
