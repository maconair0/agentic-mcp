"""
The agentic MCP: the controller desk as a service of its own.

One process that every SDN controller is onboarded to and that anything else
talks to: an agent, a planner, an operator's console. It keeps its own
manifests, its own sessions, its own audit log and its own small model, and
knows nothing about what uses it.

Two ways in:

  HTTP, JSON      /controllers, /controllers/<key>/call, /manifests, /guides, /ask ...
  MCP, over SSE   /mcp/sse, for a client that speaks the Model Context
                  Protocol: every controller's tools under one server, plus
                  tools to list, onboard and ask.

    python -m agentic_mcp serve --port 8400

A write is refused unless the mapping behind it is confirmed and the request
names who approved it. With AGENTIC_MCP_TOKEN set, anything that is not a read
of this service also needs that token, and so does the MCP endpoint.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import agent as _agent
from . import contract
from .desk import Desk

STARTED = time.time()


def create_app(desk: Optional[Desk] = None) -> FastAPI:
    desk = desk or Desk()
    token = os.getenv("AGENTIC_MCP_TOKEN", "").strip()
    app = FastAPI(title="Agentic MCP", version="1")
    app.state.desk = desk

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Reading the service is open; changing anything is not, and neither is
        # the MCP endpoint, through which every controller's tools are called.
        if token and (request.method != "GET" or request.url.path.startswith("/mcp")):
            if request.headers.get("authorization", "") != f"Bearer {token}":
                return PlainTextResponse("this needs the agentic MCP's token", status_code=401)
        return await call_next(request)

    def seat_or_404(key: str):
        try:
            return desk.seat(key)
        except LookupError as exc:
            raise HTTPException(404, str(exc))

    # ----- itself -----

    @app.get("/health")
    def health():
        return {"ok": True, "service": "agentic-mcp", "uptime_s": round(time.time() - STARTED),
                "controllers": desk.keys(), "kinds": [k["kind"] for k in desk.kinds()],
                "model": _agent.model_info(), "mcp": getattr(app.state, "mcp", "")}

    @app.get("/topology")
    def topology():
        """Itself and every controller it holds, for whoever draws the picture."""
        rows = []
        for key in desk.keys():
            try:
                d = desk.describe(key)
                why = desk.reachable(key)
            except Exception as exc:  # noqa: BLE001
                d, why = {"key": key, "label": key}, str(exc)
            rows.append({**{k: v for k, v in d.items() if k != "conformance"},
                         "verdict": (d.get("conformance") or {}).get("verdict", ""),
                         "reachable": not why, "why": why})
        return {"agent": {"name": "Agentic MCP", "model": _agent.model_info(),
                          "kinds": len(desk.kinds())}, "controllers": rows}

    @app.get("/queue")
    def queue(controller: str = ""):
        """Writes waiting for a person's approval."""
        return {"pending": desk.pending(controller)}

    # ----- manifests: what each kind of controller is -----

    @app.get("/manifests")
    def kinds():
        return {"kinds": desk.kinds()}

    @app.post("/manifests")
    async def upload(request: Request, name: str = "manifest.yaml"):
        """A controller manifest, as the body of the request (YAML or JSON)."""
        form_type = request.headers.get("content-type", "")
        if form_type.startswith("multipart/"):
            form = await request.form()
            out = []
            for field in form.values():
                if hasattr(field, "read"):
                    text = (await field.read()).decode("utf-8", "replace")
                    out.append(desk.install_manifest(text, getattr(field, "filename", "") or name))
            return {"installed": out}
        text = (await request.body()).decode("utf-8", "replace")
        return {"installed": [desk.install_manifest(text, name)]}

    @app.post("/guides")
    async def upload_guide(request: Request, kind: str, title: str = "", name: str = "", replace: bool = False):
        """A controller's user guide or API document, as the body of the request
        (text, Markdown, HTML, an OpenAPI document, or a PDF). A manifest is
        drafted from it in the standard form and kept for that kind."""
        data: Any = await request.body()
        if request.headers.get("content-type", "").startswith("multipart/"):
            form = await request.form()
            field = next((f for f in form.values() if hasattr(f, "read")), None)
            if field is None:
                raise HTTPException(400, "no file in the upload")
            data, name = await field.read(), name or getattr(field, "filename", "") or ""
        return desk.install_guide(data, kind, title, name, replace=replace)

    @app.get("/manifests/{kind}/guide", response_class=PlainTextResponse)
    def guide(kind: str):
        text = desk.guide(kind)
        if not text:
            raise HTTPException(404, f"no manifest for a controller of kind {kind}")
        return text

    # ----- controllers -----

    @app.get("/controllers")
    def controllers():
        out = []
        for key in desk.keys():
            try:
                out.append(desk.describe(key))
            except Exception as exc:  # noqa: BLE001
                out.append({"key": key, "error": str(exc)})
        return {"controllers": out}

    @app.post("/controllers")
    def onboard(body: Dict[str, Any] = Body(...)):
        if not body.get("key") or not body.get("url"):
            raise HTTPException(400, "onboarding needs a key and the controller's url")
        return desk.onboard(body["key"], body["url"], label=body.get("label", ""),
                            user_env=body.get("user_env", ""), password_env=body.get("password_env", ""),
                            spec=body.get("spec", ""), docs=body.get("docs") or [],
                            kind=body.get("kind", ""))

    @app.get("/controllers/{key}")
    def controller(key: str):
        seat = seat_or_404(key)
        return {"manifest": seat.manifest, "conformance": contract.check(seat.manifest),
                "summary": desk.summary(key), "reachable": desk.reachable(key)}

    @app.delete("/controllers/{key}")
    def remove(key: str):
        return {"removed": desk.remove(key)}

    @app.get("/controllers/{key}/tools")
    def tools(key: str):
        seat_or_404(key)
        return {"tools": desk.tools(key)}

    @app.post("/controllers/{key}/call")
    def call(key: str, body: Dict[str, Any] = Body(...)):
        seat_or_404(key)
        return desk.call(key, body.get("tool", ""), body.get("arguments") or {},
                         approved_by=str(body.get("approved_by") or ""))

    @app.post("/controllers/{key}/{what}")
    def verb(key: str, what: str, body: Dict[str, Any] = Body(...)):
        seat_or_404(key)
        args = body.get("arguments") or {}
        try:
            if what == "read":
                return desk.read(key, body["verb"], body.get("body"), **args)
            if what == "dry-run":
                return desk.dry_run(key, body["verb"], body.get("body"), **args)
            if what == "write":
                return desk.write(key, body["verb"], body.get("body"),
                                  approved_by=str(body.get("approved_by") or ""), **args)
            if what == "confirm":
                return desk.confirm(key, body["verb"], str(body.get("by") or ""), body.get("operation", ""))
        except (LookupError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))
        raise HTTPException(404, f"nothing called {what}")

    # ----- asked in words -----

    @app.post("/ask")
    def ask(body: Dict[str, Any] = Body(...)):
        return _agent.ask(desk, str(body.get("text") or ""), confirm=bool(body.get("confirm")),
                          approved_by=str(body.get("approved_by") or ""))

    _mount_mcp(app, desk)
    return app


