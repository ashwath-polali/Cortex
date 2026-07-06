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
APP_DIR = os.path.dirname(os.path.abspath(__file__))

from db import db_list, db_create, db_update, db_delete, db_log_activity, db_get_activity

def aw_list():
    return {"documents": db_list()}

def aw_create(data):
    return db_create(data["filename"], data.get("content", ""))

def aw_update(doc_id, data):
    db_update(doc_id, data["content"])

def aw_delete(doc_id):
    db_delete(doc_id)

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
    today_str = date.today().isoformat()
    resp = aw_list()
    for doc in resp["documents"]:
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
            aw_update(doc["$id"], {"content": "\n".join(new_lines)})

def seed_brain():
    resp = aw_list()
    if resp["documents"]:
        return
    for t in SEED_TEMPLATES:
        aw_create(t)
    print("Seeded brain templates into Appwrite")

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

def login_page(error=False):
    err = '<p class="err">wrong password</p>' if error else ''
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cortex</title>
<link rel="icon" type="image/png" href="/favicon.png">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: linear-gradient(145deg, #ffffff 0%, #f0f1f3 50%, #e8eaed 100%);
  height: 100vh; display: flex; align-items: center; justify-content: center;
  font-family: 'JetBrains Mono', monospace;
}}
.login {{ display: flex; flex-direction: column; align-items: center; gap: 16px; }}
.logo {{ width: 48px; height: 48px; opacity: 0.15; }}
.title {{ font-size: 11px; letter-spacing: 0.25em; color: rgba(0,0,0,0.12); text-transform: uppercase; }}
input {{
  width: 260px; padding: 10px 14px; border: 1px solid rgba(0,0,0,0.1);
  border-radius: 10px; background: #fff; font-family: 'JetBrains Mono', monospace;
  font-size: 13px; color: rgba(0,0,0,0.8); outline: none; text-align: center;
}}
input::placeholder {{ color: rgba(0,0,0,0.2); }}
input:focus {{ border-color: rgba(0,0,0,0.25); }}
button {{
  font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 8px 24px;
  border-radius: 999px; border: 1px solid rgba(0,0,0,0.1);
  background: transparent; color: rgba(0,0,0,0.45); cursor: pointer;
}}
button:hover {{ color: rgba(0,0,0,0.8); border-color: rgba(0,0,0,0.25); }}
.err {{ font-size: 11px; color: rgba(180,0,0,0.6); }}
</style>
</head>
<body>
<form class="login" method="POST">
  <img src="/favicon.png" class="logo" alt="Cortex">
  <span class="title">cortex / memory</span>
  <input type="password" name="password" placeholder="password" autofocus>
  <button type="submit">enter</button>
  {err}
