# End-to-end test of the direct JSON-RPC MCP endpoint against the real ASGI app
# + real DB, simulating Vercel (VERCEL=1, legacy key -> owner). No SSE, no session
# manager — so this fully reproduces the deployed behavior.

import os, sys, json
os.environ["VERCEL"] = "1"
os.environ["CORTEX_MCP_KEY"] = "probe-key"
sys.stdout.reconfigure(encoding="utf-8")
import importlib, db, mcp_server
importlib.reload(db); importlib.reload(mcp_server)
import api.mcp_route as R; importlib.reload(R)
import anyio

passed, failed = [], []
def check(label, cond, detail=""):
    (passed if cond else failed).append(label)
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {detail}"))

async def call(payload, key="probe-key", method="POST"):
    body = json.dumps(payload).encode()
    hdrs = [(b"content-type", b"application/json")]
    if key: hdrs.append((b"authorization", ("Bearer " + key).encode()))
    scope = {"type": "http", "method": method, "path": "/mcp", "headers": hdrs, "query_string": b""}
    sent = []; sent_body = [False]
    async def receive():
        if not sent_body[0]:
            sent_body[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    async def send(m): sent.append(m)
    with anyio.move_on_after(30):
        await R.app(scope, receive, send)
    status = next((m.get("status") for m in sent if m["type"] == "http.response.start"), None)
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try: parsed = json.loads(out) if out else None
    except Exception: parsed = out
    return status, parsed

async def main():
    # unauthorized
    s, _ = await call({"jsonrpc":"2.0","id":1,"method":"tools/list"}, key=None)
    check("no key -> 401", s == 401, s)
    s, _ = await call({"jsonrpc":"2.0","id":1,"method":"tools/list"}, key="wrong-key")
    check("bad key -> 401", s == 401, s)

    # initialize
    s, r = await call({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}})
    check("initialize 200", s == 200 and r["result"]["serverInfo"]["name"] == "cortex", r)
    check("initialize has instructions", len(r["result"]["instructions"]) > 100, "")
    check("initialize echoes protocol", r["result"]["protocolVersion"] == "2024-11-05", r["result"].get("protocolVersion"))

    # initialized notification -> 202 no body
    s, r = await call({"jsonrpc":"2.0","method":"notifications/initialized"})
    check("notification -> 202", s == 202, s)

    # tools/list
    s, r = await call({"jsonrpc":"2.0","id":2,"method":"tools/list"})
    tools = r["result"]["tools"]
    names = {t["name"] for t in tools}
    check("tools/list returns 22", len(tools) == 22, len(tools))
    check("tools have inputSchema", all("inputSchema" in t for t in tools), "")
    check("key tools present", {"get_briefing","get_outline","save_to_brain","get_brain_summary"} <= names, names)

    # tools/call get_brain_summary
    s, r = await call({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_brain_summary","arguments":{}}})
    txt = r["result"]["content"][0]["text"]
    check("get_brain_summary works", s == 200 and not r["result"]["isError"] and "identity" in txt.lower(), txt[:120])

    # tools/call get_outline
    s, r = await call({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_outline","arguments":{"filename":"projects/moxver.md"}}})
    txt = r["result"]["content"][0]["text"]
    check("get_outline works", not r["result"]["isError"] and "##" in txt, txt[:120])

    # tools/call get_briefing (heavy path)
    s, r = await call({"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"get_briefing","arguments":{"topic":"congressional app challenge","max_tokens":800}}})
    txt = r["result"]["content"][0]["text"]
    check("get_briefing works + bounded", not r["result"]["isError"] and len(txt) < 8000, f"len={len(txt)}")

    # unknown tool -> isError, not a crash
    s, r = await call({"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"nope","arguments":{}}})
    check("unknown tool -> isError", s == 200 and r["result"]["isError"], r)

    # GET -> 405 (no SSE stream)
    s, r = await call({}, method="GET")
    check("GET -> 405", s == 405, s)

    # write path: save then confirm it landed under owner, then clean up
    s, r = await call({"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"save_to_brain","arguments":{"filename":"cortex.md","bullet":"ROUTE TEST probe bullet","importance":1,"section":"build log","source":"claude_code"}}})
    check("save_to_brain works", not r["result"]["isError"] and "saved" in r["result"]["content"][0]["text"].lower(), r["result"]["content"][0]["text"][:120])
    s, r = await call({"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"delete_bullet","arguments":{"filename":"cortex.md","old_text":"ROUTE TEST probe bullet"}}})
    check("delete_bullet cleanup", not r["result"]["isError"] and "deleted" in r["result"]["content"][0]["text"].lower(), r["result"]["content"][0]["text"][:120])

    print(f"\n{len(passed)} passed, {len(failed)} failed")

anyio.run(main)
sys.exit(1 if failed else 0)
