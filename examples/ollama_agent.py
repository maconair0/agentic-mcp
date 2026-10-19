"""
An agent of your own on the agentic MCP: a model with tool calling, whose tools
are every controller's, reached over MCP (streamable HTTP).

    python examples/ollama_agent.py "which devices does metro hold?"

The model is served by Ollama here (AGENT_MODEL, default qwen3:30b-a3b-instruct-2507-q4_K_M;
any Ollama model that does tool calling). Settings:

    AGENTIC_MCP_URL     the service's MCP address (http://localhost:8400/mcp)
    AGENTIC_MCP_TOKEN   its token, when it has one
    OLLAMA_HOST         where Ollama is (http://localhost:11434)
    AGENT_TOOLS         a regular expression: only the tools it matches are offered,
                        for a small model that cannot take a long list

A write tool is refused by the service unless approved_by names a person, so
the model can ask for a change and never make one alone.
"""
import asyncio
import json
import os
import re
import sys

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.getenv("AGENTIC_MCP_URL", "http://localhost:8400/mcp")
OLLAMA = (os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
MODEL = os.getenv("AGENT_MODEL", "qwen3:30b-a3b-instruct-2507-q4_K_M")
ONLY = re.compile(os.getenv("AGENT_TOOLS", ".*"))
SYSTEM = ("You operate SDN controllers through the tools you are given. Read what you need before you answer, "
          "and answer from what the tools returned. Never set approved_by yourself: only a person approves a change.")


async def main(question: str) -> None:
    token = os.getenv("AGENTIC_MCP_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = [t for t in (await mcp.list_tools()).tools if ONLY.search(t.name)]
            offered = [{"type": "function", "function": {"name": t.name, "description": (t.description or "")[:500],
                                                         "parameters": t.inputSchema}} for t in tools]
            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
            for _ in range(8):
                said = requests.post(f"{OLLAMA}/api/chat", timeout=600, json={
                    "model": MODEL, "messages": messages, "tools": offered, "stream": False,
                    "options": {"temperature": 0}}).json()["message"]
                messages.append(said)
                if not said.get("tool_calls"):
                    print(said.get("content", ""))
                    return
                for call in said["tool_calls"]:
                    name, arguments = call["function"]["name"], call["function"].get("arguments") or {}
                    print(f"-> {name} {json.dumps(arguments)}", file=sys.stderr)
                    got = await mcp.call_tool(name, arguments)
                    text = "".join(getattr(part, "text", "") for part in got.content)
                    messages.append({"role": "tool", "tool_name": name, "content": text[:8000]})
            print("(stopped after 8 rounds of tool calls)")


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "Which controllers are there, and what devices do they hold?"))
