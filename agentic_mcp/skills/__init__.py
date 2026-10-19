"""
What a session offers for a controller of a known kind.

A kind's controller manifest (library.py) holds its verbs and its named tools
as data. A skill (this package) is code, used only when the manifest names it
(`skill: <name>`), for tools that more than data is needed for. Every controller
also gets one tool per verb its manifest maps.
"""
from __future__ import annotations

import importlib
import json
import os
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
        key = session.manifest["key"]
        watcher = Watcher(os.path.join(data, f"{key}.seen.json"))

        def watch():
            while True:
                try:
                    new = watcher.new_in(session.read(rule["when_new_in"]))
                    if new:
                        done = session.replay()
                        if done["replayed"] or done["failed"]:
                            logger.info("%s: %d new in %s; replayed %d write(s), %d failed", key, new,
                                        rule["when_new_in"], len(done["replayed"]), len(done["failed"]))
                except Exception:  # noqa: BLE001 - a controller that is down is a normal state
                    pass
                time.sleep(float(rule.get("every_s", 15)))

        threading.Thread(target=watch, name=f"replay-{key}", daemon=True).start()


class Watcher:
    """What a watched read held last time it answered, to tell what is new.

    Only a read that answered counts. A read that failed says nothing about
    what is there, so what was seen before stands: treating it as "nothing
    there" made everything new at the next good read, and one slow answer put
    every approved write on the controller again. What was seen is kept on
    disk, so starting this service again is not a reason to replay either. The
    first time ever, what is there is taken as the starting point. A controller
    that was away for several reads in a row is another matter: see AWAY_AFTER.
    """

    # This many reads in a row without an answer is the controller being away,
    # not one slow answer. When it answers again, what it holds may have been
    # started afresh with the same names (a lab rebuilt, a switch restarted):
    # everything it lists is taken as new, once.
    AWAY_AFTER = 3

    def __init__(self, path: str = ""):
        self.path = path
        self.missed = 0
        self.seen: Any = None
        try:
            with open(path) as fh:
                self.seen = set(json.load(fh))
        except (OSError, ValueError, TypeError):
            pass

    def new_in(self, got: Dict[str, Any]) -> int:
        """How many rows of this answer were not there at the last good one."""
        if not got.get("ok"):
            self.missed += 1
            return 0
        now = {json_key(r) for r in (got.get("rows") if "rows" in got else got.get("data") or [])}
        was_away, self.missed = self.missed >= self.AWAY_AFTER, 0
        new = len(now - self.seen) if self.seen is not None else 0
        if was_away and self.seen is not None:
            new = len(now)
        if now != self.seen:
            self.seen = now
            if self.path:
                try:
                    with open(self.path, "w") as fh:
                        json.dump(sorted(now), fh)
                except OSError:
                    pass
        return new


def json_key(row: Any) -> str:
    import json
    return json.dumps(row, sort_keys=True, default=str) if isinstance(row, (dict, list)) else str(row)
