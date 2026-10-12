"""
Writes waiting for a person, for any controller whose manifest asks for that.

A write tool marked `queue` does not send anything. It builds the requests,
puts them here and answers with an id. They are sent when somebody approves
that id, by the operator's token over a tool or from the command line. What
was applied is kept in order, so it can be put back on a controller that lost
it (a switch that restarted holds no flows).

This is one queue for every controller the agentic MCP holds. It has no
argument that skips it: the model that asked for a write does not get a say in
whether it is safe.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


class Queue:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.path) as fh:
                got = json.load(fh)
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, items: Dict[str, Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(items, fh, indent=1)
        os.replace(tmp, self.path)

    def request(self, controller: str, action: str, summary: str, requests: List[Dict[str, Any]],
                target: str = "", verb: str = "", policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        item = {"id": uuid.uuid4().hex[:12], "controller": controller, "action": action, "verb": verb,
                "summary": summary, "target": target, "requests": requests, "state": "pending",
                "queued": time.time()}
        with self._lock:
            items = self._load()
            items[item["id"]] = item
            self._save(items)
        return {"status": "queued_for_approval", "approval_id": item["id"], "action": action,
                "summary": summary, "target": target, "calls": len(requests),
                "policy": policy or {},
                "what_happens_next": "Nothing was sent to the controller. It is sent when this id is "
                                     "approved."}

    def get(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self._load().get(item_id)

    def pending(self, controller: str = "") -> List[Dict[str, Any]]:
        return [i for i in self._load().values()
                if i["state"] == "pending" and (not controller or i["controller"] == controller)]

    def applied(self, controller: str) -> List[Dict[str, Any]]:
        """What was applied on a controller, oldest first: what it should hold."""
        return sorted((i for i in self._load().values()
                       if i["state"] == "applied" and i["controller"] == controller),
                      key=lambda i: i.get("applied", 0))

    def claim(self, item_id: str, by: str) -> Dict[str, Any]:
        """Mark one pending item approved, so it is sent once and by one caller."""
        with self._lock:
            items = self._load()
            item = items.get(item_id)
            if item is None:
                return {"ok": False, "error": f"no queued request {item_id}"}
            if item["state"] != "pending":
                return {"ok": False, "error": f"{item_id} is already {item['state']}"}
            item.update(state="approved", decided=time.time(), by=by)
            self._save(items)
            return {"ok": True, "item": item}

    def finish(self, item_id: str, state: str, result: Any = "ok") -> None:
        with self._lock:
            items = self._load()
            if item_id in items:
                items[item_id].update(state=state, result=result, applied=time.time())
                self._save(items)

    def reject(self, item_id: str, by: str = "") -> Dict[str, Any]:
        with self._lock:
            items = self._load()
            item = items.get(item_id)
            if item is None or item["state"] != "pending":
                return {"ok": False, "error": f"no pending request {item_id}"}
            item.update(state="rejected", decided=time.time(), by=by)
            self._save(items)
        return {"ok": True, "id": item_id, "state": "rejected"}
