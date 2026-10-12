"""
What a session offers for a controller of a known kind.

A kind's controller manifest (library.py) holds its verbs and its named tools
as data. A skill (this package) is code, used only when the manifest names it
(`skill: ryu`), for tools that more than data is needed for. Every controller
also gets one tool per verb its manifest maps.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict

from .. import discover
from ..tools import Tool, from_recipe, from_verbs

logger = logging.getLogger("agentic_mcp.skills")


def family_of(name: str) -> Dict[str, Any]:
    return next((f for f in discover.families() if f.get("family") == name), {})


def _module(name: str):
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.endswith(name):
            return None
        raise


def tools_for(session: Any, data: str, approver: Callable[[], str]) -> Dict[str, Tool]:
    """Every tool this controller's session offers."""
    name = str(session.manifest.get("family") or "")
    family = family_of(name) if name else {}
    out: Dict[str, Tool] = {}
    for recipe in family.get("tools") or []:
        out[recipe["name"]] = from_recipe(recipe, session, family.get("style", "json"))
    module = _module(family["skill"]) if family.get("skill") else None
    if module is not None:
        out.update(module.tools(session, data, family.get("catalogue") or {}))
    # The verbs are offered for every controller. Where a named tool already
    # has that name (a kind whose tools are called by name), the named one stays.
    for tool_name, tool in from_verbs(session, approver).items():
        out.setdefault(tool_name, tool)
    return out


def start_background(session: Any, data: str) -> None:
    """Whatever a kind of controller keeps running beside its tools.

    `replay` in its manifest: watch one read, and when something new appears
    in it (a switch that has just connected), send again everything that was
    applied on this controller, oldest first. A controller that keeps no
    configuration of its own gets back what was approved for it.
    """
    family = family_of(str(session.manifest.get("family") or "")) if session.manifest.get("family") else {}
    module = _module(family["skill"]) if family.get("skill") else None
    if module is not None and hasattr(module, "background"):
        try:
            module.background(session, data)
        except Exception:  # noqa: BLE001
            logger.warning("background work for %s did not start", session.manifest["key"], exc_info=True)
    rule = family.get("replay") or {}
    if rule.get("when_new_in"):
        import threading
        import time

        def watch():
            seen: set = set()
            while True:
                try:
                    got = session.read(rule["when_new_in"])
                    now = {json_key(r) for r in (got.get("rows") if "rows" in got else got.get("data") or [])} \
                        if got.get("ok") else set()
                    if now - seen:
                        done = session.replay()
                        if done["replayed"] or done["failed"]:
                            logger.info("%s: %d new in %s; replayed %d write(s), %d failed",
                                        session.manifest["key"], len(now - seen), rule["when_new_in"],
                                        len(done["replayed"]), len(done["failed"]))
                    seen = now
                except Exception:  # noqa: BLE001 - a controller that is down is a normal state
                    seen = set()
                time.sleep(float(rule.get("every_s", 15)))

        threading.Thread(target=watch, name=f"replay-{session.manifest['key']}", daemon=True).start()


def json_key(row: Any) -> str:
    import json
    return json.dumps(row, sort_keys=True, default=str) if isinstance(row, (dict, list)) else str(row)
