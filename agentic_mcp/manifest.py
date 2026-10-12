"""
The manifest of one controller: what was learned at onboarding.

It is written once when a controller is onboarded and read every time a session
with that controller opens. It holds the address, where the credentials are
(never the credentials), how the controller describes its API, its domain with
the evidence, and for each verb the operation that serves it and how far that
is trusted:

  declared            the kind's controller manifest states it; a person wrote
                      it down, so it needs no confirmation of the mapping
  verified            a read that was run and answered
  mapped              a read ranked first by name, not run (it needs arguments)
  answered empty      a read that was run and holds nothing now, which the
                      wrong operation would also do
  failed              a read that was run and did not answer
  needs confirmation  a write; a person has to confirm it before it is used
  confirmed           a write a person confirmed
  missing             the controller has nothing for this verb
  not applicable      the verb does not exist in this controller's domain

`quirks` is for what no API description says and only use shows (a controller
that accepts a field and ignores it, a limit on writes per hour). It starts
empty and is added to by people and, later, by what the agent observes.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from . import discover as _discover
from . import verbs as _verbs
from .session import Session, rows

DATA = os.getenv("AGENTIC_MCP_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def path_of(key: str, data: str = DATA) -> str:
    return os.path.join(data, f"{key}.manifest.json")


def load(key: str, data: str = DATA) -> Optional[Dict[str, Any]]:
    try:
        with open(path_of(key, data)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save(manifest: Dict[str, Any], operations: Optional[List[Dict[str, Any]]] = None,
         data: str = DATA) -> str:
    os.makedirs(data, exist_ok=True)
    target = path_of(manifest["key"], data)
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=1)
    os.replace(tmp, target)
    if operations is not None:
        # The whole list is kept beside it, so a mapping can be redone or
        # corrected without asking the controller again.
        with open(os.path.join(data, f"{manifest['key']}.operations.json"), "w") as fh:
            json.dump(operations, fh)
    return target


def keys(data: str = DATA) -> List[str]:
    try:
        return sorted(n[:-len(".manifest.json")] for n in os.listdir(data) if n.endswith(".manifest.json"))
    except OSError:
        return []


def onboard(key: str, base_url: str, label: str = "", user_env: str = "", password_env: str = "",
            spec: str = "", docs: Any = (), env: Optional[Dict[str, str]] = None, http: Any = None,
            previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Learn one controller and return (manifest, every operation found)."""
    manifest: Dict[str, Any] = {
        "key": key, "label": label or key, "base_url": base_url.rstrip("/"),
        "auth": ({"type": "basic", "user_env": user_env, "password_env": password_env}
                 if user_env else {"type": "none"}),
        "onboarded_at": time.time(), "verbs": {}, "defaults": {},
        "quirks": list((previous or {}).get("quirks") or []),
    }
    session = Session(manifest, env=env, http=http)
    found = _discover.discover(base_url, session.fetch, spec=spec, docs=list(docs or ()))
    operations = found["operations"]
    manifest["defaults"] = found["defaults"]
    manifest["family"] = found["family"]
    manifest["api"] = {"styles": found["styles"], "sources": found["sources"],
                       "operations": len(operations), "modules": len(found["modules"]),
                       "restconf_root": found["restconf_root"]}
    if not operations:
        answered = [t for t in found["tried"] if t["status"]]
        manifest["api"]["problem"] = (
            "the controller publishes no API description at any usual address and is not of a "
            "known kind; upload its manifest or its user guide and say which kind it is, or hand over "
            "an API document with --spec" if answered else
            "nothing answered at this address")
        manifest["domain"] = {"kind": "unknown", "scores": {}, "evidence": {}}
        return {"manifest": manifest, "operations": operations}
    manifest["domain"] = _verbs.domain(operations, found["modules"])
    kind = next((f for f in _discover.families() if f.get("family") == found["family"]), {})
    if kind.get("domain"):
        # Its manifest says which layer it works in; the words in its API are
        # kept as the evidence they are.
        manifest["domain"]["kind"] = kind["domain"]
        manifest["domain"]["stated_by"] = f"manifest {kind['family']}"
    manifest["verbs"] = _verbs.mapping(operations, manifest["domain"]["kind"])
    declare(manifest, kind, operations)
    for quirk in kind.get("quirks") or []:
        if quirk not in manifest["quirks"]:
            manifest["quirks"].append(quirk)
    if kind.get("limits"):
        manifest["limits"] = kind["limits"]

    # A read is only trusted once it has answered. Ones that need arguments
    # (a device id, two ends) cannot be tried here and stay "mapped".
    for verb, entry in manifest["verbs"].items():
        op = entry.get("operation")
        if not op or op["writes"] or op["method"] != "GET":
            continue
        tried = [op] + [c for c in entry["candidates"] if c["method"] == "GET" and not c["writes"]]
        last: Dict[str, Any] = {}
        for candidate in tried:
            entry["operation"] = candidate
            got = session.read(verb)
            if got.get("ok"):
                # An empty answer proves the address exists, not that it is the
                # right one: the wrong container is empty too.
                entry.update(status="answered empty" if got.get("empty") else "verified", checked={
                    "status": got["status"],
                    "rows": len(got["rows"]) if "rows" in got else len(rows(got.get("data"))),
                    "empty": bool(got.get("empty"))})
                entry["candidates"] = [c for c in tried if c is not candidate]
                break
            last = got
        else:
            entry["operation"] = op
            needs = [p for p in op["params"] if p not in manifest["defaults"]]
            if not needs and last.get("status") in (400, 422):
                # It answered, and what it said is that the request lacks something.
                needs = ["arguments (it answered %s without any)" % last["status"]]
            declared = entry.get("status") == "declared"
            entry.update(status=("declared" if declared else "mapped") if needs else "failed")
            if needs:
                entry["needs"] = needs

    # What a person decided before stands: a confirmation while the operation
    # is the same, an operation they chose over the one ranked first, and a
    # template for the body.
    for verb, old in ((previous or {}).get("verbs") or {}).items():
        new = manifest["verbs"].get(verb) or {}
        old_op = old.get("operation") or {}
        if new.get("declared_by"):
            continue
        if old.get("confirmed") and old_op.get("id") in {o["id"] for o in operations}:
            new.update(operation=old_op, writes=bool(old_op.get("writes")), confirmed=True,
                       status="confirmed", confirmed_by=old.get("confirmed_by", ""))
        if old.get("template") is not None and new:
            new["template"] = old["template"]
    # The candidates are kept short; a schema is only worth its room on the
    # operation chosen.
    for entry in manifest["verbs"].values():
        for candidate in entry.get("candidates") or []:
            candidate.pop("body_schema", None)
    return {"manifest": manifest, "operations": operations}


