"""Suspicious-activity detection and the `suspicious_events` log.

Detection only *logs* (nothing here blocks a request). Rate counters live in
memory (per process) and are bounded; the events themselves are persisted and
shown on the admin dashboard.

Event types
-----------
RATE_LIMIT_EXCEEDED     one IP sent too many /api requests in a short window
TRAFFIC_SPIKE           total /api traffic is abnormally high (generic DDoS)
REGISTER_FLOOD          repeated register attempts from one IP (success or fail)
INVALID_TOKEN_PROBING   one IP keeps sending unknown tokens
ADMIN_LOGIN_BRUTEFORCE  repeated failed admin logins from one IP
FAST_SUBMISSIONS        a user submitted twice (any game type) within 20 seconds
IMPOSSIBLE_TIME_TAKEN   reported time_taken is implausibly low
AUTO_BAN                user banned for exceeding the active-hold limit
BANNED_USER_ACCESS      a banned token kept calling the API
SUBMIT_WITHOUT_HOLD     submission for a tile the user does not hold
SUBMISSION_ID_REUSED    a submission_id was reused for a different tile
MALFORMED_CANVAS        canvas that a real client would never send
"""
import json
import logging
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from .database import SessionLocal
from .models import BannedIp, SuspiciousEvent

logger = logging.getLogger("security")

INFO, WARNING, CRITICAL = "info", "warning", "critical"

# (limit, window seconds). "Exceeded" means more than `limit` hits in the window.
IP_RATE = (120, 10)          # requests per IP
GLOBAL_RATE = (1500, 10)     # requests across all IPs
REGISTER_RATE = (5, 600)     # register attempts per IP
INVALID_TOKEN_RATE = (20, 60)
ADMIN_LOGIN_RATE = (5, 60)

FAST_SUBMIT_SECONDS = 20
MIN_PLAUSIBLE_TIME_TAKEN = 1.0  # seconds

# Do not log the same condition for the same subject more often than this.
COOLDOWN_SECONDS = 60
REGISTER_PATH = "/api/user/register"

Event = Tuple[str, str, dict]  # (type, severity, details)


class Monitor:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: Dict[tuple, deque] = {}
        self._last_logged: Dict[tuple, float] = {}

    def _burst(self, key: tuple, rule: Tuple[int, int], now: float) -> bool:
        """Record a hit; True if the rule's limit is now exceeded."""
        limit, window = rule
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) > 20000:
                self._prune(now)
            q = self._hits[key] = deque(maxlen=limit + 1)
        q.append(now)
        return len(q) > limit and now - q[0] <= window

    def _prune(self, now: float) -> None:
        longest = max(w for _, w in (IP_RATE, GLOBAL_RATE, REGISTER_RATE,
                                     INVALID_TOKEN_RATE, ADMIN_LOGIN_RATE))
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > longest]:
            del self._hits[k]
        for k in [k for k, t in self._last_logged.items() if now - t > 3600]:
            del self._last_logged[k]

    def cooldown_ok(self, key: tuple, seconds: float, now: Optional[float] = None) -> bool:
        """True (and arms the cooldown) if `key` has not been logged recently."""
        now = self._clock() if now is None else now
        with self._lock:
            last = self._last_logged.get(key)
            if last is not None and now - last < seconds:
                return False
            self._last_logged[key] = now
            return True

    # ---- detectors ----------------------------------------------------------

    def check_request(self, ip: str, path: str, now: Optional[float] = None) -> List[Event]:
        """Called for every non-admin /api request."""
        now = self._clock() if now is None else now
        events: List[Event] = []
        with self._lock:
            ip_hot = self._burst(("ip", ip), IP_RATE, now)
            global_hot = self._burst(("global",), GLOBAL_RATE, now)
            register_hot = path == REGISTER_PATH and self._burst(
                ("register", ip), REGISTER_RATE, now)
        if ip_hot and self.cooldown_ok(("RATE_LIMIT_EXCEEDED", ip), COOLDOWN_SECONDS, now):
            events.append(("RATE_LIMIT_EXCEEDED", WARNING,
                           {"limit": IP_RATE[0], "window_seconds": IP_RATE[1], "path": path}))
        if global_hot and self.cooldown_ok(("TRAFFIC_SPIKE",), COOLDOWN_SECONDS, now):
            events.append(("TRAFFIC_SPIKE", CRITICAL,
                           {"limit": GLOBAL_RATE[0], "window_seconds": GLOBAL_RATE[1],
                            "latest_ip": ip}))
        if register_hot and self.cooldown_ok(("REGISTER_FLOOD", ip), REGISTER_RATE[1] / 2, now):
            events.append(("REGISTER_FLOOD", WARNING,
                           {"limit": REGISTER_RATE[0], "window_seconds": REGISTER_RATE[1]}))
        return events

    def check_invalid_token(self, ip: str, now: Optional[float] = None) -> List[Event]:
        now = self._clock() if now is None else now
        with self._lock:
            hot = self._burst(("invalid_token", ip), INVALID_TOKEN_RATE, now)
        if hot and self.cooldown_ok(("INVALID_TOKEN_PROBING", ip), COOLDOWN_SECONDS, now):
            return [("INVALID_TOKEN_PROBING", WARNING,
                     {"limit": INVALID_TOKEN_RATE[0], "window_seconds": INVALID_TOKEN_RATE[1]})]
        return []

    def check_admin_login_failure(self, ip: str, now: Optional[float] = None) -> List[Event]:
        now = self._clock() if now is None else now
        with self._lock:
            hot = self._burst(("admin_login", ip), ADMIN_LOGIN_RATE, now)
        if hot and self.cooldown_ok(("ADMIN_LOGIN_BRUTEFORCE", ip), COOLDOWN_SECONDS, now):
            return [("ADMIN_LOGIN_BRUTEFORCE", CRITICAL,
                     {"limit": ADMIN_LOGIN_RATE[0], "window_seconds": ADMIN_LOGIN_RATE[1]})]
        return []


