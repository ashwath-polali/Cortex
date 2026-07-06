import os
import json
import re
import time
import hmac
import secrets as pysecrets
from functools import wraps
from datetime import date, timedelta
from flask import Flask, request, jsonify, send_file, session, redirect
from flask_cors import CORS
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app, origins=[os.getenv("CORS_ORIGIN", "http://localhost:5000"), "http://127.0.0.1:5000", "http://localhost:5000"])

# no static fallback: a known secret key means forgeable session cookies.
# without the env var, sessions just reset on each deploy — safe failure mode.
app.secret_key = os.getenv("FLASK_SECRET_KEY") or pysecrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("VERCEL_ENV"))

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-haiku-4-5-20251001"
CORTEX_PASSWORD = os.getenv("CORTEX_PASSWORD", "")
CORTEX_MCP_KEY = os.getenv("CORTEX_MCP_KEY", "")
AI_DAILY_QUOTA = int(os.getenv("AI_DAILY_QUOTA", "10"))
MAX_BRAIN_BYTES = int(os.getenv("MAX_BRAIN_BYTES", "2000000"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))

from werkzeug.security import generate_password_hash, check_password_hash
from db import (db_list, db_create, db_update, db_delete, db_log_activity,
                db_get_activity, db_brain_bytes, db_all_users_docs,
                user_create, user_by_email, user_by_id, user_count, user_owner,
                user_set_key_hash, user_by_key_hash, user_set_password,
                user_ai_spend, user_ai_usage, hash_key, rate_check)

_owner_cache = {}


def _owner():
    if "u" not in _owner_cache:
        _owner_cache["u"] = user_owner()
    return _owner_cache["u"]


def _uid_web():
    uid = session.get("user_id")
    if uid:
        return uid
    if _dev_mode():
        o = _owner()
        if o:
            return o["id"]
    return None


def _role_web():
    return session.get("role", "owner" if _dev_mode() else "user")


def _check_storage(added_len=0):
    """Non-owner writes respect the per-user brain cap."""
    if _role_web() == "owner":
        return True
    return db_brain_bytes(_uid_web()) + added_len <= MAX_BRAIN_BYTES


def aw_list():
    return {"documents": db_list(_uid_web())}

def aw_create(data):
    return db_create(_uid_web(), data["filename"], data.get("content", ""))

def aw_update(doc_id, data):
    db_update(_uid_web(), doc_id, data["content"])

def aw_delete(doc_id):
    db_delete(_uid_web(), doc_id)

SEED_TEMPLATES = [
    {"filename": "core.md", "content": "## identity\n- name:\n- location:\n- goal:\n"},
    {"filename": "notes.md", "content": "## notes\n"},
    {"filename": "projects.md", "content": "## projects\n"},
    {"filename": "strategy.md", "content": "## strategy\n"},
    {"filename": "preferences.md", "content": "## preferences\n"},
    {"filename": "context.md", "content": "## context\n"},
    {"filename": "people.md", "content": "## people\n"},
    {"filename": "robots.md", "content": "## robotics\n"},
]

STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    "of", "with", "and", "or", "but", "not", "this", "that", "it", "be", "has",
    "had", "have", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "can", "from", "by", "as", "so", "if", "than", "then", "only", "just",
    "also", "very", "too", "more", "most", "some", "any", "all", "each", "every",
    "such", "into", "over", "after", "before", "between", "about", "like", "been",
    "being", "its", "my", "our", "your", "his", "her", "their", "what", "which",
    "who", "whom", "when", "where", "how", "why", "no", "yes", "up", "out", "new",
    "old", "first", "since", "one", "two", "three",
}


def extract_doc_keywords(doc):
    text = doc["content"].lower()
    text = re.sub(r'\{\{[^}]*\}\}', '', text)
    text = re.sub(r'<!--[^>]*-->', '', text)
    words = re.findall(r'[a-z]{3,}', text)
    freq = {}
    for w in words:
        if w not in STOP_WORDS:
            freq[w] = freq.get(w, 0) + 1
    fname_stem = doc["filename"].replace(".md", "").lower()
    keywords = [fname_stem]
    for w in fname_stem.split("_"):
        if len(w) >= 3:
            keywords.append(w)
    for w, c in sorted(freq.items(), key=lambda x: x[1], reverse=True):
        keywords.append(w)
        if len(keywords) >= 20:
            break
    headers = re.findall(r'##\s+(.+)', doc["content"])
    for h in headers:
        for w in re.findall(r'[a-z]{3,}', h.lower()):
            if w not in STOP_WORDS and w not in keywords:
                keywords.append(w)
    return keywords


