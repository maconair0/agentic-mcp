"""
The controller desk: the one place every controller's session is held.

It is the only thing that talks to a controller, whether it runs as the
service of its own (service.py) or inside the process that uses it. Each
controller has a seat: its manifest, its session and its tools. A seat knows
its own controller and nothing else. No seat holds a reference to another seat
or to the desk, so a controller's tools cannot reach another controller;
anything that involves two controllers is decided by whoever uses the desk,
which asks each seat separately.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import contract, manifest as manifests, skills
from .session import Session
from .tools import LocalSession, Tool

logger = logging.getLogger("agentic_mcp.desk")


class Seat:
    def __init__(self, manifest: Dict[str, Any], session: Session, tools: Dict[str, Tool]):
        self.manifest = manifest
        self.session = session
        self.tools = tools
        self.local = LocalSession(tools)
        self.stamp = 0.0


class Desk:
    def __init__(self, data: str = manifests.DATA, env: Optional[Dict[str, str]] = None,
                 http: Any = None, background: bool = True):
        self.data = data
        self.env = env
        self.http = http
        self.background = background
        from .queue import Queue
        self.queue = Queue(os.path.join(data, "queue.json"))
        self._seats: Dict[str, Seat] = {}
        self._started: set = set()
        self._lock = threading.RLock()
        self._who = threading.local()

    # ----- seats -----

    def keys(self) -> List[str]:
        return manifests.keys(self.data)

    def seat(self, key: str) -> Seat:
        with self._lock:
            path = manifests.path_of(key, self.data)
            try:
                stamp = os.path.getmtime(path)
            except OSError:
                stamp = 0.0
            seat = self._seats.get(key)
            if seat is not None and seat.stamp != stamp:
                # Onboarded again, or changed from a shell: read it afresh.
                seat = None
            if seat is None:
                found = manifests.load(key, self.data)
                if found is None:
                    self._seats.pop(key, None)
                    raise LookupError(f"{key} has not been onboarded")
                session = Session(found, env=self.env, http=self.http,
                                  audit=os.path.join(self.data, "audit.jsonl"), queue=self.queue)
                tools = skills.tools_for(session, self.data, self._approver)
                first = key not in self._started
                seat = self._seats[key] = Seat(found, session, tools)
                seat.stamp = stamp
                if self.background and first:
                    self._started.add(key)
                    skills.start_background(session, self.data)
            return seat

    def close(self, key: str) -> None:
        """Forget a seat; the next use opens it again from the manifest."""
        with self._lock:
            self._seats.pop(key, None)

    # ----- who approved the write being made on this thread -----

    def _approver(self) -> str:
        return getattr(self._who, "by", "")

    def approved_by(self, by: str) -> None:
        self._who.by = by

    # ----- what is asked of it -----

    def reachable(self, key: str, timeout: float = 2.5) -> str:
        """"" when the controller's address accepts a connection, else why not."""
        url = urlsplit(self.seat(key).manifest["base_url"])
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            with socket.create_connection((url.hostname, port), timeout=timeout):
                return ""
        except OSError as exc:
            return f"{url.hostname}:{port} is not answering ({type(exc).__name__}: {exc})"

    def onboard(self, key: str, base_url: str, kind: str = "", **kw) -> Dict[str, Any]:
        """Learn a controller, check it against the contract, and seat it.

        `kind` says which controller manifest it is of, for a controller that
        publishes nothing a kind could be recognised by."""
        if kind and not kw.get("spec"):
            kw["spec"] = kind
        previous = manifests.load(key, self.data)
        got = manifests.onboard(key, base_url, env=self.env, http=self.http,
                                previous=previous, **kw)
        problem = (got["manifest"].get("api") or {}).get("problem")
        if problem and previous and previous.get("verbs"):
            # Down today is not a reason to forget what was learned yesterday.
            previous.setdefault("conformance", contract.check(previous))
            return {**previous, "kept": problem}
        got["manifest"]["conformance"] = contract.check(got["manifest"])
        manifests.save(got["manifest"], got["operations"], data=self.data)
        self.close(key)
        return got["manifest"]

    def describe(self, key: str) -> Dict[str, Any]:
        seat = self.seat(key)
        return {"key": key, "label": seat.manifest.get("label", key),
                "base_url": seat.manifest["base_url"], "family": seat.manifest.get("family", ""),
                "domain": (seat.manifest.get("domain") or {}).get("kind", "unknown"),
                "tools": len(seat.tools),
                "conformance": seat.manifest.get("conformance") or contract.check(seat.manifest)}

    # ----- the same questions, asked by key: what a client far away uses too -----

    def manifest(self, key: str) -> Optional[Dict[str, Any]]:
        return manifests.load(key, self.data)

    def summary(self, key: str) -> str:
        found = self.manifest(key) or {}
        return manifests.summary(found) + "\n\n" + contract.text(contract.check(found))

    def tools(self, key: str) -> List[Dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "schema": t.schema, "write": t.write,
                 "verb": t.verb} for t in self.seat(key).tools.values()]

    def call(self, key: str, tool: str, arguments: Optional[Dict[str, Any]] = None,
             approved_by: str = "") -> Dict[str, Any]:
        """One tool of one controller. {"text", "is_error"}."""
        import asyncio
        seat = self.seat(key)

        async def go():
            self.approved_by(approved_by)
            return await seat.local.call_tool(tool, arguments or {})
        got = asyncio.run(go())
        return {"text": got.content[0].text, "is_error": bool(got.isError)}

    def read(self, key: str, verb: str, body: Any = None, **args) -> Dict[str, Any]:
        return self.seat(key).session.read(verb, body, **args)

    def dry_run(self, key: str, verb: str, body: Any = None, **args) -> Dict[str, Any]:
        return self.seat(key).session.dry_run(verb, body, **args)

    def write(self, key: str, verb: str, body: Any = None, approved_by: str = "", **args) -> Dict[str, Any]:
        return self.seat(key).session.write(verb, body, approved_by=approved_by, **args)

    def confirm(self, key: str, verb: str, by: str, operation: str = "") -> Dict[str, Any]:
        found = self.manifest(key)
        if found is None:
            raise LookupError(f"{key} has not been onboarded")
        try:
            with open(os.path.join(self.data, f"{key}.operations.json")) as fh:
                import json
                operations = json.load(fh)
        except (OSError, ValueError):
            operations = []
        entry = manifests.confirm(found, verb, by, operation, operations)
        manifests.save(found, data=self.data)
        return entry

    def pending(self, key: str = "") -> List[Dict[str, Any]]:
        """Writes waiting for approval, without the requests themselves."""
        return [{k: v for k, v in item.items() if k != "requests"} | {"calls": len(item.get("requests") or [])}
                for item in self.queue.pending(key)]

    def remove(self, key: str) -> bool:
        gone = False
        for name in (f"{key}.manifest.json", f"{key}.operations.json"):
            try:
                os.remove(os.path.join(self.data, name))
                gone = True
            except OSError:
                pass
        self.close(key)
        return gone

    def kinds(self) -> List[Dict[str, Any]]:
        from . import library
        return [{"kind": m["kind"], "title": m.get("title", m["kind"]), "version": str(m.get("version", "")),
                 "domain": m.get("domain", ""), "about": " ".join(str(m.get("about") or "").split()),
                 "verbs": sorted(m.get("verbs") or {}),
                 "tools": len(m.get("tools") or []), "skill": m.get("skill", ""),
                 "problems": library.validate(m)} for m in library.all_manifests()]

    def install_manifest(self, text: str, name: str = "manifest.yaml") -> Dict[str, Any]:
        from . import library
        return library.install_text(text, name)

    def install_guide(self, data: Any, kind: str, title: str = "", name: str = "",
                      replace: bool = False) -> Dict[str, Any]:
        """A controller's user guide or API document, made into a draft manifest."""
        from . import library
        return library.install_guide(data, kind, title, name, replace=replace)

    def guide(self, kind: str) -> str:
        from . import library
        found = library.get(kind)
        return library.guide(found) if found else ""


_DESK: Optional[Desk] = None


def desk():
    """The desk this process uses: its own, or the agentic MCP it was told of.

    With AGENTIC_MCP_ADDRESS set the desk is another process (agentic_mcp/service.py)
    and this is a client of it. Without, the desk is held here.
    """
    global _DESK
    if _DESK is None:
        url = os.getenv("AGENTIC_MCP_ADDRESS", "").strip()
        if url:
            from .client import RemoteDesk
            _DESK = RemoteDesk(url, token=os.getenv("AGENTIC_MCP_TOKEN", ""))
        else:
            _DESK = Desk()
    return _DESK
