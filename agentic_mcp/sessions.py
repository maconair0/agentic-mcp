"""
Every controller's session, tracked: is it there, since when, how it answers.

Each request to a controller passes one place (Session.fetch), and that place
tells this. So for each controller the agentic MCP knows, without asking again:

  its state       up, degraded (it answers, with errors), down (it does not
                  answer), or not asked yet; and since when
  how it answers  requests, errors, the last error, how long answers take
  the breaker     a controller that has not answered several times in a row is
                  not asked again for a while: callers are told at once instead
                  of each waiting out a timeout, and the controller is not
                  hammered while it is down. One request is let through after
                  the pause, and its answer closes the breaker or opens it again

What it knows is kept on disk (sessions.json), so a restart of this service
starts from what was known, and every change of state is in the log.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agentic_mcp.sessions")

OPEN_AFTER = int(os.getenv("AGENTIC_MCP_BREAKER_AFTER", "4"))        # failures in a row before the pause
PAUSE_S = float(os.getenv("AGENTIC_MCP_BREAKER_PAUSE_S", "20"))      # the first pause; doubled each time, to 5 minutes
PAUSE_MAX_S = 300.0
UP, DEGRADED, DOWN, UNKNOWN = "up", "degraded", "down", "not asked yet"
# The breaker holds requests back only in the service, which lives for days and
# is asked by many. A command run once, or a test, records and never holds back.
BREAKER = False


class Tracker:
    def __init__(self, path: str = ""):
        self.path = path
        self._lock = threading.RLock()
        self._s: Dict[str, Dict[str, Any]] = {}
        self._times: Dict[str, deque] = {}
        self._saved = 0.0
        if path:
            try:
                with open(path) as fh:
                    kept = json.load(fh)
                for key, rec in (kept if isinstance(kept, dict) else {}).items():
                    if isinstance(rec, dict):
                        # A breaker left open by a process that is gone is not kept: this one finds out itself.
                        self._s[key] = {**self._new(key), **rec, "failures_in_a_row": 0, "open_until": 0.0,
                                        "pause_s": PAUSE_S, "since_start": {"requests": 0, "errors": 0}}
            except (OSError, ValueError):
                pass

    @staticmethod
    def _new(key: str) -> Dict[str, Any]:
        return {"controller": key, "state": UNKNOWN, "since": time.time(), "last_ok": None, "last_error": "",
                "last_error_at": None, "requests": 0, "errors": 0, "writes": 0, "failures_in_a_row": 0,
                "open_until": 0.0, "pause_s": PAUSE_S, "history": [], "since_start": {"requests": 0, "errors": 0}}

    def _rec(self, key: str) -> Dict[str, Any]:
        if key not in self._s:
            self._s[key] = self._new(key)
        return self._s[key]

    # --- asked before a request ---

    def allow(self, key: str) -> str:
        """"" when the controller may be asked, else why not (the breaker is open)."""
        with self._lock:
            rec = self._rec(key)
            left = rec["open_until"] - time.time()
            if left > 0 and BREAKER:
                return (f"{key} did not answer {rec['failures_in_a_row']} times in a row "
                        f"({rec['last_error'][:120]}); it is asked again in {left:.0f} s")
            return ""

    # --- told after a request ---

    def record(self, key: str, status: int, ms: float, error: str = "", write: bool = False) -> None:
        now = time.time()
        with self._lock:
            rec = self._rec(key)
            rec["requests"] += 1
            rec["since_start"]["requests"] += 1
            rec["writes"] += 1 if write else 0
            answered = status != 0
            if answered:
                self._times.setdefault(key, deque(maxlen=200)).append(ms)
            bad = not answered or status >= 500
            if bad:
                rec["errors"] += 1
                rec["since_start"]["errors"] += 1
                rec["last_error"] = error or f"HTTP {status}"
                rec["last_error_at"] = now
            if answered:
                rec["last_ok"] = now
                was_open = rec["failures_in_a_row"] >= OPEN_AFTER
                rec["failures_in_a_row"], rec["open_until"], rec["pause_s"] = 0, 0.0, PAUSE_S
                self._state(rec, DEGRADED if status >= 500 else UP,
                            "answers again" if was_open or rec["state"] == DOWN else "")
            else:
                rec["failures_in_a_row"] += 1
                if rec["failures_in_a_row"] >= OPEN_AFTER:
                    rec["open_until"] = now + rec["pause_s"]
                    self._state(rec, DOWN, f"no answer {rec['failures_in_a_row']} times in a row: {rec['last_error'][:140]}; "
                                           f"not asked again for {rec['pause_s']:.0f} s")
                    rec["pause_s"] = min(PAUSE_MAX_S, rec["pause_s"] * 2)
            if now - self._saved > 15:
                self._save()

    def _state(self, rec: Dict[str, Any], state: str, why: str) -> None:
        if rec["state"] == state:
            return
        was, held = rec["state"], time.time() - float(rec.get("since") or time.time())
        rec["history"] = (rec["history"] + [{"at": time.time(), "from": was, "to": state, "why": why}])[-50:]
        rec["state"], rec["since"] = state, time.time()
        log = logger.warning if state == DOWN else logger.info
        log("%s: %s -> %s%s%s", rec["controller"], was, state, f" ({why})" if why else "",
            f", after {held:.0f} s" if was not in (UNKNOWN,) else "")
        self._save()

    def _save(self) -> None:
        self._saved = time.time()
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w") as fh:
                json.dump(self._s, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # --- for whoever asks ---

    def view(self, key: str = "") -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            for name, rec in sorted(self._s.items()):
                if key and name != key:
                    continue
                times = sorted(self._times.get(name) or [])
                out.append({**{k: v for k, v in rec.items() if k not in ("pause_s",)},
                            "breaker": "open" if rec["open_until"] > time.time() else "closed",
                            "for_s": round(time.time() - float(rec.get("since") or time.time())),
                            "answers_ms": ({"median": round(times[len(times) // 2], 1),
                                            "p95": round(times[int(0.95 * (len(times) - 1))], 1),
                                            "of_last": len(times)} if times else None)})
        return out

    def forget(self, key: str) -> None:
        with self._lock:
            self._s.pop(key, None)
            self._times.pop(key, None)
            self._save()


TRACKER = Tracker()


def use(path: str) -> Tracker:
    """The tracker this process keeps, with its record on disk."""
    global TRACKER
    if TRACKER.path != path:
        TRACKER = Tracker(path)
    return TRACKER


def text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No controller has been asked anything yet."
    lines = []
    for r in rows:
        ago = lambda t: "never" if not t else f"{time.time() - t:.0f} s ago"   # noqa: E731
        a = r.get("answers_ms") or {}
        lines.append(f"{r['controller']:<14} {r['state']:<14} for {r['for_s']} s · last answer {ago(r['last_ok'])} · "
                     f"{r['requests']} requests, {r['errors']} errors, {r['writes']} writes"
                     + (f" · answers in {a['median']} ms (95%: {a['p95']} ms)" if a else "")
                     + (f" · breaker open" if r["breaker"] == "open" else "")
                     + (f"\n{'':<14} last error {ago(r['last_error_at'])}: {r['last_error'][:150]}" if r.get("last_error") else ""))
    return "\n".join(lines)