def select_files(topic, all_docs, max_files=3):
    topic_lower = topic.lower()
    topic_words = set(re.findall(r'[a-z]{3,}', topic_lower)) - STOP_WORDS
    scored = []
    for doc in all_docs:
        if doc["filename"].startswith("_"):
            continue
        fname_stem = doc["filename"].replace(".md", "").lower()
        score = 0
        if fname_stem in topic_lower:
            score += 5
        for w in fname_stem.split("_"):
            if len(w) >= 3 and w in topic_lower:
                score += 3
        doc_kw = set(extract_doc_keywords(doc))
        overlap = topic_words & doc_kw
        score += len(overlap) * 2
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored:
        selected = [d for _, d in scored[:max_files]]
    else:
        selected = [d for d in all_docs if d["filename"] == "core.md"]
    return {doc["filename"]: doc["content"] for doc in selected}

def files_as_text(files):
    return "\n".join(f"=== {k} ===\n{v}" for k, v in files.items())


def importance_filtered_text(files, include_low=False):
    parts = []
    for fname, content in files.items():
        lines = content.split("\n")
        filtered = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- "):
                meta = parse_meta(stripped[2:])
                imp = meta["importance"]
                if not include_low and imp <= 1:
                    continue
                if imp >= 4:
                    filtered.append(line + "  [i" + str(imp) + "]")
                else:
                    filtered.append(line)
            else:
                filtered.append(line)
        parts.append(f"=== {fname} ===\n" + "\n".join(filtered))
    return "\n".join(parts)


def select_files_with_core(topic, all_docs, max_files=3):
    selected = select_files(topic, all_docs, max_files=max_files)
    for doc in all_docs:
        if doc["filename"] == "core.md" and "core.md" not in selected:
            selected["core.md"] = doc["content"]
    return selected


LEGACY_TIER_IMP = {"core": 5, "active": 3, "ref": 2, "temp": 1}


