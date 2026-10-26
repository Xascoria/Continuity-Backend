"""ASGI middleware that logs every /api/* request.

* /api/admin/*  -> `admin_requests`  (user tokens/uuids are redacted)
* everything else under /api -> `users_requests` (tokens are kept, and also
  copied into the `token` column)

Credentials (Authorization / Cookie headers) are never stored.
"""
import json
import logging
import re
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import security
from .database import SessionLocal
from .models import AdminRequest, UsersRequest

logger = logging.getLogger("request_log")

ADMIN_PREFIX = "/api/admin"
MAX_BODY_BYTES = 8192
MAX_TOKEN_LENGTH = 200
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Headers that carry credentials; never stored.
DROPPED_HEADERS = {"authorization", "cookie", "proxy-authorization"}


def _is_admin_path(path: str) -> bool:
    return path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")


def _redact(text):
    """Remove user tokens (uuids) from text stored in admin_requests."""
    return UUID_RE.sub("[uuid]", text) if text else text


class _Capture:
    """Collects up to MAX_BODY_BYTES of a streamed body."""

    def __init__(self):
        self.chunks = []
        self.size = 0
        self.truncated = False

    def add(self, data: bytes):
        room = MAX_BODY_BYTES - self.size
        if len(data) > room:
            self.truncated = True
            data = data[:max(room, 0)]
        if data:
            self.chunks.append(data)
            self.size += len(data)

    def text(self):
        text = b"".join(self.chunks).decode("utf-8", errors="replace")
        return text + ("...[truncated]" if self.truncated else "")


def _token_from(body_text):
    """The "token" field of a JSON object body, or None."""
    if not body_text:
        return None
    try:
        data = json.loads(body_text)
    except ValueError:
        return None
    token = data.get("token") if isinstance(data, dict) else None
    if token is None:
        return None
    return str(token)[:MAX_TOKEN_LENGTH]


def _write_log(scope, request_body, status, response_body, duration_ms):
    path = scope.get("path", "")
    is_admin = _is_admin_path(path)
    clean = _redact if is_admin else (lambda text: text)

    headers = {}
    for name, value in scope.get("headers", []):
        name = name.decode("latin-1").lower()
        if name not in DROPPED_HEADERS:
            headers[name] = clean(value.decode("latin-1"))
    client = scope.get("client")

    fields = dict(
        method=scope.get("method", ""),
        path=path,
        query_string=clean(scope.get("query_string", b"").decode("latin-1")) or None,
        client_ip=client[0] if client else None,
        headers=json.dumps(headers),
        request_body=clean(request_body) or None,
        response_status=status,
        response_body=clean(response_body) or None,
        duration_ms=round(duration_ms, 2),
    )
    if is_admin:
        row = AdminRequest(**fields)
    else:
        # The token the client sent; for register (no token yet) the one issued.
        token = _token_from(request_body) or (
            _token_from(response_body) if path.endswith("/register") else None
        )
        row = UsersRequest(token=token, user_id=scope.get("user_id"), **fields)

    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
    except Exception:
        logger.exception("Failed to write request log")
        db.rollback()
    finally:
        db.close()


async def _reject_banned_ip(scope, receive, send):
    """403 for a banned IP. Not logged: a banned client could flood the tables."""
    await JSONResponse(
        {"success": False, "error": "IP_BANNED"}, status_code=403
    )(scope, receive, send)


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api"):
            await self.app(scope, receive, send)
            return

        if not _is_admin_path(scope["path"]):
            client = scope.get("client")
            ip = client[0] if client else "?"
            try:
                if security.ip_bans.stale():
                    await run_in_threadpool(security.ip_bans.refresh)
                if security.ip_bans.is_banned(ip):
                    await _reject_banned_ip(scope, receive, send)
                    return
            except Exception:
                logger.exception("IP ban check failed")
            try:
                events = security.monitor.check_request(ip, scope["path"])
                if events:
                    await run_in_threadpool(security.log_events, events, ip)
            except Exception:
                logger.exception("Traffic check failed")

        started = time.perf_counter()
        req, resp = _Capture(), _Capture()
        status = None

        async def logging_receive():
            message = await receive()
            if message["type"] == "http.request":
                req.add(message.get("body", b""))
            return message

        async def logging_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                resp.add(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, logging_receive, logging_send)
        except Exception:
            status = status or 500
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            await run_in_threadpool(
                _write_log, scope, req.text(), status, resp.text(), duration_ms
            )
