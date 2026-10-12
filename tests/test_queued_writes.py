"""
Writes that wait for a person, and tools that are data.

Ryu has no code of its own in the agentic MCP: its tools are entries in its
manifest. What its MCP server's code did has to hold all the same: a write is
checked and queued and never sent by the call that asked for it; it is sent
once, by whoever holds the operator's token; what was applied is put back on a
switch that comes back empty.
"""
import asyncio
import json
import os
import tempfile
import unittest

from tests import manifests_for_tests  # noqa: F401
from agentic_mcp import skills
from agentic_mcp.queue import Queue
from agentic_mcp.session import Session
from agentic_mcp.tools import checked, number

SWITCHES = [{"dpid": "0000000000de0102", "ports": [{"port_no": "00000001", "name": "p1", "hw_addr": "aa"}]}]
PORTDESC = {"14549250": [{"port_no": "LOCAL", "name": "duesseldorf_SW1"}, {"port_no": 1, "name": "p1"}]}
FLOWS = {"14549250": [{"priority": 0, "match": {}, "actions": ["OUTPUT:CONTROLLER"], "packet_count": 9},
                      {"priority": 200, "match": {"in_port": 1}, "packet_count": 3, "byte_count": 90,
                       "actions": ["PUSH_VLAN:33024", "SET_FIELD: {vlan_vid:4198}", "OUTPUT:3"]}]}


