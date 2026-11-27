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
AGENTIC_MCP_TOKENS adds narrower tokens (access.py): some controllers only,
reads only, no approving. Only an HTTP caller may name who approved a write;
over MCP approved_by is not taken, since the caller there may be a model.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import agent as _agent
from . import access as _access
from . import contract
from .desk import Desk

STARTED = time.time()


def create_app(desk: Optional[Desk] = None) -> FastAPI:
    desk = desk or Desk()
    token = os.getenv("AGENTIC_MCP_TOKEN", "").strip()
    callers = _access.Callers(token, _access.load_tokens())
    app = FastAPI(title="Agentic MCP", version="1")
    app.state.desk = desk
    from . import sessions as _sessions
    tracker = _sessions.use(os.path.join(desk.data, "sessions.json"))

    @app.exception_handler(Exception)
    async def anything_else(request: Request, exc: Exception):
        # Whatever goes wrong inside a request is that request's answer, with
        # an id to find it in the log by. It is never the service going down
        # or a traceback sent to the caller.
        import logging
        import uuid
        ref = uuid.uuid4().hex[:8]
        logging.getLogger("agentic_mcp.service").exception("request %s %s failed [%s]", request.method,
                                                           request.url.path, ref)
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}", "ref": ref}, status_code=500)

    @app.get("/sessions")
    def all_sessions(text: bool = False):
        """Every controller's session: its state, since when, how it answers."""
        known = {r["controller"]: r for r in tracker.view()}
        rows = [known.get(key) or {"controller": key, "state": _sessions.UNKNOWN, "for_s": 0, "requests": 0,
                                   "errors": 0, "writes": 0, "last_ok": None, "last_error": "", "last_error_at": None,
                                   "breaker": "closed", "answers_ms": None, "history": []} for key in desk.keys()]
        return PlainTextResponse(_sessions.text(rows)) if text else {"sessions": rows}

    @app.get("/sessions/{key}")
    def one_session(key: str):
        seat_or_404(key)
        return (tracker.view(key) or [{"controller": key, "state": _sessions.UNKNOWN}])[0]

    class Guard:
        """Reading the service is open; changing anything is not, and neither
        is the MCP endpoint, through which every controller's tools are called.

        Written as plain ASGI: the helper that wraps a function as middleware
        reads a response as one start and then its body, which a stream that
        stays open (the MCP endpoint) is not, and it raised on every client
        that connected there."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if callers.required and scope["type"] == "http":
                given = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
                who = callers.who(given[7:] if given.startswith("Bearer ") else "")
                if who is None and (scope["method"] != "GET" or scope["path"].startswith("/mcp")):
                    return await PlainTextResponse("this needs the agentic MCP's token", status_code=401)(
                        scope, receive, send)
                # A GET without a token reads the service as before; it reaches no controller.
                _access.CURRENT.set(who or _access.Principal("anonymous", frozenset(), False, False, False))
            await self.inner(scope, receive, send)

    app.add_middleware(Guard)

    def must(ok: bool, why: str) -> None:
        if not ok:
            raise HTTPException(403, f"{_access.current().name}: {why}")

    def admin_only() -> None:
        must(_access.current().admin, "this token may not change what the agentic MCP holds")

    def reach(key: str) -> None:
        must(_access.current().reaches(key), f"this token does not reach {key}")

    def approver(by: str) -> str:
        by = str(by or "")
        if by.strip():
            must(_access.current().approve, "this token may not approve a write (approved_by)")
        return by

    def seat_or_404(key: str):
        try:
            return desk.seat(key)
        except LookupError as exc:
            raise HTTPException(404, str(exc))

    # ----- itself -----

    @app.get("/health")
    def health():
        return {"ok": True, "service": "agentic-mcp", "uptime_s": round(time.time() - STARTED),
                "controllers": desk.keys(), "holding": len(desk.keys()), "kinds": [k["kind"] for k in desk.kinds()],
                "model": _agent.model_info(), "mcp": getattr(app.state, "mcp", ""),
                "sessions": {r["controller"]: r["state"] for r in tracker.view()}}

    @app.get("/topology")
    def topology():
        """Itself and every controller it holds, for whoever draws the picture."""
        from .sessions import each
        keys = desk.keys()
        state = {r["controller"]: r for r in tracker.view()}

        def one(key):
            return desk.describe(key), desk.reachable(key)
        rows = []
        # All of them at once: the picture takes as long as the slowest one
        # allowed (3 s), not the sum, and one that fails is one row.
        for key, got in each(keys, one, wait_s=3.0).items():
            d, why = got["value"] if got["ok"] else ({"key": key, "label": key}, got["error"])
            rows.append({**{k: v for k, v in d.items() if k != "conformance"},
                         "verdict": (d.get("conformance") or {}).get("verdict", ""),
                         "reachable": not why, "why": why,
                         "session": (state.get(key) or {}).get("state", "not asked yet")})
        up = sum(1 for r in rows if r["reachable"])
        return {"agent": {"name": "Agentic MCP", "model": _agent.model_info(), "kinds": len(desk.kinds()),
                          "controllers": len(rows), "reachable": up, "uptime_s": round(time.time() - STARTED)},
                "controllers": rows}

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
        admin_only()
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
        admin_only()
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

    # ----- the contract every controller is checked against -----

    def verdicts(spec=None):
        out = []
        for key in desk.keys():
            try:
                r = contract.check(desk.seat(key).manifest, spec)
                out.append({"key": key, "verdict": r["verdict"], "gaps": r["gaps"], "lacking": r["lacking"]})
            except Exception as exc:  # noqa: BLE001
                out.append({"key": key, "error": str(exc)})
        return out

    @app.get("/contract")
    def get_contract():
        from .verbs import CONTRACT, CONTRACT_FILE
        return {"contract": CONTRACT, "file": CONTRACT_FILE, "controllers": verdicts()}

    @app.post("/contract/check")
    def check_contract(body: Dict[str, Any] = Body(...)):
        """A contract checked, and what each onboarded controller's verdict would be under it; nothing changes."""
        from .verbs import contract_problems
        spec = body.get("contract", body)
        problems = contract_problems(spec)
        return {"ok": not problems, "problems": problems, "controllers": [] if problems else verdicts(spec)}

    @app.put("/contract")
    def put_contract(body: Dict[str, Any] = Body(...)):
        """Replace the contract (the token, when one is set): the old file is kept as a .bak beside it."""
        from .verbs import replace_contract
        admin_only()
        try:
            done = replace_contract(body.get("contract", body))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {**done, "controllers": verdicts()}

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
        admin_only()
        if not body.get("key") or not body.get("url"):
            raise HTTPException(400, "onboarding needs a key and the controller's url")
        try:
            return desk.onboard(body["key"], body["url"], label=body.get("label", ""),
                                user_env=body.get("user_env", ""), password_env=body.get("password_env", ""),
                                spec=body.get("spec", ""), docs=body.get("docs") or [],
                                kind=body.get("kind", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/controllers/{key}")
    def controller(key: str):
        seat = seat_or_404(key)
        return {"manifest": seat.manifest, "conformance": contract.check(seat.manifest),
                "summary": desk.summary(key), "reachable": desk.reachable(key)}

    @app.delete("/controllers/{key}")
    def remove(key: str):
        admin_only()
        return {"removed": desk.remove(key)}

    @app.get("/controllers/{key}/tools")
    def tools(key: str):
        seat_or_404(key)
        return {"tools": desk.tools(key)}

    @app.post("/controllers/{key}/call")
    def call(key: str, body: Dict[str, Any] = Body(...)):
        reach(key)
        seat_or_404(key)
        name = body.get("tool", "")
        if any(t["write"] for t in desk.tools(key) if t["name"] == name):
            must(_access.current().write, f"this token may not write ({name})")
        return desk.call(key, name, body.get("arguments") or {}, approved_by=approver(body.get("approved_by")))

    @app.post("/controllers/{key}/{what}")
    def verb(key: str, what: str, body: Dict[str, Any] = Body(...)):
        reach(key)
        seat_or_404(key)
        if what == "write":
            must(_access.current().write, "this token may not write")
        if what == "confirm":
            admin_only()
        args = body.get("arguments") or {}
        try:
            if what == "read":
                return desk.read(key, body["verb"], body.get("body"), **args)
            if what == "dry-run":
                return desk.dry_run(key, body["verb"], body.get("body"), **args)
            if what == "write":
                return desk.write(key, body["verb"], body.get("body"),
                                  approved_by=approver(body.get("approved_by")), **args)
            if what == "confirm":
                return desk.confirm(key, body["verb"], str(body.get("by") or ""), body.get("operation", ""))
        except (LookupError, ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))
        raise HTTPException(404, f"nothing called {what}")

    # ----- asked in words -----

    @app.post("/ask")
    def ask(body: Dict[str, Any] = Body(...)):
        # Words may name any controller, so only a token that reaches all of them asks in words.
        must(_access.current().reaches("*"), "asking in words needs a token that reaches every controller")
        if body.get("confirm"):
            must(_access.current().write, "this token may not write")
        approver(body.get("approved_by"))
        return _agent.ask(desk, str(body.get("text") or ""), confirm=bool(body.get("confirm")),
                          approved_by=str(body.get("approved_by") or ""))

    _mount_mcp(app, desk, callers)
    return app


def _mount_mcp(app: FastAPI, desk: Desk, callers: Any = None) -> None:
    """The same desk, for a client that speaks MCP."""
    try:
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.sse import SseServerTransport
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
                     "answered with a dry run: a person approves writes over HTTP, never through MCP.",
                     {"text": {"type": "string"}}),
        "desk_onboard": ("Onboard a controller from its own API: key, url, and the names of the "
                         "settings holding its credentials. kind names its controller manifest, for one "
                         "that publishes nothing it could be recognised by.",
                         {"key": {"type": "string"}, "url": {"type": "string"}, "kind": {"type": "string"},
                          "user_env": {"type": "string"}, "password_env": {"type": "string"}}),
        "desk_kinds": ("Every kind of controller there is a manifest for.", {}),
        "desk_sessions": ("Every controller's session: up, degraded or down, since when, requests, errors, "
                          "how long it takes to answer.", {}),
    }

    def caller() -> Any:
        """Who is calling: from this message's own headers where the transport
        gives them, else from the request the stream was opened with."""
        try:
            request = server.request_context.request
            given = request.headers.get("authorization", "") if request is not None else None
        except (LookupError, AttributeError):
            given = None
        if given is not None and callers is not None and callers.required:
            who = callers.who(given[7:] if given.startswith("Bearer ") else "")
            if who is None:
                raise PermissionError("this needs the agentic MCP's token")
            return who
        return _access.current()

    def public(key: str, name: str) -> str:
        return name if name.startswith(key + "_") else f"{key}__{name}"

    @server.list_tools()
    async def list_tools() -> List[Any]:
        who = caller()
        own = {n: v for n, v in OWN.items()
               if (n != "desk_onboard" or who.admin) and (n != "desk_ask" or who.reaches("*"))}
        out = [types.Tool(name=n, description=d, inputSchema={"type": "object", "properties": p})
               for n, (d, p) in own.items()]
        for key in desk.keys():
            if not who.reaches(key):
                continue
            try:
                tools = desk.tools(key)
            except Exception:  # noqa: BLE001
                continue
            for t in tools:
                if t["write"] and not who.write:
                    continue
                # No approved_by here: whoever calls over MCP may be a model, and a model does not approve.
                # A write sent from here is queued or refused; a person approves it over HTTP.
                schema = dict(t["schema"] or {"type": "object", "properties": {}})
                out.append(types.Tool(name=public(key, t["name"]), inputSchema=schema,
                                      description=f"[{key}] {t['description']}"[:1000]))
        return out

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]) -> List[Any]:
        import asyncio
        arguments = dict(arguments or {})
        arguments.pop("approved_by", None)
        who = caller()

        def run() -> str:
            if name == "desk_controllers":
                return json.dumps([{k: v for k, v in desk.describe(key).items() if k != "conformance"}
                                   | {"verdict": desk.describe(key)["conformance"]["verdict"]}
                                   for key in desk.keys() if who.reaches(key)], indent=1)
            if name == "desk_sessions":
                from . import sessions as _s
                return _s.text([r for r in _s.TRACKER.view() if who.reaches(r["controller"])])
            if name == "desk_kinds":
                return json.dumps(desk.kinds(), indent=1)
            if name == "desk_ask":
                if not who.reaches("*"):
                    return "Refused: asking in words needs a token that reaches every controller"
                return json.dumps(_agent.ask(desk, arguments.get("text", ""), False, ""), indent=1, default=str)
            if name == "desk_onboard":
                if not who.admin:
                    return "Refused: this token may not onboard a controller"
                try:
                    return _onboard()
                except ValueError as exc:
                    return f"Refused: {exc}"
            for key in desk.keys():
                if not who.reaches(key):
                    continue
                for t in desk.tools(key):
                    if public(key, t["name"]) == name:
                        if t["write"] and not who.write:
                            return f"Refused: this token may not write ({name})"
                        return desk.call(key, t["name"], arguments, approved_by="")["text"]
            return f"Unknown tool: {name}"

        def _onboard() -> str:
            found = desk.onboard(arguments["key"], arguments["url"], kind=arguments.get("kind", ""),
                                 user_env=arguments.get("user_env", ""),
                                 password_env=arguments.get("password_env", ""))
            return desk.summary(found["key"])

        return [types.TextContent(type="text", text=await asyncio.to_thread(run))]

    class Stream:
        """The stream a client stays connected to. It is the whole response:
        nothing is sent after it ends."""

        async def __call__(self, scope, receive, send):
            async with sse.connect_sse(scope, receive, send) as streams:
                await server.run(streams[0], streams[1], server.create_initialization_options())

    app.router.routes.append(Route("/mcp/sse", endpoint=Stream()))
    app.router.routes.append(Mount("/mcp/messages/", app=sse.handle_post_message))


