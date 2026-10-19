"""
The agentic MCP as a service of its own.

What has to hold: it stands alone (it imports nothing but itself and what requirements.txt lists); a
client far away gets the same answers the desk gives in-process; a manifest
is uploaded to it and not to anybody's document corpus; a request in words is
read by rule when it is plain and by the small model only otherwise, and what
the model answers is checked before it is used.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import manifests_for_tests  # noqa: F401
from agentic_mcp import agent, library
from agentic_mcp.client import RemoteDesk
from agentic_mcp.desk import Desk
from tests import example_controller

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "manifests", "example", "metro.yaml")
KNOWN = {"core": {"label": "Core", "family": "packet_core"}, "edge": {"label": "Edge", "family": "switch"},
         "access": {"label": "Access", "family": "optical_access"},
         "optical": {"label": "Optical", "family": "optical"}}


class StandAloneTests(unittest.TestCase):
    def test_it_imports_only_itself_and_what_requirements_txt_lists(self):
        import ast
        import sys
        allowed = set(sys.stdlib_module_names) | {"agentic_mcp", "requests", "yaml", "fastapi", "uvicorn",
                                                  "starlette", "anyio", "mcp", "pypdf", "jsonschema"}
        for folder, _, files in os.walk(os.path.join(ROOT, "agentic_mcp")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(folder, name)) as fh:
                    tree = ast.parse(fh.read())
                for node in ast.walk(tree):
                    names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                             [node.module] if isinstance(node, ast.ImportFrom) and not node.level and node.module
                             else [])
                    for found in names:
                        self.assertTrue(found.split(".")[0] in allowed, f"{name} imports {found}")

    def test_its_manifests_are_kept_in_its_own_folder(self):
        old = {k: os.environ.pop(k, None) for k in ("CONTROLLER_MANIFESTS_DIR", "AGENTIC_MCP_MANIFESTS", "AGENTIC_MCP_DATA")}
        try:
            self.assertTrue(library.corpus().endswith(os.path.join("agentic_mcp", "data", "manifests")))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v


class TokenTests(unittest.TestCase):
    """With a token set, only reading the service is open."""

    def setUp(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        from agentic_mcp.service import create_app
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        with mock.patch.dict(os.environ, {"AGENTIC_MCP_TOKEN": "open-sesame"}):
            self.http = TestClient(create_app(Desk(data=self.data, env={}, background=False)))
        self.key = {"Authorization": "Bearer open-sesame"}

    def test_a_read_of_the_service_needs_no_token(self):
        self.assertEqual(self.http.get("/health").status_code, 200)
        self.assertEqual(self.http.get("/controllers").json(), {"controllers": []})

    def test_a_change_needs_it(self):
        body = {"key": "metro", "url": "http://127.0.0.1:9"}
        self.assertEqual(self.http.post("/controllers", json=body).status_code, 401)
        self.assertEqual(self.http.post("/controllers", json=body, headers={"Authorization": "Bearer guess"}).status_code, 401)
        self.assertEqual(self.http.post("/manifests", content=b"not a manifest").status_code, 401)
        self.assertEqual(self.http.post("/manifests", content=b"not a manifest", headers=self.key).status_code, 200)

    def test_so_does_the_mcp_endpoint_where_every_controllers_tools_are(self):
        self.assertEqual(self.http.get("/mcp/sse").status_code, 401)
        self.assertEqual(self.http.post("/mcp/messages/?session_id=x", json={}).status_code, 401)
        self.assertEqual(self.http.post("/mcp", json={}).status_code, 401)


class RuleTests(unittest.TestCase):
    def test_a_plain_request_is_read_with_no_model(self):
        for text, controller, action in (
                ("list the devices on core", "core", "read_devices"),
                ("show the links of optical", "optical", "read_links"),
                ("which services does access hold", "access", "read_services"),
                ("free spectrum on optical", "optical", "read_spectrum"),
                ("is every controller healthy", "all", "health"),
                ("which controllers are onboarded", "all", "controllers"),
                ("delete service name=LP3 on optical", "optical", "teardown")):
            got = agent.by_rule(text, KNOWN)
            self.assertEqual((got["controller"], got["action"]), (controller, action), text)

    def test_arguments_are_read_from_the_request(self):
        got = agent.by_rule("create service name=demo vlan=20 on edge", KNOWN)
        self.assertEqual(got["arguments"], {"name": "demo", "vlan": 20})

    def test_a_request_that_names_no_controller_is_not_plain(self):
        # "the boxes at each site" is one of the controllers, and knowing which
        # takes knowing what each controller runs
        self.assertIsNone(agent.by_rule("what has been set up on the boxes at each site so far", KNOWN))

    def test_a_change_never_goes_to_every_controller(self):
        self.assertIsNone(agent.by_rule("delete the service name=x on all controllers", KNOWN))

    def test_two_reads_in_one_request_are_not_plain(self):
        self.assertIsNone(agent.by_rule("devices and links on core", KNOWN))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.real = agent._say
        self.addCleanup(setattr, agent, "_say", self.real)

    def answer(self, *answers):
        queue = list(answers)

        def say(prompt, timeout):
            self.said.append(prompt)
            return queue.pop(0)
        agent._say = say

    def test_it_is_asked_two_small_questions(self):
        self.answer({"action": "read_alarms"}, {"controller": "optical"})
        got = agent.by_model("anything complaining on the optical core", KNOWN)
        self.assertEqual((got["controller"], got["action"]), ("optical", "read_alarms"))
        self.assertEqual(len(self.said), 2)

    def test_a_named_controller_is_not_asked_about(self):
        self.answer({"action": "read_statistics"})
        got = agent.by_model("how busy are the ports on the core routers", KNOWN)
        self.assertEqual(got["controller"], "core")
        self.assertEqual(len(self.said), 1)

    def test_an_action_it_made_up_is_not_used(self):
        self.answer({"action": "reboot_everything"})
        self.assertIsNone(agent.by_model("turn it off and on again", KNOWN))

    def test_a_controller_it_made_up_becomes_all_and_not_a_guess(self):
        self.answer({"action": "read_devices"}, {"controller": "cisco"})
        self.assertEqual(agent.by_model("what boxes are there", KNOWN)["controller"], "all")

    def test_no_model_answering_is_a_request_not_understood(self):
        def say(prompt, timeout):
            raise ConnectionError("no model")
        agent._say = say
        self.assertIsNone(agent.by_model("what boxes are there", KNOWN))
        self.assertEqual(agent.understand("what boxes are there", KNOWN)["by"], "nobody")


class ServiceTests(unittest.TestCase):
    """The service on a port, the example controller behind it, a client in front."""

    @classmethod
    def setUpClass(cls):
        import uvicorn
        from agentic_mcp.service import create_app
        cls.box = example_controller.Controller().start()
        cls.old = os.environ["CONTROLLER_MANIFESTS_DIR"]
        cls.manifests = tempfile.mkdtemp()
        cls.data = tempfile.mkdtemp()
        os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.manifests
        cls.app_desk = Desk(data=cls.data, env={}, background=False)
        config = uvicorn.Config(create_app(cls.app_desk), host="127.0.0.1", port=0, log_level="error")
        cls.server = uvicorn.Server(config)
        threading.Thread(target=cls.server.run, daemon=True).start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        port = cls.server.servers[0].sockets[0].getsockname()[1]
        cls.desk = RemoteDesk(f"http://127.0.0.1:{port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.box.stop()
        os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.old
        shutil.rmtree(cls.manifests, True)
        shutil.rmtree(cls.data, True)

    def setUp(self):
        self.box.services.clear()

    def test_1_a_manifest_is_uploaded_to_the_service_and_checked_there(self):
        with open(EXAMPLE) as fh:
            got = self.desk.install_manifest(fh.read(), "metro.yaml")
        self.assertTrue(got["ok"])
        self.assertIn("example_metro", [k["kind"] for k in self.desk.kinds()])
        bad = self.desk.install_manifest("manifest: agentic-mcp.controller/v1\nkind: nope\n", "bad.yaml")
        self.assertFalse(bad["ok"])
        self.assertIn("To provision", self.desk.guide("example_metro"))

    def test_2_a_controller_is_onboarded_through_it(self):
        found = self.desk.onboard("metro", self.box.url, label="Metro")
        self.assertEqual(found["family"], "example_metro")
        self.assertEqual(self.desk.keys(), ["metro"])
        self.assertEqual(self.desk.reachable("metro"), "")
        self.assertEqual(self.desk.describe("metro")["conformance"]["verdict"], "accepted, with gaps")

    def test_3_reads_and_writes_go_through_it(self):
        self.assertEqual(len(self.desk.read("metro", "read_devices")["rows"]), 3)
        dry = self.desk.dry_run("metro", "provision", name="s", a_end="sw1", z_end="sw2", vlan=7)
        self.assertTrue(dry["ok"])
        self.assertEqual(self.box.services, {})
        refused = self.desk.write("metro", "provision", name="s", a_end="sw1", z_end="sw2", vlan=7)
        self.assertTrue(refused["refused"])                       # nobody approved it
        sent = self.desk.write("metro", "provision", approved_by="root", name="s", a_end="sw1", z_end="sw2", vlan=7)
        self.assertTrue(sent["ok"])
        self.assertIn("s", self.box.services)

    def test_4_the_seat_a_client_gets_reads_like_a_local_one(self):
        import asyncio
        seat = self.desk.seat("metro")
        names = [t.name for t in asyncio.run(seat.local.list_tools()).tools]
        self.assertIn("metro_list_devices", names)
        got = asyncio.run(seat.local.call_tool("metro_list_devices", {}))
        self.assertEqual(len(json.loads(got.content[0].text)["devices"]), 3)
        self.desk.approved_by("")
        refused = asyncio.run(seat.local.call_tool(
            "metro_create_service", {"name": "t", "a_end": "sw1", "z_end": "sw2", "vlan": "7"}))
        self.assertTrue(refused.isError)

    def test_5_asked_in_words_a_read_is_done_and_a_change_is_only_shown(self):
        got = self.desk.ask("list the devices on metro")
        self.assertEqual(got["understood"]["by"], "rule")
        self.assertIn("3 devices", got["answer"])
        shown = self.desk.ask("create service name=w a_end=sw1 z_end=sw2 vlan=9 on metro")
        self.assertIn("Nothing was sent", shown["answer"])
        self.assertEqual(self.box.services, {})
        done = self.desk.ask("create service name=w a_end=sw1 z_end=sw2 vlan=9 on metro",
                             confirm=True, approved_by="root")
        self.assertIn("Sent, approved by root", done["answer"])
        self.assertIn("w", self.box.services)

    def test_6_it_says_what_it_holds_for_whoever_draws_the_picture(self):
        t = self.desk.topology()
        self.assertEqual(t["agent"]["name"], "Agentic MCP")
        self.assertEqual([(c["label"], c["reachable"]) for c in t["controllers"]], [("Metro", True)])

    def test_8_a_user_guide_uploaded_to_the_service_becomes_a_draft_manifest_to_onboard_from(self):
        quiet = example_controller.Controller(describes_itself=False).start()
        self.addCleanup(quiet.stop)
        with open(os.path.join(os.path.dirname(EXAMPLE), "metro-user-guide.md"), "rb") as fh:
            got = self.desk.install_guide(fh.read(), "metro_guide", "Metro from its guide", "metro-user-guide.md")
        self.assertTrue(got["ok"] and got["draft"], got)
        self.assertEqual((got["operations"], got["writes"]), (11, 3))
        self.assertIn("manifest: agentic-mcp.controller/v1", got["yaml"])
        found = self.desk.onboard("guided", quiet.url, kind="metro_guide")
        self.assertEqual(found["family"], "metro_guide")
        self.assertEqual(found["verbs"]["teardown"]["status"], "needs confirmation")
        self.assertEqual(len(self.desk.read("guided", "read_links")["data"]["links"]), 2)
        self.desk.remove("guided")

    def test_9_a_client_that_connects_to_the_mcp_stream_and_leaves_raises_nothing(self):
        import logging
        import requests
        seen = []
        handler = logging.Handler()
        handler.emit = lambda record: seen.append(record.getMessage())
        logging.getLogger("uvicorn.error").addHandler(handler)
        self.addCleanup(logging.getLogger("uvicorn.error").removeHandler, handler)
        with requests.get(self.desk.url + "/mcp/sse", stream=True, timeout=5) as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn("event-stream", r.headers["content-type"])
            first = next(r.iter_lines())
            self.assertIn(b"endpoint", first)
        time.sleep(0.5)
        self.assertEqual([m for m in seen if "Exception" in m], [])
        self.assertEqual(self.desk.health()["mcp"], "/mcp/sse")

    def test_10_a_client_of_streamable_http_lists_the_same_tools(self):
        import requests
        head = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        hello = requests.post(self.desk.url + "/mcp", headers=head, timeout=10, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}})
        self.assertEqual(hello.status_code, 200, hello.text)
        self.assertEqual(hello.json()["result"]["serverInfo"]["name"], "agentic-mcp")
        listed = requests.post(self.desk.url + "/mcp", headers=head, timeout=10,
                               json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in listed.json()["result"]["tools"]]
        self.assertIn("desk_controllers", names)
        self.assertEqual(self.desk.health()["mcp_http"], "/mcp")

    def test_7_a_service_that_is_not_there_is_said_plainly(self):
        gone = RemoteDesk("http://127.0.0.1:9")
        self.assertIn("not answering", gone.answers())
        self.assertEqual(gone.keys(), [])


class ModelApiTests(unittest.TestCase):
    """The same question to each API the model can be served by, with the API answered here."""

    def setUp(self):
        from agentic_mcp import model
        self.model = model
        for name in ("API", "URL", "KEY_ENV"):
            self.addCleanup(setattr, model, name, getattr(model, name))
        self.sent = []
        real = model.requests.post
        self.addCleanup(setattr, model.requests, "post", real)

        def post(url, json=None, headers=None, timeout=None):
            self.sent.append({"url": url, "json": json, "headers": headers or {}})
            reply = self.replies.pop(0)
            r = mock.Mock(status_code=reply[0])
            r.json.return_value = reply[1]
            r.raise_for_status = (lambda: None) if reply[0] < 400 else mock.Mock(side_effect=RuntimeError(reply[0]))
            return r
        model.requests.post = post

    def use(self, api, url, key_env=""):
        self.model.API, self.model.URL, self.model.KEY_ENV = api, url, key_env

    def test_ollama_is_asked_for_json_on_its_own_api(self):
        self.use("ollama", "http://gpu-box:11434")
        self.replies = [(200, {"message": {"content": '{"action": "read_devices"}'}})]
        self.assertEqual(self.model.chat_json("q", "qwen", max_tokens=40)["action"], "read_devices")
        self.assertEqual(self.sent[0]["url"], "http://gpu-box:11434/api/chat")
        self.assertEqual(self.sent[0]["json"]["options"]["num_predict"], 40)

    def test_an_openai_compatible_server_gets_the_key_from_the_named_setting(self):
        self.use("openai", "http://localhost:8000/v1", "TEST_MODEL_KEY")
        self.replies = [(200, {"choices": [{"message": {"content": '```json\n{"controller": "core"}\n```'}}]})]
        with mock.patch.dict(os.environ, {"TEST_MODEL_KEY": "k-123"}):
            self.assertEqual(self.model.chat_json("q", "llama")["controller"], "core")
        self.assertEqual(self.sent[0]["url"], "http://localhost:8000/v1/chat/completions")
        self.assertEqual(self.sent[0]["headers"]["Authorization"], "Bearer k-123")
        self.assertEqual(self.sent[0]["json"]["response_format"], {"type": "json_object"})

    def test_a_server_without_json_mode_is_asked_again_in_plain_text(self):
        self.use("openai", "http://localhost:1234/v1")
        self.replies = [(400, {}), (200, {"choices": [{"message": {"content": 'Sure: {"action": "health"}'}}]})]
        self.assertEqual(self.model.chat_json("q", "local")["action"], "health")
        self.assertNotIn("response_format", self.sent[1]["json"])
        self.assertNotIn("Authorization", self.sent[1]["headers"])

    def test_anthropic_gets_its_own_headers(self):
        self.use("anthropic", "https://api.anthropic.com", "TEST_MODEL_KEY")
        self.replies = [(200, {"content": [{"type": "text", "text": '{"action": "read_links"}'}]})]
        with mock.patch.dict(os.environ, {"TEST_MODEL_KEY": "k-456"}):
            self.assertEqual(self.model.chat_json("q", "claude")["action"], "read_links")
        self.assertEqual(self.sent[0]["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(self.sent[0]["headers"]["x-api-key"], "k-456")

    def test_no_key_and_no_model_are_said_not_guessed(self):
        self.use("anthropic", "https://api.anthropic.com", "TEST_MODEL_KEY_UNSET")
        with self.assertRaises(RuntimeError):
            self.model.chat_json("q", "claude")
        with self.assertRaises(RuntimeError):
            self.model.chat_json("q", "")
        self.assertEqual(self.sent, [])
        self.assertIn("NOT set", self.model.info("claude")["key"])
        self.assertNotIn("k-", json.dumps(self.model.info("claude")))


if __name__ == "__main__":
    unittest.main()