class Ryu:
    """Stands where Ryu's REST API is."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def request(self, method, url, json=None, **kw):
        self.sent.append((method, url.split(":1", 1)[1], json))
        path = url.split(":1", 1)[1]
        body = {"/stats/switches": [14549250], "/v1.0/topology/switches": SWITCHES, "/v1.0/topology/links": [],
                "/v1.0/topology/hosts": [], "/stats/portdesc/14549250": PORTDESC,
                "/stats/flow/14549250": FLOWS}.get(path, "")
        me = self

        class R:
            status_code = 500 if (me.fail and method == "POST") else 200
            headers = {"content-type": "application/json"}
            text = globals()["json"].dumps(body) if body != "" else ""
        return R()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ryu = Ryu()
        from agentic_mcp import discover, manifest
        m = {"key": "ryu", "label": "Ryu", "base_url": "http://r:1", "family": "ryu", "defaults": {}, "verbs": {}}
        manifest.declare(m, next(f for f in discover.families() if f["family"] == "ryu"), [])
        self.session = Session(m, env={"RYU_MCP_APPROVER_TOKEN": "s3cret"}, http=self.ryu,
                               audit=os.path.join(self.dir.name, "audit.jsonl"),
                               queue=Queue(os.path.join(self.dir.name, "queue.json")))
        self.tools = skills.tools_for(self.session, self.dir.name, lambda: "")
        self.vlan = dict(dpid="0000000000de0102", client_port=1, uplink_port=3, vlan_id=102)

    def call(self, tool, **args):
        got = self.tools[tool].fn(**args)
        return json.loads(got[0]), got[1]

    def posts(self):
        return [s for s in self.ryu.sent if s[0] == "POST"]


class QueuedWriteTests(Base):
    def test_a_write_is_queued_and_nothing_reaches_the_controller(self):
        got, _ = self.call("ryu_map_vlan", **self.vlan)
        self.assertEqual(got["status"], "queued_for_approval")
        self.assertEqual(got["summary"], "map 0000000000de0102 port 1 <-> uplink 3 vlan 102")
        self.assertEqual(got["calls"], 2)
        self.assertEqual(self.posts(), [])

    def test_what_breaks_the_manifests_rules_is_refused_before_it_is_queued(self):
        for bad, why in ((dict(self.vlan, vlan_id=5000), "vlan_id must be 1..4094"),
                         (dict(self.vlan, uplink_port=1), "client_port and uplink_port are the same")):
            got, _ = self.call("ryu_map_vlan", **bad)
            self.assertEqual((got["ok"], got["error"]), (False, "invalid"))
            self.assertIn(why, got["detail"])
        got, _ = self.call("ryu_add_flow", dpid="1", match={"in_prot": 1}, actions=[{"type": "OUTPUT", "port": 2}])
        self.assertIn("in_prot", got["detail"])
        self.assertEqual(self.session.queue.pending(), [])

    def test_only_the_operators_token_approves(self):
        queued, _ = self.call("ryu_map_vlan", **self.vlan)
        got, _ = self.call("ryu_approve_request", approval_id=queued["approval_id"], approver_token="guess")
        self.assertEqual(got["error"], "refused")
        self.assertEqual(self.posts(), [])
        got, _ = self.call("ryu_approve_request", approval_id=queued["approval_id"], approver_token="s3cret")
        self.assertEqual((got["ok"], got["state"], got["action"]), (True, "applied", "ryu_map_vlan"))
        self.assertEqual([p[1] for p in self.posts()], ["/stats/flowentry/add"] * 2)

    def test_with_no_token_configured_nothing_is_approved_this_way(self):
        self.session.env = {}
        queued, _ = self.call("ryu_map_vlan", **self.vlan)
        got, _ = self.call("ryu_approve_request", approval_id=queued["approval_id"], approver_token="")
        self.assertEqual(got["error"], "disabled")

    def test_a_request_is_sent_once(self):
        queued, _ = self.call("ryu_map_vlan", **self.vlan)
        self.session.apply_queued(queued["approval_id"], "root")
        again = self.session.apply_queued(queued["approval_id"], "root")
        self.assertFalse(again["ok"])
        self.assertEqual(len(self.posts()), 2)

    def test_a_refusal_by_the_controller_is_a_failed_request_and_is_not_replayed(self):
        self.ryu.fail = True
        queued, _ = self.call("ryu_map_vlan", **self.vlan)
        got = self.session.apply_queued(queued["approval_id"], "root")
        self.assertEqual(got["state"], "failed")
        self.assertEqual(self.session.queue.applied("ryu"), [])

    def test_what_was_applied_is_put_back_in_the_order_it_was_applied(self):
        for tool in ("ryu_map_vlan", "ryu_unmap_vlan"):
            queued, _ = self.call(tool, **self.vlan)
            self.session.apply_queued(queued["approval_id"], "root")
        del self.ryu.sent[:]
        got = self.session.replay()
        self.assertEqual(len(got["replayed"]), 2)
        self.assertEqual([p[1].rsplit("/", 1)[1] for p in self.posts()], ["add", "add", "delete_strict", "delete_strict"])

    def test_a_replay_does_not_count_against_the_hourly_limit(self):
        queued, _ = self.call("ryu_map_vlan", **self.vlan)
        self.session.apply_queued(queued["approval_id"], "root")
        self.session.manifest["verbs"]["provision"]["policy"] = {"max_per_hour": 2}
        for _ in range(5):
            self.session.replay()
        self.assertEqual(self.session._over_limit("provision"), "")

    def test_the_limit_refuses_before_queueing(self):
        self.session.manifest["verbs"]["provision"]["policy"] = {"max_per_hour": 1}
        queued, _ = self.call("ryu_map_vlan", **self.vlan)
        self.session.apply_queued(queued["approval_id"], "root")
        got, _ = self.call("ryu_map_vlan", **dict(self.vlan, vlan_id=103))
        self.assertEqual(got["error"], "rate_limited")


class ReplayWatchTests(unittest.TestCase):
    """When what was approved is put back on a controller."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "ryu.seen.json")
        self.three = {"ok": True, "rows": [{"dpid": 1}, {"dpid": 2}, {"dpid": 3}]}

    def test_the_first_look_ever_is_the_starting_point(self):
        self.assertEqual(skills.Watcher(self.path).new_in(self.three), 0)

    def test_a_switch_that_appears_is_new_and_one_that_stays_is_not(self):
        w = skills.Watcher(self.path)
        w.new_in({"ok": True, "rows": [{"dpid": 1}]})
        self.assertEqual(w.new_in(self.three), 2)
        self.assertEqual(w.new_in(self.three), 0)

    def test_a_read_that_failed_does_not_make_everything_new_afterwards(self):
        w = skills.Watcher(self.path)
        w.new_in(self.three)
        self.assertEqual(w.new_in({"ok": False, "error": "timed out"}), 0)
        self.assertEqual(w.new_in(self.three), 0)

    def test_a_switch_that_went_away_and_came_back_is_new(self):
        w = skills.Watcher(self.path)
        w.new_in(self.three)
        w.new_in({"ok": True, "rows": [{"dpid": 1}, {"dpid": 2}]})
        self.assertEqual(w.new_in(self.three), 1)

    def test_starting_the_service_again_is_not_a_reason_to_replay(self):
        skills.Watcher(self.path).new_in(self.three)
        again = skills.Watcher(self.path)
        self.assertEqual(again.new_in(self.three), 0)
        self.assertEqual(again.new_in({"ok": True, "rows": self.three["rows"] + [{"dpid": 4}]}), 1)


