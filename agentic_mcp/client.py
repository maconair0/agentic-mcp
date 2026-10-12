"""
The agentic MCP, from another process.

`RemoteDesk` answers the same questions `Desk` does, by asking the service
(service.py) over HTTP. What uses a desk does not need to know which
of the two it holds.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import requests


class RemoteDesk:
    def __init__(self, url: str, token: str = "", timeout: float = 240.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"
        self._who = threading.local()
        self._kept: Dict[str, Any] = {}

    # ----- plumbing -----

    def _ask(self, method: str, path: str, body: Any = None, timeout: Optional[float] = None,
             text: bool = False, **kw) -> Any:
        r = self.http.request(method, self.url + path, json=body, timeout=timeout or self.timeout, **kw)
        if r.status_code == 404:
            raise LookupError(_detail(r))
        if r.status_code >= 400:
            raise ValueError(_detail(r))
        return r.text if text else r.json()

    def _cached(self, name: str, fetch, hold: float = 4.0) -> Any:
        kept = self._kept.get(name)
        if kept and time.time() - kept[0] < hold:
            return kept[1]
        value = fetch()
        self._kept[name] = (time.time(), value)
        return value

    def _forget(self) -> None:
        self._kept.clear()

    # ----- the desk's questions -----

    def answers(self) -> str:
        """"" when the agentic MCP itself answers, else why not."""
        try:
            self._ask("GET", "/health", timeout=3)
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"the agentic MCP at {self.url} is not answering ({type(exc).__name__})"

    def health(self) -> Dict[str, Any]:
        return self._ask("GET", "/health", timeout=5)

    def topology(self) -> Dict[str, Any]:
        return self._ask("GET", "/topology", timeout=20)

    def keys(self) -> List[str]:
        try:
            return self._cached("keys", lambda: self._ask("GET", "/health", timeout=5)["controllers"])
        except Exception:  # noqa: BLE001 - not answering is "none that can be used now"
            return []

    def manifest(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            return self._cached(f"m:{key}", lambda: self._ask("GET", f"/controllers/{key}", timeout=20))["manifest"]
        except LookupError:
            return None

    def summary(self, key: str) -> str:
        return self._ask("GET", f"/controllers/{key}", timeout=20)["summary"]

    def describe(self, key: str) -> Dict[str, Any]:
        found = self._ask("GET", f"/controllers/{key}", timeout=20)
        m = found["manifest"]
        return {"key": key, "label": m.get("label", key), "base_url": m["base_url"],
                "family": m.get("family", ""), "domain": (m.get("domain") or {}).get("kind", "unknown"),
                "conformance": found["conformance"]}

    def reachable(self, key: str) -> str:
        return self.answers() or self._ask("GET", f"/controllers/{key}", timeout=20)["reachable"]

    def tools(self, key: str) -> List[Dict[str, Any]]:
        return self._cached(f"t:{key}", lambda: self._ask("GET", f"/controllers/{key}/tools")["tools"], 30)

    def call(self, key: str, tool: str, arguments: Optional[Dict[str, Any]] = None,
             approved_by: str = "") -> Dict[str, Any]:
        return self._ask("POST", f"/controllers/{key}/call",
                         {"tool": tool, "arguments": arguments or {}, "approved_by": approved_by})

    def read(self, key: str, verb: str, body: Any = None, **args) -> Dict[str, Any]:
        return self._ask("POST", f"/controllers/{key}/read", {"verb": verb, "body": body, "arguments": args})

    def dry_run(self, key: str, verb: str, body: Any = None, **args) -> Dict[str, Any]:
        return self._ask("POST", f"/controllers/{key}/dry-run", {"verb": verb, "body": body, "arguments": args})

    def write(self, key: str, verb: str, body: Any = None, approved_by: str = "", **args) -> Dict[str, Any]:
        self._forget()
        return self._ask("POST", f"/controllers/{key}/write",
                         {"verb": verb, "body": body, "arguments": args, "approved_by": approved_by})

    def confirm(self, key: str, verb: str, by: str, operation: str = "") -> Dict[str, Any]:
        self._forget()
        return self._ask("POST", f"/controllers/{key}/confirm", {"verb": verb, "by": by, "operation": operation})

    def pending(self, key: str = "") -> List[Dict[str, Any]]:
        return self._ask("GET", "/queue", timeout=10, params={"controller": key})["pending"]

    def onboard(self, key: str, base_url: str, **kw) -> Dict[str, Any]:
        self._forget()
        return self._ask("POST", "/controllers", {"key": key, "url": base_url, **kw})

    def remove(self, key: str) -> bool:
        self._forget()
        return bool(self._ask("DELETE", f"/controllers/{key}")["removed"])

    def kinds(self) -> List[Dict[str, Any]]:
        return self._ask("GET", "/manifests", timeout=20)["kinds"]

    def install_manifest(self, text: str, name: str = "manifest.yaml") -> Dict[str, Any]:
        self._forget()
        r = self.http.post(f"{self.url}/manifests", params={"name": name}, data=text.encode("utf-8"),
                           headers={"Content-Type": "text/plain"}, timeout=60)
        if r.status_code >= 400:
            raise ValueError(_detail(r))
        return r.json()["installed"][0]

    def install_guide(self, data: Any, kind: str, title: str = "", name: str = "",
                      replace: bool = False) -> Dict[str, Any]:
        self._forget()
        body = data.encode("utf-8") if isinstance(data, str) else data
        r = self.http.post(f"{self.url}/guides", data=body, timeout=120,
                           params={"kind": kind, "title": title, "name": name, "replace": str(bool(replace)).lower()},
                           headers={"Content-Type": "application/octet-stream"})
        if r.status_code >= 400:
            raise ValueError(_detail(r))
        return r.json()

    def guide(self, kind: str) -> str:
        try:
            return self._ask("GET", f"/manifests/{kind}/guide", text=True, timeout=20)
        except LookupError:
            return ""

    def ask(self, text: str, confirm: bool = False, approved_by: str = "") -> Dict[str, Any]:
        return self._ask("POST", "/ask", {"text": text, "confirm": confirm, "approved_by": approved_by})

    # ----- what a caller that held a local seat expects of one -----

    def approved_by(self, by: str) -> None:
        self._who.by = by

    def close(self, key: str) -> None:
        self._forget()

    def seat(self, key: str):
        found = self.manifest(key)
        if found is None:
            raise LookupError(f"{key} has not been onboarded at the agentic MCP")
        desk = self

        class Local:
            async def list_tools(self):
                return SimpleNamespace(tools=[
                    SimpleNamespace(name=t["name"], description=t["description"], inputSchema=t["schema"])
                    for t in desk.tools(key)])

            async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None):
                got = desk.call(key, name, arguments, approved_by=getattr(desk._who, "by", ""))  # noqa: SLF001
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=got["text"])],
                                       isError=bool(got["is_error"]))

        return SimpleNamespace(manifest=found, local=Local())


def _detail(r) -> str:
    try:
        return str(r.json().get("detail") or r.text)
    except ValueError:
        return r.text[:300]
