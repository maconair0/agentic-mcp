"""
The session with one controller.

A read runs. A write runs only when both of these hold:

  - a person confirmed that the manifest maps the verb to the right operation
    (`confirmed`), since the mapping was made from names;
  - the call names who approved this one change (`approved_by`).

Every write, sent or refused, is appended to the audit log. Credentials are not
in the manifest: it names the environment variables that hold them.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import requests

AUDIT = os.path.join(os.getenv("AGENTIC_MCP_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                     "audit.jsonl")
TIMEOUT = float(os.getenv("AGENTIC_MCP_TIMEOUT") or os.getenv("CONNECTOR_TIMEOUT") or "20")
# Connecting is given far less than answering: a controller on this network
# accepts a connection in milliseconds, so one that has not in a few seconds is
# not there (its host is off, or the packets are dropped). With one timeout for
# both, a controller whose host was down held each caller twenty seconds just
# to fail to connect. Answering keeps the whole TIMEOUT: a big read is slow.
CONNECT_TIMEOUT = float(os.getenv("AGENTIC_MCP_CONNECT_TIMEOUT", "3"))
POOL = int(os.getenv("AGENTIC_MCP_CONNECTIONS", "32"))


def _client() -> requests.Session:
    """One controller is asked by many callers at once. The library keeps ten
    connections by default and opens and throws away one for every request
    beyond that, saying so in the log each time."""
    http = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=POOL)
    http.mount("http://", adapter)
    http.mount("https://", adapter)
    return http


_PARAM = re.compile(r"\{([^{}]+)\}")


def rows(data: Any, depth: int = 5) -> List[Any]:
    """The list a listing answer is about: the longest list found near the top.

    Controllers wrap their listings differently ({"devices": [...]}, a bare
    list, three containers deep). This finds the list without knowing which.
    """
    best: List[Any] = []
    if isinstance(data, list):
        best = data
    if isinstance(data, (dict, list)) and depth > 0:
        for value in (data.values() if isinstance(data, dict) else data[:50]):
            found = rows(value, depth - 1)
            if len(found) > len(best):
                best = found
    return best


class Session:
    def __init__(self, manifest: Dict[str, Any], env: Optional[Dict[str, str]] = None,
                 http: Any = None, audit: str = AUDIT, queue: Any = None):
        self.manifest = manifest
        if queue is None:
            from .queue import Queue
            queue = Queue(os.path.join(os.path.dirname(audit), "queue.json"))
        self.queue = queue
        self.env = os.environ if env is None else env
        self.http = http or _client()
        self.audit = audit
        self.host = "{0.scheme}://{0.netloc}".format(urlsplit(manifest["base_url"]))

    # ----- plumbing -----

    def _auth(self) -> Optional[Tuple[str, str]]:
        auth = self.manifest.get("auth") or {}
        if auth.get("type") != "basic":
            return None
        return self.env.get(auth.get("user_env", ""), ""), self.env.get(auth.get("password_env", ""), "")

    def fetch(self, method: str, url: str, body: Any = None, timeout: float = TIMEOUT,
              params: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
        """(status, content type, text); status 0 when nothing answered.

        Every request to this controller goes through here, whichever tool
        made it, so there is one place that holds its address and credentials.
        """
        from . import sessions
        key = str(self.manifest.get("key") or self.host)
        tracker = sessions.TRACKER
        write = str(method).upper() != "GET"
        held = tracker.allow(key)
        if held:
            # The breaker is open: said at once, without waiting out a timeout
            # and without one more request to a controller that is not there.
            return 0, "", f"NotAsked: {held}"
        # A read that got no answer at all is asked once more: a connection
        # dropped between two requests is not the controller being down. A
        # write is never repeated here: it may have arrived.
        if not write and tracker.probing(key):
            timeout = min(timeout, sessions.PROBE_TIMEOUT_S)
        full = tracker.enter(key)
        if full:
            return 0, "", f"NotAsked: {full}"
        try:
            return self._ask(tracker, key, write, method, url, body, timeout, params, headers)
        finally:
            tracker.leave(key)

    def _ask(self, tracker: Any, key: str, write: bool, method: str, url: str, body: Any, timeout: float,
             params: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]) -> Tuple[int, str, str]:
        for attempt in range(1 if write else 2):
            started = time.monotonic()
            try:
                r = self.http.request(method, url, json=body, auth=self._auth(),
                                      timeout=(min(CONNECT_TIMEOUT, timeout), timeout),
                                      params=params,
                                      headers=headers or {"Accept": "application/json, */*;q=0.5"})
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt == 0 and not write and not isinstance(exc, requests.Timeout):
                    time.sleep(0.2)
                    continue
                tracker.record(key, 0, (time.monotonic() - started) * 1000, error, write)
                return 0, "", error
            except Exception as exc:  # noqa: BLE001 - nothing a controller sends may take this service down
                error = f"{type(exc).__name__}: {exc}"
                tracker.record(key, 0, (time.monotonic() - started) * 1000, error, write)
                return 0, "", error
            tracker.record(key, r.status_code, (time.monotonic() - started) * 1000,
                           f"HTTP {r.status_code}: {r.text[:120]}" if r.status_code >= 500 else "", write)
            return r.status_code, r.headers.get("content-type", ""), r.text
        return 0, "", "no answer"

    def _url(self, op: Dict[str, Any], params: Dict[str, Any]) -> str:
        given = {**(self.manifest.get("defaults") or {}), **params}
        missing = [p for p in _PARAM.findall(op["path"]) if p not in given]
        if missing:
            raise ValueError("this operation needs: " + ", ".join(missing))
        return self.host + _PARAM.sub(lambda m: quote(str(given[m.group(1)]), safe=""), op["path"])

    def _verb(self, verb: str) -> Dict[str, Any]:
        entry = (self.manifest.get("verbs") or {}).get(verb)
        if not entry or not entry.get("operation"):
            status = (entry or {}).get("status", "missing")
            raise LookupError(f"{self.manifest['key']} has no operation for {verb} ({status})")
        return entry

    # ----- reads -----

    def _query(self, entry: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        given = {**(self.manifest.get("defaults") or {}), **params}
        query = {name: given[arg] for name, arg in (entry.get("query") or {}).items() if arg in given}
        return query or None

    def read(self, verb: str, body: Optional[Dict[str, Any]] = None, **params) -> Dict[str, Any]:
        entry = self._verb(verb)
        op = entry["operation"]
        if op["writes"]:
            return {"ok": False, "error": f"{verb} maps to a write ({op['id']}); use write()"}
        try:
            url = self._url(op, params)
            body = self.body_for(verb, body, params)
        except ValueError as exc:
            return {"ok": False, "operation": op["id"], "error": str(exc)}
        status, _, text = self.fetch(op["method"], url, body, params=self._query(entry, params))
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        ok = 200 <= status < 300
        out = {"ok": ok, "status": status, "operation": op["id"], "data": data}
        if not ok and "data-missing" in text:
            # RESTCONF's way of saying the container exists and holds nothing.
            out.update(ok=True, data=None, empty=True)
        elif not ok:
            out["error"] = (text or "no answer")[:300] if status else text
        if out["ok"] and entry.get("view"):
            from .tools import view_rows
            out["rows"] = view_rows(entry["view"], data) if data is not None else []
        return out

    # ----- writes -----

    def body_for(self, verb: str, body: Any, params: Dict[str, Any], template: Any = "entry") -> Any:
        """The body given, or the verb's template with the arguments filled in."""
        if template == "entry":
            template = self._verb(verb).get("template")
        if body is None and template is not None:
            from .tools import _fill, placeholders
            given = {**(self.manifest.get("defaults") or {}), **params}
            missing = [p for p in placeholders(template) if p not in given]
            if missing:
                raise ValueError("this operation needs: " + ", ".join(missing))
            return _fill(template, given)
        return body

    def requests_for(self, verb: str, body: Any = None, **params) -> List[Dict[str, Any]]:
        """Every request a verb sends, in order. Most verbs send one."""
        entry = self._verb(verb)
        steps = entry.get("steps") or [{"operation": entry["operation"], "template": entry.get("template")}]
        out = []
        for step in steps:
            op = step["operation"]
            out.append({"controller": self.manifest["key"], "verb": verb, "method": op["method"],
                        "url": self._url(op, params),
                        "body": self.body_for(verb, body, params, template=step.get("template")),
                        "operation": op["id"], "confirmed": bool(entry.get("confirmed"))})
        return out

    def prepare(self, verb: str, body: Optional[Dict[str, Any]] = None, **params) -> Dict[str, Any]:
        """The request a write would send (its first, with any others under `then`). Nothing is sent."""
        requests_ = self.requests_for(verb, body, **params)
        return {**requests_[0], "then": requests_[1:]} if len(requests_) > 1 else requests_[0]

    def _over_limit(self, verb: str) -> str:
        """Why this write is one too many for the hour, by the manifest's policy."""
        limit = int(((self._verb(verb).get("policy") or {}).get("max_per_hour")) or 0)
        if not limit:
            return ""
        since, sent, times = time.time() - 3600, 0, []
        try:
            with open(self.audit) as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (row.get("sent") and row.get("at", 0) >= since and row.get("verb") == verb
                            and row.get("controller") == self.manifest["key"] and not row.get("step")
                            and not row.get("replay")):
                        sent += 1
                        times.append(float(row.get("at", 0)))
        except OSError:
            return ""
        if sent < limit:
            return ""
        # The next one may go when the oldest of the last `limit` is an hour old.
        again = sorted(times)[-limit] + 3600
        return (f"{self.manifest.get('label', self.manifest['key'])} takes at most {limit} "
                f"{verb.replace('_', ' ')} request(s) an hour and {sent} were sent in the last hour; "
                f"the next can be sent at {time.strftime('%H:%M', time.localtime(again))} "
                f"(in {max(1, int((again - time.time()) / 60) + 1)} min)")

    def dry_run(self, verb: str, body: Optional[Dict[str, Any]] = None, **params) -> Dict[str, Any]:
        """Everything short of sending: is the request complete and well formed.

        Each address is built, each body is filled in from its template, and
        the body is checked against the schema the controller published for
        the operation. Passing says the request is one the controller's own
        description allows. It does not say the controller will accept it, and
        it is never a reason to send without asking.
        """
        entry = self._verb(verb)
        try:
            requests_ = self.requests_for(verb, body, **params)
        except ValueError as exc:
            return {"ok": False, "sent": False, "error": str(exc)}
        problems, checked = [], False
        steps = entry.get("steps") or [{"operation": entry["operation"]}]
        for request, step in zip(requests_, steps):
            schema = step["operation"].get("body_schema")
            if schema and request["body"] is not None:
                checked = True
                try:
                    import jsonschema
                    validator = jsonschema.Draft7Validator(schema)
                    problems += [("/".join(str(p) for p in e.path) or "body") + ": " + e.message[:160]
                                 for e in list(validator.iter_errors(request["body"]))[:6]]
                except Exception as exc:  # noqa: BLE001 - a schema this cannot read checks nothing
                    request["schema_note"] = f"the published schema could not be used ({type(exc).__name__})"
            elif step["operation"].get("body") and request["body"] is None:
                problems.append(f"{request['operation']} takes a body and none was given")
        over = self._over_limit(verb)
        if over:
            problems.append(over)
        first = {**requests_[0], "then": requests_[1:]} if len(requests_) > 1 else requests_[0]
        return {"ok": not problems, "sent": False, "request": first, "requests": requests_,
                "problems": problems, "checked_against_schema": checked,
                "confirmed": bool(entry.get("confirmed"))}

    def write(self, verb: str, body: Optional[Dict[str, Any]] = None, approved_by: str = "",
              **params) -> Dict[str, Any]:
        entry = self._verb(verb)
        try:
            requests_ = self.requests_for(verb, body, **params)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        request = requests_[0]
        refused = ""
        if not entry["operation"]["writes"]:
            refused = f"{verb} maps to a read; use read()"
        elif not entry.get("confirmed"):
            refused = (f"the mapping of {verb} to {entry['operation']['id']} was made from its "
                       f"name and nobody has confirmed it")
        elif not str(approved_by).strip():
            refused = "a write names who approved it"
        else:
            refused = self._over_limit(verb)
        if refused:
            self._log(request, approved_by, sent=False, result=refused)
            return {"ok": False, "refused": True, "error": refused, "request": request}
        out: Dict[str, Any] = {"ok": True, "operation": entry["operation"]["id"], "sent": []}
        for i, request in enumerate(requests_):
            status, _, text = self.fetch(request["method"], request["url"], request["body"],
                                         params=self._query(entry, params))
            ok = 200 <= status < 300
            self._log(request, approved_by, sent=True, result=status, step=i)
            try:
                data = json.loads(text) if text else None
            except ValueError:
                data = None
            out.update(status=status, data=data)
            out["sent"].append(request["operation"])
            if not ok:
                # Later requests of this verb are not sent. Earlier ones stay
                # as they are: undoing them is the operator's decision.
                out.update(ok=False, error=(text or "no answer")[:300],
                           not_sent=[r["operation"] for r in requests_[i + 1:]])
                break
        return out

    # ----- writes that wait for a person -----

    def queue_write(self, action: str, requests_: List[Dict[str, Any]], summary: str = "",
                    target: str = "", verb: str = "") -> Dict[str, Any]:
        """Queue these requests for approval. Nothing is sent."""
        over = self._over_limit(verb) if verb and (self.manifest.get("verbs") or {}).get(verb) else ""
        if over:
            return {"ok": False, "error": "rate_limited", "detail": over}
        policy = dict(((self.manifest.get("verbs") or {}).get(verb) or {}).get("policy") or {})
        return {"ok": True, **self.queue.request(self.manifest["key"], action, summary, requests_,
                                                 target=target, verb=verb, policy=policy)}

    def apply_queued(self, item_id: str, by: str) -> Dict[str, Any]:
        """Approve one queued write and send it now: that one only."""
        claimed = self.queue.claim(item_id, by)
        if not claimed.get("ok"):
            return claimed
        item = claimed["item"]
        if item["controller"] != self.manifest["key"]:
            self.queue.finish(item_id, "pending")
            return {"ok": False, "error": f"{item_id} is not a request for {self.manifest['key']}"}
        state, result = self._send_all(item, by)
        self.queue.finish(item_id, state, result)
        return {"ok": state == "applied", "id": item_id, "state": state, "action": item.get("action"),
                "summary": item.get("summary"), "target": item.get("target"),
                **({"error": result} if state == "failed" else {})}

    def _send_all(self, item: Dict[str, Any], by: str, replay: bool = False):
        for i, request in enumerate(item["requests"]):
            status, _, text = self.fetch(request["method"], request["url"], request.get("body"),
                                         timeout=float(request.get("timeout") or TIMEOUT))
            self._log({**request, "controller": self.manifest["key"], "verb": item.get("verb") or item.get("action")},
                      by, sent=True, result=status, step=i, replay=replay)
            failed = "" if 200 <= status < 300 else f"HTTP {status}: {(text or 'no answer')[:200]}"
            if not failed and request.get("expect"):
                # A controller that answers 200 with a refusal inside the body.
                from .tools import unmet
                try:
                    failed = unmet(request["expect"], json.loads(text) if text else None)
                except ValueError:
                    failed = ""
            if failed:
                return "failed", failed
        return "applied", "ok"

    def replay(self) -> Dict[str, Any]:
        """Send again everything applied here, oldest first: what it should hold."""
        done, failed = [], []
        for item in self.queue.applied(self.manifest["key"]):
            state, _ = self._send_all(item, "replay", replay=True)
            (done if state == "applied" else failed).append(item["id"])
        return {"replayed": done, "failed": failed}

    def _log(self, request: Dict[str, Any], by: str, sent: bool, result: Any, step: int = 0,
             replay: bool = False) -> None:
        line = {"at": time.time(), "controller": request["controller"], "verb": request["verb"],
                "method": request["method"], "url": request["url"], "body": request["body"],
                "approved_by": by, "sent": sent, "result": result, "step": step,
                **({"replay": True} if replay else {})}
        try:
            os.makedirs(os.path.dirname(self.audit), exist_ok=True)
            with open(self.audit, "a") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError:
            pass