def declare(manifest: Dict[str, Any], kind: Dict[str, Any], operations: List[Dict[str, Any]]) -> None:
    """Put what the kind's controller manifest says about each verb in place.

    A verb a person wrote down takes the place of one ranked by name: the
    operation, the body to send, how to read the answer, the limit on writes.
    Writing it in a manifest and putting that manifest in the corpus is the
    confirmation of the mapping. It is not a confirmation of any one write:
    each still has its dry run and is asked about.
    """
    if not kind.get("verbs"):
        return
    by_id = {op["id"]: op for op in operations}
    said_by = f"manifest {kind['family']}" + (f" v{kind['version']}" if kind.get("version") else "")

    def op_of(text: str, writes: bool) -> Dict[str, Any]:
        method, path = text.split(" ", 1)
        op = dict(by_id.get(text) or _discover.operation(method, path, source="manifest"))
        op.update(writes=writes, body=bool(op.get("body")) or method.upper() != "GET" and writes)
        return op

    for verb, said in kind["verbs"].items():
        rule = _verbs.VERBS.get(verb)
        if rule is None:
            continue
        writes = bool(rule["writes"]) and not said.get("reads")
        steps = said.get("steps") or [said]
        first = op_of(steps[0]["operation"], writes)
        entry: Dict[str, Any] = {"status": "declared", "why": said.get("why") or rule["why"],
                                 "operation": first, "writes": writes, "candidates": [],
                                 "declared_by": said_by}
        if writes:
            entry.update(confirmed=True, confirmed_by=said_by)
        for key in ("arguments", "view", "policy", "query"):
            if said.get(key) is not None:
                entry[key] = said[key]
        if said.get("steps"):
            entry["steps"] = [{"operation": op_of(st["operation"], writes), "template": st.get("body")}
                              for st in steps]
        elif said.get("body") is not None:
            entry["template"] = said["body"]
        manifest["verbs"][verb] = entry


