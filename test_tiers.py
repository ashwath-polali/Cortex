# Offline test for the tier system: sub-clusters, deep nesting, sibling links,
# move/rename tools. Monkeypatches the db layer with an in-memory store so the
# real brain is never touched. Run: .venv/Scripts/python.exe test_tiers.py

import re
import mcp_server as m

# ---- in-memory fake db ------------------------------------------------------
_store = {}
_next_id = [1]


def _fake_list(include_internal=False):
    docs = [{"$id": k, "filename": v["filename"], "content": v["content"]} for k, v in _store.items()]
    if not include_internal:
        docs = [d for d in docs if not d["filename"].startswith("__")]
    return docs


def _fake_find(filename):
    for k, v in _store.items():
        if v["filename"] == filename:
            return {"$id": k, "filename": v["filename"], "content": v["content"]}
    return None


def _fake_create(filename, content):
    doc_id = str(_next_id[0])
    _next_id[0] += 1
    _store[doc_id] = {"filename": filename, "content": content}
    return {"$id": doc_id, "filename": filename, "content": content}


def _fake_update(doc_id, content):
    _store[doc_id]["content"] = content


def _fake_delete(doc_id):
    del _store[doc_id]


def _fake_rename(doc_id, filename):
    _store[doc_id]["filename"] = filename


_cas_fail = [0]  # set >0 to make the next N CAS writes fail (conflict simulation)


def _fake_update_cas(doc_id, old_content, new_content):
    if _cas_fail[0] > 0:
        _cas_fail[0] -= 1
        return False
    if _store[doc_id]["content"] != old_content:
        return False
    _store[doc_id]["content"] = new_content
    return True


def _fake_names():
    return sorted(v["filename"] for v in _store.values() if not v["filename"].startswith("__"))


m.db_list = _fake_list
m.db_find = _fake_find
m.db_names = _fake_names
m.db_create = _fake_create
m.db_update = _fake_update
m.db_update_cas = _fake_update_cas
m.db_delete = _fake_delete
m.db_rename = _fake_rename
m.db_log_activity = lambda e: None
m.db_get_activity = lambda: []

# FastMCP may wrap tools; resolve to the underlying callable
def tool(name):
    fn = getattr(m, name)
    return getattr(fn, "fn", fn)


passed = []
failed = []


def check(label, cond, detail=""):
    (passed if cond else failed).append(label)
    if not cond:
        print(f"FAIL: {label}\n      {detail}")


# ---- seed -------------------------------------------------------------------
_fake_create("projects.md", "<!-- scope:build -->\n## overview\n- top level projects doc {{2026-01-01|i:3|s:manual}}\n\n## build log\n- old entry one {{2026-01-02|i:2|s:claude_code}}\n")
_fake_create("core.md", "## identity\n- name: Testuser {{2026-01-01|i:5|s:manual}}\n")

# ---- sub-cluster creation + scope inheritance -------------------------------
r = tool("create_cluster")("projects/wisegraph", "## status\n- wisegraph is deployed", source="claude_code")
check("create sub-cluster", "created projects/wisegraph.md" in r, r)

r = tool("create_cluster")("bad/name/deep", "## x\n- y", source="claude_code")
check("reject 2-deep filename", "invalid filename" in r, r)

r = tool("list_clusters")()
check("list shows sub indented", "\n  projects/wisegraph.md" in r, r)

r = tool("list_clusters")(scope="build")
check("sub inherits build scope", "projects/wisegraph.md [build]" in r, r)

r = tool("get_cluster")("projects")
check("parent shows sub-cluster footer", "read separately: projects/wisegraph.md]" in r, r)

# ---- deep nesting -----------------------------------------------------------
t_save = tool("save_to_brain")
r = t_save("projects/wisegraph", "parent memory alpha", importance=3, section="status", source="claude_code")
check("save to sub-cluster section", "saved to projects/wisegraph.md > status" in r, r)

r = t_save("projects/wisegraph", "child memory beta", importance=2, parent="parent memory alpha", source="claude_code")
check("save child", "saved under" in r, r)

r = t_save("projects/wisegraph", "grandchild gamma", importance=1, parent="child memory beta", source="claude_code")
check("save grandchild", "saved under" in r, r)

r = t_save("projects/wisegraph", "second child delta", importance=2, parent="parent memory alpha", source="claude_code")
content = _fake_find("projects/wisegraph.md")["content"]
lines = content.split("\n")
ia = next(i for i, l in enumerate(lines) if "parent memory alpha" in l)
check("child indent 2sp", lines[ia + 1].startswith("  - child memory beta"), repr(lines[ia + 1]))
check("grandchild indent 4sp", lines[ia + 2].startswith("    - grandchild gamma"), repr(lines[ia + 2]))
check("second child after subtree", lines[ia + 3].startswith("  - second child delta"), repr(lines[ia + 3]))

# ---- edit preserves metadata --------------------------------------------------
t_save("projects.md", "temp fact with extras", importance=4, section="overview", expiry="2027-01-01", rec="pending", source="claude")
r = tool("edit_bullet")("projects.md", "temp fact with extras", "temp fact edited", source="claude_code")
line = next(l for l in _fake_find("projects.md")["content"].split("\n") if "temp fact edited" in l)
check("edit keeps importance", "|i:4" in line, line)
check("edit keeps expiry", "|x:2027-01-01" in line, line)
check("edit keeps rec", "|rec:pending" in line, line)

# ---- sibling links ------------------------------------------------------------
t_link = tool("link_memories")
r = t_link("projects/wisegraph", "wisegraph is deployed", "projects.md", "top level projects doc")
check("link two memories", "linked as siblings" in r, r)
lid = re.search(r"group ([a-z0-9]+)", r).group(1)