monitor = Monitor()


# ---- persistence --------------------------------------------------------------


def log_event(
    event_type: str,
    severity: str = WARNING,
    client_ip: Optional[str] = None,
    user_id: Optional[int] = None,
    details: Optional[dict] = None,
    cooldown_key: Optional[tuple] = None,
    cooldown: float = COOLDOWN_SECONDS,
) -> None:
    """Persist one event. Never raises: logging must not break a request."""
    try:
        if cooldown_key is not None and not monitor.cooldown_ok(
            (event_type,) + cooldown_key, cooldown
        ):
            return
        db = SessionLocal()
        try:
            db.add(SuspiciousEvent(
                event_type=event_type, severity=severity, client_ip=client_ip,
                user_id=user_id, details=json.dumps(details or {}),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to write suspicious event")


def log_events(events: List[Event], client_ip: Optional[str] = None,
               user_id: Optional[int] = None) -> None:
    for event_type, severity, details in events:
        log_event(event_type, severity, client_ip, user_id, details)


# Game errors that are worth recording: code -> (event, severity, cooldown seconds)
ERROR_EVENTS = {
    "HOLD_NOT_FOUND": ("SUBMIT_WITHOUT_HOLD", INFO, 30),
    "SUBMISSION_ID_REUSED": ("SUBMISSION_ID_REUSED", WARNING, 30),
    "CANVAS_INVALID": ("MALFORMED_CANVAS", WARNING, 60),
    "USER_BANNED": ("BANNED_USER_ACCESS", INFO, 300),
}


def report_game_error(code: str, client_ip: Optional[str], user_id: Optional[int],
                      path: str) -> None:
    """Record a rejected user request, if the error code is suspicious."""
    if code == "INVALID_TOKEN":
        log_events(monitor.check_invalid_token(client_ip or "?"), client_ip, user_id)
        return
    rule = ERROR_EVENTS.get(code)
    if rule is None:
        return
    event_type, severity, cooldown = rule
    log_event(event_type, severity, client_ip, user_id, {"error": code, "path": path},
              cooldown_key=(user_id, client_ip), cooldown=cooldown)


# ---- banned IPs ---------------------------------------------------------------


class IpBanCache:
    """Set of banned IPs, reloaded from the database every few seconds so that
    bans made through any worker take effect everywhere."""

    TTL = 5.0

    def __init__(self):
        self._ips = frozenset()
        self._loaded_at = None

    def stale(self) -> bool:
        return self._loaded_at is None or time.monotonic() - self._loaded_at > self.TTL

    def refresh(self) -> None:
        db = SessionLocal()
        try:
            self._ips = frozenset(ip for (ip,) in db.query(BannedIp.ip))
        finally:
            db.close()
        self._loaded_at = time.monotonic()

    def invalidate(self) -> None:
        self._loaded_at = None

    def is_banned(self, ip: str) -> bool:
        return ip in self._ips


ip_bans = IpBanCache()
