import sys
import os
import json
import hmac
import inspect
import anyio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import mcp_server
from mcp_server import mcp
from db import hash_key, user_by_key_hash, user_owner

# Direct, stateless JSON-RPC MCP endpoint.
#
# The MCP SDK's streamable-HTTP transport (SSE responses + a lifespan-created
# session-manager task group) does not survive Vercel's serverless runtime:
# SSE streams get buffered/killed (4-min client hangs) and the per-request user
# context did not propagate through the shared task group (tools failed closed).
# A tools-only server does not need any of that machinery — each POST is a
# self-contained JSON-RPC call with a single JSON reply. We dispatch it directly
# so the user context is set immediately before the tool runs, in one path we
# fully control and can test end to end.

CORTEX_MCP_KEY = os.getenv("CORTEX_MCP_KEY", "")
PROTOCOL_VERSION = "2024-11-05"

_owner_cache = {}

# raw tool functions by name (sync) + their accepted parameter names
_TOOL_FNS = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
_TOOL_PARAMS = {name: set(inspect.signature(fn).parameters) for name, fn in _TOOL_FNS.items()}
_TOOLS_SCHEMA = None  # built lazily (async) and cached


def _resolve_user(key):
    """Map an API key to (user_id, role); None if invalid."""
    if CORTEX_MCP_KEY and hmac.compare_digest(key.encode(), CORTEX_MCP_KEY.encode()):
        if "u" not in _owner_cache:
            _owner_cache["u"] = user_owner()
        o = _owner_cache["u"]
        return (o["id"], "owner") if o else None
    u = user_by_key_hash(hash_key(key))
    return (u["id"], u["role"]) if u else None


async def _tools_schema():
    global _TOOLS_SCHEMA
    if _TOOLS_SCHEMA is None:
        tools = await mcp.list_tools()
        _TOOLS_SCHEMA = [t.model_dump(by_alias=True, exclude_none=True) for t in tools]
    return _TOOLS_SCHEMA


def _run_tool(name, arguments, user):
    """Runs in a worker thread. Sets the tenant context here so it is bound in the
    same thread that executes the (blocking) tool + DB calls."""
    fn = _TOOL_FNS.get(name)
    if fn is None:
        raise ValueError(f"unknown tool '{name}'")
    args = {k: v for k, v in (arguments or {}).items() if k in _TOOL_PARAMS[name]}
    tokens = mcp_server.set_request_user(user[0], user[1])
    try:
        return fn(**args)
    finally:
        mcp_server.reset_request_user(tokens)


async def _dispatch(msg, user):
    """Handle one JSON-RPC message. Returns a response dict, or None for a
    notification (no reply)."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    method = msg.get("method")
    mid = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        params = msg.get("params") or {}
        result = {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cortex", "version": "2.0.0"},
            "instructions": mcp.instructions or "",
        }
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method and method.startswith("notifications/"):
        return None
    if is_notification:
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": await _tools_schema()}}

    if method in ("prompts/list", "resources/list", "resources/templates/list"):
        key = {"prompts/list": "prompts", "resources/list": "resources",
               "resources/templates/list": "resourceTemplates"}[method]
        return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            with anyio.fail_after(25):
                text = await anyio.to_thread.run_sync(_run_tool, name, arguments, user)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": str(text)}], "isError": False}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"tool error: {e}"}], "isError": True}}

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


async def _read_body(receive):
    chunks = []
    while True:
        m = await receive()
        if m["type"] == "http.request":
            chunks.append(m.get("body", b""))
            if not m.get("more_body"):
                break
        elif m["type"] == "http.disconnect":
            break
    return b"".join(chunks)


async def _send(send, status, body, ctype=b"application/json"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        [b"content-type", ctype],
        [b"content-length", str(len(data)).encode()],
    ]})
    await send({"type": "http.response.body", "body": data})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif m["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if scope["type"] != "http":
        return

    method = scope.get("method", "GET")
    if method == "GET":
        # optional server->client SSE stream; a tools-only server sends none
        return await _send(send, 405, {"jsonrpc": "2.0", "id": None,
                                       "error": {"code": -32000, "message": "method not allowed"}})
    if method != "POST":
        return await _send(send, 405, {"error": "method not allowed"})

    headers = dict(scope.get("headers", []))
    auth = headers.get(b"authorization", b"").decode()
    api_key = headers.get(b"x-api-key", b"").decode()
    key = auth[7:] if auth.startswith("Bearer ") else (auth or api_key)

    user = None
    if key:
        try:
            # generous: a cold Atlas connection is ~8-9s on the first call
            with anyio.fail_after(22):
                user = await anyio.to_thread.run_sync(_resolve_user, key)
        except Exception:
            user = None
    if not user:
        return await _send(send, 401, {"jsonrpc": "2.0", "id": None,
                                       "error": {"code": -32000, "message": "unauthorized"}})

    raw = await _read_body(receive)
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return await _send(send, 400, {"jsonrpc": "2.0", "id": None,
                                       "error": {"code": -32700, "message": "parse error"}})

    if isinstance(payload, list):
        out = []
        for m in payload:
            r = await _dispatch(m, user)
            if r is not None:
                out.append(r)
        if not out:
            return await _send(send, 202, b"")
        return await _send(send, 200, out)

    resp = await _dispatch(payload, user)
    if resp is None:
        return await _send(send, 202, b"")
    return await _send(send, 200, resp)
