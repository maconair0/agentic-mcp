"""
The one place the agentic MCP talks to a language model.

Two things ask a model, and both only for proposals checked afterwards: the
verb a request in words means (agent.py) and a draft at onboarding
(advisor.py). Which model answers is a setting, not code:

  AGENTIC_MCP_MODEL_API       ollama (default), openai or anthropic
  AGENTIC_MCP_MODEL_URL       where it is served; each API has a usual address
  AGENTIC_MCP_MODEL           the model's name
  AGENTIC_MCP_MODEL_KEY_ENV   the NAME of the setting that holds the API key
                              (default OPENAI_API_KEY or ANTHROPIC_API_KEY);
                              the key itself is never written in a file here

"openai" is any server with the OpenAI chat-completions API: OpenAI itself,
and vLLM, LM Studio, llama.cpp's server, LiteLLM or Ollama's own /v1 run
locally. A hosted API receives the request's text; the default stays local.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import requests

API = (os.getenv("AGENTIC_MCP_MODEL_API") or "ollama").strip().lower()
USUAL_URL = {"ollama": os.getenv("OLLAMA_HOST") or "http://localhost:11434",
             "openai": "https://api.openai.com/v1",
             "anthropic": "https://api.anthropic.com"}
USUAL_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
URL = (os.getenv("AGENTIC_MCP_MODEL_URL") or USUAL_URL.get(API, "")).rstrip("/")
KEY_ENV = os.getenv("AGENTIC_MCP_MODEL_KEY_ENV") or USUAL_KEY_ENV.get(API, "")
ANTHROPIC_VERSION = "2023-06-01"


def default_model() -> str:
    """A small local model when Ollama serves; no guess for an API that is paid for."""
    return "qwen2.5:3b-instruct-q4_K_M" if API == "ollama" else ""


def info(model: str) -> Dict[str, Any]:
    out = {"api": API, "name": model or "(none set: AGENTIC_MCP_MODEL)", "at": URL}
    if KEY_ENV:
        out["key"] = f"${KEY_ENV} " + ("set" if os.getenv(KEY_ENV) else "NOT set")
    return out


def parse(text: str) -> Any:
    """JSON out of what a model said: without code fences, with a bare $placeholder
    quoted (the commonest slip), and the first {...} when there is prose around it."""
    text = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.M).strip()
    text = re.sub(r":\s*(\$\w+)", r': "\1"', text)
    try:
        return json.loads(text)
    except ValueError:
        found = re.search(r"\{.*\}", text, re.S)
        if not found:
            raise
        return json.loads(found.group(0))


def chat_json(prompt: str, model: str, timeout: float = 60.0, max_tokens: Optional[int] = None,
              ollama_options: Optional[Dict[str, Any]] = None) -> Any:
    """One question, one JSON answer. Raises when no model answers or the answer
    is not JSON; callers treat that as "not understood", never as a decision."""
    if not model:
        raise RuntimeError("no model is set (AGENTIC_MCP_MODEL)")
    messages = [{"role": "user", "content": prompt}]
    if API == "ollama":
        got = requests.post(f"{URL}/api/chat", timeout=timeout, json={
            "model": model, "stream": False, "format": "json", "keep_alive": "30m",
            "options": {"temperature": 0, **({"num_predict": max_tokens} if max_tokens else {}),
                        **(ollama_options or {})},
            "messages": messages})
        got.raise_for_status()
        return parse(got.json()["message"]["content"])
    key = os.getenv(KEY_ENV, "") if KEY_ENV else ""
    if API == "openai":
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {"model": model, "messages": messages, "temperature": 0,
                "response_format": {"type": "json_object"}}
        got = requests.post(f"{URL}/chat/completions", headers=headers, json=body, timeout=timeout)
        if got.status_code == 400:            # a server without JSON mode: ask again in plain text
            body.pop("response_format")
            got = requests.post(f"{URL}/chat/completions", headers=headers, json=body, timeout=timeout)
        got.raise_for_status()
        return parse(got.json()["choices"][0]["message"]["content"])
    if API == "anthropic":
        if not key:
            raise RuntimeError(f"${KEY_ENV} is not set")
        got = requests.post(f"{URL}/v1/messages", timeout=timeout, json={
            "model": model, "max_tokens": max(max_tokens or 0, 1024), "temperature": 0,
            "system": "Answer with one JSON object and nothing else.", "messages": messages},
            headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
        got.raise_for_status()
        return parse("".join(b.get("text", "") for b in got.json()["content"] if b.get("type") == "text"))
    raise RuntimeError(f"AGENTIC_MCP_MODEL_API={API!r}: use ollama, openai or anthropic")
