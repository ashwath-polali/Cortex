# Cortex MCP Server — connects any AI to your brain
#
# Claude Desktop config — add to claude_desktop_config.json:
# {
#   "mcpServers": {
#     "cortex": {
#       "command": "python",
#       "args": ["/path/to/Cortex/mcp_server.py"],
#       "env": {
#         "MONGODB_URI": "mongodb+srv://...",
#         "MONGODB_DB": "cortex",
#         "MONGODB_COLLECTION": "memories"
#       }
#     }
#   }
# }
#
# Claude Code — add to .mcp.json in project root or ~/.claude/mcp.json:
# {
#   "cortex": {
#     "command": "python",
#     "args": ["/path/to/Cortex/mcp_server.py"]
#   }
# }

import os
import re
from datetime import date
from difflib import SequenceMatcher
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from db import db_list, db_find, db_create, db_update, db_delete

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json as _json
import time as _time

def _aw_activity_doc():
    return db_find("__activity__")

def emit_activity(cluster, action="read", source=""):
    try:
        event = _json.dumps({"cluster": cluster, "action": action, "source": source, "ts": _time.time()})
        doc = _aw_activity_doc()
        if doc:
            old = doc["content"].strip().split("\n") if doc["content"].strip() else []
            old.append(event)
            old = old[-30:]
            db_update(doc["$id"], "\n".join(old))
        else:
            db_create("__activity__", event)
    except Exception:
        pass

def _clamp_imp(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 3
    return 1 if n < 1 else 5 if n > 5 else n


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
    for w, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True):
        keywords.append(w)
        if len(keywords) >= 20:
            break
    headers = re.findall(r'##\s+(.+)', doc["content"])
    for h in headers:
        for w in re.findall(r'[a-z]{3,}', h.lower()):
            if w not in STOP_WORDS and w not in keywords:
                keywords.append(w)
    return keywords


def aw_list():
    return db_list()


def get_scope(doc):
    m = re.search(r'<!--\s*scope:(\w+)\s*-->', doc["content"])
    return m.group(1) if m else "all"


def filter_by_scope(docs, scope):
    if not scope:
        return docs
    return [d for d in docs if get_scope(d) in (scope, "all")]


def aw_find(filename):
    for doc in aw_list():
        if doc["filename"] == filename:
            return doc
    return None


def select_files(topic, docs, max_files=3):
    topic_lower = topic.lower()
    topic_words = set(re.findall(r'[a-z]{3,}', topic_lower)) - STOP_WORDS
    scored = []
    for doc in docs:
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
        return [d for _, d in scored[:max_files]]
    return [d for d in docs if d["filename"] == "core.md"]


mcp = FastMCP(
    "cortex",
    stateless_http=True,
    instructions=(
        "Cortex is Ashwath's personal memory system — his external brain. You have FULL autonomous access.\n\n"

        "=== EVERY MESSAGE, DO THIS ===\n\n"
        "1. READ: Call get_context_for_topic or search_brain. On first message, call get_brain_summary. Always read before responding.\n"
        "2. SAVE: Actively look for facts to save. Default is to save — only skip if the message is a pure generic question with zero personal context. "
        "But ONLY save facts you are confident are accurate. If the user's message is ambiguous, or you're unsure you understood correctly, ask before saving. "
        "Never save guesses, vague impressions, or unverified claims. Bad data in Cortex is worse than missing data. "
        "Every memory you save MUST include an importance 1-5 — save freely and rank honestly rather than withholding info.\n"
        "3. REPORT: End every response with [Cortex: ...] — what you read, what you saved/edited, or why you skipped.\n\n"

        "=== ACCURACY GUARD ===\n\n"
        "Before saving, ask yourself: 'Am I confident this is what the user actually said/meant?' "
        "If yes → save. If not sure → ask: 'Worth saving [X] to [cluster > section]?' "
        "If the user was vague, joking, or speculating → do NOT save as fact. "
        "If you misunderstood and saved wrong info, it compounds — every future AI reads it. Precision over volume.\n\n"

        "=== RECOMMENDATIONS ===\n\n"
        "When giving grounded, actionable advice: save facts first (no rec param), then save the recommendation with rec='pending'. "
        "Treat rec:pending as 60-80% confidence — flag when using. User confirms → edit to rec:confirmed. User rejects → delete.\n\n"

        "=== SOURCES ===\n\n"
        "Always set source: 'claude' (Desktop), 'claude_code', or 'perplexity'. "
        "Importance 1-5 on every memory: 5=life-defining identity/values/major goals, 4=major projects/key relationships/hard commitments, 3=normal useful fact, 2=minor/secondary detail, 1=trivial or short-lived. "
        "Use expiry (YYYY-MM-DD) only for genuinely time-bound facts. For a fact that continues another bullet, pass parent= to nest it as a sub-bullet instead of a dangling sibling.\n\n"

        "=== CLIENT DEFAULTS ===\n\n"
        "Claude Code: scope=build, auto-save build logs. Perplexity: scope=strategy. Claude Desktop: full access, no default scope."
    ),
)


