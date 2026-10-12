"""
A controller nobody has written anything for.

It is a small HTTP service with an OpenAPI document, a handful of switches,
links and hosts, and services that can be created, changed and removed. Nothing
here was written for it, so whatever the agentic MCP can do with it, it
learned from the controller at onboarding. That makes it the test of the
claim that a new controller is added without writing code.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

SERVICE = {"type": "object", "required": ["name", "a_end", "z_end"],
           "properties": {"name": {"type": "string"}, "a_end": {"type": "string"},
                          "z_end": {"type": "string"}, "vlan": {"type": "integer"},
                          "rate_mbps": {"type": "integer"}}}


def openapi(base: str = "/api") -> Dict[str, Any]:
    by_name = [{"name": "name", "in": "path", "required": True, "schema": {"type": "string"}}]
    return {"openapi": "3.0.3", "info": {"title": "Example metro controller", "version": "1"},
            "servers": [{"url": base}],
            "paths": {
                "/health": {"get": {"summary": "Is the controller up"}},
                "/switches": {"get": {"summary": "Every switch"}},
                "/switches/{id}": {"get": {"summary": "One switch"}},
                "/links": {"get": {"summary": "Every link between switches"}},
                "/hosts": {"get": {"summary": "Every attached host"}},
                "/paths/{src}/{dst}": {"get": {"summary": "A route between two switches"}},
                "/services": {
                    "get": {"summary": "Every service"},
                    "post": {"summary": "Create a service", "requestBody": {"content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Service"}}}}}},
                "/services/{name}": {
                    "get": {"summary": "One service", "parameters": by_name},
                    "put": {"summary": "Change a service", "parameters": by_name, "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Service"}}}}},
                    "delete": {"summary": "Remove a service", "parameters": by_name}}},
            "components": {"schemas": {"Service": SERVICE}}}


class Controller:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, describes_itself: bool = True):
        # False: it publishes no API document, like a controller whose API is
        # only in its user guide.
        self.describes_itself = describes_itself
        self.switches = [{"id": f"sw{i}", "name": f"metro-{i}", "status": "up"} for i in (1, 2, 3)]
        self.links = [{"src": "sw1", "dst": "sw2"}, {"src": "sw2", "dst": "sw3"}]
        self.hosts = [{"mac": "00:00:00:00:00:01", "switch": "sw1", "port": 1}]
        self.services: Dict[str, Dict[str, Any]] = {}
        self.requests: list = []
        me = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return

            def _send(self, status: int, body: Any = None) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> Any:
                size = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(size)) if size else None
                except ValueError:
                    return None

            def _route(self, method: str) -> None:
                path = self.path.split("?")[0].rstrip("/")
                me.requests.append((method, path))
                if not path.startswith("/api"):
                    return self._send(404, {"error": "not found"})
                parts = [p for p in path[4:].split("/") if p]
                if method == "GET" and parts == ["openapi.json"] and me.describes_itself:
                    return self._send(200, openapi())
                if method == "GET" and parts == ["health"]:
                    return self._send(200, {"status": "up", "switches": len(me.switches)})
                if method == "GET" and parts == ["switches"]:
                    return self._send(200, {"switches": me.switches})
                if method == "GET" and len(parts) == 2 and parts[0] == "switches":
                    found = next((s for s in me.switches if s["id"] == parts[1]), None)
                    return self._send(200 if found else 404, found or {"error": "no such switch"})
                if method == "GET" and parts == ["links"]:
                    return self._send(200, {"links": me.links})
                if method == "GET" and parts == ["hosts"]:
                    return self._send(200, {"hosts": me.hosts})
                if method == "GET" and len(parts) == 3 and parts[0] == "paths":
                    return self._send(200, {"path": [parts[1], "sw2", parts[2]] if parts[1] != parts[2]
                                            else [parts[1]]})
                if parts == ["services"] and method == "GET":
                    return self._send(200, {"services": list(me.services.values())})
                if parts == ["services"] and method == "POST":
                    body = self._body()
                    if not isinstance(body, dict) or not all(body.get(k) for k in ("name", "a_end", "z_end")):
                        return self._send(400, {"error": "a service needs name, a_end and z_end"})
                    me.services[body["name"]] = {**body, "state": "up"}
                    return self._send(201, me.services[body["name"]])
                if len(parts) == 2 and parts[0] == "services":
                    if parts[1] not in me.services:
                        return self._send(404, {"error": "no such service"})
                    if method == "GET":
                        return self._send(200, me.services[parts[1]])
                    if method == "PUT":
                        me.services[parts[1]].update(self._body() or {})
                        return self._send(200, me.services[parts[1]])
                    if method == "DELETE":
                        del me.services[parts[1]]
                        return self._send(204)
                return self._send(404, {"error": "not found"})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

            def do_DELETE(self):
                self._route("DELETE")

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.host, self.port = host, self.server.server_address[1]
        self.url = f"http://{host}:{self.port}/api"

    def start(self) -> "Controller":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