def confirm(manifest: Dict[str, Any], verb: str, by: str, operation_id: str = "",
            operations: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """A person says which operation a verb is. Needed before any write."""
    if not str(by).strip():
        raise ValueError("a confirmation names who gave it")
    entry = manifest["verbs"].get(verb)
    if entry is None:
        raise LookupError(f"no such verb: {verb}")
    if operation_id:
        pool = [entry.get("operation")] + list(entry.get("candidates") or []) + list(operations or [])
        chosen = next((op for op in pool if op and op["id"] == operation_id), None)
        if chosen is None:
            raise LookupError(f"{manifest['key']} has no operation {operation_id}")
        entry["operation"] = {k: v for k, v in chosen.items() if k != "score"}
        entry["writes"] = bool(chosen["writes"])
    if not entry.get("operation"):
        raise LookupError(f"{verb} has no operation to confirm")
    entry.update(confirmed=True, status="confirmed", confirmed_by=by, confirmed_at=time.time())
    return entry


def summary(manifest: Dict[str, Any]) -> str:
    api = manifest.get("api") or {}
    dom = manifest.get("domain") or {}
    kind = f", a known kind: {manifest['family']}" if manifest.get("family") else ", not a known kind"
    lines = [f"{manifest['label']} ({manifest['key']}) at {manifest['base_url']}{kind}",
             f"  API: {', '.join(api.get('styles') or ['none found'])}; "
             f"{api.get('operations', 0)} operations, {api.get('modules', 0)} YANG modules"]
    if api.get("problem"):
        lines.append(f"  PROBLEM: {api['problem']}")
    words = "; ".join(f"{k}: {', '.join(v)}" for k, v in (dom.get("evidence") or {}).items())
    lines.append(f"  domain: {dom.get('kind', 'unknown')}" + (f" ({words})" if words else ""))
    for verb, entry in (manifest.get("verbs") or {}).items():
        op = (entry.get("operation") or {}).get("id", "")
        extra = ""
        if entry.get("checked"):
            extra = (", holds nothing now" if entry["checked"].get("empty")
                     else f", {entry['checked']['rows']} rows")
        if entry.get("needs"):
            extra = f", needs {', '.join(entry['needs'])}"
        lines.append(f"  {verb:<14} {entry['status']:<19} {op}{extra}")
    for quirk in manifest.get("quirks") or []:
        lines.append(f"  quirk: {quirk}")
    return "\n".join(lines)


def set_template(manifest: Dict[str, Any], verb: str, template: Any, by: str) -> Dict[str, Any]:
    """The body a verb sends, with "$name" where an argument goes.

    Given by a person, or drafted by the model and then read by a person. It
    makes a write of this verb a call with named arguments (the ends, a name, a
    rate) instead of a whole body written out each time. Setting one withdraws
    the confirmation: what was confirmed was a different request.
    """
    entry = manifest["verbs"].get(verb)
    if not entry or not entry.get("operation"):
        raise LookupError(f"{verb} has no operation to give a body to")
    entry.update(template=template, template_by=by, confirmed=False,
                 status="needs confirmation" if entry["operation"]["writes"] else entry["status"])
    return entry