r = t_link("projects/wisegraph", "wisegraph is deployed", "core.md", "name: Testuser")
check("extend group to third", f"group {lid}" in r, r)

r = tool("get_linked")("core.md", "name: Testuser")
check("get_linked finds all 3", r.count("|ln:" + lid) == 3 and "(3 memories)" in r, r)

r = tool("unlink_memory")("core.md", "name: Testuser")
check("unlink", f"unlinked from group {lid}" in r, r)
check("unlink removed token", "|ln:" not in _fake_find("core.md")["content"], _fake_find("core.md")["content"])

r = tool("get_linked")("projects.md", "top level projects doc")
check("group survives unlink", "(2 memories)" in r, r)

# edit preserves link token
r = tool("edit_bullet")("projects.md", "top level projects doc", "top level projects doc updated", source="claude_code")
line = next(l for l in _fake_find("projects.md")["content"].split("\n") if "top level projects doc updated" in l)
check("edit keeps link", f"|ln:{lid}" in line, line)

# ---- move_bullet ---------------------------------------------------------------
r = tool("move_bullet")("projects/wisegraph", "parent memory alpha", "projects.md", dest_section="build log", source="claude_code")
check("move reports children", "+3 child memories" in r, r)
src = _fake_find("projects/wisegraph.md")["content"]
check("moved out of src", "parent memory alpha" not in src, src)
dst = _fake_find("projects.md")["content"].split("\n")
ip = next(i for i, l in enumerate(dst) if "parent memory alpha" in l)
sec = max(i for i in range(ip) if dst[i].startswith("## "))
check("moved under build log", dst[sec] == "## build log", dst[sec])
check("move keeps stamp verbatim", "{{" in dst[ip] and "|i:3|s:claude_code" in dst[ip], dst[ip])
check("children re-indented", dst[ip + 1].startswith("  - child memory beta") and dst[ip + 2].startswith("    - grandchild gamma"), repr(dst[ip + 1 : ip + 3]))

# move back under a parent
r = tool("move_bullet")("projects.md", "second child delta", "projects/wisegraph.md", dest_parent="wisegraph is deployed", source="claude_code")
wl = _fake_find("projects/wisegraph.md")["content"].split("\n")
iw = next(i for i, l in enumerate(wl) if "wisegraph is deployed" in l)
check("move under dest_parent nests", wl[iw + 1].startswith("  - second child delta"), repr(wl[iw + 1]))

# ---- delete subtree (delta was moved out above, so 2 children remain) -----------
r = tool("delete_bullet")("projects.md", "parent memory alpha")
check("delete reports children", "+2 child memories" in r, r)
check("subtree gone", "grandchild gamma" not in _fake_find("projects.md")["content"], "")

# ---- renames --------------------------------------------------------------------
r = tool("rename_section")("projects.md", "overview", "summary")
check("rename section", "## summary" in _fake_find("projects.md")["content"], r)

r = tool("rename_cluster")("projects/wisegraph.md", "projects/wise_graph.md")
check("rename cluster", _fake_find("projects/wise_graph.md") is not None, r)

# ---- lint orphan link -------------------------------------------------------------
# unlink one of the two remaining group members, leaving the other orphaned
tool("unlink_memory")("projects/wise_graph.md", "wisegraph is deployed")
r = tool("lint_brain")()
check("lint flags orphan link", "ORPHAN LINK" in r, r)

# ---- stable ids ---------------------------------------------------------------------
r = tool("save_to_brain")("core.md", "id test fact", importance=2, section="identity", source="claude_code")
line = next(l for l in _fake_find("core.md")["content"].split("\n") if "id test fact" in l)
check("save stamps id", re.search(r"\|id:[a-f0-9]{6}", line), line)
bid = re.search(r"\|id:([a-f0-9]{6})", line).group(1)

r = tool("edit_bullet")("core.md", "id:" + bid, "id test fact edited", source="claude_code")
line = next(l for l in _fake_find("core.md")["content"].split("\n") if "id test fact edited" in l)
check("edit by id ref", f"|id:{bid}" in line, r)

r = tool("set_importance")("core.md", "id:" + bid, 4)
line = next(l for l in _fake_find("core.md")["content"].split("\n") if "id test fact edited" in l)
check("set_importance by id", "|i:4" in line, line)

# ---- CAS retry ------------------------------------------------------------------------
_cas_fail[0] = 1
r = tool("save_to_brain")("core.md", "cas retry fact", importance=1, section="identity", source="claude_code")
check("save survives one CAS conflict", "saved to" in r, r)

# ---- length nudge -----------------------------------------------------------------------
r = tool("save_to_brain")("core.md", "x" * 700, importance=1, section="identity", source="claude_code")
check("length nudge fires", "long bullets bloat" in r, r)

# ---- outline ---------------------------------------------------------------------------
r = tool("get_outline")("projects.md")
check("outline has section", "## summary" in r, r)
check("outline has compact meta with id", re.search(r"<\d{4}-\d{2}-\d{2}\|[^>]*id:", r), r)
check("outline truncates long bullets", "…" in r or all(len(l) < 220 for l in r.split("\n")), r[:300])

# ---- briefing --------------------------------------------------------------------------
r = tool("get_briefing")("wisegraph deployment status")
check("briefing finds wisegraph", "wisegraph" in r.lower(), r[:400])
check("briefing shows location", "[projects/wise_graph.md" in r, r[:400])
check("briefing shows age+importance", re.search(r"\((today|\d+d|\d+mo|\d+y), i\d\)", r), r[:400])

r = tool("get_briefing")("the of and")
check("briefing rejects generic topic", "too generic" in r, r)

# ---- summary ----------------------------------------------------------------------
print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    raise SystemExit(1)
print("ALL OK")

