"""Who may do what, and where a controller's credentials may go (access.py)."""
import asyncio
import base64
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from agentic_mcp import access
from agentic_mcp.desk import Desk
from tests import example_controller

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "manifests", "example", "metro.yaml")
ENV = {"METRO_USER": "ada", "METRO_PASSWORD": "fake-secret"}


class Catcher:
    """A server that records every login it is sent."""

    def __init__(self):
        seen = self.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                got = self.headers.get("Authorization", "")
                if got:
                    seen.append(base64.b64decode(got.split()[1]).decode())
                self.send_response(404)
                self.end_headers()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.desk = Desk(data=self.data, env=ENV, background=False)
        self.own, self.other = Catcher(), Catcher()
        self.addCleanup(self.own.stop)
        self.addCleanup(self.other.stop)

    def test_a_login_goes_to_the_controller_only_not_to_a_page_elsewhere(self):
        self.desk.onboard("m", self.own.url, user_env="METRO_USER", password_env="METRO_PASSWORD",
                          docs=[self.other.url + "/guide"])
        self.assertIn("ada:fake-secret", self.own.seen)
        self.assertEqual(self.other.seen, [])

    def test_a_login_is_bound_to_the_address_it_was_first_onboarded_with(self):
        self.desk.onboard("m", self.own.url, user_env="METRO_USER", password_env="METRO_PASSWORD")
        with open(os.path.join(self.data, "credential_scope.json")) as fh:
            self.assertEqual(json.load(fh)["METRO_PASSWORD"], [access.origin(self.own.url)])
        with self.assertRaises(ValueError) as said:
            self.desk.onboard("stolen", self.other.url, user_env="METRO_USER", password_env="METRO_PASSWORD")
        self.assertIn("belongs to", str(said.exception))
        self.assertEqual(self.other.seen, [])

    def test_an_administrator_can_add_an_address_for_it(self):
        with open(os.path.join(self.data, "credential_scope.json"), "w") as fh:
            json.dump({"METRO_USER": [self.own.url, self.other.url],
                       "METRO_PASSWORD": [self.own.url, self.other.url]}, fh)
        self.desk.onboard("m2", self.other.url, user_env="METRO_USER", password_env="METRO_PASSWORD")
        self.assertIn("ada:fake-secret", self.other.seen)

    def test_a_manifest_edited_to_point_elsewhere_gets_no_login(self):
        self.desk.onboard("m", self.own.url, user_env="METRO_USER", password_env="METRO_PASSWORD")
        from agentic_mcp import manifest as manifests
        found = manifests.load("m", self.data)
        found["base_url"] = self.other.url
        manifests.save(found, data=self.data)
        self.desk.seat("m").session.fetch("GET", self.other.url + "/x")
        self.assertEqual(self.other.seen, [])

    def test_an_unreadable_scope_sends_no_login(self):
        self.desk.onboard("m", self.own.url, user_env="METRO_USER", password_env="METRO_PASSWORD")
        self.own.seen.clear()
        with open(os.path.join(self.data, "credential_scope.json"), "w") as fh:
            fh.write("{not json")
        self.desk.seat("m").session.fetch("GET", self.own.url + "/x")
        self.assertEqual(self.own.seen, [])


TOKENS = [
    {"name": "noc", "token": "sha256:" + __import__("hashlib").sha256(b"noc-token").hexdigest(),
     "controllers": ["a"], "write": False, "approve": False},
    {"name": "writer", "token": "writer-token", "controllers": ["a"], "write": True, "approve": False},
    {"name": "approver", "token": "approver-token", "controllers": "*", "write": True, "approve": True},
]


def _service(data, tokens_file):
    from agentic_mcp.service import create_app
    with mock.patch.dict(os.environ, {"AGENTIC_MCP_TOKEN": "operator-token", "AGENTIC_MCP_TOKENS": tokens_file}):
        return create_app(Desk(data=data, env={}, background=False))


class ScopedTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.box = example_controller.Controller().start()
        cls.old = os.environ.get("CONTROLLER_MANIFESTS_DIR")
        cls.manifests = tempfile.mkdtemp()
        os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.manifests
        cls.data = tempfile.mkdtemp()
        cls.tokens = os.path.join(cls.data, "tokens.json")
        with open(cls.tokens, "w") as fh:
            json.dump(TOKENS, fh)
        cls.http = TestClient(_service(cls.data, cls.tokens))
        op = cls.auth("operator-token")
        with open(EXAMPLE, "rb") as fh:
            assert cls.http.post("/manifests", content=fh.read(), headers=op).status_code == 200
        for key in ("a", "b"):
            got = cls.http.post("/controllers", json={"key": key, "url": cls.box.url}, headers=op)
            assert got.status_code == 200, got.text
            cls.http.post(f"/controllers/{key}/confirm", json={"verb": "provision", "by": "test"}, headers=op)

    @classmethod
    def tearDownClass(cls):
        cls.box.stop()
        if cls.old is None:
            os.environ.pop("CONTROLLER_MANIFESTS_DIR", None)
        else:
            os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.old
        shutil.rmtree(cls.manifests, True)
        shutil.rmtree(cls.data, True)

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}

    def read(self, key, token):
        return self.http.post(f"/controllers/{key}/read", json={"verb": "read_devices"}, headers=self.auth(token))

    def write(self, token, by=""):
        return self.http.post("/controllers/a/write", headers=self.auth(token), json={
            "verb": "provision", "approved_by": by,
            "arguments": {"name": f"s{time.monotonic_ns()}", "a_end": "sw1", "z_end": "sw2", "vlan": "10"}})

    def test_a_token_reaches_its_controllers_only(self):
        self.assertEqual(self.read("a", "noc-token").status_code, 200)
        self.assertEqual(self.read("b", "noc-token").status_code, 403)
        self.assertEqual(self.read("a", "guess").status_code, 401)

    def test_a_read_only_token_cannot_write(self):
        self.assertEqual(self.write("noc-token").status_code, 403)

    def test_only_a_token_that_may_approve_names_an_approver(self):
        self.assertEqual(self.write("writer-token", by="alice").status_code, 403)
        got = self.write("writer-token")
        self.assertEqual(got.status_code, 200)
        self.assertFalse(got.json()["ok"])                       # refused: nobody approved it
        self.assertTrue(self.write("approver-token", by="alice").json()["ok"])

    def test_the_operator_token_does_everything_as_before(self):
        self.assertEqual(self.read("b", "operator-token").status_code, 200)
        self.assertTrue(self.write("operator-token", by="alice").json()["ok"])

    def test_only_an_administrator_changes_what_is_held(self):
        for token in ("noc-token", "writer-token", "approver-token"):
            got = self.http.post("/controllers", json={"key": "c", "url": self.box.url}, headers=self.auth(token))
            self.assertEqual(got.status_code, 403, token)
            self.assertEqual(self.http.delete("/controllers/a", headers=self.auth(token)).status_code, 403, token)
            self.assertEqual(self.http.put("/contract", json={}, headers=self.auth(token)).status_code, 403, token)

    def test_asking_in_words_needs_every_controller(self):
        self.assertEqual(self.http.post("/ask", json={"text": "list the devices on b"},
                                        headers=self.auth("noc-token")).status_code, 403)


class McpTests(unittest.TestCase):
    """Over MCP, on both transports: a token sees its controllers only, and nobody approves."""

    @classmethod
    def setUpClass(cls):
        import uvicorn
        cls.box = example_controller.Controller().start()
        cls.old = os.environ.get("CONTROLLER_MANIFESTS_DIR")
        cls.manifests = tempfile.mkdtemp()
        os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.manifests
        cls.data = tempfile.mkdtemp()
        tokens = os.path.join(cls.data, "tokens.json")
        with open(tokens, "w") as fh:
            json.dump(TOKENS, fh)
        app = _service(cls.data, tokens)
        from fastapi.testclient import TestClient
        http = TestClient(app)
        op = {"Authorization": "Bearer operator-token"}
        with open(EXAMPLE, "rb") as fh:
            http.post("/manifests", content=fh.read(), headers=op)
        for key in ("a", "b"):
            http.post("/controllers", json={"key": key, "url": cls.box.url}, headers=op)
            http.post(f"/controllers/{key}/confirm", json={"verb": "provision", "by": "test"}, headers=op)
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.base = f"http://127.0.0.1:{cls.server.servers[0].sockets[0].getsockname()[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.box.stop()
        if cls.old is None:
            os.environ.pop("CONTROLLER_MANIFESTS_DIR", None)
        else:
            os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.old
        shutil.rmtree(cls.manifests, True)
        shutil.rmtree(cls.data, True)

    def session(self, transport, token, work):
        from mcp import ClientSession

        async def go():
            headers = {"Authorization": f"Bearer {token}"}
            if transport == "sse":
                from mcp.client.sse import sse_client
                opened = sse_client(self.base + "/mcp/sse", headers=headers)
            else:
                from mcp.client.streamable_http import streamablehttp_client
                opened = streamablehttp_client(self.base + "/mcp", headers=headers)
            async with opened as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    return await work(s)
        return asyncio.run(go())

    def names(self, transport, token):
        async def work(s):
            return {t.name: t for t in (await s.list_tools()).tools}
        return self.session(transport, token, work)

    def test_a_token_lists_its_controllers_tools_only(self):
        for transport in ("sse", "http"):
            tools = self.names(transport, "noc-token")
            self.assertTrue(any(n.startswith("a_") or n.startswith("a__") for n in tools), transport)
            self.assertFalse(any(n.startswith("b_") or n.startswith("b__") for n in tools), transport)
            self.assertNotIn("desk_onboard", tools)
            self.assertFalse(any(t.name.startswith("a_") and "create_service" in t.name for t in tools.values()))

    def test_no_write_offers_or_takes_an_approver(self):
        for transport in ("sse", "http"):
            tools = self.names(transport, "approver-token")
            writes = [t for n, t in tools.items() if "create_service" in n]
            self.assertTrue(writes, transport)
            for t in writes:
                self.assertNotIn("approved_by", (t.inputSchema or {}).get("properties", {}))
            name = writes[0].name

            async def work(s, name=name):
                got = await s.call_tool(name, {"name": "x", "a_end": "sw1", "z_end": "sw2", "vlan": "10", "approved_by": "me"})
                return got.content[0].text
            said = self.session(transport, "approver-token", work)
            self.assertIn("a write names who approved it", said, transport)
            self.assertNotIn("x", [s.get("name") for s in self.box.services.values()]
                             if isinstance(self.box.services, dict) else [])

    def test_a_token_cannot_call_another_controllers_tool(self):
        async def work(s):
            got = await s.call_tool("b_list_devices", {})
            return got.content[0].text
        for transport in ("sse", "http"):
            self.assertIn("Unknown tool", self.session(transport, "noc-token", work), transport)


if __name__ == "__main__":
    unittest.main()