def _mount_mcp(app: FastAPI, desk: Desk) -> None:
    """The same desk, for a client that speaks MCP."""
    try:
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.sse import SseServerTransport
        from starlette.responses import Response
        from starlette.routing import Mount, Route
    except Exception:  # noqa: BLE001 - the MCP package is optional; HTTP is not
        app.state.mcp = "not served: the mcp package is not installed"
        return
    if not hasattr(Server, "list_tools") or not hasattr(Server, "call_tool"):
        # A release of the package with another server API. The service is
        # still everything it is over HTTP; it says so instead of not starting.
        import logging
        app.state.mcp = "not served: this was written for mcp 1.x (pip install 'mcp>=1.2,<2')"
        logging.getLogger("agentic_mcp.service").warning("/mcp/sse is %s", app.state.mcp)
        return
    app.state.mcp = "/mcp/sse"
    server = Server("agentic-mcp")
    sse = SseServerTransport("/mcp/messages/")
    OWN = {
        "desk_controllers": ("Every controller onboarded here, with its domain and how it fares "
                             "against the contract.", {}),
        "desk_ask": ("Ask about the controllers in words, e.g. 'list the devices on onos'. A write is "
                     "answered with a dry run unless confirm is true and approved_by names a person.",
                     {"text": {"type": "string"}, "confirm": {"type": "boolean"},
                      "approved_by": {"type": "string"}}),
        "desk_onboard": ("Onboard a controller from its own API: key, url, and the names of the "
                         "settings holding its credentials. kind names its controller manifest, for one "
                         "that publishes nothing it could be recognised by.",
                         {"key": {"type": "string"}, "url": {"type": "string"}, "kind": {"type": "string"},
                          "user_env": {"type": "string"}, "password_env": {"type": "string"}}),
        "desk_kinds": ("Every kind of controller there is a manifest for.", {}),
    }

    def public(key: str, name: str) -> str:
        return name if name.startswith(key + "_") else f"{key}__{name}"

    @server.list_tools()
    async def list_tools() -> List[Any]:
        out = [types.Tool(name=n, description=d, inputSchema={"type": "object", "properties": p})
               for n, (d, p) in OWN.items()]
        for key in desk.keys():
            try:
                tools = desk.tools(key)
            except Exception:  # noqa: BLE001
                continue
            for t in tools:
                schema = dict(t["schema"] or {"type": "object", "properties": {}})
                if t["write"]:
                    schema = {**schema, "properties": {**(schema.get("properties") or {}),
                              "approved_by": {"type": "string", "description": "who approved this change"}}}
                out.append(types.Tool(name=public(key, t["name"]), inputSchema=schema,
                                      description=f"[{key}] {t['description']}"[:1000]))
        return out

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]) -> List[Any]:
        import asyncio
        arguments = dict(arguments or {})

        def run() -> str:
            if name == "desk_controllers":
                return json.dumps([{k: v for k, v in desk.describe(key).items() if k != "conformance"}
                                   | {"verdict": desk.describe(key)["conformance"]["verdict"]}
                                   for key in desk.keys()], indent=1)
            if name == "desk_kinds":
                return json.dumps(desk.kinds(), indent=1)
            if name == "desk_ask":
                return json.dumps(_agent.ask(desk, arguments.get("text", ""), bool(arguments.get("confirm")),
                                             arguments.get("approved_by", "")), indent=1, default=str)
            if name == "desk_onboard":
                found = desk.onboard(arguments["key"], arguments["url"], kind=arguments.get("kind", ""),
                                     user_env=arguments.get("user_env", ""),
                                     password_env=arguments.get("password_env", ""))
                return desk.summary(found["key"])
            by = str(arguments.pop("approved_by", "") or "")
            for key in desk.keys():
                for t in desk.tools(key):
                    if public(key, t["name"]) == name:
                        return desk.call(key, t["name"], arguments, approved_by=by)["text"]
            return f"Unknown tool: {name}"

        return [types.TextContent(type="text", text=await asyncio.to_thread(run))]

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:  # noqa: SLF001
            await server.run(streams[0], streams[1], server.create_initialization_options())
        return Response()

    app.router.routes.append(Route("/mcp/sse", endpoint=handle_sse))
    app.router.routes.append(Mount("/mcp/messages/", app=sse.handle_post_message))


def serve(host: str = "127.0.0.1", port: int = 8400) -> None:
    import logging

    import uvicorn
    # What the service does by itself (a replay, a controller that stopped
    # answering) is said in its log; each request is not.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
