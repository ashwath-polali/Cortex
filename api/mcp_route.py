import sys
import os
import hmac
import anyio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import mcp_server
from mcp_server import mcp
from db import hash_key, user_by_key_hash, user_owner

CORTEX_MCP_KEY = os.getenv("CORTEX_MCP_KEY", "")

_sdk_app = mcp.streamable_http_app()
_handler = _sdk_app.routes[0].app

_owner_cache = {}


def _resolve_user(key):
    """Map an API key to (user_id, role); None if invalid."""
    if CORTEX_MCP_KEY and hmac.compare_digest(key.encode(), CORTEX_MCP_KEY.encode()):
        # legacy owner key from env — resolves to the owner account
        if "u" not in _owner_cache:
            _owner_cache["u"] = user_owner()
        o = _owner_cache["u"]
        return (o["id"], "owner") if o else None
    u = user_by_key_hash(hash_key(key))
    return (u["id"], u["role"]) if u else None


async def _send_json(send, status, body):
    import json
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(data)).encode()],
    ]})
    await send({"type": "http.response.body", "body": data})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        return await _sdk_app(scope, receive, send)

    if scope["type"] == "http":
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()
        api_key = headers.get(b"x-api-key", b"").decode()

        key = None
        if auth.startswith("Bearer "):
            key = auth[7:]
        elif auth:
            key = auth
        if not key:
            key = api_key

        # auth touches MongoDB (blocking pymongo) — run it off the event loop so a
        # cold/slow connection can never stall the async transport. Fail closed.
        user = None
        if key:
            try:
                with anyio.fail_after(9):
                    user = await anyio.to_thread.run_sync(_resolve_user, key)
            except (TimeoutError, Exception):
                user = None
        if not user:
            return await _send_json(send, 401, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32000, "message": "unauthorized"},
            })

        scope["path"] = "/"
        tokens = mcp_server.set_request_user(user[0], user[1])
        try:
            return await _handler(scope, receive, send)
        finally:
            mcp_server.reset_request_user(tokens)

    scope["path"] = "/"
    return await _handler(scope, receive, send)
