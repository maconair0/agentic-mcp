"""
Who may do what, and where a controller's credentials may go.

Callers. AGENTIC_MCP_TOKEN is the operator's token and may do everything, as
before. AGENTIC_MCP_TOKENS names a JSON file of further, narrower tokens:

  [{"name": "noc-onos", "token": "sha256:<hex of the token>",
    "controllers": ["onos"], "write": false, "approve": false, "admin": false}]

  controllers  the controllers this token reaches ("*" for all); others are
               neither listed nor callable with it
  write        may call a write (it is then still refused without an approver)
  approve      may name who approved a write (`approved_by`): the only way a
               write is sent. Over MCP nobody may, so a model never approves
  admin        may onboard, remove, install manifests and change the contract

A token may be given in clear ("token": "abc") or as "sha256:<hex>".

Credentials. A manifest names the settings that hold its controller's user and
password, never the values. Each such setting is bound to the address of the
controller it was first onboarded with, in credential_scope.json beside the
manifests ({"ONOS_PASSWORD": ["http://10.0.0.5:8181"]}). A login is sent only to
the controller's own address, and onboarding that pairs a setting with another
address is refused: an administrator adds that address to the file first.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


# ----- callers -----

@dataclass(frozen=True)
class Principal:
    name: str
    controllers: frozenset = field(default_factory=lambda: frozenset({"*"}))
    write: bool = True
    approve: bool = True
    admin: bool = True

    def reaches(self, key: str) -> bool:
        return "*" in self.controllers or key in self.controllers


# Everything, as with no token at all: a service started without tokens is open, as it always was.
OPEN = Principal("open")
OPERATOR = Principal("operator")
CURRENT: contextvars.ContextVar[Principal] = contextvars.ContextVar("agentic_mcp_principal", default=OPEN)


def current() -> Principal:
    return CURRENT.get()


def _digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def load_tokens(path: str = "") -> List[Dict[str, Any]]:
    path = path or os.getenv("AGENTIC_MCP_TOKENS", "").strip()
    if not path:
        return []
    with open(path) as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        raise ValueError(f"{path}: a list of tokens was expected")
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("token"):
            raise ValueError(f"{path}: entry {i} has no token")
    return rows


class Callers:
    """Who a bearer token is."""

    def __init__(self, operator_token: str = "", rows: Optional[List[Dict[str, Any]]] = None):
        self.operator = operator_token.strip()
        self.rows = list(rows or [])

    @property
    def required(self) -> bool:
        return bool(self.operator or self.rows)

    def who(self, given: str) -> Optional[Principal]:
        """The caller this token is, or None for no or an unknown token."""
        if not given:
            return None
        if self.operator and hmac.compare_digest(given, self.operator):
            return OPERATOR
        digest = _digest(given)
        for row in self.rows:
            want = str(row["token"])
            if hmac.compare_digest(digest, want) if want.startswith("sha256:") else hmac.compare_digest(given, want):
                ctl = row.get("controllers", "*")
                ctl = {"*"} if ctl == "*" else {str(c) for c in (ctl or [])}
                return Principal(str(row.get("name") or "token"), frozenset(ctl), bool(row.get("write")),
                                 bool(row.get("approve")), bool(row.get("admin")))
        return None


# ----- where credentials may go -----

def origin(url: str) -> str:
    """scheme://host:port, with the port written even where it is the default."""
    u = urlsplit(str(url))
    scheme = (u.scheme or "http").lower()
    port = u.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{(u.hostname or '').lower()}:{port}"


class CredentialScope:
    """Which addresses each credential setting may be sent to."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, List[str]]:
        try:
            with open(self.path) as fh:
                got = json.load(fh)
            return {str(k): [origin(o) for o in (v if isinstance(v, list) else [v])] for k, v in got.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, AttributeError) as exc:
            # An unreadable scope is not taken as "anything goes".
            raise PermissionError(f"{self.path} cannot be read ({exc}); no credential is sent until it is fixed")

    def _save(self, scope: Dict[str, List[str]]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(scope, fh, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def settings(manifest: Dict[str, Any]) -> List[str]:
        auth = manifest.get("auth") or {}
        return [str(auth[k]) for k in ("user_env", "password_env") if auth.get(k)]

    def refusal(self, settings: List[str], base_url: str) -> str:
        """Why these settings may not go to this address, or ""."""
        scope, where = self._load(), origin(base_url)
        for name in settings:
            allowed = scope.get(name)
            if allowed and where not in allowed:
                return (f"the credential setting {name} belongs to {', '.join(allowed)}, not {where}; "
                        f"an administrator adds {where} for it in {self.path} if it is meant to go there")
        return ""

    def bind(self, settings: List[str], base_url: str) -> None:
        """Record the address of a setting not yet bound to one (first use)."""
        if not settings:
            return
        with self._lock:
            scope, changed = self._load(), False
            for name in settings:
                if name not in scope:
                    scope[name] = [origin(base_url)]
                    changed = True
            if changed:
                self._save(scope)

    def allows(self, settings: List[str], url: str) -> bool:
        try:
            return not self.refusal(settings, url)
        except PermissionError:
            return False