def serve(host: str = "127.0.0.1", port: int = 8400) -> None:
    import logging

    import uvicorn
    # What the service does by itself (a replay, a controller that stopped
    # answering) is said in its log; each request is not.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = create_app()
    from . import sessions as _sessions
    _sessions.BREAKER = True

    @app.on_event("startup")
    async def _room() -> None:
        # Requests are served on threads. Enough of them that every controller
        # can have its whole share waiting and the others are still served.
        import anyio.to_thread
        want = int(os.getenv("AGENTIC_MCP_THREADS", "0")) or max(64, _sessions.SLOTS * (len(app.state.desk.keys()) + 2))
        anyio.to_thread.current_default_thread_limiter().total_tokens = want
    _watch(app.state.desk)
    logging.getLogger("agentic_mcp.service").info(
        "agentic MCP up on %s:%s, holding %s", host, port, ", ".join(app.state.desk.keys()) or "no controller yet")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _watch(desk: Desk, every_s: float = float(os.getenv("AGENTIC_MCP_WATCH_S", "30"))) -> None:
    """Look at every controller from time to time, whether or not anybody is
    asking it anything: a controller that goes away is then known, and logged,
    when it goes and not when it is next needed."""
    import logging
    import threading

    from . import sessions as _sessions
    log = logging.getLogger("agentic_mcp.service")

    looking: set = set()

    def look(key: str) -> None:
        try:
            known = {r["controller"]: r for r in _sessions.TRACKER.view()}.get(key) or {}
            if known.get("last_ok") and time.time() - known["last_ok"] < every_s and known.get("state") == _sessions.UP:
                return                                # it answered somebody a moment ago
            seat = desk.seat(key)
            entry = (seat.manifest.get("verbs") or {}).get("health") or {}
            if entry.get("operation"):
                seat.session.read("health")
            else:
                seat.session.fetch("GET", seat.manifest["base_url"])
        except Exception as exc:  # noqa: BLE001 - the watch itself never stops
            log.debug("%s: not looked at (%s)", key, exc)
        finally:
            looking.discard(key)

    def loop():
        while True:
            time.sleep(every_s)
            # Each controller is looked at on a thread of its own, so one that
            # hangs for a whole timeout does not make the others' look late. A
            # controller still being looked at from last time is not asked twice.
            for key in desk.keys():
                if key in looking:
                    continue
                looking.add(key)
                threading.Thread(target=look, args=(key,), name=f"watch-{key}", daemon=True).start()
    threading.Thread(target=loop, name="session-watch", daemon=True).start()
