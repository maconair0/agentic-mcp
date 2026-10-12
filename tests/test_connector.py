"""
A controller is learned from what the controller publishes.

These hold what matters about that: an operation is found from each kind of
description, a controller that describes nothing is said to describe nothing,
reads are tried before they are trusted, and no write leaves on the strength of
a name.
"""
import json
import os
import tempfile
import unittest

from tests import manifests_for_tests  # noqa: F401  (points the library at the shipped manifests)

from agentic_mcp import discover, manifest, verbs
from agentic_mcp.session import Session, rows

SWAGGER = {"swagger": "2.0", "basePath": "/ctl/v1", "paths": {
    "/devices": {"get": {"summary": "all devices"}},
    "/devices/{id}": {"get": {"summary": "one device"}},
    "/links": {"get": {"summary": "all links"}},
    "/intents": {"get": {"summary": "all intents"},
                 "post": {"summary": "submit", "parameters": [{"in": "body", "name": "stream"}]}},
    "/intents/{appId}/{key}": {"delete": {"summary": "withdraw"}},
    "/paths/{src}/{dst}": {"get": {"summary": "paths"}},
    "/flows": {"get": {"summary": "flows"}},
}}


class FakeHttp:
    """Answers GETs from a table; records everything else."""

    def __init__(self, pages):
        self.pages = pages
        self.sent = []

    def request(self, method, url, json=None, auth=None, timeout=None, headers=None, params=None):
        self.sent.append((method, url, json, auth))
        body = self.pages.get((method, url))

        class R:
            status_code = 200 if body is not None else 404
            headers = {"content-type": "application/json"}
            text = body if isinstance(body, str) else globals()["json"].dumps(body) if body is not None else ""
        return R()


def pages(**extra):
    base = "http://ctl:1/ctl/v1"
    out = {("GET", base + "/swagger.json"): SWAGGER,
           ("GET", base + "/devices"): {"devices": [{"id": "a"}, {"id": "b"}]},
           ("GET", base + "/links"): {"links": [{"src": "a", "dst": "b"}]},
           ("GET", base + "/intents"): {"intents": []}}
    out.update(extra)
    return out


class DiscoverTests(unittest.TestCase):
    def test_a_swagger_document_gives_every_operation_under_its_base_path(self):
        ops = discover.from_openapi(SWAGGER)
        ids = {op["id"] for op in ops}
        self.assertIn("GET /ctl/v1/devices", ids)
        self.assertIn("DELETE /ctl/v1/intents/{appId}/{key}", ids)
        self.assertEqual(len(ops), 8)

    def test_a_get_reads_and_everything_else_writes(self):
        ops = {op["id"]: op for op in discover.from_openapi(SWAGGER)}
        self.assertFalse(ops["GET /ctl/v1/intents"]["writes"])
        self.assertTrue(ops["POST /ctl/v1/intents"]["writes"])

    def test_an_rpc_that_only_reads_is_told_from_one_that_writes(self):
        ops = {op["id"]: op for op in discover.from_restconf_operations("/rests", {
            "ietf-restconf:operations": {"tapi:get-service-list": [None], "svc:service-create": [None]}})}
        self.assertFalse(ops["POST /rests/operations/tapi:get-service-list"]["writes"])
        self.assertTrue(ops["POST /rests/operations/svc:service-create"]["writes"])

    def test_the_document_is_found_behind_a_documentation_page(self):
        base = "http://ctl:1/ctl/v1"
        http = FakeHttp({("GET", base + "/docs/index.html"): 'url = "/ctl/v1/docs/apis/swagger.json";',
                         ("GET", "http://ctl:1/ctl/v1/docs/apis/swagger.json"): SWAGGER})
        s = Session({"key": "c", "base_url": base}, env={}, http=http)
        found = discover.discover(base, s.fetch)
        self.assertEqual(len(found["operations"]), 8)

    def test_a_controller_that_describes_nothing_yields_nothing(self):
        s = Session({"key": "c", "base_url": "http://ctl:1"}, env={}, http=FakeHttp({}))
        found = discover.discover("http://ctl:1", s.fetch)
        self.assertEqual(found["operations"], [])

    def test_a_known_kind_is_recognised_by_the_one_address_it_answers(self):
        http = FakeHttp({("GET", "http://ctl:1/stats/switches"): [1, 2]})
        s = Session({"key": "c", "base_url": "http://ctl:1"}, env={}, http=http)
        found = discover.discover("http://ctl:1", s.fetch)
        self.assertIn("manifest of kind ryu", found["styles"])
        self.assertTrue(found["operations"])