@mcp.tool()
def get_full_brain(scope: str = "") -> str:
    """Get ALL brain data across every cluster. Use this when you need comprehensive
    context about the user — who they are, everything they're working on, their full
    background. Returns all memory files with their contents.
    Scope filters clusters: 'build' for project/code context, 'strategy' for
    personal/life context. Empty string returns everything."""
    docs = filter_by_scope(aw_list(), scope)
    parts = []
    for doc in sorted(docs, key=lambda d: d["filename"]):
        content = doc["content"]
        lines = [l for l in content.split("\n") if not l.strip().startswith("<!--")]
        parts.append(f"=== {doc['filename']} ===\n" + "\n".join(lines))
        emit_activity(doc["filename"], "read")
    return "\n\n".join(parts) if parts else "brain is empty"


@mcp.tool()
def get_cluster(filename: str) -> str:
    """Read a specific brain cluster by filename (e.g. 'core.md', 'projects.md').
    Use this when you need detailed info from one specific area."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = aw_find(filename)
    if not doc:
        available = [d["filename"] for d in aw_list()]
        return f"'{filename}' not found. Available: {', '.join(available)}"
    lines = [l for l in doc["content"].split("\n") if not l.strip().startswith("<!-- color:")]
    emit_activity(filename, "read")
    return "\n".join(lines)


@mcp.tool()
def list_clusters(scope: str = "") -> str:
    """List all brain clusters with their section headers and bullet counts.
    Use this to understand what memory areas exist before diving deeper.
    Scope filters: 'build' for project/code clusters, 'strategy' for life/goals clusters."""
    docs = filter_by_scope(aw_list(), scope)
    lines = []
    for doc in sorted(docs, key=lambda d: d["filename"]):
        headers = []
        bullet_count = 0
        doc_scope = get_scope(doc)
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if stripped.startswith("##"):
                headers.append(stripped)
            elif stripped.startswith("- "):
                bullet_count += 1
        header_str = ", ".join(headers) if headers else "(no sections)"
        scope_tag = f" [{doc_scope}]" if doc_scope != "all" else ""
        lines.append(f"{doc['filename']}{scope_tag} — {bullet_count} items — {header_str}")
    return "\n".join(lines) if lines else "no clusters"


@mcp.tool()
def search_brain(query: str, scope: str = "") -> str:
    """Search across all brain data for a keyword or phrase. Returns matching
    lines with their source file. Uses exact matching first, then falls back
    to fuzzy matching if no exact results found. Scope filters: 'build' or 'strategy'.
    Zero results does NOT mean the info is absent — try get_cluster on likely clusters."""
    q = query.lower().strip()
    if not q:
        return "empty query"
    docs = filter_by_scope(aw_list(), scope)
    results = []
    hit_files = set()
    fuzzy_candidates = []
    for doc in docs:
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("<!--"):
                continue
            stripped_lower = stripped.lower()
            if q in stripped_lower:
                results.append(f"[{doc['filename']}] {stripped}")
                hit_files.add(doc["filename"])
            else:
                clean = re.sub(r'\{\{[^}]*\}\}', '', stripped_lower).strip()
                ratio = SequenceMatcher(None, q, clean).ratio()
                words_in_q = set(q.split())
                words_in_line = set(clean.split())
                word_overlap = len(words_in_q & words_in_line)
                score = ratio + (word_overlap * 0.15)
                if score > 0.4:
                    fuzzy_candidates.append((score, doc["filename"], stripped))
    if results:
        for f in hit_files:
            emit_activity(f, "read")
        return "\n".join(results)
    if fuzzy_candidates:
        fuzzy_candidates.sort(key=lambda x: x[0], reverse=True)
        top = fuzzy_candidates[:8]
        fuzzy_results = [f"[{f}] (fuzzy {s:.0%}) {line}" for s, f, line in top]
        for _, f, _ in top:
            emit_activity(f, "read")
        return f"no exact matches for '{query}', but found fuzzy matches:\n" + "\n".join(fuzzy_results)
    return f"no matches for '{query}' — try get_cluster on the most likely cluster"


@mcp.tool()
def get_context_for_topic(topic: str, scope: str = "") -> str:
    """Get brain data relevant to a specific topic. Automatically selects the
    most relevant clusters based on keywords. Use this instead of get_full_brain
    when you only need info about a specific area. Scope filters: 'build' or 'strategy'."""
    docs = filter_by_scope(aw_list(), scope)
    selected = select_files(topic, docs, max_files=4)
    parts = []
    for doc in selected:
        lines = [l for l in doc["content"].split("\n") if not l.strip().startswith("<!--")]
        parts.append(f"=== {doc['filename']} ===\n" + "\n".join(lines))
        emit_activity(doc["filename"], "read")
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


@mcp.tool()
def save_to_brain(filename: str, bullet: str, importance: int = 3, section: str = "", expiry: str = "", source: str = "", rec: str = "", parent: str = "") -> str:
    """Save a memory to a brain cluster.

    BEFORE CALLING THIS: call get_cluster on the target file first — confirm the
    section exists and check for duplicates. If a bullet about this topic already
    exists, use edit_bullet instead.

    RULES:
    - importance (1-5) is REQUIRED — rank honestly: 5 = life-defining identity / values /
      major goals, 4 = major projects / key relationships / hard commitments, 3 = normal
      useful fact, 2 = minor or secondary detail, 1 = trivial or short-lived. Save freely;
      do not skip useful info — just rank it.
    - Pass the section parameter (exact ## header name).
    - One complete thought per bullet. If a fact directly continues or extends another
      bullet, pass 'parent' (a unique substring of that bullet) to nest this as a
      sub-bullet, instead of creating a dangling separate bullet.
    - Source is REQUIRED: 'claude', 'claude_code', 'perplexity', or 'manual'.
    - expiry (YYYY-MM-DD): only for genuinely time-bound facts — they auto-delete after.
    - rec: 'pending' for AI recommendations. Leave empty for confirmed facts.
    - If no cluster fits, use create_cluster instead."""
    if not filename.endswith(".md"):
        filename += ".md"
    bullet = bullet.strip()
    if bullet.startswith("- "):
        bullet = bullet[2:].strip()
    importance = _clamp_imp(importance)
    if not source:
        source = "mcp"
    if rec and rec not in ("pending", "confirmed"):
        rec = ""
    doc = aw_find(filename)
    if not doc:
        available = [d["filename"] for d in aw_list()]
        return f"'{filename}' not found. Available: {', '.join(available)}"

    def _stamp(prefix):
        meta = "{{" + date.today().isoformat() + "|i:" + str(importance)
        if source:
            meta += "|s:" + source
        if expiry:
            meta += "|x:" + expiry
        if rec:
            meta += "|rec:" + rec
        meta += "}}"
        return prefix + bullet + " " + meta

    lines = doc["content"].split("\n")
    if parent:
        pidx = -1
        for i, line in enumerate(lines):
            if line.strip().startswith("- ") and parent.strip() in line:
                pidx = i
                break
        if pidx == -1:
            return f"parent bullet matching '{parent}' not found in {filename}"
        child = _stamp("  - ")
        ins = pidx + 1
        while ins < len(lines) and lines[ins].startswith("  - "):
            ins += 1
        lines.insert(ins, child)
        db_update(doc["$id"], "\n".join(lines))
        emit_activity(filename, "write", source)
        return f"saved under '{parent.strip()}' in {filename}: {child.strip()}"

    stamped = _stamp("- ")
    content = doc["content"]
    if section:
        content = _insert_under_section(content, section, stamped)
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += stamped + "\n"
    db_update(doc["$id"], content)
    target = f"{filename} > {section}" if section else filename
    emit_activity(filename, "write", source)
    return f"saved to {target}: {stamped}"


@mcp.tool()
def create_cluster(filename: str, content: str, source: str = "", scope: str = "") -> str:
    """Create a new brain cluster. Use when info doesn't fit any existing cluster.
    Call list_clusters first to confirm no existing cluster fits.
    Provide full markdown with ## sections and - bullet points. Each bullet = one fact.
    Set scope: 'build' for project/code, 'strategy' for life/goals.
    Source is REQUIRED: 'claude', 'claude_code', 'perplexity', or 'manual'.
    Example: '## section name\\n- fact one\\n- fact two'"""
    if not filename.endswith(".md"):
        filename += ".md"
    if aw_find(filename):
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
    db_create(filename, final)
    emit_activity(filename, "write", source)
    return f"created {filename} [{scope or 'all'}] with {sum(1 for l in stamped if l.strip().startswith('- '))} bullets"


@mcp.tool()
def edit_bullet(filename: str, old_text: str, new_text: str, source: str = "") -> str:
    """Edit an existing bullet in a cluster. Use to update outdated information.
    MUST call get_cluster first to find the exact bullet text.
    old_text: a unique substring of the bullet line (excluding metadata).
    new_text: the full replacement bullet (start with '- ').
    Source is REQUIRED: 'claude', 'claude_code', 'perplexity', or 'manual'."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = aw_find(filename)
    if not doc:
        available = [d["filename"] for d in aw_list()]
        return f"'{filename}' not found. Available: {', '.join(available)}"
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
    mi = re.search(r"\|i:(\d)", old_line)
    if mi:
        old_imp = _clamp_imp(mi.group(1))
    else:
        mt = re.search(r"\|t:(\w+)", old_line)
        old_imp = {"core": 5, "active": 3, "ref": 2, "temp": 1}.get(mt.group(1) if mt else "", 3)
    if not new_text.startswith("- "):
        new_text = "- " + new_text
    if not source:
        source = "mcp"
    if not re.search(r"\{\{\d{4}-\d{2}-\d{2}", new_text):
        meta = "{{" + date.today().isoformat() + "|i:" + str(old_imp) + "|s:" + source + "}}"
        new_text = new_text.rstrip() + " " + meta
    lines[match_idx] = indent + new_text
    content = "\n".join(lines)
    db_update(doc["$id"], content)
    emit_activity(filename, "write", source)
    return f"edited in {filename}: '{old_text.strip()}' → '{new_text.strip()}'"


@mcp.tool()
def set_importance(filename: str, bullet_text: str, importance: int) -> str:
    """Update the 1-5 importance ranking of an existing memory bullet.
    Use when re-evaluating how much a memory matters (5 = life-defining, 1 = trivial).
    bullet_text: a unique substring of the bullet to re-rank."""
    if not filename.endswith(".md"):
        filename += ".md"
    importance = _clamp_imp(importance)
    doc = aw_find(filename)
    if not doc:
        available = [d["filename"] for d in aw_list()]
        return f"'{filename}' not found. Available: {', '.join(available)}"
    lines = doc["content"].split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("- ") and bullet_text.strip() in line:
            if "{{" not in line:
                lines[i] = line.rstrip() + " {{" + date.today().isoformat() + "|i:" + str(importance) + "}}"
            else:
                line = re.sub(r"\|t:\w+", "", line)
                if re.search(r"\|i:\d", line):
                    line = re.sub(r"\|i:\d+", "|i:" + str(importance), line)
                else:
                    line = re.sub(r"(\{\{\d{4}-\d{2}-\d{2})", r"\1|i:" + str(importance), line, count=1)
                lines[i] = line
            db_update(doc["$id"], "\n".join(lines))
            emit_activity(filename, "write")
            return f"set importance {importance} on: {bullet_text.strip()}"
    return f"no bullet matching '{bullet_text}' in {filename}"


@mcp.tool()
def delete_bullet(filename: str, old_text: str) -> str:
    """Delete a bullet from a cluster. MUST call get_cluster first to find the exact text.
    old_text: a unique substring of the bullet line to remove."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = aw_find(filename)
    if not doc:
        available = [d["filename"] for d in aw_list()]
        return f"'{filename}' not found. Available: {', '.join(available)}"
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
    db_update(doc["$id"], content)
    emit_activity(filename, "write")
    return f"deleted from {filename}: {removed.strip()}"


@mcp.tool()
def delete_section(filename: str, section: str) -> str:
    """Delete an entire ## section and all its bullets from a cluster.
    Use for removing outdated sections. Call get_cluster first to confirm the section name."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = aw_find(filename)
    if not doc:
        available = [d["filename"] for d in aw_list()]
        return f"'{filename}' not found. Available: {', '.join(available)}"
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
    db_update(doc["$id"], content)
    emit_activity(filename, "write")
    return f"deleted section '{section}' ({removed_count} bullets) from {filename}"


@mcp.tool()
def get_brain_summary() -> str:
    """Get a quick structured summary of the user's identity and key facts.
    Pulls from core.md and gives a snapshot. Use this for a fast intro to
    who the user is without loading everything."""
    doc = aw_find("core.md")
    if not doc:
        return "core.md not found"
    lines = [l for l in doc["content"].split("\n") if not l.strip().startswith("<!-- color:")]
    emit_activity("core.md", "read")
    return "\n".join(lines)


@mcp.tool()
def get_session_stats(source: str = "") -> str:
    """Check how actively Cortex is being used. Returns read/write counts
    per source over the last 24 hours. Call this on your first message in a
    conversation to calibrate — if your source shows 0 writes over many hours,
    you are likely under-saving. Source: 'claude', 'claude_code', 'perplexity'."""
    doc = _aw_activity_doc()
    if not doc or not doc["content"].strip():
        return "no activity recorded yet"
    events = []
    for line in doc["content"].strip().split("\n"):
        try:
            events.append(_json.loads(line))
        except Exception:
            continue
    cutoff = _time.time() - 86400
    recent = [e for e in events if e.get("ts", 0) > cutoff]
    if source:
        recent = [e for e in recent if e.get("source", "") == source]
    reads = sum(1 for e in recent if e.get("action") == "read")
    writes = sum(1 for e in recent if e.get("action") == "write")
    sources_seen = set(e.get("source", "unknown") for e in recent)
    clusters_written = set(e["cluster"] for e in recent if e.get("action") == "write")
    scope_label = f" (source={source})" if source else ""
    lines = [
        f"last 24h activity{scope_label}:",
        f"  reads: {reads}",
        f"  writes: {writes}",
        f"  clusters written: {', '.join(sorted(clusters_written)) if clusters_written else 'none'}",
        f"  active sources: {', '.join(sorted(sources_seen)) if sources_seen else 'none'}",
    ]
    if writes == 0 and reads > 0:
        lines.append("  WARNING: reads but zero writes — you are likely under-saving")
    return "\n".join(lines)


@mcp.tool()
def lint_brain() -> str:
    """Run a health check on the entire brain. Finds: duplicate/near-duplicate
    bullets across clusters, empty sections, active-tier bullets older than 90 days
    (stale), and bullets missing metadata. Returns a report. Call this periodically
    or when the user asks for a brain cleanup."""
    docs = aw_list()
    all_bullets = []
    issues = []
    for doc in docs:
        section = "(top)"
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if stripped.startswith("##"):
                section = stripped
            elif stripped.startswith("- "):
                text = re.sub(r'\{\{[^}]*\}\}', '', stripped).strip()
                all_bullets.append({"text": text, "full": stripped, "file": doc["filename"], "section": section})
    for doc in docs:
        lines = doc["content"].split("\n")
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith("##"):
                has_bullets = False
                j = i + 1
                while j < len(lines) and not lines[j].strip().startswith("##"):
                    if lines[j].strip().startswith("- "):
                        has_bullets = True
                        break
                    j += 1
                if not has_bullets:
                    issues.append(f"EMPTY SECTION: {doc['filename']} > {stripped}")
                i = j if not has_bullets else i + 1
            else:
                i += 1
    for b in all_bullets:
        meta = re.search(r'\{\{([^}]*)\}\}', b["full"])
        if not meta:
            issues.append(f"NO METADATA: {b['file']} > {b['text'][:60]}")
            continue
        m = meta.group(1)
        mi = re.search(r"\|i:(\d)", m)
        if mi:
            imp = int(mi.group(1))
        else:
            mt = re.search(r"\|t:(\w+)", m)
            imp = {"core": 5, "active": 3, "ref": 2, "temp": 1}.get(mt.group(1) if mt else "", 3)
        if imp <= 3:
            date_match = re.match(r'(\d{4}-\d{2}-\d{2})', m)
            if date_match:
                try:
                    d = date.fromisoformat(date_match.group(1))
                    if (date.today() - d).days > 90:
                        issues.append(f"STALE (>90d, i{imp}): {b['file']} > {b['text'][:60]}")
                except ValueError:
                    pass
    seen = []
    for i, a in enumerate(all_bullets):
        for b in all_bullets[i+1:]:
            if a["file"] == b["file"] and a["section"] == b["section"]:
                continue
            ratio = SequenceMatcher(None, a["text"].lower(), b["text"].lower()).ratio()
            if ratio > 0.8:
                key = tuple(sorted([a["text"][:40], b["text"][:40]]))
                if key not in seen:
                    seen.append(key)
                    issues.append(f"NEAR-DUPLICATE ({ratio:.0%}): [{a['file']}] {a['text'][:50]} <-> [{b['file']}] {b['text'][:50]}")
    if not issues:
        return "brain is clean — no issues found"
    return f"found {len(issues)} issues:\n" + "\n".join(f"  {x}" for x in issues[:30])


if __name__ == "__main__":
    mcp.run()
