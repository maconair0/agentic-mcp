"""
Is this controller complete enough to be run through?

The contract (contract.json) splits a controller's work into domains: inventory,
topology, services, path computation, resources, provisioning, adjustment,
health, device configuration. Each domain lists verbs. At onboarding the
controller's manifest is checked against it, domain by domain.

A controller is accepted when every required domain that applies to its layer
has at least one verb it can serve. That is deliberately not "every verb": a
controller with no in-place reconfigure is still one a service can be built
and removed on, and the report says which verbs are absent so nothing plans
around one that is not there.

A verb counts as served when the manifest maps it to an operation, when the
kind's controller manifest declares it, or when a tool of that kind provides
it (the manifest's `provides`).
What is served but not yet proven is listed separately: a read that needs
arguments nobody has given, a write nobody has confirmed.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import discover
from .verbs import CONTRACT

SERVED = ("verified", "declared", "mapped", "answered empty", "needs confirmation", "confirmed")
PROVEN = ("verified", "confirmed", "declared")


def _provides(manifest: Dict[str, Any]) -> Dict[str, str]:
    name = manifest.get("family") or ""
    family = next((f for f in discover.families() if f.get("family") == name), {}) if name else {}
    return dict(family.get("provides") or {})


def check(manifest: Dict[str, Any]) -> Dict[str, Any]:
    provided = _provides(manifest)
    verbs = manifest.get("verbs") or {}
    domains: List[Dict[str, Any]] = []
    for dom in CONTRACT["domains"]:
        features: Dict[str, str] = {}
        for verb in dom["verbs"]:
            status = (verbs.get(verb) or {}).get("status", "missing")
            if status == "not applicable":
                continue
            if verb in provided:
                features[verb] = f"tool {provided[verb]}"
            elif status in SERVED:
                features[verb] = status
            else:
                features[verb] = "missing"
        served = [v for v, s in features.items() if s != "missing"]
        state = ("not applicable" if not features else "covered" if len(served) == len(features)
                 else "partly" if served else "missing")
        domains.append({"domain": dom["name"], "required": bool(dom["required"]), "why": dom["why"],
                        "status": state, "features": features})
    lacking = [d["domain"] for d in domains if d["required"] and d["status"] == "missing"]
    gaps = [f"{d['domain']}: no {verb.replace('_', ' ')}" for d in domains
            for verb, s in d["features"].items() if s == "missing"]
    unproven = [f"{verb.replace('_', ' ')} ({s})" for d in domains for verb, s in d["features"].items()
                if s in SERVED and s not in PROVEN]
    if lacking:
        verdict = "not accepted"
    elif gaps:
        verdict = "accepted, with gaps"
    else:
        verdict = "accepted"
    return {"verdict": verdict, "contract_version": CONTRACT["version"], "lacking": lacking,
            "gaps": gaps, "unproven": unproven, "domains": domains,
            # Without provisioning a controller can be watched and not built on.
            "can_provision": not lacking and any(
                d["domain"] == "provisioning" and d["status"] != "missing" for d in domains)}


def text(report: Dict[str, Any]) -> str:
    lines = [f"Contract v{report['contract_version']}: {report['verdict'].upper()}"]
    if report["lacking"]:
        lines.append("  Required and absent: " + ", ".join(report["lacking"]))
    for d in report["domains"]:
        if d["status"] == "not applicable":
            continue
        need = "required" if d["required"] else "optional"
        feats = ", ".join(f"{v.replace('_', ' ')}: {s}" for v, s in d["features"].items())
        lines.append(f"  {d['domain']:<21} {need:<9} {d['status']:<8} {feats}")
    if report["unproven"]:
        lines.append("  Served but not yet proven: " + "; ".join(report["unproven"]))
    return "\n".join(lines)