class DomainTests(unittest.TestCase):
    def ops(self, *paths):
        return [discover.operation("GET", p) for p in paths]

    def test_openflow_words_make_a_packet_controller(self):
        self.assertEqual(verbs.domain(self.ops("/flows", "/intents", "/hosts"))["kind"], "packet")

    def test_roadm_words_make_an_optical_controller(self):
        got = verbs.domain(self.ops("/x"), ["org-openroadm-device", "spectrum-thing"])
        self.assertEqual(got["kind"], "optical")

    def test_both_in_strength_is_multi_layer(self):
        got = verbs.domain(self.ops("/flows", "/vlan", "/optical_links", "/spectrum"))
        self.assertEqual(got["kind"], "multi-layer")

    def test_no_evidence_is_unknown_and_not_a_guess(self):
        self.assertEqual(verbs.domain(self.ops("/a", "/b"))["kind"], "unknown")


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.map = verbs.mapping(discover.from_openapi(SWAGGER), "packet")

    def test_each_verb_gets_the_operation_a_person_would_pick(self):
        want = {"read_devices": "GET /ctl/v1/devices", "read_links": "GET /ctl/v1/links",
                "read_services": "GET /ctl/v1/intents", "provision": "POST /ctl/v1/intents",
                "teardown": "DELETE /ctl/v1/intents/{appId}/{key}",
                "compute_path": "GET /ctl/v1/paths/{src}/{dst}"}
        for verb, op in want.items():
            self.assertEqual(self.map[verb]["operation"]["id"], op, verb)

    def test_what_the_controller_lacks_is_missing_and_not_filled_in(self):
        self.assertEqual(self.map["adjust"]["status"], "missing")

    def test_spectrum_is_not_asked_of_a_packet_controller(self):
        self.assertEqual(self.map["read_spectrum"]["status"], "not applicable")

    def test_a_write_is_never_trusted_from_its_name(self):
        for verb in ("provision", "teardown"):
            self.assertEqual(self.map[verb]["status"], "needs confirmation")
            self.assertFalse(self.map[verb]["confirmed"])


class OnboardTests(unittest.TestCase):
    def onboard(self, table=None, **kw):
        self.http = FakeHttp(pages() if table is None else table)
        return manifest.onboard("ctl", "http://ctl:1/ctl/v1", user_env="U", password_env="P",
                                env={"U": "me", "P": "secret"}, http=self.http, **kw)["manifest"]

    def test_a_read_that_answers_is_verified_with_what_it_returned(self):
        m = self.onboard()
        self.assertEqual(m["verbs"]["read_devices"]["status"], "verified")
        self.assertEqual(m["verbs"]["read_devices"]["checked"]["rows"], 2)

    def test_a_read_that_needs_arguments_is_mapped_and_says_which(self):
        m = self.onboard()
        self.assertEqual(m["verbs"]["compute_path"]["status"], "mapped")
        self.assertEqual(m["verbs"]["compute_path"]["needs"], ["src", "dst"])

    def test_a_read_that_does_not_answer_falls_to_the_next_candidate(self):
        table = pages()
        del table[("GET", "http://ctl:1/ctl/v1/intents")]
        table[("GET", "http://ctl:1/ctl/v1/flows")] = {"flows": [{"id": 1}]}
        m = self.onboard(table)
        self.assertEqual(m["verbs"]["read_services"]["operation"]["id"], "GET /ctl/v1/flows")
        self.assertEqual(m["verbs"]["read_services"]["status"], "verified")

    def test_onboarding_sends_no_write(self):
        self.onboard()
        self.assertEqual({method for method, *_ in self.http.sent}, {"GET"})

    def test_the_manifest_names_where_credentials_are_and_never_holds_them(self):
        m = self.onboard()
        self.assertNotIn("secret", json.dumps(m))
        self.assertEqual(m["auth"]["password_env"], "P")

    def test_nothing_published_is_reported_as_a_problem(self):
        m = self.onboard({("GET", "http://ctl:1/ctl/v1/whatever"): {}})
        self.assertIn("problem", m["api"])
        self.assertEqual(m["verbs"], {})

    def test_a_confirmation_survives_onboarding_again(self):
        first = self.onboard()
        manifest.confirm(first, "provision", by="root")
        again = self.onboard(previous=first)
        self.assertTrue(again["verbs"]["provision"]["confirmed"])

    def test_it_is_stored_and_read_back(self):
        m = self.onboard()
        with tempfile.TemporaryDirectory() as d:
            manifest.save(m, [], data=d)
            self.assertEqual(manifest.load("ctl", data=d)["key"], "ctl")
            self.assertEqual(manifest.keys(data=d), ["ctl"])


class WriteGateTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp(pages())
        self.m = manifest.onboard("ctl", "http://ctl:1/ctl/v1", env={}, http=self.http)["manifest"]
        self.http.sent.clear()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.audit = os.path.join(self.dir.name, "audit.jsonl")
        self.s = Session(self.m, env={}, http=self.http, audit=self.audit)

    def test_an_unconfirmed_write_is_refused_and_nothing_is_sent(self):
        got = self.s.write("provision", {"type": "x"}, approved_by="root")
        self.assertTrue(got["refused"])
        self.assertEqual(self.http.sent, [])

    def test_a_confirmed_write_without_an_approver_is_refused(self):
        manifest.confirm(self.m, "provision", by="root")
        self.assertTrue(self.s.write("provision", {"type": "x"})["refused"])
        self.assertEqual(self.http.sent, [])

    def test_a_confirmed_and_approved_write_is_sent_once(self):
        manifest.confirm(self.m, "provision", by="root")
        self.s.write("provision", {"type": "x"}, approved_by="root")
        self.assertEqual([(m, u) for m, u, *_ in self.http.sent],
                         [("POST", "http://ctl:1/ctl/v1/intents")])

    def test_refused_and_sent_writes_are_both_in_the_audit_log(self):
        self.s.write("provision", {"type": "x"}, approved_by="root")
        manifest.confirm(self.m, "provision", by="root")
        self.s.write("provision", {"type": "x"}, approved_by="root")
        with open(self.audit) as fh:
            lines = [json.loads(line) for line in fh]
        self.assertEqual([line["sent"] for line in lines], [False, True])

    def test_a_person_can_choose_a_different_operation_when_confirming(self):
        manifest.confirm(self.m, "read_services", by="root", operation_id="GET /ctl/v1/flows",
                         operations=discover.from_openapi(SWAGGER))
        self.assertEqual(self.m["verbs"]["read_services"]["operation"]["id"], "GET /ctl/v1/flows")

    def test_a_path_argument_is_filled_in_and_a_missing_one_is_named(self):
        self.assertIn("needs: src, dst", self.s.read("compute_path")["error"])
        self.s.read("compute_path", src="a/1", dst="b")
        self.assertEqual(self.http.sent[-1][1], "http://ctl:1/ctl/v1/paths/a%2F1/b")

    def test_restconf_data_missing_is_an_empty_answer_and_not_a_failure(self):
        self.http.pages[("GET", "http://ctl:1/ctl/v1/devices")] = None

        class Http(FakeHttp):
            def request(self, *a, **k):
                class R:
                    status_code = 409
                    headers = {}
                    text = '{"errors":{"error":[{"error-tag":"data-missing"}]}}'
                return R()
        got = Session(self.m, env={}, http=Http({})).read("read_devices")
        self.assertTrue(got["ok"] and got["empty"])


class RowsTests(unittest.TestCase):
    def test_the_listing_is_found_however_it_is_wrapped(self):
        self.assertEqual(len(rows([1, 2, 3])), 3)
        self.assertEqual(len(rows({"devices": [{}, {}]})), 2)
        self.assertEqual(len(rows({"a": {"b": {"node": [{}, {}, {}], "x": [1]}}})), 3)
        self.assertEqual(rows({"a": 1}), [])


if __name__ == "__main__":
    unittest.main()
