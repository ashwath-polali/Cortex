import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from mcp_server import mcp

CORTEX_MCP_KEY = os.getenv("CORTEX_MCP_KEY", "")

_sdk_app = mcp.streamable_http_app()
_handler = _sdk_app.routes[0].app


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
        # fail closed: no configured key means no access, not open access
        if not CORTEX_MCP_KEY:
            return await _send_json(send, 503, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32000, "message": "server not configured"},
            })
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

        import hmac
        if not key or not hmac.compare_digest(key.encode(), CORTEX_MCP_KEY.encode()):
            return await _send_json(send, 401, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32000, "message": "unauthorized"},
            })

    scope["path"] = "/"
    return await _handler(scope, receive, send)
