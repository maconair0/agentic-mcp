"""
The verbs every controller is asked in, its domain, and which of its
operations serves which verb.

Whatever uses a controller should not need its own names. It asks for one of
the verbs of the contract (contract.json), and the controller's manifest says
which operation that is.

The mapping is a ranking by name, and a name is evidence, not proof. So a read
is tried against the controller before it is trusted, and a write is never
trusted from its name alone: it stays "needs confirmation" until a person has
looked at it. `candidates` keeps the runners-up, because the first by name has
been the wrong one before.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Tuple

CONTRACT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contract.json")


def load_contract(path: str = CONTRACT_FILE) -> Dict[str, Any]:
    with open(path) as fh:
        return json.load(fh)


CONTRACT = load_contract()
# verb: what it is for, the methods it may use, name fragments for and against.
VERBS: Dict[str, Dict[str, Any]] = {
    verb: {**rule, "domain": dom["name"], "writes": bool(rule.get("writes")),
           "methods": tuple(rule["methods"])}
    for dom in CONTRACT["domains"] for verb, rule in dom["verbs"].items()}

DOMAIN_WORDS: Dict[str, Tuple[str, ...]] = {
    "optical": ("openroadm", "roadm", "optical", "spectrum", "wavelength", "transponder",
                "osnr", "otn", "lightpath", "photonic", "olm"),
    "packet": ("openflow", "flow", "intent", "vlan", "switch", "hosts", "mac", "ipv4",
               "meter", "l2vpn", "l3vpn"),
}
# A layer counts when it has this share of all the evidence found.
SHARE = 0.2


def domain(operations: Iterable[Dict[str, Any]], modules: Iterable[str] = ()) -> Dict[str, Any]:
    """Which layer this controller works in, from the words in its own API."""
    counts = {kind: {} for kind in DOMAIN_WORDS}
    texts = [op["path"].lower() for op in operations] + [str(m).lower() for m in modules]
    for text in texts:
        for kind, words in DOMAIN_WORDS.items():
            for word in words:
                if word in text:
                    counts[kind][word] = counts[kind].get(word, 0) + 1
    scores = {kind: sum(found.values()) for kind, found in counts.items()}
    total = sum(scores.values())
    present = [k for k, s in scores.items() if total and s / total >= SHARE]
    kind = "unknown" if not present else present[0] if len(present) == 1 else "multi-layer"
    evidence = {k: sorted(found, key=found.get, reverse=True)[:5] for k, found in counts.items() if found}
    return {"kind": kind, "scores": scores, "evidence": evidence}


def score(verb: str, op: Dict[str, Any]) -> float:
    rule = VERBS[verb]
    if op["method"] not in rule["methods"]:
        return 0.0
    if rule["writes"] != bool(op["writes"]) and not rule.get("computes"):
        return 0.0
    text = (op["path"] + "/").lower() if op["path"].endswith(tuple("}")) else op["path"].lower()
    best = max((w for frag, w in rule["for"].items() if frag in text), default=0)
    if not best:
        return 0.0
    tail = text
    best -= 3 * sum(1 for frag in rule["against"] if frag in tail)
    # A list read should need nothing named; a change to one service has to name it.
    wants_params = bool(rule.get("one")) and op["method"] != "POST"
    if not wants_params:
        best -= 1.5 * len(op["params"])
    elif not op["params"]:
        best -= 3
    best -= 0.01 * len(op["path"])
    return max(best, 0.0)


def candidates(verb: str, operations: Iterable[Dict[str, Any]], keep: int = 4) -> List[Dict[str, Any]]:
    ranked = sorted(((score(verb, op), op) for op in operations), key=lambda p: -p[0])
    return [dict(op, score=round(s, 2)) for s, op in ranked[:keep] if s > 0]


def mapping(operations: List[Dict[str, Any]], kind: str = "") -> Dict[str, Dict[str, Any]]:
    """Each verb with the operation ranked first for it and the runners-up."""
    out = {}
    for verb, rule in VERBS.items():
        layers = rule.get("layers")
        if layers and kind != "unknown" and kind not in layers:
            out[verb] = {"status": "not applicable", "writes": False, "candidates": [],
                         "why": f"a {kind} controller is not asked for this"}
            continue
        found = candidates(verb, operations)
        if not found:
            out[verb] = {"status": "missing", "writes": rule["writes"], "candidates": []}
            continue
        first = found[0]
        out[verb] = {"status": "needs confirmation" if first["writes"] else "mapped",
                     "why": rule["why"],
                     "operation": first, "writes": bool(first["writes"]),
                     "confirmed": False, "candidates": found[1:]}
    return out