class SmallThingsTests(unittest.TestCase):
    def test_a_number_in_any_spelling(self):
        self.assertEqual([number(x) for x in (257, "257", "0x101", "0000000000000101", "de0102")],
                         [257, 257, 257, 257, 14549250])

    def test_rules_on_arguments(self):
        checked({"between": {"v": [1, 9]}}, {"v": "5"})
        with self.assertRaises(ValueError):
            checked({"each": {"actions": {"field": "type", "one_of": ["OUTPUT"]}}}, {"actions": [{"type": "FLY"}]})

    def test_no_code_for_ryu_is_left_in_the_agentic_mcp(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agentic_mcp")
        self.assertFalse(os.path.exists(os.path.join(root, "skills", "ryu")))



class TransportPceTests(unittest.TestCase):
    """TransportPCE's tools as data: the safeguards its code had."""

    def setUp(self):
        from agentic_mcp import discover, manifest
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.sent, self.answers = [], {}
        test = self

        class Http:
            def request(self, method, url, json=None, **kw):
                path = url.split(":1", 1)[1]
                test.sent.append((method, path.rsplit("/", 1)[1].split(":")[-1], json))
                status, body = test.answers.get(path.rsplit("/", 1)[1].split(":")[-1], (200, {"output": {
                    "configuration-response-common": {"response-code": "200"}}}))

                class R:
                    status_code, headers = status, {"content-type": "application/json"}
                    text = globals()["json"].dumps(body) if not isinstance(body, str) else body
                return R()
        m = {"key": "transportpce", "label": "TransportPCE", "base_url": "http://t:1", "family": "transportpce",
             "defaults": {}, "verbs": {}, "api": {"restconf_root": "/rests"}}
        manifest.declare(m, next(f for f in discover.families() if f["family"] == "transportpce"), [])
        self.session = Session(m, env={"TPCE_MCP_APPROVER_TOKEN": "t0k"}, http=Http(),
                               audit=os.path.join(self.dir.name, "audit.jsonl"),
                               queue=Queue(os.path.join(self.dir.name, "queue.json")))
        self.tools = skills.tools_for(self.session, self.dir.name, lambda: "")
        way = [{"index": 0, "port": {"port-device-name": "x", "port-type": "fixed", "port-name": "p",
                                     "port-rack": "0", "port-shelf": "0"}}]
        self.end = {"service-format": "Ethernet", "service-rate": 100, "clli": "X", "node-id": "X",
                    "tx-direction": way, "rx-direction": way}
        self.services = lambda *states: (200, {"org-openroadm-service:service-list": {"services": [
            {"service-name": f"S{i}", "operational-state": st} for i, st in enumerate(states)]}})

    def call(self, tool, **args):
        text, _ = self.tools[tool].fn(**args)
        return json.loads(text)

    def path(self):
        return self.call("tpce_compute_path", service_name="p", service_a_end=self.end, service_z_end=self.end)

    def test_a_path_computation_is_armed_first(self):
        self.answers["service-list"] = self.services("inService")
        self.assertIn("path", self.path()["raw"])
        self.assertEqual([s[1] for s in self.sent],
                         ["service-list", "service-feasibility-check", "path-computation-request"])

    def test_it_is_not_sent_while_a_service_is_being_set_up(self):
        self.answers["service-list"] = self.services("inService", "outOfService")
        got = self.path()
        self.assertEqual((got["ok"], got["error"]), (False, "busy"))
        self.assertTrue(got["detail"].startswith("UNAVAILABLE"))
        self.assertEqual([s[1] for s in self.sent], ["service-list"])

    def test_it_is_not_sent_when_the_arming_was_refused(self):
        self.answers["service-list"] = (409, '{"errors": {"error": [{"error-tag": "data-missing"}]}}')
        self.answers["service-feasibility-check"] = (200, {"output": {"configuration-response-common": {
            "response-code": "500", "response-message": "validation failed"}}})
        got = self.path()
        self.assertEqual(got["error"], "busy")
        self.assertNotIn("path-computation-request", [s[1] for s in self.sent])

    def test_an_empty_datastore_is_an_empty_answer_and_a_dead_controller_is_not(self):
        self.answers["service-list"] = (409, '{"errors": {"error": [{"error-tag": "data-missing"}]}}')
        raw = self.call("tpce_list_services")
        self.assertIsNone(raw["raw"]["services"])
        self.answers["service-list"] = (500, "boom")
        text, is_error = self.tools["tpce_list_services"].fn()
        self.assertTrue(is_error)

    def test_a_create_is_queued_and_a_refusal_inside_a_200_is_a_failure(self):
        queued = self.call("tpce_service_create", service_name="LP9", service_a_end=self.end, service_z_end=self.end)
        self.assertEqual((queued["status"], queued["target"]), ("queued_for_approval", "LP9"))
        self.assertEqual(queued["summary"], "create OpenROADM service LP9 (X -> X, 100G)")
        self.assertEqual(self.sent, [])
        self.answers["service-create"] = (200, {"output": {"configuration-response-common": {
            "response-code": "500", "response-message": "no path"}}})
        got = self.call("tpce_approve_request", approval_id=queued["approval_id"], approver_token="t0k")
        self.assertEqual((got["ok"], got["state"]), (False, "failed"))

class CollectTests(unittest.TestCase):
    def test_one_list_out_of_the_answers_of_a_foreach_step(self):
        from agentic_mcp.tools import _shape
        got = {"svc": {"services": [{}, {}]},
               "each": {"s1": {"connections": [{"id": 1}]}, "s2": {"connections": [{"id": 2}, {"id": 3}]}}}
        out = _shape({"collect": {"from": "each", "field": "connections", "count_of": "svc.services",
                                  "count_as": "service_count", "echo": ["context_uuid"]}},
                     got, {"context_uuid": "admin"})
        self.assertEqual(out, {"context_uuid": "admin", "service_count": 2,
                               "connections": [{"id": 1}, {"id": 2}, {"id": 3}]})


if __name__ == "__main__":
    unittest.main()
