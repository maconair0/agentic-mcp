"""
What the model is asked, and how far its answer is trusted.

Tried on four real controllers (12 October 2026), a local model was:

  right   telling a read from a write by a documentation page's own titles
          (the two POSTs of one controller that only read, out of seventeen);
  right   drafting a request body for a small, flat schema;
  wrong   choosing which operation holds a controller's free spectrum, with
          0.95 confidence, and drafting a body for a deep schema (OpenROADM's
          service-create), where the draft looks right and is not.

So it proposes and never decides. A draft it writes is checked against the
controller's published schema by the dry run, shown to a person, and kept only
when that person asks for it. Nothing at onboarding waits for a model.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List

import requests

HOST = (os.getenv("AGENTIC_MCP_MODEL_URL") or os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
# Drafting a body is harder than choosing a verb: a larger model may be named
# for it, and without one the agentic MCP's own small model is asked.
MODEL = (os.getenv("AGENTIC_MCP_DRAFT_MODEL") or os.getenv("CONNECTOR_MODEL") or os.getenv("CENTRAL_MODEL")
         or os.getenv("AGENTIC_MCP_MODEL") or "qwen2.5:3b-instruct-q4_K_M")


def ask(prompt: str, timeout: float = 240.0) -> Any:
    """The model's answer as JSON, or None when it gave none that parses."""
    try:
        got = requests.post(f"{HOST}/api/chat", timeout=timeout, json={
            "model": MODEL, "stream": False, "format": "json",
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "user", "content": prompt}]}).json()
        text = got["message"]["content"]
    except Exception:  # noqa: BLE001 - no model is a normal state here
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    # A placeholder left bare ("rate": $rate) is the commonest slip.
    text = re.sub(r":\s*(\$\w+)", r': "\1"', text)
    try:
        return json.loads(text)
    except ValueError:
        return None


def draft_template(operation: Dict[str, Any], arguments: Iterable[str]) -> Dict[str, Any]:
    """A body template for one operation, drafted from its published schema."""
    schema = operation.get("body_schema")
    if not schema:
        return {"ok": False, "error": "the controller publishes no schema for this operation's body"}
    names = ", ".join(f'"${a}"' for a in arguments)
    got = ask(f"An SDN controller's operation {operation['id']} ({operation.get('summary', '')}) takes a "
              f"request body that must follow this JSON schema:\n{json.dumps(schema)[:5000]}\n\n"
              f"Write a body TEMPLATE. Use the placeholders {names} as values where those arguments go. "
              f"Use only fields the schema has, and leave out optional fields you have no value for. "
              f'Answer JSON: {{"template": {{...}}, "unsure_about": ["..."]}}')
    if not isinstance(got, dict) or not isinstance(got.get("template"), dict):
        return {"ok": False, "error": "the model gave no usable draft"}
    return {"ok": True, "template": got["template"], "unsure_about": got.get("unsure_about") or [],
            "by": f"model {MODEL}"}


def reads_among(operations: List[Dict[str, Any]]) -> List[str]:
    """Which non-GET operations only read, going by their documented titles.

    A proposal. An operation named here is still treated as a write until a
    person confirms it.
    """
    rows = [o for o in operations if o["method"] != "GET" and o.get("summary")]
    if not rows:
        return []
    got = ask("For each operation of a controller's REST API, say whether calling it CHANGES the network "
              '(write) or only READS it. Answer JSON: {"<operation>": "read"|"write"}\n'
              + "\n".join(f"{o['id']} - {o['summary']}" for o in rows))
    known = {o["id"] for o in rows}
    return sorted(k for k, v in (got or {}).items() if v == "read" and k in known) if isinstance(got, dict) else []
