"""
Asked in words: the agentic MCP's own small model, and what it is trusted with.

A request such as "which lightpaths does transportpce hold" has to become a
controller, a verb of the contract and its arguments. Most requests say all
three plainly and are read by rule, with no model at all. The model is asked
only for the rest, and then only to choose: from the controllers that are
onboarded and from the verbs of the contract, as JSON. Whatever it answers is
checked against those two lists before it is used, so a wrong answer is a
request not understood and never a call nobody asked for.

The model is small on purpose (AGENTIC_MCP_MODEL, served by Ollama at
AGENTIC_MCP_MODEL_URL): it has to answer in about a second on a CPU, and
choosing one of twenty is a task a small model does well. It never decides a
write. A write asked for in words is answered with a dry run, and is sent only
when the request says confirm and names who approved it.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .session import rows as find_rows
from .verbs import VERBS

MODEL_URL = (os.getenv("AGENTIC_MCP_MODEL_URL") or os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
MODEL = os.getenv("AGENTIC_MCP_MODEL", "qwen2.5:3b-instruct-q4_K_M")
# -1 lets Ollama use a GPU when there is one; 0 keeps the model on the CPU.
MODEL_GPU_LAYERS = int(os.getenv("AGENTIC_MCP_MODEL_GPU", "-1"))
OWN = ("controllers", "describe")

# The words a request uses for each verb, most telling first.
WORDS: List[Tuple[str, str]] = [
    ("onboard", r"\bonboard\b"),
    ("teardown", r"\b(delete|remove|tear ?down|withdraw|release)\b"),
    ("provision", r"\b(create|provision|build|set ?up|add|map)\b.*\b(service|lightpath|circuit|flow|intent|vlan|connection)\b"),
    ("reroute", r"\b(reroute|re-route|restore)\b"),
    ("adjust", r"\b(change|update|modify|adjust|reconfigure)\b.*\b(service|lightpath|circuit|flow|intent)\b"),
    ("configure_device", r"\b(configure|set)\b.*\b(device|port|transponder|roadm|switch)\b"),
    ("compute_path", r"\b(path|route)\b.*\b(between|from)\b|\bcompute\b.*\bpath\b"),
    ("check_feasibility", r"\bfeasib"),
    ("read_spectrum", r"\b(spectrum|wavelengths?|frequenc\w+|channels?|slots?)\b"),
    ("read_statistics", r"\b(statistics|counters|traffic|packets|bytes)\b"),
    ("read_alarms", r"\b(alarms?|alerts?)\b"),
    ("read_hosts", r"\bhosts?\b"),
    ("read_ports", r"\bports?\b"),
    ("read_services", r"\b(services?|intents?|flows?|lightpaths?|circuits?|connections?)\b"),
    ("read_links", r"\b(links?|topology|connected|adjacenc\w+|fib(er|re)s?)\b"),
    ("read_devices", r"\b(devices?|switch(es)?|nodes?|routers?|transponders?|roadms?|inventory|equipment)\b"),
    ("health", r"\b(health\w*|alive|reachable|is .* (up|down|working)|status)\b"),
    ("describe", r"\b(manifest|contract|conformance|learn\w*|capabilit\w+|what can)\b"),
    ("controllers", r"\b(which|what|list|show)\b.*\bcontrollers\b"),
]


def model_info() -> Dict[str, Any]:
    return {"name": MODEL, "at": MODEL_URL, "on": "cpu" if MODEL_GPU_LAYERS == 0 else "gpu when there is one"}


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def controllers_named(text: str, known: Dict[str, Dict[str, str]]) -> List[str]:
    """The onboarded controllers a request names, by key, label or kind."""
    flat = _plain(text)
    out = []
    for key, names in known.items():
        if any(n and _plain(n) in flat for n in (key, names.get("label", ""), names.get("family", ""))):
            out.append(key)
    return out


def arguments(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, value in re.findall(r"([\w\-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,]+)", text):
        value = value.strip("\"'")
        out[name] = int(value) if re.fullmatch(r"-?\d+", value) else value
    return out


def by_rule(text: str, known: Dict[str, Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """What a request asks, when it says so plainly. None when it does not."""
    t = text.lower()
    hits = [verb for verb, pattern in WORDS if re.search(pattern, t)]
    if not hits:
        return None
    # A write word outranks the noun beside it ("delete the service" reads
    # services too); between two reads the request is not plain.
    writes = [v for v in hits if v == "onboard" or (VERBS.get(v) or {}).get("writes")]
    special = [v for v in hits if v in ("compute_path", "check_feasibility", "describe", "controllers")]
    reads = [v for v in hits if v not in writes and v not in special]
    if writes:
        action = writes[0]
    elif special:
        action = special[0]
    elif len(reads) == 1:
        action = reads[0]
    else:
        return None
    named = controllers_named(text, known)
    everywhere = bool(re.search(r"\b(all|every|each|any)\b.*\bcontrollers?\b|\bcontrollers\b|\beverywhere\b", t))
    if len(named) > 1:
        return None
    if not named and action != "controllers":
        # No controller named: "the edge switches", "the optical core". Which
        # controller that is takes knowing what each one runs, so it is not
        # plain, unless the request is about all of them.
        if not everywhere or (VERBS.get(action) or {}).get("writes"):
            return None
    return {"controller": named[0] if named else "all", "action": action, "arguments": arguments(text)}


# What each action is, in the words a request would use. For the model only.
HELP = {
    "read_devices": "the equipment itself: devices, boxes, inventory, switches, routers, transponders, ROADMs",
    "read_device": "one named device in detail",
    "read_ports": "the list of ports of one named device",
    "read_links": "how devices are connected: links, fibres, wiring, topology",
    "read_hosts": "end hosts or computers attached to the switches",
    "read_services": "what is provisioned: services, circuits, lightpaths, intents, flows, what was set up",
    "read_service": "one named service in detail",
    "compute_path": "a route between two named ends",
    "check_feasibility": "whether a NEW service between two named ends would fit",
    "read_spectrum": "free wavelengths, frequencies, channels, colours of light",
    "read_statistics": "traffic counters: how busy, load, packets, bytes",
    "provision": "create a new service",
    "teardown": "remove a service",
    "adjust": "change an existing service",
    "reroute": "move a service to another path",
    "health": "is the controller itself reachable, answering, alive, working",
    "read_alarms": "alarms, faults, anything complaining",
    "configure_device": "change a setting on one device",
}

EXAMPLES = (
    ("what boxes does it have", "read_devices"), ("how are they wired together", "read_links"),
    ("what has been set up so far", "read_services"), ("anything complaining", "read_alarms"),
    ("how busy are the ports", "read_statistics"), ("what is plugged into the switches", "read_hosts"),
    ("which colours of light are unused", "read_spectrum"), ("can it still be talked to", "health"),
    ("tell me about that controller", "describe"), ("take the circuit away", "teardown"),
)


def _say(prompt: str, timeout: float) -> Any:
    got = requests.post(f"{MODEL_URL}/api/chat", timeout=timeout, json={
        "model": MODEL, "stream": False, "format": "json", "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 40,
                    **({"num_gpu": 0} if MODEL_GPU_LAYERS == 0 else {})},
        "messages": [{"role": "user", "content": prompt}]}).json()
    return json.loads(got["message"]["content"])


def by_model(text: str, known: Dict[str, Dict[str, str]], timeout: float = 60.0) -> Optional[Dict[str, Any]]:
    """What a request asks, chosen by the small model from closed lists.

    Two small questions and not one large one: which action, then which
    controller. A model of this size answers each well and both at once badly.
    """
    actions = list(VERBS) + list(OWN)
    listed = "\n".join(f"- {v}: {HELP.get(v) or (VERBS.get(v) or {}).get('why') or v}" for v in VERBS)
    shots = "\n".join(f'Request: {q}\n{{"action": "{a}"}}' for q, a in EXAMPLES)
    try:
        said = _say(f"Pick the ONE action a request about a network controller asks for.\nActions:\n{listed}\n"
                    f"- controllers: list which controllers there are\n"
                    f"- describe: everything known about one controller, what it can do\n\n"
                    f"{shots}\nRequest: {text}\n", timeout)
        action = str(said.get("action") or "").strip().lower().replace(" ", "_")
        if action not in actions:
            return None
        named = controllers_named(text, known)
        if len(named) == 1:
            controller = named[0]
        else:
            roster = "\n".join(f"- {k}: {n.get('label', k)}, {n.get('about') or n.get('domain') or 'a controller'}"
                               for k, n in known.items())
            said = _say(f"Pick the ONE controller a request is about, or all when it names none.\n"
                        f"Controllers:\n{roster}\n- all: every controller\n\nRequest: {text}\n"
                        f'Answer with JSON only: {{"controller": "<key>"}}', timeout)
            controller = str(said.get("controller") or "all").strip().lower()
            if controller not in known:
                match = controllers_named(controller, known)
                controller = match[0] if len(match) == 1 else "all"
    except Exception:  # noqa: BLE001 - no model is a normal state
        return None
    return {"controller": controller, "action": action, "arguments": arguments(text)}


def understand(text: str, known: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    started = time.time()
    got = by_rule(text, known)
    how = "rule"
    if got is None:
        got, how = by_model(text, known), f"model {MODEL}"
    out = dict(got or {"controller": "", "action": "", "arguments": {}})
    out.update(by=how if got else "nobody", ms=round((time.time() - started) * 1000))
    return out


def _lines(items: List[Any], limit: int = 30) -> List[str]:
    out = []
    for item in items[:limit]:
        if isinstance(item, dict):
            head = [str(item[k]) for k in ("name", "id", "src", "dst", "status", "type", "state") if item.get(k) not in (None, "")]
            out.append("  " + ("  ".join(head) if head else json.dumps(item)[:160]))
        else:
            out.append(f"  {item}")
    if len(items) > limit:
        out.append(f"  ... and {len(items) - limit} more")
    return out


def ask(desk: Any, text: str, confirm: bool = False, approved_by: str = "") -> Dict[str, Any]:
    """Do what a request in words asks of the controllers held here."""
    known = {}
    said = {k["kind"]: k for k in desk.kinds()}
    for key in desk.keys():
        m = desk.manifest(key) or {}
        kind = said.get(m.get("family", ""), {})
        known[key] = {"label": m.get("label", key), "family": m.get("family", ""),
                      "domain": (m.get("domain") or {}).get("kind", ""),
                      "about": (f"{(m.get('domain') or {}).get('kind', '')} domain. "
                                + str(kind.get("about") or "")).strip()[:200]}
    got = understand(text, known)
    action, controller, args = got["action"], got["controller"], got["arguments"]
    out: Dict[str, Any] = {"understood": got, "ok": True}
    if not action:
        out.update(ok=False, answer="I could not tell what that asks of a controller. Name a controller and "
                                    "what you want of it, for example: list the devices on onos.")
        return out
    if action == "controllers" or (action == "describe" and controller == "all"):
        rows = []
        for key in known:
            d = desk.describe(key)
            rows.append(f"  {d['label']}  {d['base_url']}  {d['domain']}  {d['conformance']['verdict']}")
        out["answer"] = f"{len(rows)} controller(s) onboarded:\n" + "\n".join(rows)
        return out
    if action == "describe":
        out["answer"] = desk.summary(controller)
        return out
    if action == "onboard":
        out.update(ok=False, answer="Onboarding is asked for with a name and an address: POST /controllers "
                                    "{key, url}, or 'python -m agentic_mcp onboard <key> --url <address>'.")
        return out
    targets = list(known) if controller == "all" else [controller]
    writes = bool((VERBS.get(action) or {}).get("writes"))
    if writes and len(targets) != 1:
        out.update(ok=False, answer="A change names the one controller it is for.")
        return out
    def one(key):
        parts, data, flag = [], {}, [True]
        for key in [key]:
            label = known[key]["label"]
            entry = ((desk.manifest(key) or {}).get("verbs") or {}).get(action) or {}
            if not entry.get("operation"):
                parts.append(f"{label}: has nothing for {action.replace('_', ' ')}.")
                continue
            try:
                if writes:
                    dry = desk.dry_run(key, action, **args)
                    data[key] = dry
                    shown = [f"{label}, dry run of {action.replace('_', ' ')}:"] + [
                        f"  {r['method']} {r['url']}" + (f"  {json.dumps(r['body'])[:300]}" if r.get("body") is not None else "")
                        for r in dry.get("requests") or []]
                    problems = list(dry.get("problems") or []) + ([dry["error"]] if dry.get("error") else [])
                    if problems:
                        shown.append("  It cannot be sent: " + "; ".join(problems))
                    elif not dry.get("confirmed"):
                        shown.append("  Its mapping has not been confirmed, so it cannot be sent yet.")
                    elif confirm and approved_by.strip():
                        sent = desk.write(key, action, approved_by=approved_by, **args)
                        data[key] = sent
                        shown.append(f"  Sent, approved by {approved_by}: " + (f"answered {sent.get('status')}"
                                     if sent.get("ok") else f"refused: {sent.get('error')}"))
                        flag[0] = bool(sent.get("ok"))
                    else:
                        shown.append("  Nothing was sent. Ask again with confirm and who approved it to send it.")
                    parts.append("\n".join(shown))
                    continue
                read = desk.read(key, action, **args)
                data[key] = read.get("rows", read.get("data"))
                if not read.get("ok"):
                    parts.append(f"{label}: {read.get('error')}")
                    continue
                items = read["rows"] if "rows" in read else find_rows(read.get("data"))
                if action == "health":
                    parts.append(f"{label}: answering.")
                elif "rows" in read and not items:
                    parts.append(f"{label}: none.")
                elif items:
                    parts.append(f"{label}: {len(items)} {action.split('_', 1)[-1]}\n" + "\n".join(_lines(items)))
                else:
                    parts.append(f"{label}: " + (json.dumps(read.get("data"))[:400] if read.get("data") else "nothing"))
            except Exception as exc:  # noqa: BLE001
                parts.append(f"{label}: {exc}")
        return parts, data, flag[0]

    # Each controller is asked on a thread of its own: one that is slow or
    # down is its own line in the answer and does not keep the others waiting.
    from .sessions import each
    parts, data = [], {}
    for key, got in each(targets, one, wait_s=25.0).items():
        if not got["ok"]:
            parts.append(f"{known[key]['label']}: {got['error']}")
            continue
        p, d, fine = got["value"]
        parts += p
        data.update(d)
        if not fine:
            out["ok"] = False
    out.update(answer="\n\n".join(parts), data=data)
    return out
