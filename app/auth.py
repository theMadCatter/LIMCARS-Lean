"""Password gate for mutating endpoints, with escalating per-IP lockout.

Design (user's spec): one shared password; 3 wrong tries -> 60 s freeze,
next 3 -> 5 min, next 3 -> 30 min, further -> 2 h cap. Counter resets on a
successful auth. State persists in data/auth.json so restarts do not clear
locks. The old "localhost always exempt" rule is GONE as of the secure-by-
default pass — a reverse proxy, a container, or any socket forwarding
appears to the server as 127.0.0.1 and used to bypass the admin password.
If a specific deployment really wants an auth-free local admin surface it
sets ``allow_exec_localhost_exempt`` on in config and accepts that risk
explicitly. Passwords are stored as salted scrypt hashes (pre-2026 sha256
entries still verify) and compared with hmac.compare_digest.

In addition to the password/lockout, AuthGate now owns the in-process
browser session table (random 32-byte opaque tokens → issued-at/exp).
Session tokens are **in-memory only** so a restart logs everyone out —
there is nothing on disk for an attacker to lift, and a fresh boot cannot
inherit a stale session that already escaped lockout.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# (min_failures_since_success, lock_seconds) — highest matching tier wins
TIERS = ((12, 7200.0), (9, 1800.0), (6, 300.0), (3, 60.0))


class AuthGate:
    # Session tokens expire after this many seconds of inactivity. 12 h is a
    # usable "stay logged in all day" default; server.serve() can override
    # by passing a different `session_ttl` at construction time.
    DEFAULT_SESSION_TTL = 43200

    def __init__(self, path: str, session_ttl: float | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._now = time.time
        self.salt: str | None = None
        self.hash: str | None = None
        self.ips: dict[str, dict] = {}   # ip -> {"n": failures, "until": ts}
        # token -> {"exp": absolute expiry, "ip": issuing ip}. Lives in memory
        # only; a restart invalidates every outstanding login (intentional).
        self._sessions: dict[str, dict] = {}
        self.session_ttl = float(session_ttl or self.DEFAULT_SESSION_TTL)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.salt, self.hash = d.get("salt"), d.get("hash")
            self.ips = {k: {"n": int(v.get("n", 0)), "until": float(v.get("until", 0.0))}
                        for k, v in (d.get("ips") or {}).items()}
        except (OSError, ValueError):
            pass

    # --- persistence ---

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # audit: 0664 left the scrypt hash + lockout state world-readable.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"salt": self.salt, "hash": self.hash, "ips": self.ips}, f, indent=1)
        os.replace(tmp, self.path)

    # --- password ---

    @property
    def enabled(self) -> bool:
        return bool(self.hash)

    def _digest(self, pw: str) -> str:
        return hashlib.sha256(((self.salt or "") + pw).encode("utf-8")).hexdigest()

    def _digest_scrypt(self, pw: str) -> str:
        # audit L: single-round sha256 is brute-forceable offline if
        # data/auth.json leaks; new hashes use scrypt. "s2:" prefix marks the
        # format so pre-existing sha256 entries keep verifying until reset
        return "s2:" + hashlib.scrypt(
            pw.encode("utf-8"), salt=(self.salt or "").encode("utf-8"),
            n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024).hex()

    def _matches(self, pw: str) -> bool:
        want = self.hash or ""
        got = (self._digest_scrypt(pw) if want.startswith("s2:")
               else self._digest(pw))
        return hmac.compare_digest(want, got)

    def verify(self, pw: str) -> bool:
        if not self.enabled or not pw:
            return False
        return self._matches(pw)

    def set_password(self, new: str | None, current: str | None = None) -> bool:
        """Set/change/disable. Changing an existing password requires `current`
        (callers enforce this for remote IPs; admin/localhost may overwrite).
        Empty/None `new` disables the gate. Returns False if current is wrong.

        Any password change invalidates every outstanding session cookie —
        the old password's cookies MUST stop working the moment it rotates,
        otherwise a stolen token outlives the credential it was minted from.
        """
        with self._lock:
            if self.enabled and current is not None and not self.verify(current):
                return False
            if new:
                self.salt = secrets.token_hex(16)
                self.hash = self._digest_scrypt(new)
            else:
                self.salt = self.hash = None
            self.ips = {}
            self._sessions.clear()
            self._save()
            return True

    # --- lockout ---

    def locked_for(self, ip: str) -> float:
        """Seconds of lockout remaining for ip (0.0 when free)."""
        with self._lock:
            ent = self.ips.get(ip)
            if not ent:
                return 0.0
            return max(0.0, ent["until"] - self._now())

    def failure(self, ip: str) -> float:
        """Record a wrong password; returns the lock just applied (0 if none)."""
        with self._lock:
            ent = self.ips.setdefault(ip, {"n": 0, "until": 0.0})
            ent["n"] += 1
            lock = 0.0
            for mn, secs in TIERS:
                if ent["n"] >= mn and (ent["n"] - mn) % 3 == 0:
                    lock = secs
                    break
            if lock:
                ent["until"] = self._now() + lock
            self._save()
            return lock

    def success(self, ip: str) -> None:
        with self._lock:
            if self.ips.pop(ip, None) is not None:
                self._save()

    def attempt(self, ip: str, pw: str) -> tuple[bool, float]:
        """Lockout-check + verify + ladder update in ONE lock hold.

        The server used to call locked_for() then verify() then failure() as
        three separate lock acquisitions; with ThreadingHTTPServer's
        thread-per-connection, parallel guesses read the same failure count
        and cap the ladder at level 1 forever (audit H1). This is the only
        auth path mutating endpoints may use. Returns (ok, locked_remaining).
        """
        with self._lock:
            ent = self.ips.get(ip)
            if ent:
                rem = max(0.0, ent["until"] - self._now())
                if rem > 0:
                    return False, rem
            if self.enabled and pw and self._matches(pw):
                if self.ips.pop(ip, None) is not None:
                    self._save()
                return True, 0.0
            ent = self.ips.setdefault(ip, {"n": 0, "until": 0.0})
            ent["n"] += 1
            lock = 0.0
            for mn, secs in TIERS:
                if ent["n"] >= mn and (ent["n"] - mn) % 3 == 0:
                    lock = secs
                    break
            if lock:
                ent["until"] = self._now() + lock
            self._save()
            return False, lock

    # --- sessions (browser cookie login) ---
    #
    # Sessions are the browser-friendly counterpart to the bearer token:
    # /api/auth/login takes a password (routed through attempt() so the
    # ladder applies) OR a matching auth_token, and hands back an opaque
    # 32-byte token which the server sets as an HttpOnly, SameSite=Strict
    # cookie. Every subsequent request can present either the cookie or a
    # Bearer token. The table is in-memory on purpose — a restart logs
    # everyone out and there is nothing on disk for an attacker to lift
    # (same reason the password hash file was already 0o600).

    def _sweep(self, now: float) -> None:
        """Drop expired sessions. Caller holds `_lock`."""
        dead = [t for t, e in self._sessions.items() if e["exp"] <= now]
        for t in dead:
            self._sessions.pop(t, None)

    def issue_session(self, ip: str) -> str:
        """Mint a fresh opaque session token and return it. The CALLER is
        responsible for having verified the password or bearer token first —
        this method does NO auth check so bearer-only installs can also use
        the cookie flow without a password being set."""
        tok = secrets.token_urlsafe(32)
        now = self._now()
        with self._lock:
            self._sweep(now)
            self._sessions[tok] = {"exp": now + self.session_ttl, "ip": ip}
        return tok

    def check_session(self, token: str) -> bool:
        """Validate a session cookie. Treats a missing/empty token as fail
        (hmac.compare_digest for the ONE legitimate stored value prevents a
        timing side-channel on the token lookup itself)."""
        if not token:
            return False
        now = self._now()
        with self._lock:
            self._sweep(now)
            ent = self._sessions.get(token)
            if not ent:
                # Walk every live token with a constant-time compare so a
                # brute-forcer can't learn presence from response time. The
                # table stays tiny (one entry per logged-in tab), so this is
                # O(sessions) and effectively free.
                for stored in self._sessions:
                    hmac.compare_digest(stored, token)
                return False
            # Sliding expiry: every valid hit renews the clock, so an active
            # tab never gets booted mid-session.
            ent["exp"] = now + self.session_ttl
            return True

    def revoke_session(self, token: str) -> None:
        """Called by the logout endpoint and when the password changes
        (change-password invalidates every outstanding cookie)."""
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all_sessions(self) -> None:
        with self._lock:
            self._sessions.clear()