</form>
</body>
</html>"""

def _dev_mode():
    # localhost dev without a password stays usable; any deployed env fails closed
    return not os.getenv("VERCEL_ENV") and not os.getenv("CORS_ORIGIN")


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not CORTEX_PASSWORD:
            if _dev_mode():
                return f(*args, **kwargs)
            return jsonify({"error": "server not configured (CORTEX_PASSWORD unset)"}), 503
        if not session.get("authenticated"):
            if request.is_json:
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def _client_ip():
    # first hop of X-Forwarded-For on Vercel; remote_addr locally
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


_login_attempts = {}

@app.route("/login", methods=["GET", "POST"])
def login():
    if not CORTEX_PASSWORD:
        if _dev_mode():
            return redirect("/")
        return login_page(error=True)
    if request.method == "GET":
        return login_page()
    ip = _client_ip()
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < 300]
    if len(attempts) >= 5:
        return login_page(error=True)
    password = request.form.get("password", "")
    if hmac.compare_digest(password.encode(), CORTEX_PASSWORD.encode()):
        _login_attempts.pop(ip, None)
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        return redirect("/")
    attempts.append(now)
    _login_attempts[ip] = attempts
    return login_page(error=True)


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
def save():
    try:
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

MCP_TOOLS = [
    {
        "name": "get_full_brain",
        "description": "Get ALL brain data across every cluster. Returns all memory files with their contents.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_cluster",
        "description": "Read a specific brain cluster by filename. Use when you need detailed info from one area.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string", "description": "Cluster filename, e.g. 'core.md'"}}, "required": ["filename"]},
    },
    {
        "name": "list_clusters",
        "description": "List all brain clusters with section headers and bullet counts. Use to see what memory areas exist.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_brain",
        "description": "Search across all brain data for a keyword or phrase. Returns matching lines with source file.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "Search term"}}, "required": ["query"]},
    },
    {
        "name": "get_context_for_topic",
        "description": "Get brain data relevant to a specific topic. Auto-selects the most relevant clusters.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string", "description": "Topic to get context for"}}, "required": ["topic"]},
    },
    {
        "name": "save_to_brain",
        "description": "Append a new memory to a brain cluster. REQUIRED: give every memory an importance 1-5 (5=life-defining identity/values/major goals, 4=major projects/key relationships/hard commitments, 3=normal useful fact, 2=minor/secondary detail, 1=trivial or short-lived). Save freely - do not skip saving useful info; just rank it honestly. Put one complete thought per bullet; if a fact directly continues/extends another, pass 'parent' to nest it as a sub-bullet instead of creating a dangling separate bullet.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string", "description": "Target cluster filename"}, "bullet": {"type": "string", "description": "Fact to save as a bullet point (one complete thought)"}, "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1-5 importance. Required - rank honestly."}, "section": {"type": "string", "description": "Target ## section name inside the cluster. Read the cluster first to find section names."}, "parent": {"type": "string", "description": "Optional: a unique substring of an existing bullet this fact continues/extends. Nests this as a sub-bullet under it instead of a separate sibling."}, "expiry": {"type": "string", "description": "Optional expiry date YYYY-MM-DD for genuinely time-bound facts (auto-deletes after)"}, "source": {"type": "string", "description": "Source client (claude, perplexity, etc)"}, "rec": {"type": "string", "enum": ["pending", "confirmed"], "description": "Recommendation status. Use 'pending' when saving an AI recommendation. Use 'confirmed' when upgrading a recommendation the user accepted. Omit for normal factual memories."}}, "required": ["filename", "bullet", "importance"]},
    },
    {
        "name": "set_importance",
        "description": "Update the 1-5 importance ranking of an existing memory bullet. Use when re-evaluating how much a memory matters.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string", "description": "Cluster filename"}, "bullet_text": {"type": "string", "description": "Unique substring of the bullet to re-rank"}, "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "New 1-5 importance"}}, "required": ["filename", "bullet_text", "importance"]},
    },
    {
        "name": "create_cluster",
        "description": "Create a new brain cluster with initial content. Use when information doesn't fit any existing cluster. Provide full markdown with ## sections and - bullet points. Each bullet should be a single fact.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string", "description": "New cluster filename (e.g. 'hobbies.md')"}, "content": {"type": "string", "description": "Full markdown content with ## sections and - bullets"}, "source": {"type": "string", "description": "Source client (claude, perplexity, etc)"}, "scope": {"type": "string", "enum": ["build", "strategy"], "description": "Cluster scope: 'build' for project/code, 'strategy' for life/goals"}}, "required": ["filename", "content"]},
    },
    {
        "name": "get_brain_summary",
        "description": "Quick identity snapshot — who the user is, core facts. Use for a fast intro without loading everything.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "edit_bullet",
        "description": "Edit an existing bullet in a cluster. Call get_cluster first to find the exact bullet text. Pass old_text as a unique substring of the bullet. new_text is the replacement.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string", "description": "Cluster filename"}, "old_text": {"type": "string", "description": "Unique substring of the bullet to find"}, "new_text": {"type": "string", "description": "Replacement bullet text"}, "source": {"type": "string", "description": "Source client"}}, "required": ["filename", "old_text", "new_text"]},
    },
    {
        "name": "delete_bullet",
        "description": "Delete a bullet from a cluster. Call get_cluster first to find the exact bullet text. Pass old_text as a unique substring of the bullet line to remove.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string", "description": "Cluster filename"}, "old_text": {"type": "string", "description": "Unique substring of the bullet to delete"}}, "required": ["filename", "old_text"]},
    },
    {
        "name": "delete_section",
        "description": "Delete an entire ## section and all its bullets from a cluster. Call get_cluster first to confirm the section name.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string", "description": "Cluster filename"}, "section": {"type": "string", "description": "Exact ## section header name to delete"}}, "required": ["filename", "section"]},
    },
]


def mcp_get_full_brain():
    docs = aw_list()["documents"]
    parts = []
    for doc in sorted(docs, key=lambda d: d["filename"]):
        lines = [l for l in doc["content"].split("\n") if not l.strip().startswith("<!-- color:")]
        parts.append(f"=== {doc['filename']} ===\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else "brain is empty"


def mcp_get_cluster(filename):
    if not filename.endswith(".md"):
        filename += ".md"
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == filename:
            lines = [l for l in doc["content"].split("\n") if not l.strip().startswith("<!-- color:")]
            return "\n".join(lines)
    available = [d["filename"] for d in docs]
    return f"'{filename}' not found. Available: {', '.join(available)}"


def mcp_list_clusters():
    docs = aw_list()["documents"]
    lines = []
    for doc in sorted(docs, key=lambda d: d["filename"]):
        headers = []
        bullet_count = 0
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if stripped.startswith("##"):
                headers.append(stripped)
            elif stripped.startswith("- "):
                bullet_count += 1
        lines.append(f"{doc['filename']} — {bullet_count} items — {', '.join(headers) if headers else '(no sections)'}")
    return "\n".join(lines) if lines else "no clusters"


def mcp_search_brain(query):
    q = query.lower().strip()
    if not q:
        return "empty query"
    docs = aw_list()["documents"]
    results = []
    for doc in docs:
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("<!-- color:"):
                continue
            if q in stripped.lower():
                results.append(f"[{doc['filename']}] {stripped}")
    return "\n".join(results) if results else f"no matches for '{query}'"


def mcp_get_context_for_topic(topic):
    docs = aw_list()["documents"]
    selected = select_files(topic, docs, max_files=4)
    parts = []
    for fname, content in selected.items():
        lines = [l for l in content.split("\n") if not l.strip().startswith("<!-- color:")]
        parts.append(f"=== {fname} ===\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else f"no relevant data for '{topic}'"


def _insert_under_section(content, section, bullet):
    lines = content.split("\n")
    section_lower = section.lower().strip().lstrip("#").strip()
    best_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("##"):
            header_text = line.strip().lstrip("#").strip().lower()
            if header_text == section_lower:
                best_idx = i
                break
    if best_idx == -1:
        if not content.endswith("\n"):
            content += "\n"
        return content + "\n## " + section.strip().lstrip("#").strip() + "\n" + bullet + "\n"
    insert_at = best_idx + 1
    while insert_at < len(lines):
        stripped = lines[insert_at].strip()
        if stripped.startswith("##"):
            break
        if stripped == "":
            insert_at += 1
            continue
        insert_at += 1
    lines.insert(insert_at, bullet)
    return "\n".join(lines)


def mcp_save_to_brain(filename, bullet, importance=3, expiry=None, source=None, section=None, rec=None, parent=None):
    if not filename.endswith(".md"):
        filename += ".md"
    bullet = bullet.strip()
    if bullet.startswith("- "):
        bullet = bullet[2:].strip()
    if not source:
        source = "mcp"
    if rec and rec not in ("pending", "confirmed"):
        rec = None
    importance = clamp_importance(importance)
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == filename:
            lines = doc["content"].split("\n")
            if parent:
                pidx = -1
                for i, line in enumerate(lines):
                    if line.strip().startswith("- ") and parent.strip() in line:
                        pidx = i
                        break
                if pidx == -1:
                    return f"parent bullet matching '{parent}' not found in {filename}"
                child = stamp_bullet("  - " + bullet, importance=importance, expiry=expiry, source=source, rec=rec)
                ins = pidx + 1
                while ins < len(lines) and lines[ins].startswith("  - "):
                    ins += 1
                lines.insert(ins, child)
                aw_update(doc["$id"], {"content": "\n".join(lines)})
                return f"saved under '{parent.strip()}' in {filename}: {child.strip()}"
            stamped = stamp_bullet("- " + bullet, importance=importance, expiry=expiry, source=source, rec=rec)
            content = doc["content"]
            if section:
                content = _insert_under_section(content, section, stamped)
            else:
                if not content.endswith("\n"):
                    content += "\n"
                content += stamped + "\n"
            aw_update(doc["$id"], {"content": content})
            target = f"{filename} > {section}" if section else filename
            return f"saved to {target}: {stamped}"
    available = [d["filename"] for d in docs]
    return f"'{filename}' not found. Available: {', '.join(available)}"


def mcp_create_cluster(filename, content, source=None, scope=None):
    if not filename.endswith(".md"):
        filename += ".md"
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == filename:
            return f"'{filename}' already exists — use save_to_brain to add to it"
    if not source:
        source = "mcp"
    lines = content.split("\n")
    stamped = []
    if scope in ("build", "strategy"):
        stamped.append(f"<!-- scope:{scope} -->")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") and not re.search(r"\{\{\d{4}-\d{2}-\d{2}", stripped):
            meta = "{{" + date.today().isoformat() + "|i:3|s:" + source + "}}"
            stamped.append(stripped.rstrip() + " " + meta)
        else:
            stamped.append(line)
    final = "\n".join(stamped)
    if not final.endswith("\n"):
        final += "\n"
    aw_create({"filename": filename, "content": final})
    return f"created {filename} [{scope or 'all'}] with {sum(1 for l in stamped if l.strip().startswith('- '))} bullets"


def mcp_get_brain_summary():
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == "core.md":
            lines = [l for l in doc["content"].split("\n") if not l.strip().startswith("<!-- color:")]
            return "\n".join(lines)
    return "core.md not found"


def mcp_edit_bullet(filename, old_text, new_text, source=None):
    if not filename.endswith(".md"):
        filename += ".md"
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == filename:
            lines = doc["content"].split("\n")
            match_idx = -1
            for i, line in enumerate(lines):
                if old_text.strip() in line:
                    match_idx = i
                    break
            if match_idx == -1:
                return f"no bullet matching '{old_text}' found in {filename}"
            old_line = lines[match_idx]
            indent = "  " if (old_line.startswith("  - ") or old_line.startswith("\t- ")) else ""
            old_imp = parse_meta(old_line.strip()[2:])["importance"] if old_line.strip().startswith("- ") else 3
            if not new_text.startswith("- "):
                new_text = "- " + new_text
            if not source:
                source = "mcp"
            new_text = stamp_bullet(new_text, importance=old_imp, source=source)
            lines[match_idx] = indent + new_text
            content = "\n".join(lines)
            aw_update(doc["$id"], {"content": content})
            return f"edited in {filename}: '{old_text.strip()}' → '{new_text.strip()}'"
    available = [d["filename"] for d in docs]
    return f"'{filename}' not found. Available: {', '.join(available)}"


def mcp_delete_bullet(filename, old_text):
    if not filename.endswith(".md"):
        filename += ".md"
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == filename:
            lines = doc["content"].split("\n")
            match_idx = -1
            for i, line in enumerate(lines):
                if old_text.strip() in line:
                    match_idx = i
                    break
            if match_idx == -1:
                return f"no bullet matching '{old_text}' found in {filename}"
            removed = lines.pop(match_idx)
            content = "\n".join(lines)
            aw_update(doc["$id"], {"content": content})
            return f"deleted from {filename}: {removed.strip()}"
    available = [d["filename"] for d in docs]
    return f"'{filename}' not found. Available: {', '.join(available)}"


def mcp_delete_section(filename, section):
    if not filename.endswith(".md"):
        filename += ".md"
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == filename:
            lines = doc["content"].split("\n")
            section_lower = section.lower().strip().lstrip("#").strip()
            start_idx = -1
            for i, line in enumerate(lines):
                if line.strip().startswith("##"):
                    header_text = line.strip().lstrip("#").strip().lower()
                    if header_text == section_lower:
                        start_idx = i
                        break
            if start_idx == -1:
                return f"section '{section}' not found in {filename}"
            end_idx = start_idx + 1
            while end_idx < len(lines):
                if lines[end_idx].strip().startswith("##"):
                    break
                end_idx += 1
            removed_count = sum(1 for l in lines[start_idx:end_idx] if l.strip().startswith("- "))
            del lines[start_idx:end_idx]
            content = "\n".join(lines)
            aw_update(doc["$id"], {"content": content})
            return f"deleted section '{section}' ({removed_count} bullets) from {filename}"
    available = [d["filename"] for d in docs]
    return f"'{filename}' not found. Available: {', '.join(available)}"


def _set_line_importance(line, importance):
    if "{{" not in line:
        return stamp_bullet(line, importance=importance)
    line = re.sub(r"\|t:\w+", "", line)
    if re.search(r"\|i:\d", line):
        return re.sub(r"\|i:\d+", "|i:" + str(importance), line)
    return re.sub(r"(\{\{\d{4}-\d{2}-\d{2})", r"\1|i:" + str(importance), line, count=1)


def mcp_set_importance(filename, bullet_text, importance):
    if not filename.endswith(".md"):
        filename += ".md"
    importance = clamp_importance(importance)
    docs = aw_list()["documents"]
    for doc in docs:
        if doc["filename"] == filename:
            lines = doc["content"].split("\n")
            for i, line in enumerate(lines):
                if line.strip().startswith("- ") and bullet_text.strip() in line:
                    lines[i] = _set_line_importance(line, importance)
                    aw_update(doc["$id"], {"content": "\n".join(lines)})
                    return f"set importance {importance} on: {bullet_text.strip()}"
            return f"no bullet matching '{bullet_text}' in {filename}"
    available = [d["filename"] for d in docs]
    return f"'{filename}' not found. Available: {', '.join(available)}"


def _d(fn, cluster=None, action="read"):
    def wrapper(args):
        result = fn(args)
        c = args.get(cluster) if cluster else "all"
        if c and not c.endswith(".md"):
            c += ".md"
        _track(c or "all", action, args.get("source", ""))
        return result
    return wrapper

MCP_DISPATCH = {
    "get_full_brain": _d(lambda args: mcp_get_full_brain()),
    "get_cluster": _d(lambda args: mcp_get_cluster(args["filename"]), "filename", "read"),
    "list_clusters": _d(lambda args: mcp_list_clusters()),
    "search_brain": _d(lambda args: mcp_search_brain(args["query"])),
    "get_context_for_topic": _d(lambda args: mcp_get_context_for_topic(args["topic"])),
    "save_to_brain": _d(lambda args: mcp_save_to_brain(args["filename"], args["bullet"], args.get("importance", 3), args.get("expiry"), args.get("source"), args.get("section"), args.get("rec"), args.get("parent")), "filename", "write"),
    "create_cluster": _d(lambda args: mcp_create_cluster(args["filename"], args["content"], args.get("source"), args.get("scope")), "filename", "write"),
    "get_brain_summary": _d(lambda args: mcp_get_brain_summary()),
    "edit_bullet": _d(lambda args: mcp_edit_bullet(args["filename"], args["old_text"], args["new_text"], args.get("source")), "filename", "write"),
    "set_importance": _d(lambda args: mcp_set_importance(args["filename"], args["bullet_text"], args["importance"]), "filename", "write"),
    "delete_bullet": _d(lambda args: mcp_delete_bullet(args["filename"], args["old_text"]), "filename", "write"),
    "delete_section": _d(lambda args: mcp_delete_section(args["filename"], args["section"]), "filename", "write"),
}




def _track(cluster, action="read", source=""):
    if not cluster:
        return
    try:
        db_log_activity({"cluster": cluster, "action": action, "source": source, "ts": time.time()})
    except Exception:
        pass

@app.route("/activity", methods=["POST"])
def post_activity():
    key = request.headers.get("X-Api-Key", "")
    authed = session.get("authenticated") or (
        CORTEX_MCP_KEY and hmac.compare_digest(key.encode(), CORTEX_MCP_KEY.encode()))
    if not authed:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    _track(data.get("cluster", ""), data.get("action", "read"), data.get("source", ""))
    return jsonify({"ok": True})

@app.route("/activity", methods=["GET"])
def get_activity():
    if not session.get("authenticated"):
        return jsonify([])
    try:
        events = db_get_activity()
    except Exception:
        return jsonify([])
    cutoff = time.time() - 30
    return jsonify([e for e in events if e.get("ts", 0) > cutoff])


_seeded = False

@app.before_request
def ensure_seeded():
    global _seeded
    if _seeded:
        return
    _seeded = True
    try:
        seed_brain()
        auto_decay()
    except Exception:
        pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
