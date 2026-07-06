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
from db import (db_list, db_find, db_names, db_create, db_update, db_update_cas,
                db_delete, db_rename, db_log_activity, db_get_activity)

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json as _json
import time as _time

def emit_activity(cluster, action="read", source=""):
    try:
        db_log_activity({"cluster": cluster, "action": action, "source": source, "ts": _time.time()})
    except Exception:
        pass

def _clamp_imp(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 3
    return 1 if n < 1 else 5 if n > 5 else n


# --- hierarchy helpers -------------------------------------------------------
# Tier model: cluster (doc) -> sub-cluster (doc named 'parent/sub.md') ->
# ## section -> parent bullet -> child bullet (2-space indent per level).
# Sibling links: |ln:<id> metadata token shared by 2+ bullets anywhere in the brain.

def _stem(filename):
    return filename[:-3] if filename.endswith(".md") else filename


def _parent_cluster(filename):
    s = _stem(filename)
    return s.split("/")[0] + ".md" if "/" in s else None


def _sub_clusters(docs, filename):
    prefix = _stem(filename) + "/"
    return [d for d in docs if d["filename"].startswith(prefix)]


def _is_bullet(line):
    return line.lstrip(" \t").startswith("- ")


def _indent_level(line):
    return (len(line) - len(line.lstrip(" "))) // 2


def _subtree_end(lines, idx):
    """Index just past the last line of the subtree rooted at lines[idx]."""
    base = _indent_level(lines[idx])
    j = idx + 1
    while j < len(lines):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            break
        if _is_bullet(lines[j]) and _indent_level(lines[j]) <= base:
            break
        j += 1
    return j


_ID_RE = re.compile(r"\|id:([a-f0-9]+)")


def _new_id():
    import uuid
    return uuid.uuid4().hex[:6]


def _bullet_id(line):
    m = _ID_RE.search(line)
    return m.group(1) if m else None


def _with_id(line):
    """Ensure a bullet line carries a stable |id: token."""
    if _ID_RE.search(line) or "{{" not in line:
        return line
    return re.sub(r"\}\}", "|id:" + _new_id() + "}}", line, count=1)


def _find_bullet(lines, text):
    """Locate a bullet by unique substring, or by stable id via 'id:xxxxxx'."""
    ref = text.strip()
    if re.fullmatch(r"id:[a-f0-9]{4,}", ref):
        tok = "|" + ref
        for i, line in enumerate(lines):
            if _is_bullet(line) and tok in line:
                return i
        return -1
    for i, line in enumerate(lines):
        if _is_bullet(line) and ref in line:
            return i
    return -1


_LINK_RE = re.compile(r"\|ln:([a-z0-9]+)")


def _link_id(line):
    m = _LINK_RE.search(line)
    return m.group(1) if m else None


def _with_link_id(line, lid):
    if _LINK_RE.search(line):
        line = _LINK_RE.sub("", line)
    if "{{" in line:
        return re.sub(r"\}\}", "|ln:" + lid + "}}", line, count=1)
    return line.rstrip() + " {{" + date.today().isoformat() + "|i:3|ln:" + lid + "}}"


def _new_link_id(docs):
    import uuid
    used = set()
    for d in docs:
        used.update(_LINK_RE.findall(d["content"]))
    while True:
        lid = uuid.uuid4().hex[:4]
        if lid not in used:
            return lid


_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+(/[a-zA-Z0-9_\-]+)?\.md$")


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
    for w in re.split(r"[_/]", fname_stem):
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


def aw_find(filename):
    return db_find(filename)


def _not_found(filename):
    return f"'{filename}' not found. Available: {', '.join(db_names())}"


def get_scope(doc, docs=None):
    m = re.search(r'<!--\s*scope:(\w+)\s*-->', doc["content"])
    if m:
        return m.group(1)
    # sub-clusters inherit the parent cluster's scope unless they set their own
    parent = _parent_cluster(doc["filename"])
    if parent and docs is not None:
        for d in docs:
            if d["filename"] == parent:
                pm = re.search(r'<!--\s*scope:(\w+)\s*-->', d["content"])
                if pm:
                    return pm.group(1)
                break
    return "all"


def filter_by_scope(docs, scope):
    if not scope:
        return docs
    return [d for d in docs if get_scope(d, docs) in (scope, "all")]




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
        for w in re.split(r"[_/]", fname_stem):
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
        "Cortex is the user's personal memory system - his external brain, stored in a database. "
        "Every AI that connects shares it. Use it on EVERY message: read before you answer, save what you learn, keep it clean.\n\n"

        "=== EVERY MESSAGE ===\n"
        "1. READ FIRST: call get_briefing(topic) for ranked, token-budgeted context; on your first message call get_brain_summary. "
        "To navigate a big cluster, get_outline first (cheap map with ids), then read only what you need. "
        "If a search returns nothing, get_cluster the likeliest cluster - empty results mean the words didn't match, not that the fact is absent. Don't answer from assumption.\n"
        "2. SAVE what's new: default to saving, skip only a pure generic question with zero personal content. Save facts, decisions, and recommendations you generate. Don't gatekeep - save it and rank it. "
        "Keep bullets SHORT (headline under ~600 chars); put detail in child bullets via parent=... - long essay-bullets bloat every future read.\n"
        "3. RECONCILE, don't just append: check if the info already exists. If it updates/contradicts a bullet, edit_bullet to fix it (or delete_bullet if now wrong) - never leave stale and new side by side. If weight changed, set_importance. To relocate a memory, use move_bullet (preserves its metadata) - never delete+resave.\n"
        "4. STRUCTURE: one complete thought per bullet; pass the exact section. Save to the most specific tier that fits. create_cluster only if nothing fits.\n"
        "5. IDS: every memory carries a stable |id: token. Wherever a tool takes a bullet reference, you can pass 'id:xxxxxx' instead of a text substring - it's exact and never ambiguous. Prefer ids when you have them (outline/briefing show them).\n"
        "6. REPORT: end every response with [Cortex: ...] - what you read, saved/edited/reranked, or why you skipped.\n\n"

        "=== STRUCTURE TIERS ===\n"
        "cluster -> sub-cluster -> ## section -> parent memory -> child memory, plus non-hierarchical sibling links.\n"
        "- CLUSTER: a topic doc (projects.md). SUB-CLUSTER: a separate doc named 'parent/sub.md' (projects/wisegraph.md) - a distinct sub-area that deserves its own small, readable doc. Prefer reading/saving at the sub-cluster when one exists; reads stay small.\n"
        "- SECTION: ## header inside a doc. PARENT/CHILD: a fact that continues/elaborates a bullet is saved with parent='<unique words of that bullet>' and nests under it (depth-aware, keep within ~3 levels) - never drop a fragment as a dangling separate bullet.\n"
        "- SIBLINGS: link_memories ties 2+ related memories across any clusters (shared |ln: tag, non-hierarchical); get_linked retrieves the group; unlink_memory removes one.\n\n"

        "=== IMPORTANCE (required on every save) ===\n"
        "Rank 1-5 how much it matters long-term: 5=life-defining identity/values/central goals; 4=major projects/key relationships/hard commitments; 3=normal useful fact (default); 2=minor/secondary/reference; 1=trivial or short-lived. "
        "Rank honestly - low-importance memories are fine, they just sit small. expiry=YYYY-MM-DD only for time-bound facts (auto-delete). Re-rank with set_importance when weight changes.\n\n"

        "=== ACCURACY GUARD ===\n"
        "Only save what you're confident he actually said/meant. Sure -> save. Unsure -> ask 'worth saving [X] to [cluster > section]?'. Vague/joking/speculating -> don't save as fact. Bad data compounds - precision over volume.\n\n"

        "=== RECOMMENDATIONS ===\n"
        "Save the underlying facts first (no rec), then the recommendation with rec='pending' (60-80% confidence, flag when leaning on it). He accepts -> edit_bullet to rec:confirmed. He rejects -> delete_bullet.\n\n"

        "=== ENGAGE ===\n"
        "Be ruthless and direct - no sugarcoating, no filler encouragement, no hedging. If something's wrong say so and why; if a deadline will slip, name what must happen by when. He wants execution help, not validation.\n\n"

        "=== SOURCE ===\n"
        "Set source to your client: 'claude' (Desktop), 'claude_code' (Code, scope=build, auto-save build logs), or 'perplexity'."
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
    """Read a specific brain cluster or sub-cluster by filename (e.g. 'core.md',
    'projects.md', 'projects/wisegraph.md'). Use this when you need detailed info
    from one specific area. Prefer reading a sub-cluster over its whole parent
    when you only need that sub-topic — reads stay small."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = db_find(filename)
    names = db_names()
    subs = [n for n in names if n.startswith(_stem(filename) + "/")]
    if not doc:
        if subs:
            return f"'{filename}' is a cluster group with no top-level doc. Sub-clusters: {', '.join(subs)}"
        return f"'{filename}' not found. Available: {', '.join(names)}"
    lines = [l for l in doc["content"].split("\n") if not l.strip().startswith("<!-- color:")]
    emit_activity(filename, "read")
    out = "\n".join(lines)
    if subs:
        out += f"\n\n[sub-clusters — read separately: {', '.join(subs)}]"
    return out


@mcp.tool()
def list_clusters(scope: str = "") -> str:
    """List all brain clusters (and their sub-clusters, indented) with section
    headers and bullet counts. Use this to understand what memory areas exist
    before diving deeper. Sub-clusters are named 'parent/sub.md' and are shown
    nested under their parent.
    Scope filters: 'build' for project/code clusters, 'strategy' for life/goals clusters."""
    all_docs = aw_list()
    docs = filter_by_scope(all_docs, scope)
    lines = []
    for doc in sorted(docs, key=lambda d: d["filename"]):
        headers = []
        bullet_count = 0
        doc_scope = get_scope(doc, all_docs)
        for line in doc["content"].split("\n"):
            stripped = line.strip()
            if stripped.startswith("##"):
                headers.append(stripped)
            elif stripped.startswith("- "):
                bullet_count += 1
        header_str = ", ".join(headers) if headers else "(no sections)"
        scope_tag = f" [{doc_scope}]" if doc_scope != "all" else ""
        indent = "  " if "/" in _stem(doc["filename"]) else ""
        lines.append(f"{indent}{doc['filename']}{scope_tag} — {bullet_count} items — {header_str}")
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


def _compact_meta(line):
    m = re.search(r"\{\{([^}]*)\}\}", line)
    if not m:
        return "<no-meta>"
    parts = m.group(1).split("|")
    keep = [parts[0]] + [p for p in parts[1:] if p.startswith(("i:", "s:", "x:", "rec:", "ln:", "id:"))]
    return "<" + "|".join(keep) + ">"


def _age_days(line):
    m = re.search(r"\{\{(\d{4}-\d{2}-\d{2})", line)
    if not m:
        return None
    try:
        return (date.today() - date.fromisoformat(m.group(1))).days
    except ValueError:
        return None


def _age_label(days):
    if days is None:
        return ""
    if days < 1:
        return "today"
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _iter_bullets(docs):
    """Yield every bullet with its cluster, section, indent, parent head, and raw line."""
    for doc in docs:
        section = "(top)"
        parent_at = {}
        for line in doc["content"].split("\n"):
            s = line.strip()
            if s.startswith("##"):
                section = s.lstrip("#").strip()
                parent_at = {}
            elif _is_bullet(line):
                ind = _indent_level(line)
                text = re.sub(r"\s*\{\{[^}]*\}\}", "", s)[2:].strip()
                rec = {"file": doc["filename"], "section": section, "indent": ind,
                       "text": text, "line": s,
                       "parent": parent_at.get(ind - 1, {}).get("text") if ind > 0 else None}
                parent_at[ind] = rec
                for k in [k for k in parent_at if k > ind]:
                    del parent_at[k]
                yield rec


@mcp.tool()
def get_outline(filename: str) -> str:
    """Get a compact map of a cluster: every bullet's first ~100 chars with its
    metadata (importance, date, source, id) and nesting, plus section headers.
    Costs a fraction of get_cluster — use this FIRST to navigate a big cluster,
    then read/edit specific memories by their id ('id:xxxxxx' works as the
    bullet reference in edit_bullet / delete_bullet / move_bullet / set_importance /
    link_memories / save_to_brain parent)."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = db_find(filename)
    if not doc:
        names = db_names()
        subs = [n for n in names if n.startswith(_stem(filename) + "/")]
        if subs:
            return f"'{filename}' is a cluster group with no top-level doc. Sub-clusters: {', '.join(subs)}"
        return f"'{filename}' not found. Available: {', '.join(names)}"
    out = []
    for line in doc["content"].split("\n"):
        s = line.strip()
        if s.startswith("<!--") and "scope:" in s:
            out.append(s)
        elif s.startswith("##"):
            out.append("")
            out.append(s)
        elif _is_bullet(line):
            ind = "  " * _indent_level(line)
            text = re.sub(r"\s*\{\{[^}]*\}\}", "", s)[2:].strip()
            head = text[:100] + ("…" if len(text) > 100 else "")
            out.append(f"{ind}- {head} {_compact_meta(s)}")
    subs = [n for n in db_names() if n.startswith(_stem(filename) + "/")]
    if subs:
        out.append("")
        out.append(f"[sub-clusters: {', '.join(subs)}]")
    emit_activity(filename, "read")
    return "\n".join(out).strip() or f"{filename} is empty"


@mcp.tool()
def get_briefing(topic: str, max_tokens: int = 2000, scope: str = "") -> str:
    """Get a token-budgeted briefing on a topic: the most relevant memories across
    the whole brain, ranked by relevance (BM25) x importance x recency, each with
    its cluster, section, age, and id — children carry their parent's headline,
    sibling-linked memories are pulled in. PREFER this over get_context_for_topic
    or whole-cluster reads when you need working context, not an exhaustive dump.
    Scope: 'build' or 'strategy'."""
    import math
    q_terms = [w for w in re.findall(r"[a-z0-9]{3,}", topic.lower()) if w not in STOP_WORDS]
    if not q_terms:
        return "topic too generic — give a few content words"
    docs = filter_by_scope(aw_list(), scope)
    bullets = list(_iter_bullets(docs))
    if not bullets:
        return "brain is empty"

    toks = []
    df = {}
    for b in bullets:
        t = [w for w in re.findall(r"[a-z0-9]{3,}", (b["file"] + " " + b["section"] + " " + b["text"]).lower())
             if w not in STOP_WORDS]
        toks.append(t)
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    n = len(bullets)
    avgdl = sum(len(t) for t in toks) / n
    k1, kb = 1.5, 0.75

    scored = []
    for b, t in zip(bullets, toks):
        if not t:
            continue
        tf = {}
        for w in t:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in q_terms:
            f = tf.get(w, 0)
            if not f:
                continue
            idf = math.log((n - df[w] + 0.5) / (df[w] + 0.5) + 1)
            s += idf * f * (k1 + 1) / (f + k1 * (1 - kb + kb * len(t) / avgdl))
        if s <= 0:
            continue
        mi = re.search(r"\|i:(\d)", b["line"])
        imp = _clamp_imp(mi.group(1)) if mi else 3
        days = _age_days(b["line"])
        recency = 1.0 if days is None or days <= 30 else max(0.6, 1 - 0.003 * (days - 30))
        scored.append((s * (0.7 + 0.15 * imp) * recency, imp, b))
    if not scored:
        return f"nothing relevant to '{topic}' — try search_brain or get_cluster on a likely cluster"
    scored.sort(key=lambda x: -x[0])

    budget = max(500, max_tokens) * 4
    out = [f"briefing: {topic} (top matches, ranked; use id:xxxxxx to edit/move/link)"]
    used = len(out[0])
    included = set()
    link_seen = set()
    for _, imp, b in scored:
        if id(b) in included:
            continue
        text = b["text"][:500] + ("…" if len(b["text"]) > 500 else "")
        entry = f"[{b['file']} > {b['section']}] ({_age_label(_age_days(b['line']))}, i{imp}) {text} {_compact_meta(b['line'])}"
        if b["parent"]:
            entry += f"\n    (child of: {b['parent'][:80]})"
        lid = _link_id(b["line"])
        if lid and lid not in link_seen:
            link_seen.add(lid)
            sibs = [x for x in bullets if _link_id(x["line"]) == lid and x is not b]
            for sx in sibs[:3]:
                entry += f"\n    (sibling: [{sx['file']}] {sx['text'][:80]})"
        if used + len(entry) > budget:
            break
        out.append(entry)
        used += len(entry)
        included.add(id(b))
    return "\n".join(out)


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
    - Save to the most specific tier: prefer a sub-cluster ('projects/wisegraph.md')
      over its parent cluster when one exists for the topic.
    - Pass the section parameter (exact ## header name).
    - One complete thought per bullet. If a fact directly continues or extends another
      bullet, pass 'parent' (a unique substring of that bullet) to nest this as a
      child memory under it, instead of creating a dangling separate bullet. The parent
      may itself be a child — nesting is depth-aware (keep it within ~3 levels).
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

    def _stamp(prefix):
        meta = "{{" + date.today().isoformat() + "|i:" + str(importance)
        if source:
            meta += "|s:" + source
        if expiry:
            meta += "|x:" + expiry
        if rec:
            meta += "|rec:" + rec
        meta += "|id:" + _new_id() + "}}"
        return prefix + bullet + " " + meta

    nudge = ""
    if len(bullet) > 600:
        nudge = (f" NOTE: this bullet is {len(bullet)} chars — long bullets bloat every future read. "
                 "Next time save the headline as the bullet and the detail as child bullets (parent=...).")

    for _ in range(3):
        doc = db_find(filename)
        if not doc:
            return _not_found(filename)
        lines = doc["content"].split("\n")
        if parent:
            pidx = _find_bullet(lines, parent)
            if pidx == -1:
                return f"parent bullet matching '{parent}' not found in {filename}"
            child_indent = "  " * (_indent_level(lines[pidx]) + 1)
            child = _stamp(child_indent + "- ")
            ins = _subtree_end(lines, pidx)
            lines.insert(ins, child)
            if db_update_cas(doc["$id"], doc["content"], "\n".join(lines)):
                emit_activity(filename, "write", source)
                return f"saved under '{parent.strip()}' in {filename}: {child.strip()}{nudge}"
            continue
        stamped = _stamp("- ")
        content = doc["content"]
        if section:
            content = _insert_under_section(content, section, stamped)
        else:
            if not content.endswith("\n"):
                content += "\n"
            content += stamped + "\n"
        if db_update_cas(doc["$id"], doc["content"], content):
            target = f"{filename} > {section}" if section else filename
            emit_activity(filename, "write", source)
            return f"saved to {target}: {stamped}{nudge}"
    return f"write conflict on {filename} — another session is writing; retry"


@mcp.tool()
def create_cluster(filename: str, content: str, source: str = "", scope: str = "") -> str:
    """Create a new brain cluster or sub-cluster. Use when info doesn't fit any
    existing cluster. Call list_clusters first to confirm no existing cluster fits.
    Sub-clusters are named 'parent/sub.md' (e.g. 'projects/wisegraph.md') — use one
    when a topic is a distinct sub-area of an existing cluster (each project under
    projects/, etc). Sub-clusters inherit the parent's scope unless overridden.
    Provide full markdown with ## sections and - bullet points. Each bullet = one fact.
    Set scope: 'build' for project/code, 'strategy' for life/goals.
    Source is REQUIRED: 'claude', 'claude_code', 'perplexity', or 'manual'.
    Example: '## section name\\n- fact one\\n- fact two'"""
    if not filename.endswith(".md"):
        filename += ".md"
    if not _FILENAME_RE.match(filename):
        return f"invalid filename '{filename}' — use letters/digits/_/- with at most one '/' (sub-cluster), ending in .md"
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
            meta = "{{" + date.today().isoformat() + "|i:3|s:" + source + "|id:" + _new_id() + "}}"
            stamped.append(line[:len(line) - len(line.lstrip())] + stripped.rstrip() + " " + meta)
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
    if not source:
        source = "mcp"
    for _ in range(3):
        doc = db_find(filename)
        if not doc:
            return _not_found(filename)
        lines = doc["content"].split("\n")
        match_idx = _find_bullet(lines, old_text)
        if match_idx == -1:
            # fall back to matching any line (legacy behavior for non-bullet lines)
            for i, line in enumerate(lines):
                if old_text.strip() in line:
                    match_idx = i
                    break
        if match_idx == -1:
            return f"no bullet matching '{old_text}' found in {filename}"
        old_line = lines[match_idx]
        indent = "  " * _indent_level(old_line) if _is_bullet(old_line) else ""
        mi = re.search(r"\|i:(\d)", old_line)
        if mi:
            old_imp = _clamp_imp(mi.group(1))
        else:
            mt = re.search(r"\|t:(\w+)", old_line)
            old_imp = {"core": 5, "active": 3, "ref": 2, "temp": 1}.get(mt.group(1) if mt else "", 3)
        text = new_text if new_text.startswith("- ") else "- " + new_text
        if not re.search(r"\{\{\d{4}-\d{2}-\d{2}", text):
            # fresh stamp, but carry over expiry / rec / link / id tokens from the old line
            meta = "{{" + date.today().isoformat() + "|i:" + str(old_imp) + "|s:" + source
            for pat in (r"\|x:\d{4}-\d{2}-\d{2}", r"\|rec:\w+", r"\|ln:[a-z0-9]+", r"\|id:[a-f0-9]+"):
                mm = re.search(pat, old_line)
                if mm:
                    meta += mm.group(0)
            meta += "}}"
            text = text.rstrip() + " " + meta
        text = _with_id(text)
        lines[match_idx] = indent + text.strip()
        if db_update_cas(doc["$id"], doc["content"], "\n".join(lines)):
            emit_activity(filename, "write", source)
            return f"edited in {filename}: '{old_text.strip()}' → '{text.strip()}'"
    return f"write conflict on {filename} — another session is writing; retry"


@mcp.tool()
def set_importance(filename: str, bullet_text: str, importance: int) -> str:
    """Update the 1-5 importance ranking of an existing memory bullet.
    Use when re-evaluating how much a memory matters (5 = life-defining, 1 = trivial).
    bullet_text: a unique substring of the bullet to re-rank."""
    if not filename.endswith(".md"):
        filename += ".md"
    importance = _clamp_imp(importance)
    for _ in range(3):
        doc = db_find(filename)
        if not doc:
            return _not_found(filename)
        lines = doc["content"].split("\n")
        i = _find_bullet(lines, bullet_text)
        if i == -1:
            return f"no bullet matching '{bullet_text}' in {filename}"
        line = lines[i]
        if "{{" not in line:
            lines[i] = line.rstrip() + " {{" + date.today().isoformat() + "|i:" + str(importance) + "|id:" + _new_id() + "}}"
        else:
            line = re.sub(r"\|t:\w+", "", line)
            if re.search(r"\|i:\d", line):
                line = re.sub(r"\|i:\d+", "|i:" + str(importance), line)
            else:
                line = re.sub(r"(\{\{\d{4}-\d{2}-\d{2})", r"\1|i:" + str(importance), line, count=1)
            lines[i] = line
        if db_update_cas(doc["$id"], doc["content"], "\n".join(lines)):
            emit_activity(filename, "write")
            return f"set importance {importance} on: {bullet_text.strip()}"
    return f"write conflict on {filename} — another session is writing; retry"


@mcp.tool()
def delete_bullet(filename: str, old_text: str) -> str:
    """Delete a bullet from a cluster. MUST call get_cluster first to find the exact text.
    old_text: a unique substring of the bullet line to remove.
    If the bullet has child memories nested under it, they are deleted with it."""
    if not filename.endswith(".md"):
        filename += ".md"
    for _ in range(3):
        doc = db_find(filename)
        if not doc:
            return _not_found(filename)
        lines = doc["content"].split("\n")
        match_idx = _find_bullet(lines, old_text)
        if match_idx == -1:
            for i, line in enumerate(lines):
                if old_text.strip() in line:
                    match_idx = i
                    break
        if match_idx == -1:
            return f"no bullet matching '{old_text}' found in {filename}"
        if _is_bullet(lines[match_idx]):
            end = _subtree_end(lines, match_idx)
        else:
            end = match_idx + 1
        removed = lines[match_idx]
        child_count = end - match_idx - 1
        del lines[match_idx:end]
        if db_update_cas(doc["$id"], doc["content"], "\n".join(lines)):
            emit_activity(filename, "write")
            extra = f" (+{child_count} child memories)" if child_count else ""
            return f"deleted from {filename}: {removed.strip()}{extra}"
    return f"write conflict on {filename} — another session is writing; retry"


@mcp.tool()
def move_bullet(filename: str, bullet_text: str, dest_filename: str, dest_section: str = "", dest_parent: str = "", source: str = "") -> str:
    """Move a memory (and its nested child memories) to another cluster, sub-cluster,
    or section — WITHOUT losing its original date/importance/source metadata. This is
    THE tool for reorganizing the brain: never delete+resave to move a memory.
    bullet_text: unique substring of the bullet to move.
    dest_section: exact ## header in the destination (created if missing).
    dest_parent: optional — unique substring of a bullet in the destination to nest
    the moved memory under (as a child). Overrides dest_section placement."""
    if not filename.endswith(".md"):
        filename += ".md"
    if not dest_filename.endswith(".md"):
        dest_filename += ".md"
    src = aw_find(filename)
    if not src:
        return _not_found(filename)
    src_lines = src["content"].split("\n")
    idx = _find_bullet(src_lines, bullet_text)
    if idx == -1:
        return f"no bullet matching '{bullet_text}' found in {filename}"
    end = _subtree_end(src_lines, idx)
    block = src_lines[idx:end]
    base_indent = _indent_level(block[0])
    # normalize the block to zero indent, keeping relative child depth
    norm = [("  " * max(0, _indent_level(l) - base_indent)) + l.lstrip(" ") for l in block]
    del src_lines[idx:end]

    same_doc = dest_filename == filename
    if same_doc:
        dest = {"$id": src["$id"], "filename": filename, "content": "\n".join(src_lines)}
    else:
        dest = aw_find(dest_filename)
        if not dest:
            return f"destination '{dest_filename}' not found — create_cluster it first (nothing was moved)"
    dest_lines = dest["content"].split("\n")

    if dest_parent:
        pidx = _find_bullet(dest_lines, dest_parent)
        if pidx == -1:
            return f"dest_parent matching '{dest_parent}' not found in {dest_filename} (nothing was moved)"
        shift = _indent_level(dest_lines[pidx]) + 1
        block_out = [("  " * shift) + l for l in norm]
        ins = _subtree_end(dest_lines, pidx)
        dest_lines[ins:ins] = block_out
        dest_content = "\n".join(dest_lines)
    elif dest_section:
        # insert the head bullet under the section, then splice its children right after it
        marker = "\x00MOVE_MARKER\x00"
        dest_content = _insert_under_section("\n".join(dest_lines), dest_section, marker)
        dl = dest_content.split("\n")
        mi = dl.index(marker)
        dl[mi:mi + 1] = norm
        dest_content = "\n".join(dl)
    else:
        dest_content = dest["content"]
        if not dest_content.endswith("\n"):
            dest_content += "\n"
        dest_content += "\n".join(norm) + "\n"

    if same_doc:
        db_update(src["$id"], dest_content)
    else:
        db_update(src["$id"], "\n".join(src_lines))
        db_update(dest["$id"], dest_content)
    emit_activity(dest_filename, "write", source)
    moved = block[0].strip()
    extra = f" (+{len(block) - 1} child memories)" if len(block) > 1 else ""
    target = f"{dest_filename} > {dest_section}" if dest_section else dest_filename
    if dest_parent:
        target = f"{dest_filename} under '{dest_parent.strip()}'"
    return f"moved to {target}: {moved}{extra}"


@mcp.tool()
def rename_cluster(old_filename: str, new_filename: str) -> str:
    """Rename a cluster, including converting it into a sub-cluster or back
    (e.g. 'wisegraph.md' -> 'projects/wisegraph.md'). Content, colors, and all
    memory metadata are untouched."""
    if not old_filename.endswith(".md"):
        old_filename += ".md"
    if not new_filename.endswith(".md"):
        new_filename += ".md"
    if not _FILENAME_RE.match(new_filename):
        return f"invalid new filename '{new_filename}' — use letters/digits/_/- with at most one '/', ending in .md"
    doc = aw_find(old_filename)
    if not doc:
        return _not_found(old_filename)
    if aw_find(new_filename):
        return f"'{new_filename}' already exists"
    db_rename(doc["$id"], new_filename)
    emit_activity(new_filename, "write")
    return f"renamed {old_filename} -> {new_filename}"


@mcp.tool()
def rename_section(filename: str, old_section: str, new_section: str) -> str:
    """Rename a ## section header inside a cluster. Bullets are untouched."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = aw_find(filename)
    if not doc:
        return _not_found(filename)
    lines = doc["content"].split("\n")
    target = old_section.lower().strip().lstrip("#").strip()
    for i, line in enumerate(lines):
        if line.strip().startswith("##") and line.strip().lstrip("#").strip().lower() == target:
            lines[i] = "## " + new_section.strip().lstrip("#").strip()
            db_update(doc["$id"], "\n".join(lines))
            emit_activity(filename, "write")
            return f"renamed section '{old_section}' -> '{new_section}' in {filename}"
    return f"section '{old_section}' not found in {filename}"


@mcp.tool()
def link_memories(filename_a: str, bullet_a: str, filename_b: str, bullet_b: str) -> str:
    """Link two memories as siblings — related, non-hierarchical. They may be in any
    clusters (or the same one). Linking adds a shared |ln:<id> tag to both; use
    get_linked on either to retrieve the whole group. To link a third memory into an
    existing group, call this with one already-linked memory and the new one."""
    if not filename_a.endswith(".md"):
        filename_a += ".md"
    if not filename_b.endswith(".md"):
        filename_b += ".md"
    docs = aw_list()
    by_name = {d["filename"]: d for d in docs}
    da, db_ = by_name.get(filename_a), by_name.get(filename_b)
    if not da:
        return f"'{filename_a}' not found"
    if not db_:
        return f"'{filename_b}' not found"
    la = da["content"].split("\n")
    ia = _find_bullet(la, bullet_a)
    if ia == -1:
        return f"no bullet matching '{bullet_a}' in {filename_a}"
    # same-doc handling: operate on one line list
    lb = la if db_["$id"] == da["$id"] else db_["content"].split("\n")
    ib = _find_bullet(lb, bullet_b)
    if ib == -1 or (db_["$id"] == da["$id"] and ib == ia):
        # if identical match, try to find b after a
        if db_["$id"] == da["$id"] and ib == ia:
            ib = -1
            for i, line in enumerate(lb):
                if i != ia and _is_bullet(line) and bullet_b.strip() in line:
                    ib = i
                    break
        if ib == -1:
            return f"no bullet matching '{bullet_b}' in {filename_b}"
    id_a, id_b = _link_id(la[ia]), _link_id(lb[ib])
    if id_a and id_b and id_a == id_b:
        return f"already linked (group {id_a})"
    if id_a and id_b:
        # merge group b into group a across the whole brain
        for d in docs:
            if f"|ln:{id_b}" in d["content"]:
                db_update(d["$id"], d["content"].replace(f"|ln:{id_b}", f"|ln:{id_a}"))
        return f"merged link groups {id_b} -> {id_a}"
    lid = id_a or id_b or _new_link_id(docs)
    la[ia] = _with_link_id(la[ia], lid)
    lb[ib] = _with_link_id(lb[ib], lid)
    db_update(da["$id"], "\n".join(la))
    if db_["$id"] != da["$id"]:
        db_update(db_["$id"], "\n".join(lb))
    emit_activity(filename_a, "write")
    return f"linked as siblings (group {lid}): [{filename_a}] {bullet_a.strip()[:50]} <-> [{filename_b}] {bullet_b.strip()[:50]}"


@mcp.tool()
def unlink_memory(filename: str, bullet_text: str) -> str:
    """Remove a memory from its sibling link group. Other group members keep the link."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = aw_find(filename)
    if not doc:
        return f"'{filename}' not found"
    lines = doc["content"].split("\n")
    idx = _find_bullet(lines, bullet_text)
    if idx == -1:
        return f"no bullet matching '{bullet_text}' in {filename}"
    lid = _link_id(lines[idx])
    if not lid:
        return "that memory is not linked to anything"
    lines[idx] = _LINK_RE.sub("", lines[idx])
    db_update(doc["$id"], "\n".join(lines))
    emit_activity(filename, "write")
    return f"unlinked from group {lid}"


@mcp.tool()
def get_linked(filename: str, bullet_text: str) -> str:
    """Get all sibling memories linked to a given memory (its |ln: group),
    across every cluster. Returns each with its cluster and section."""
    if not filename.endswith(".md"):
        filename += ".md"
    docs = aw_list()
    doc = next((d for d in docs if d["filename"] == filename), None)
    if not doc:
        return f"'{filename}' not found"
    lines = doc["content"].split("\n")
    idx = _find_bullet(lines, bullet_text)
    if idx == -1:
        return f"no bullet matching '{bullet_text}' in {filename}"
    lid = _link_id(lines[idx])
    if not lid:
        return "that memory is not linked to anything"
    results = []
    for d in docs:
        section = "(top)"
        for line in d["content"].split("\n"):
            stripped = line.strip()
            if stripped.startswith("##"):
                section = stripped.lstrip("#").strip()
            elif _is_bullet(line) and f"|ln:{lid}" in line:
                results.append(f"[{d['filename']} > {section}] {stripped}")
    return f"sibling group {lid} ({len(results)} memories):\n" + "\n".join(results)


@mcp.tool()
def delete_section(filename: str, section: str) -> str:
    """Delete an entire ## section and all its bullets from a cluster.
    Use for removing outdated sections. Call get_cluster first to confirm the section name."""
    if not filename.endswith(".md"):
        filename += ".md"
    doc = aw_find(filename)
    if not doc:
        return _not_found(filename)
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
    events = db_get_activity()
    if not events:
        return "no activity recorded yet"
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
    link_groups = {}
    for b in all_bullets:
        m = _LINK_RE.search(b["full"])
        if m:
            link_groups.setdefault(m.group(1), []).append(b)
    for lid, members in link_groups.items():
        if len(members) < 2:
            issues.append(f"ORPHAN LINK (group {lid} has 1 member): {members[0]['file']} > {members[0]['text'][:60]}")
    seen = []
    for i, a in enumerate(all_bullets):
        la = len(a["text"])
        for b in all_bullets[i+1:]:
            if a["file"] == b["file"] and a["section"] == b["section"]:
                continue
            # length gates keep this O(n^2) pass fast: long essay-bullets and
            # very different lengths can't be near-duplicates worth flagging
            lb = len(b["text"])
            if la > 400 or lb > 400 or min(la, lb) < 0.7 * max(la, lb):
                continue
            sm = SequenceMatcher(None, a["text"].lower(), b["text"].lower())
            if sm.quick_ratio() <= 0.8:
                continue
            ratio = sm.ratio()
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