def clamp_importance(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 3
    return 1 if n < 1 else 5 if n > 5 else n


def parse_meta(text):
    m = re.search(r'\{\{([^}]+)\}\}', text)
    if not m:
        return {'text': text.strip(), 'created': None, 'reviewed': None, 'importance': 3, 'tags': [], 'expiry': None, 'source': None, 'rec': None, 'link': None, 'id': None}
    clean = text[:m.start()].rstrip()
    parts = m.group(1).split('|')
    created = parts[0] if parts else None
    reviewed = None
    importance = None
    legacy_tier = None
    tags = []
    expiry = None
    source = None
    rec = None
    link = None
    bid = None
    for p in parts[1:]:
        if p.startswith('r:'):
            reviewed = p[2:]
        elif p.startswith('i:'):
            importance = clamp_importance(p[2:])
        elif p.startswith('t:'):
            legacy_tier = p[2:]
        elif p.startswith('#'):
            tags = [t.strip() for t in p[1:].split(',') if t.strip()]
        elif p.startswith('x:'):
            expiry = p[2:]
        elif p.startswith('s:'):
            source = p[2:]
        elif p.startswith('rec:'):
            rec = p[4:]
        elif p.startswith('ln:'):
            link = p[3:]
        elif p.startswith('id:'):
            bid = p[3:]
    if importance is None:
        importance = LEGACY_TIER_IMP.get(legacy_tier, 3)
    return {'text': clean, 'created': created, 'reviewed': reviewed, 'importance': importance, 'tags': tags, 'expiry': expiry, 'source': source, 'rec': rec, 'link': link, 'id': bid}


def stamp_bullet(line, importance=3, expiry=None, source=None, rec=None):
    if re.search(r"\{\{\d{4}-\d{2}-\d{2}", line):
        return line
    meta = "{{" + date.today().isoformat() + "|i:" + str(clamp_importance(importance))
    if source:
        meta += "|s:" + source
    if expiry:
        meta += "|x:" + expiry
    if rec in ("pending", "confirmed"):
        meta += "|rec:" + rec
    meta += "}}"
    return line.rstrip() + " " + meta


def strip_timestamp(text):
    return re.sub(r"\s*\{\{[^}]*\}\}", "", text).strip()


def word_overlap(a, b):
    sa = set(strip_timestamp(a).lower().split())
    sb = set(strip_timestamp(b).lower().split())
    sa.discard("-")
    sb.discard("-")
    if not sa or not sb:
        return 0
    return len(sa & sb) / max(len(sa), len(sb))


def find_conflicts(new_bullet, existing_content):
    new_clean = strip_timestamp(new_bullet)
    conflicts = []
    for line in existing_content.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        score = word_overlap(new_clean, stripped)
        if score > 0.6:
            conflicts.append({"existing": stripped, "type": "duplicate", "score": round(score, 2)})
    return conflicts


def auto_decay():
    # maintenance across every user's brain: expired |x: bullets are removed
    today_str = date.today().isoformat()
    for user_id, doc in db_all_users_docs():
        if doc["filename"].startswith("_"):
            continue
        lines = doc["content"].split("\n")
        new_lines = []
        changed = False
        for line in lines:
            m = re.search(r'\|x:(\d{4}-\d{2}-\d{2})', line)
            if m and m.group(1) < today_str:
                changed = True
                continue
            new_lines.append(line)
        if changed:
            db_update(user_id, doc["$id"], "\n".join(new_lines))

def read_brain():
    resp = aw_list()
    files = {}
    for doc in resp["documents"]:
        files[doc["filename"]] = doc["content"]
    return files

def brain_as_text():
    parts = []
    for fname, content in read_brain().items():
        parts.append(f"=== {fname} ===\n{content}")
    return "\n".join(parts)

AUTH_CSS = """
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: linear-gradient(145deg, #ffffff 0%, #f0f1f3 50%, #e8eaed 100%);
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  font-family: 'JetBrains Mono', monospace;
}
.login { display: flex; flex-direction: column; align-items: center; gap: 16px; }
.logo { width: 48px; height: 48px; opacity: 0.15; }
.title { font-size: 11px; letter-spacing: 0.25em; color: rgba(0,0,0,0.12); text-transform: uppercase; }
input {
  width: 260px; padding: 10px 14px; border: 1px solid rgba(0,0,0,0.1);
  border-radius: 10px; background: #fff; font-family: 'JetBrains Mono', monospace;
  font-size: 13px; color: rgba(0,0,0,0.8); outline: none; text-align: center;
}
input::placeholder { color: rgba(0,0,0,0.2); }
input:focus { border-color: rgba(0,0,0,0.25); }
button {
  font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 8px 24px;
  border-radius: 999px; border: 1px solid rgba(0,0,0,0.1);
  background: transparent; color: rgba(0,0,0,0.45); cursor: pointer;
}
button:hover { color: rgba(0,0,0,0.8); border-color: rgba(0,0,0,0.25); }
.err { font-size: 11px; color: rgba(180,0,0,0.6); }
.alt { font-size: 10px; color: rgba(0,0,0,0.3); }
.alt a { color: rgba(0,0,0,0.45); }
.hp { position: absolute; left: -9999px; opacity: 0; }
.card { background: #fff; border: 1px solid rgba(0,0,0,0.08); border-radius: 14px; padding: 24px 28px; width: 340px; display: flex; flex-direction: column; gap: 12px; }
.card h2 { font-size: 11px; letter-spacing: 0.2em; color: rgba(0,0,0,0.3); text-transform: uppercase; }
.row { font-size: 12px; color: rgba(0,0,0,0.6); display: flex; justify-content: space-between; }
.key { font-size: 11px; word-break: break-all; background: rgba(0,0,0,0.04); padding: 8px; border-radius: 8px; color: rgba(0,0,0,0.7); }
"""


def _page(body):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cortex</title>
<link rel="icon" type="image/png" href="/favicon.png">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{AUTH_CSS}</style>
</head>
<body>{body}</body>
</html>"""


def login_page(error=""):
    err = f'<p class="err">{error}</p>' if error else ''
    return _page(f"""
<form class="login" method="POST" action="/login">
  <img src="/favicon.png" class="logo" alt="Cortex">
  <span class="title">cortex / memory</span>
  <input type="email" name="email" placeholder="email" autofocus autocomplete="username">
  <input type="password" name="password" placeholder="password" autocomplete="current-password">
  <button type="submit">enter</button>
  {err}
  <span class="alt">no account? <a href="/signup">sign up</a></span>
</form>""")


def signup_page(error=""):
    err = f'<p class="err">{error}</p>' if error else ''
    return _page(f"""
<form class="login" method="POST" action="/signup">
  <img src="/favicon.png" class="logo" alt="Cortex">
  <span class="title">cortex / new brain</span>
  <input type="email" name="email" placeholder="email" autofocus autocomplete="username">
  <input type="password" name="password" placeholder="password (8+ chars)" autocomplete="new-password">
  <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off">
  <button type="submit">create account</button>
  {err}
  <span class="alt">have an account? <a href="/login">log in</a></span>
</form>""")


def account_page(user, new_key=None, msg="", error=""):
    today = date.today().isoformat()
    if user["role"] == "owner":
        usage = "unlimited"
        cap = "unlimited"
    else:
        usage = f"{user_ai_usage(user['id'], today)} / {AI_DAILY_QUOTA} today"
        cap = f"{db_brain_bytes(user['id']) // 1024} / {MAX_BRAIN_BYTES // 1024} KB"
    key_html = f'<div class="key">{new_key}</div><span class="alt">copy it now — it is shown once and stored hashed</span>' if new_key else ''
    note = f'<p class="err">{error}</p>' if error else (f'<span class="alt">{msg}</span>' if msg else '')
    return _page(f"""
<div class="login">
  <img src="/favicon.png" class="logo" alt="Cortex">
  <span class="title">cortex / account</span>
  <div class="card">
    <h2>identity</h2>
    <div class="row"><span>email</span><span>{user['email']}</span></div>
    <div class="row"><span>role</span><span>{user['role']}</span></div>
    <div class="row"><span>ai calls</span><span>{usage}</span></div>
    <div class="row"><span>brain size</span><span>{cap}</span></div>
  </div>
  <div class="card">
    <h2>mcp api key</h2>
    {key_html}
    <form method="POST" action="/account/key"><button type="submit">generate new key</button></form>
    <span class="alt">connects Claude / Perplexity to your brain via /mcp</span>
  </div>
  <div class="card">
    <h2>change password</h2>
    <form method="POST" action="/account/password" style="display:flex;flex-direction:column;gap:8px;">
      <input type="password" name="current" placeholder="current password" autocomplete="current-password">
      <input type="password" name="new" placeholder="new password (8+ chars)" autocomplete="new-password">
      <button type="submit">change</button>
    </form>
  </div>
  {note}
  <span class="alt"><a href="/">back to brain</a> · <a href="/logout">log out</a></span>
</div>""")

def _dev_mode():
    # localhost dev stays usable as the owner; any deployed env requires login
    return not os.getenv("VERCEL_ENV") and not os.getenv("CORS_ORIGIN")


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("user_id") or _dev_mode():
            return f(*args, **kwargs)
        if request.is_json:
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/login")
    return decorated


def require_ai_quota(f):
    """Haiku endpoints: owner unlimited, others AI_DAILY_QUOTA calls/day."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _role_web() != "owner":
            if not user_ai_spend(_uid_web(), date.today().isoformat(), AI_DAILY_QUOTA):
                return jsonify({"error": f"daily AI quota reached ({AI_DAILY_QUOTA}/day)"}), 429
        return f(*args, **kwargs)
    return decorated


def _client_ip():
    # first hop of X-Forwarded-For on Vercel; remote_addr locally
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_ok(bucket, limit, window):
    try:
        return rate_check(bucket, limit, window)
    except Exception:
        return True  # a broken limiter must not lock out logins entirely


def _login_user(u):
    session.clear()
    session.permanent = True
    session["user_id"] = u["id"]
    session["role"] = u["role"]


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return redirect("/") if _dev_mode() and not user_count() else login_page()
    if not _rate_ok(f"login:{_client_ip()}", 8, 300):
        return login_page("too many attempts — wait a few minutes")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    u = user_by_email(email) if email else None
    if u and check_password_hash(u["pw_hash"], password):
        _login_user(u)
        return redirect("/")
    # legacy bootstrap: no accounts yet + the original single password matches
    # -> this login becomes the owner account and adopts all existing memories
    if (not u and user_count() == 0 and CORTEX_PASSWORD and password
            and hmac.compare_digest(password.encode(), CORTEX_PASSWORD.encode())
            and EMAIL_RE.match(email)):
        uid = user_create(email, generate_password_hash(password), role="owner")
        from db import adopt_orphan_docs
        adopt_orphan_docs(uid)
        _owner_cache.clear()
        _login_user(user_by_id(uid))
        return redirect("/")
    return login_page("wrong email or password")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return signup_page()
    if request.form.get("website"):
        return signup_page("signup failed")  # honeypot
    if not _rate_ok(f"signup:{_client_ip()}", 5, 3600):
        return signup_page("too many signups from this address — try later")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not EMAIL_RE.match(email):
        return signup_page("enter a valid email")
    if len(password) < 8:
        return signup_page("password must be at least 8 characters")
    if user_by_email(email):
        return signup_page("that email already has an account")
    uid = user_create(email, generate_password_hash(password))
    for t in SEED_TEMPLATES:
        db_create(uid, t["filename"], t["content"])
    _login_user(user_by_id(uid))
    return redirect("/")


@app.route("/account")
@require_auth
def account():
    u = user_by_id(_uid_web())
    if not u:
        return redirect("/login")
    return account_page(u)


@app.post("/account/key")
@require_auth
def account_key():
    u = user_by_id(_uid_web())
    token = "ctx_" + pysecrets.token_urlsafe(32)
    user_set_key_hash(u["id"], hash_key(token))
    return account_page(u, new_key=token)


@app.post("/account/password")
@require_auth
def account_password():
    u = user_by_id(_uid_web())
    cur = request.form.get("current", "")
    new = request.form.get("new", "")
    if not check_password_hash(u["pw_hash"], cur):
        return account_page(u, error="current password is wrong")
    if len(new) < 8:
        return account_page(u, error="new password must be at least 8 characters")
    user_set_password(u["id"], generate_password_hash(new))
    return account_page(user_by_id(u["id"]), msg="password changed")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if os.getenv("VERCEL_ENV"):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

@app.route("/favicon.png")
def favicon():
    return send_file(os.path.join(APP_DIR, "favicon.png"), mimetype="image/png")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
@require_auth
def index():
    return send_file(os.path.join(APP_DIR, "index.html"))

@app.get("/brain")
@require_auth
def brain():
    resp = aw_list()
    files = []
    today_str = date.today().isoformat()
    for doc in resp["documents"]:
        if doc["filename"].startswith("_"):
            continue
        color = None
        scope = "all"
        nodes = []
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if stripped.startswith("<!-- color:") and stripped.endswith("-->"):
                color = stripped.replace("<!-- color:", "").replace("-->", "").strip()
                continue
            if stripped.startswith("<!-- scope:") and stripped.endswith("-->"):
                scope = stripped.replace("<!-- scope:", "").replace("-->", "").strip()
                continue
            if not stripped or stripped.startswith("##"):
                continue
            if stripped.startswith("- "):
                meta = parse_meta(stripped[2:])
                if meta["expiry"] and meta["expiry"] < today_str:
                    continue
                indent = (len(line) - len(line.lstrip(" "))) // 2
                nodes.append({
                    "text": meta["text"],
                    "importance": meta["importance"],
                    "created": meta["created"],
                    "reviewed": meta.get("reviewed"),
                    "tags": meta["tags"],
                    "source": meta["source"] or "manual",
                    "rec": meta.get("rec"),
                    "indent": indent,
                    "link": meta.get("link"),
                    "id": meta.get("id"),
                })
        files.append({"name": doc["filename"].replace(".md", ""), "nodes": nodes, "color": color, "scope": scope})
    return jsonify({"files": files})

@app.post("/save")
@require_auth
@require_ai_quota
def save():
    try:
        if not _check_storage(2000):
            return jsonify({"error": "brain storage quota reached"}), 413
        msg = request.json.get("message", "")
        target_hint = request.json.get("target", "")
        all_docs = aw_list()["documents"]
        file_summaries = []
        for d in all_docs:
            if d["filename"].startswith("_"):
                continue
            headers = [l.strip() for l in d["content"].split("\n") if l.strip().startswith("##")]
            bullet_count = sum(1 for l in d["content"].split("\n") if l.strip().startswith("- "))
            file_summaries.append(f"{d['filename']}: {bullet_count} items, sections: {', '.join(headers) if headers else '(none)'}")
        file_overview = "\n".join(file_summaries)

        relevant = select_files(msg, all_docs, max_files=3)
        if target_hint:
            target_fname = target_hint if target_hint.endswith(".md") else target_hint + ".md"
            for d in all_docs:
                if d["filename"] == target_fname:
                    relevant = {d["filename"]: d["content"]}
                    break
        relevant_preview = ""
        for fname, content in relevant.items():
            lines = content.split("\n")
            preview_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("<!-- color:"):
                    continue
                preview_lines.append(line)
            preview = "\n".join(preview_lines[-40:]) if len(preview_lines) > 40 else "\n".join(preview_lines)
            relevant_preview += f"\n--- {fname} (current content) ---\n{preview}\n"

        sys_prompt = (
            "You are a memory manager. The user has shared new information. "
            "Identify distinct facts from their message. Append each fact as a single "
            "- bullet to the most appropriate brain file.\n\n"
            f"Available files:\n{file_overview}\n\n"
            f"Most relevant files (check for duplicates before adding):\n{relevant_preview}\n\n"
            "For each fact, assign an importance from 1 to 5 (how much this matters to the user long-term):\n"
            "- 5: life-defining identity, values, major goals, pivotal events\n"
            "- 4: major ongoing projects, key relationships, hard commitments\n"
            "- 3: normal useful facts, preferences, active work (default)\n"
            "- 2: minor details, reference info, secondary or past items\n"
            "- 1: trivial or short-lived notes\n"
            "For genuinely time-bound facts also set an expiry date YYYY-MM-DD (auto-deletes after).\n\n"
            "IMPORTANT: Check the existing content above. Do NOT add facts that already exist. "
            "If a fact updates an existing one, still add it (the user will resolve conflicts).\n\n"
            + (f"The user wants to save to: {target_hint}\n\n" if target_hint else "")
            + "If no existing file fits well, you may suggest a new filename ending in .md. "
            "If the user says 'new cluster', 'new file', or 'new markdown', create a new file. "
            "Do not rewrite existing content. Do not add headers. "
            'Respond ONLY with valid JSON: '
            '{"updates": [{"file": "filename.md", "line": "- fact text", "importance": 3, "expiry": null}]}. '
            "No other text."
        )
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=sys_prompt,
            messages=[{"role": "user", "content": msg}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return jsonify({"error": "parse_failed", "raw": raw}), 200
        updates = data.get("updates", [])
        existing = {d["filename"]: d for d in all_docs}
        has_conflicts = False
        for u in updates:
            importance = clamp_importance(u.get("importance", 3))
            expiry = u.get("expiry") or None
            u["line"] = stamp_bullet(u["line"], importance=importance, expiry=expiry, source="manual")
            if u["file"] in existing:
                conflicts = find_conflicts(u["line"], existing[u["file"]]["content"])
                if conflicts:
                    u["conflicts"] = conflicts
                    u["pending"] = True
                    has_conflicts = True
                else:
                    u["conflicts"] = []
                    u["pending"] = False
            else:
                u["conflicts"] = []
                u["pending"] = False
        for u in updates:
            if u["pending"]:
                continue
            if u["file"] in existing:
                doc = existing[u["file"]]
                existing_content = doc["content"]
                if existing_content and not existing_content.endswith("\n"):
                    existing_content += "\n"
                aw_update(doc["$id"], {"content": existing_content + u["line"] + "\n"})
            else:
                aw_create({"filename": u["file"], "content": u["line"] + "\n"})
        return jsonify({"updates": updates, "has_conflicts": has_conflicts})
    except Exception:
        app.logger.exception("internal error")
        return jsonify({"error": "internal error"}), 500

@app.post("/save/confirm")
@require_auth
def save_confirm():
    try:
        if not _check_storage(2000):
            return jsonify({"error": "brain storage quota reached"}), 413
        items = request.json.get("updates", [])
        all_docs = aw_list()["documents"]
        existing = {d["filename"]: d for d in all_docs}
        for item in items:
            action = item.get("action", "discard")
            fname = item["file"]
            new_line = item["line"]
            if action == "discard":
                continue
            if fname not in existing:
                continue
            doc = existing[fname]
            content = doc["content"]
            if action == "replace":
                old_bullet = item.get("replace_line", "")
                if old_bullet:
                    lines = content.split("\n")
                    lines = [l for l in lines if l.strip() != old_bullet.strip()]
                    content = "\n".join(lines)
                    if not content.endswith("\n"):
                        content += "\n"
            if not content.endswith("\n"):
                content += "\n"
            content += new_line + "\n"
            aw_update(doc["$id"], {"content": content})
        return jsonify({"ok": True})
    except Exception:
        app.logger.exception("internal error")
        return jsonify({"error": "internal error"}), 500


@app.post("/context")
@require_auth
@require_ai_quota
def context():
    try:
        topic = request.json.get("topic", "").strip()
        mode = request.json.get("mode", "normal")
        all_docs = aw_list()["documents"]

        if mode == "compact":
            files = select_files_with_core(topic, all_docs, max_files=2)
            brain_text = importance_filtered_text(files)
            sys_prompt = (
                "Generate a compact context handoff for another AI.\n"
                "Third person only.\n"
                "No prose. No headers. No markdown emphasis. No analysis. No advice.\n"
                "No strategic notes. Only factual bullets.\n\n"
                "Output rules:\n"
                "- Maximum 4 bullets\n"
                "- Maximum 8 words per bullet\n"
                "- One complete fact per bullet\n"
                "- End every bullet cleanly\n"
                "- Do not include any intro or outro text\n"
                "- Return only bullets"
            )
            resp = client.messages.create(
                model=MODEL,
                max_tokens=140,
                system=f"{sys_prompt}\n\nData:\n{brain_text}",
                messages=[{"role": "user", "content": f"Context{' about ' + topic if topic else ''}."}],
            )
            text = resp.content[0].text.strip()
            if resp.stop_reason == "max_tokens":
                retry_prompt = (
                    "Retry. Maximum 3 bullets. Maximum 6 words per bullet. "
                    "Facts only. No intro. No outro."
                )
                resp2 = client.messages.create(
                    model=MODEL,
                    max_tokens=100,
                    system=f"{retry_prompt}\n\nData:\n{brain_text}",
                    messages=[{"role": "user", "content": f"Context{' about ' + topic if topic else ''}."}],
                )
                text = resp2.content[0].text.strip()
                truncated = resp2.stop_reason == "max_tokens"
            else:
                truncated = False
            return jsonify({"context": text, "truncated": truncated})

        elif mode == "expand":
            files = select_files_with_core(topic, all_docs, max_files=6)
            if not topic:
                files = {d["filename"]: d["content"] for d in all_docs if not d["filename"].startswith("_")}
            brain_text = importance_filtered_text(files, include_low=True)
            sys_prompt = (
                "Generate a comprehensive context handoff for another AI assistant. "
                "Third person only ('The user is...', 'They are...'). "
                "No second person. No 'You are'. "
                "Be exhaustive. Include all relevant details: identity, background, "
                "goals, current projects, relationships, constraints, deadlines, "
                "achievements, strategies, and any other operationally useful info. "
                "Use labeled sections. Be specific with names, dates, numbers. "
                "Do not summarize or abbreviate. Include everything that could help "
                "another AI understand this user fully."
                + (f" Focus especially on: {topic}." if topic else "")
            )
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=f"{sys_prompt}\n\nFull brain data:\n{brain_text}",
                messages=[{"role": "user", "content": f"Generate comprehensive context{' about ' + topic if topic else ''}."}],
            )
            text = resp.content[0].text.strip()
            truncated = resp.stop_reason == "max_tokens"
            return jsonify({"context": text, "truncated": truncated})

        else:
            files = select_files_with_core(topic, all_docs, max_files=3)
            brain_text = importance_filtered_text(files)
            sys_prompt = (
                "Generate a structured context handoff for another AI. "
                "Third person only ('The user is...', 'They are...'). "
                "No second person. No 'You are'. "
                "Use labeled sections: identity, current goals, relevant context, constraints. "
                "Be specific and concrete. No narrative prose. "
                "End with: [end of context]. Do not truncate."
                + (f" Focus on: {topic}." if topic else "")
            )
            resp = client.messages.create(
                model=MODEL,
                max_tokens=600,
                system=f"{sys_prompt}\n\nBrain data:\n{brain_text}",
                messages=[{"role": "user", "content": f"Generate context{' about ' + topic if topic else ''}."}],
            )
            text = resp.content[0].text.strip()
            truncated = resp.stop_reason == "max_tokens"
            return jsonify({"context": text, "truncated": truncated})
    except Exception:
        app.logger.exception("internal error")
        return jsonify({"error": "internal error"}), 500

@app.get("/memory")
@require_auth
def get_memory():
    filename = request.args.get("file", "")
    if not filename:
        return jsonify({"error": "missing file param"}), 400
    resp = aw_list()
    for doc in resp["documents"]:
        if doc["filename"] == filename:
            return jsonify({"filename": doc["filename"], "content": doc["content"], "id": doc["$id"]})
    return jsonify({"error": "not found"}), 404

@app.post("/memory")
@require_auth
def update_memory():
    doc_id = request.json.get("id", "")
    content = request.json.get("content", "")
    if not doc_id:
        return jsonify({"error": "missing id"}), 400
    if not _check_storage(len(content)):
        return jsonify({"error": "brain storage quota reached"}), 413
    aw_update(doc_id, {"content": content})
    return jsonify({"ok": True})

@app.delete("/memory")
@require_auth
def delete_memory():
    try:
        filename = request.json.get("filename", "")
        if not filename:
            return jsonify({"error": "missing filename"}), 400
        resp = aw_list()
        for doc in resp["documents"]:
            if doc["filename"] == filename:
                aw_delete(doc["$id"])
                return jsonify({"ok": True})
        return jsonify({"error": "not found"}), 404
    except Exception:
        app.logger.exception("internal error")
        return jsonify({"error": "internal error"}), 500

@app.post("/memory/new")
@require_auth
def create_memory():
    try:
        filename = request.json.get("filename", "")
        content = request.json.get("content", "")
        if not filename:
            return jsonify({"error": "missing filename"}), 400
        if not filename.endswith(".md"):
            filename += ".md"
        if not _check_storage(len(content)):
            return jsonify({"error": "brain storage quota reached"}), 413
        resp = aw_list()
        for doc in resp["documents"]:
            if doc["filename"] == filename:
                return jsonify({"error": "already exists"}), 400
        aw_create({"filename": filename, "content": content})
        return jsonify({"ok": True})
    except Exception:
        app.logger.exception("internal error")
        return jsonify({"error": "internal error"}), 500

@app.get("/search")
@require_auth
def search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify({"results": []})
    resp = aw_list()
    results = []
    for doc in resp["documents"]:
        matches = []
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("##"):
                continue
            if q in stripped.lower():
                matches.append(stripped)
        if q in doc["filename"].lower():
            matches = matches or ["(filename match)"]
        if matches:
            results.append({"filename": doc["filename"], "matches": matches[:5]})
    return jsonify({"results": results})

@app.post("/chat")
@require_auth
@require_ai_quota
def chat():
    try:
        msg = request.json.get("message", "")
        compact = request.json.get("compact", False)
        all_docs = aw_list()["documents"]
        files = select_files_with_core(msg, all_docs, max_files=3)
        brain_text = importance_filtered_text(files)
        suffix = (
            "\n\nIMPORTANT: Bare minimum words. Key facts only. No filler. Extreme brevity."
            if compact else ""
        )
        resp = client.messages.create(
            model=MODEL,
            max_tokens=300 if compact else 600,
            system=brain_text + suffix,
            messages=[{"role": "user", "content": msg}],
        )
        return jsonify({"reply": resp.content[0].text.strip()})
    except Exception:
        app.logger.exception("internal error")
        return jsonify({"error": "internal error"}), 500

def _track(user_id, cluster, action="read", source=""):
    if not cluster or not user_id:
        return
    try:
        db_log_activity(user_id, {"cluster": cluster, "action": action, "source": source, "ts": time.time()})
    except Exception:
        pass

@app.route("/activity", methods=["POST"])
def post_activity():
    uid = _uid_web() if (session.get("user_id") or _dev_mode()) else None
    if not uid:
        key = request.headers.get("X-Api-Key", "")
        if key:
            if CORTEX_MCP_KEY and hmac.compare_digest(key.encode(), CORTEX_MCP_KEY.encode()):
                o = _owner()
                uid = o["id"] if o else None
            else:
                u = user_by_key_hash(hash_key(key))
                uid = u["id"] if u else None
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    _track(uid, data.get("cluster", ""), data.get("action", "read"), data.get("source", ""))
    return jsonify({"ok": True})

@app.route("/activity", methods=["GET"])
def get_activity():
    if not (session.get("user_id") or _dev_mode()):
        return jsonify([])
    try:
        events = db_get_activity(_uid_web())
    except Exception:
        return jsonify([])
    cutoff = time.time() - 30
    return jsonify([e for e in events if e.get("ts", 0) > cutoff])


_maintained = False

@app.before_request
def run_maintenance():
    # seeding now happens per-user at signup; this only expires |x: bullets
    global _maintained
    if _maintained:
        return
    _maintained = True
    try:
        auto_decay()
    except Exception:
        pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
