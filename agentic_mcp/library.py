"""
Controller manifests: one document per kind of controller, in a standard form.

A controller manifest (`manifest: agentic-mcp.controller/v1`) says what a kind of
controller is and how every verb of the contract is done on it: the operation,
the arguments, the body to send, how to read the answer, the limits on writes,
and the things about it that no API description says. It is a document, in
YAML or JSON, written once per kind of controller by whoever knows it.

The manifests are uploaded to the agentic MCP (its /manifests address, the
an operator's console, or `python -m agentic_mcp install`) and kept in its
own folder. That is all adding a kind of controller takes: the desk onboards
from it, and a readable guide is written beside it for whoever asks about the
controller in words.

Where nobody has written a manifest yet, the controller's user guide or API
document is uploaded instead, and a manifest is drafted from it
(`draft_from_guide`): the operations the guide names, in the standard form. A
draft states no verb, so nothing in it is trusted because a guide said so:
onboarding maps the verbs by name, tries every read on the controller, and
leaves every write waiting for a person's confirmation.

Nothing about a kind of controller is kept in this package's code or data.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

FORMAT = "agentic-mcp.controller/v1"
# The name the form had while this lived inside one system. A manifest written
# under it is read as it always was.
FORMATS = (FORMAT, "edgelm.controller/v1")
HERE = os.path.dirname(os.path.abspath(__file__))
EXTENSIONS = (".yaml", ".yml", ".json")
_BLOCK = re.compile(r"```(?:yaml|json|controller-manifest)\s*\n(.*?)\n```", re.S)
_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}

SECTIONS = ("manifest", "kind", "title", "version", "domain", "about", "recognise", "api", "defaults",
            "operations", "verbs", "tools", "catalogue", "provides", "skill", "replay", "quirks", "limits",
            "guide", "not_carried_over")
VERB_KEYS = ("operation", "steps", "arguments", "body", "query", "view", "policy", "why", "reads")


def corpus() -> str:
    """Where manifests are kept: the agentic MCP's own folder.

    The agentic MCP stands alone and keeps what it is given.
    """
    return (os.getenv("CONTROLLER_MANIFESTS_DIR") or os.getenv("AGENTIC_MCP_MANIFESTS")
            or os.path.join(os.getenv("AGENTIC_MCP_DATA") or os.path.join(HERE, "data"), "manifests"))


def parse(text: str) -> Optional[Dict[str, Any]]:
    """A manifest from YAML, JSON, or a Markdown page with one fenced block."""
    if not any(name in text for name in FORMATS):
        return None
    block = _BLOCK.search(text)
    for candidate in ([block.group(1)] if block else []) + [text]:
        try:
            doc = json.loads(candidate)
        except ValueError:
            try:
                import yaml
                doc = yaml.safe_load(candidate)
            except Exception:  # noqa: BLE001
                doc = None
        if isinstance(doc, dict) and doc.get("manifest") in FORMATS:
            return doc
    return None


def read(path: str) -> Optional[Dict[str, Any]]:
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    kept = _CACHE.get(path)
    if kept and kept[0] == stamp:
        return kept[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = parse(fh.read())
    except OSError:
        doc = None
    _CACHE[path] = (stamp, doc)
    return doc


def all_manifests(folder: str = "") -> List[Dict[str, Any]]:
    """Every controller manifest in the corpus, one per kind (the newest file wins)."""
    folder = folder or corpus()
    found: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    for base, _dirs, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(EXTENSIONS + (".md",)):
                continue
            path = os.path.join(base, name)
            doc = read(path)
            if doc and doc.get("kind"):
                stamp = os.path.getmtime(path)
                if doc["kind"] not in found or stamp > found[doc["kind"]][0]:
                    found[doc["kind"]] = (stamp, {**doc, "_file": path})
    return [doc for _, doc in sorted(found.values(), key=lambda p: p[1]["kind"])]


def get(kind: str, folder: str = "") -> Optional[Dict[str, Any]]:
    return next((m for m in all_manifests(folder) if m.get("kind") == kind), None)


# ----- is it well formed -----

def validate(doc: Any) -> List[str]:
    """What is wrong with a manifest. Empty when it can be used."""
    from .verbs import VERBS
    if not isinstance(doc, dict):
        return ["a manifest is a mapping"]
    problems = []
    if doc.get("manifest") not in FORMATS:
        problems.append(f"manifest: has to be {FORMAT}")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(doc.get("kind") or "")):
        problems.append("kind: a short lower-case name for this kind of controller, e.g. onos")
    if doc.get("domain") not in (None, "optical", "packet", "multi-layer"):
        problems.append("domain: optical, packet or multi-layer (or leave it out to have it worked out)")
    for key in doc:
        if key not in SECTIONS and not key.startswith("_"):
            problems.append(f"{key}: not a section of a controller manifest")
    recognise = doc.get("recognise") or {}
    if not (recognise.get("fingerprint") or recognise.get("operation_contains")):
        problems.append("recognise: give fingerprint (an address only this kind answers) or "
                        "operation_contains (a path only this kind's API has)")
    for i, op in enumerate(doc.get("operations") or []):
        if not isinstance(op, dict) or not op.get("method") or not str(op.get("path", "")).startswith("/"):
            problems.append(f"operations[{i}]: needs a method and a path beginning with /")
    for verb, entry in (doc.get("verbs") or {}).items():
        if verb not in VERBS:
            problems.append(f"verbs.{verb}: not a verb of the contract ({', '.join(VERBS)})")
            continue
        if not isinstance(entry, dict) or not (entry.get("operation") or entry.get("steps")):
            problems.append(f"verbs.{verb}: needs an operation ('GET /path') or steps")
            continue
        for key in entry:
            if key not in VERB_KEYS:
                problems.append(f"verbs.{verb}.{key}: not something a verb has")
        for step in [entry] + list(entry.get("steps") or []):
            op = step.get("operation")
            if op and not re.fullmatch(r"(GET|POST|PUT|PATCH|DELETE) /\S*", str(op)):
                problems.append(f"verbs.{verb}: '{op}' is not 'METHOD /path'")
        view = entry.get("view")
        if view is not None and not (isinstance(view, dict) and "rows" in view):
            problems.append(f"verbs.{verb}.view: needs rows (where the list is in the answer)")
    for i, tool in enumerate(doc.get("tools") or []):
        if not isinstance(tool, dict) or not tool.get("name") or not (
                (tool.get("method") and tool.get("path")) or tool.get("steps") or tool.get("verb")
                or tool.get("approve") or "const" in (tool.get("reply") or {})):
            problems.append(f"tools[{i}]: needs a name and one of: method and path, steps, verb, approve")
    for verb in doc.get("provides") or {}:
        if verb not in VERBS:
            problems.append(f"provides.{verb}: not a verb of the contract")
    return problems


# ----- the form the rest of this package reads -----

def as_family(doc: Dict[str, Any]) -> Dict[str, Any]:
    api = doc.get("api") or {}
    recognise = doc.get("recognise") or {}
    return {"family": doc["kind"], "title": doc.get("title", doc["kind"]), "about": doc.get("about", ""),
            "style": api.get("style", "json"), "docs": list(api.get("docs") or []),
            "spec_paths": list(api.get("openapi") or []),
            "fingerprint": recognise.get("fingerprint", ""),
            "recognise": {"operation_contains": recognise.get("operation_contains", "")},
            "defaults": dict(doc.get("defaults") or {}), "operations": list(doc.get("operations") or []),
            "tools": list(doc.get("tools") or []), "catalogue": dict(doc.get("catalogue") or {}),
            "provides": dict(doc.get("provides") or {}), "verbs": dict(doc.get("verbs") or {}),
            "skill": doc.get("skill", ""), "replay": dict(doc.get("replay") or {}),
            "domain": doc.get("domain", ""),
            "quirks": list(doc.get("quirks") or []), "limits": dict(doc.get("limits") or {}),
            "version": str(doc.get("version", "")), "file": doc.get("_file", "")}


def families(folder: str = "") -> List[Dict[str, Any]]:
    return [as_family(m) for m in all_manifests(folder) if not validate(m)]


# ----- a guide the retriever can read -----

def guide(doc: Dict[str, Any]) -> str:
    """The manifest as prose: what a person would ask about this controller."""
    from .verbs import VERBS
    title = doc.get("title", doc["kind"])
    lines = [f"# {title}: controller manifest", "",
             f"{title} is a controller of kind `{doc['kind']}`"
             + (f" in the {doc['domain']} domain" if doc.get("domain") else "") + ".",
             str(doc.get("about") or "").strip(), ""]
    api = doc.get("api") or {}
    how = []
    if api.get("openapi"):
        how.append("it publishes an API document that is read at onboarding")
    if api.get("docs"):
        how.append("its operations are read from its documentation: " + ", ".join(api["docs"]))
    if doc.get("operations"):
        how.append(f"{len(doc['operations'])} operations are listed in its manifest")
    if how:
        lines += ["## How its API is learned", "", "; ".join(how).capitalize() + ".", ""]
    if doc.get("verbs"):
        lines += [f"## What can be done on {title}", ""]
        for verb, entry in doc["verbs"].items():
            what = (VERBS.get(verb) or {}).get("why", verb)
            ops = [entry["operation"]] if entry.get("operation") else [s["operation"] for s in entry["steps"]]
            line = f"- To {verb.replace('_', ' ')} ({what}) on {title}: {' then '.join(ops)}."
            args = entry.get("arguments") or {}
            if args:
                line += " Arguments: " + "; ".join(f"{a} ({d})" for a, d in args.items()) + "."
            policy = entry.get("policy") or {}
            if policy.get("max_per_hour"):
                line += f" At most {policy['max_per_hour']} per hour."
            if entry.get("why"):
                line += " " + str(entry["why"]).strip()
            lines.append(line)
        lines.append("")
    if doc.get("tools"):
        lines += [f"## Tools of {title}", "",
                  f"{title} has {len(doc['tools'])} named tools, each one request: "
                  + ", ".join(t["name"] for t in doc["tools"][:60])
                  + (" and more." if len(doc["tools"]) > 60 else "."), ""]
    if doc.get("quirks"):
        lines += [f"## What to know about {title} that its API does not say", ""]
        lines += [f"- {q}" for q in doc["quirks"]] + [""]
    if doc.get("limits"):
        lines += [f"## Limits on {title}", ""] + [f"- {k}: {v}" for k, v in doc["limits"].items()] + [""]
    if doc.get("guide"):
        lines += [f"## Working with {title}", "", str(doc["guide"]).strip(), ""]
    return "\n".join(lines)


def install_text(text: str, name: str = "manifest.yaml", folder: str = "") -> Dict[str, Any]:
    """A manifest handed over as text (an upload), checked and kept."""
    import tempfile
    ext = os.path.splitext(name)[1].lower()
    with tempfile.NamedTemporaryFile("w", suffix=ext if ext in EXTENSIONS else ".yaml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(text)
    try:
        return install(fh.name, folder)
    finally:
        os.remove(fh.name)


def install(path: str, folder: str = "") -> Dict[str, Any]:
    """Put a manifest into the corpus, with its guide beside it."""
    folder = folder or corpus()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = parse(fh.read())
    except OSError as exc:
        return {"ok": False, "problems": [str(exc)]}
    if doc is None:
        return {"ok": False, "problems": [f"not a controller manifest (no 'manifest: {FORMAT}')"]}
    problems = validate(doc)
    if problems:
        return {"ok": False, "kind": doc.get("kind"), "problems": problems}
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, f"controller-{doc['kind']}{os.path.splitext(path)[1].lower()}")
    if os.path.abspath(path) != os.path.abspath(target):
        # One manifest per kind: an earlier one in another form is replaced.
        for ext in EXTENSIONS:
            old = os.path.join(folder, f"controller-{doc['kind']}{ext}")
            if os.path.exists(old):
                os.remove(old)
        shutil.copyfile(path, target)
    written = write_guide(doc, folder)
    return {"ok": True, "kind": doc["kind"], "title": doc.get("title", doc["kind"]), "file": target,
            "guide": written, "verbs": sorted(doc.get("verbs") or {}), "tools": len(doc.get("tools") or [])}


def write_guide(doc: Dict[str, Any], folder: str = "") -> str:
    target = os.path.join(folder or corpus(), f"controller-{doc['kind']}.guide.md")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(guide(doc))
    return target


def registered(path: str) -> Optional[Dict[str, Any]]:
    """For a file just put in the corpus: is it a manifest, and is it usable."""
    doc = read(path)
    if doc is None:
        return None
    problems = validate(doc)
    out = {"kind": doc.get("kind"), "title": doc.get("title"), "ok": not problems, "problems": problems}
    if not problems:
        out["guide"] = os.path.basename(write_guide(doc, os.path.dirname(path)))
    return out


# ----- a manifest drafted from the controller's user guide -----

def text_of(data: Any, name: str = "") -> str:
    """An uploaded document as text. A PDF is read when `pypdf` is installed."""
    if isinstance(data, str):
        return data
    if name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        try:
            import io

            from pypdf import PdfReader
        except ImportError:
            raise ValueError("a PDF needs the pypdf package (pip install pypdf); or upload the guide as text or HTML")
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    return data.decode("utf-8", "replace")


def operations_in(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """(the operations a document names, what kind of document it was)."""
    from . import discover
    doc: Any = None
    if text.lstrip()[:1] in ("{", "["):
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
    elif re.search(r"^(openapi|swagger)\s*:", text, re.M) and re.search(r"^paths\s*:", text, re.M):
        try:
            import yaml
            doc = yaml.safe_load(text)
        except Exception:  # noqa: BLE001 - not YAML after all: read it as a page
            doc = None
    if isinstance(doc, dict) and doc.get("paths"):
        return discover.from_openapi(doc, source="uploaded API document"), "an API document"
    return discover.from_docs(text, source="uploaded user guide"), "a user guide"


def _mark(operations: List[Dict[str, Any]]) -> str:
    """A path to recognise this kind by: its longest read that needs nothing named."""
    plain = [op["path"] for op in operations if op["method"] == "GET" and not op["params"]]
    return max(plain or [op["path"] for op in operations], key=len)


def draft_from_guide(text: str, kind: str, title: str = "", name: str = "") -> Dict[str, Any]:
    """A manifest in the standard form, drafted from a user guide or API document.

    {"ok", "manifest", "yaml", "operations", "reads", "writes"} or {"ok": False,
    "problems"}. The draft lists operations and states no verb.
    """
    kind = re.sub(r"[^a-z0-9_]", "_", str(kind or "").strip().lower()).strip("_")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
        return {"ok": False, "problems": ["kind: a short lower-case name for this kind of controller, e.g. metro"]}
    operations, what = operations_in(text)
    if not operations:
        return {"ok": False, "kind": kind, "problems": [
            "no operation could be read out of this document. An operation is found where a method stands beside "
            "a path (`GET /api/devices`, or a table with Method and URI). Upload the page of the guide that lists "
            "the API, or an OpenAPI document, or write the manifest by hand from the template."]}
    reads = sum(1 for op in operations if not op["writes"])
    doc: Dict[str, Any] = {
        "manifest": FORMAT, "kind": kind, "title": title or kind, "version": "draft",
        "about": (f"Drafted from {what}{' (' + name + ')' if name else ''}: {len(operations)} operations were "
                  f"read out of it, {reads} that read and {len(operations) - reads} that change something. "
                  f"Nothing here was written by a person who knows this controller."),
        "recognise": {"operation_contains": _mark(operations)},
        "operations": [{"method": op["method"], "path": op["path"],
                        **({"summary": op["summary"]} if op.get("summary") else {}),
                        **({"body": True} if op.get("body") else {}), "writes": bool(op["writes"])}
                       for op in operations],
        "guide": ("This manifest is a draft. It lists the controller's operations and states no verb, so at "
                  "onboarding each verb is matched to an operation by name, every read is tried on the "
                  "controller, and every write waits for a person to confirm it. To finish it, state the verbs: "
                  "the operation, its arguments, the body to send and how to read the answer."),
    }
    problems = validate(doc)
    if problems:
        return {"ok": False, "kind": kind, "problems": problems}
    import yaml
    return {"ok": True, "kind": kind, "manifest": doc, "operations": len(operations), "reads": reads,
            "writes": len(operations) - reads, "read_as": what,
            "yaml": yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)}


def install_guide(data: Any, kind: str, title: str = "", name: str = "", folder: str = "",
                  replace: bool = False) -> Dict[str, Any]:
    """Draft a manifest from an uploaded user guide and keep it, unless a
    manifest a person wrote for that kind is already there."""
    try:
        text = text_of(data, name)
    except ValueError as exc:
        return {"ok": False, "problems": [str(exc)]}
    got = draft_from_guide(text, kind, title, name)
    if not got["ok"]:
        return got
    held = get(got["kind"], folder)
    if held and str(held.get("version")) != "draft" and not replace:
        return {"ok": False, "kind": got["kind"], "yaml": got["yaml"], "problems": [
            f"there is already a manifest for {got['kind']} that is not a draft; it is kept. "
            f"Pass replace to put this draft in its place"]}
    kept = install_text(got["yaml"], f"{got['kind']}.yaml", folder)
    return {**kept, **{k: got[k] for k in ("operations", "reads", "writes", "read_as", "yaml")}, "draft": True,
            "next": (f"onboard a controller of this kind (kind {got['kind']}); then confirm each write it "
                     f"should make, or state the verbs in the manifest and upload it again")}


SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.schema.json")


def sync_schema(path: str = SCHEMA_FILE) -> int:
    """Rewrite the published schema's verb lists from contract.json, the one
    place a verb is defined. A list counts as a verb list when every name in it
    is a verb of the contract. Returns how many lists changed."""
    import json as _json
    from .verbs import VERBS
    with open(path) as fh:
        schema = _json.load(fh)
    verbs, changed = list(VERBS), 0

    def walk(node):
        nonlocal changed
        if isinstance(node, dict):
            for key, value in node.items():
                if (key == "enum" and isinstance(value, list) and len(value) >= 10
                        and all(isinstance(v, str) and v in VERBS for v in value) and value != verbs):
                    node[key] = verbs
                    changed += 1
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(schema)
    if changed:
        with open(path, "w") as fh:
            _json.dump(schema, fh, indent=1)
            fh.write("\n")
    return changed

