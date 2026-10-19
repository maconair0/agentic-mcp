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
# A request that timed out cost a whole timeout, so far fewer of them in a row
# say the controller is not answering than refusals, which cost nothing. One
# tool call can make several requests in turn, each waiting out its timeout:
# with four needed, the first caller of a controller that hangs waited four.
OPEN_AFTER_TIMEOUTS = int(os.getenv("AGENTIC_MCP_BREAKER_AFTER_TIMEOUTS", "2"))
TIMEOUTS = ("ReadTimeout", "ConnectTimeout", "Timeout")
# The one request let through after a pause only asks whether the controller is
# back. It is somebody's request (a chat turn, an edge agent's poll), so it is
# not given the whole timeout: a controller that is back answers in well under this.
PROBE_TIMEOUT_S = float(os.getenv("AGENTIC_MCP_PROBE_TIMEOUT_S", "5"))
# How long the one request let through after a pause may take before another is.
PROBE_S = PROBE_TIMEOUT_S + 5
UP, DEGRADED, DOWN, UNKNOWN = "up", "degraded", "down", "not asked yet"
# The breaker holds requests back only in the service, which lives for days and
# is asked by many. A command run once, or a test, records and never holds back.
BREAKER = False


# How many requests may be on their way to one controller at once. A controller
# that has stopped answering holds each of its callers for a whole timeout; with
# a share of its own it cannot take the threads the other controllers' callers
# need. Past it a caller is told at once.
SLOTS = int(os.getenv("AGENTIC_MCP_PER_CONTROLLER", "24"))


def each(keys: List[str], fn: Any, wait_s: float = 10.0) -> Dict[str, Dict[str, Any]]:
    """Ask the same of several controllers at the same time.

    {key: {"ok": True, "value": ...} | {"ok": False, "error": "..."}}, in the
    order asked. Each controller has a thread of its own, so one that is slow
    or fails costs the others nothing; one that has not answered within
    `wait_s` is reported as late and left to finish on its own."""
    out: Dict[str, Dict[str, Any]] = {k: {"ok": False, "error": f"no answer within {wait_s:g} s", "late": True} for k in keys}
    if not keys:
        return out

    def run(key: str) -> None:
        try:
            out[key] = {"ok": True, "value": fn(key)}
        except Exception as exc:  # noqa: BLE001 - one controller's failure is its own answer
            out[key] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if len(keys) == 1:
        run(keys[0])
        return out
    threads = [threading.Thread(target=run, args=(k,), name=f"each-{k}", daemon=True) for k in keys]
    for t in threads:
        t.start()
    deadline = time.monotonic() + wait_s
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return dict(out)


def _stalled(rec: Dict[str, Any]) -> bool:
    """Enough failures in a row that the controller is taken not to be answering."""
    return (rec["failures_in_a_row"] >= OPEN_AFTER
            or rec.get("timeouts_in_a_row", 0) >= OPEN_AFTER_TIMEOUTS)


class Tracker:
    def __init__(self, path: str = ""):
        self.path = path
        self._lock = threading.RLock()
        self._s: Dict[str, Dict[str, Any]] = {}
        self._times: Dict[str, deque] = {}
        self._busy: Dict[str, int] = {}
        self._saved = 0.0
        if path:
            try:
                with open(path) as fh:
                    kept = json.load(fh)
                for key, rec in (kept if isinstance(kept, dict) else {}).items():
                    if isinstance(rec, dict):
                        # A breaker left open by a process that is gone is not kept: this one finds out itself.
                        self._s[key] = {**self._new(key), **rec, "failures_in_a_row": 0, "timeouts_in_a_row": 0, "probe": False, "open_until": 0.0,
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
        """"" when the controller may be asked, else why not (the breaker is open).

        When the pause is over, one request is let through to find out whether
        the controller is back, and the others are told at once while it is on
        its way: letting them all through together held each of them for a
        whole timeout, all at the same moment."""
        with self._lock:
            rec = self._rec(key)
            now = time.time()
            left = rec["open_until"] - now
            if left > 0 and BREAKER:
                return (f"{key} did not answer {rec['failures_in_a_row']} times in a row "
                        f"({rec['last_error'][:120]}); it is asked again in {left:.0f} s")
            if BREAKER and _stalled(rec):
                rec["probe"] = True
                # This one is the probe; its answer (record) closes the breaker or opens it again.
                rec["open_until"] = now + PROBE_S
            return ""

    def probing(self, key: str) -> bool:
        """Whether the request just allowed is the probe of a controller that was not answering."""
        with self._lock:
            return bool(self._rec(key).pop("probe", False))

    def enter(self, key: str) -> str:
        """Take one of the controller's places; "" when there was one, else why not."""
        with self._lock:
            now = self._busy.get(key, 0)
            if BREAKER and now >= SLOTS:
                return (f"{key} already has {now} requests waiting for it, which is all it is given; "
                        f"the other controllers are not held up by it")
            self._busy[key] = now + 1
            return ""

    def leave(self, key: str) -> None:
        with self._lock:
            self._busy[key] = max(0, self._busy.get(key, 0) - 1)

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
                was_open = _stalled(rec)
                rec["failures_in_a_row"], rec["timeouts_in_a_row"] = 0, 0
                rec["open_until"], rec["pause_s"] = 0.0, PAUSE_S
                self._state(rec, DEGRADED if status >= 500 else UP,
                            "answers again" if was_open or rec["state"] == DOWN else "")
            else:
                rec["failures_in_a_row"] += 1
                if str(error).split(":")[0] in TIMEOUTS:
                    rec["timeouts_in_a_row"] = rec.get("timeouts_in_a_row", 0) + 1
                if _stalled(rec):
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
                            "in_flight": self._busy.get(name, 0), "slots": SLOTS,
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
