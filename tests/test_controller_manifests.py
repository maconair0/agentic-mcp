"""
Controller manifests: one document per kind of controller, in a standard form.

What has to hold: a manifest in the corpus is all a kind of controller needs;
a malformed one is refused with what is wrong; a verb it states is done the way
it states, with no confirmation of the mapping and still a dry run and an
approver for each write; and a kind's own limit per hour is kept.
"""
import asyncio
import glob
import json
import os
import shutil
import tempfile
import unittest

from tests import manifests_for_tests  # noqa: F401
from agentic_mcp import library, manifest
from agentic_mcp.desk import Desk
from agentic_mcp.tools import _fill, pick, view_rows
from tests import example_controller

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = os.path.join(ROOT, "tests", "fixtures")
EXAMPLE = os.path.join(ROOT, "manifests", "example", "metro.yaml")


class FormatTests(unittest.TestCase):
    def test_every_shipped_manifest_is_well_formed(self):
        files = glob.glob(os.path.join(SHIPPED, "*.yaml")) + [EXAMPLE]
        self.assertEqual(len(files), 3)          # the two fixtures and the example
        for path in files:
            self.assertEqual(library.validate(library.read(path)), [], path)

    def test_it_is_read_from_yaml_json_and_a_page_with_a_fenced_block(self):
        doc = {"manifest": library.FORMAT, "kind": "k", "recognise": {"fingerprint": "/x"}}
        self.assertEqual(library.parse(json.dumps(doc))["kind"], "k")
        self.assertEqual(library.parse(f"manifest: {library.FORMAT}\nkind: k\n")["kind"], "k")
        page = f"# K\n\nSome words.\n\n```yaml\nmanifest: {library.FORMAT}\nkind: k\n```\n"
        self.assertEqual(library.parse(page)["kind"], "k")

    def test_a_document_that_is_not_a_manifest_is_left_alone(self):
        self.assertIsNone(library.parse("# A paper about optical networks\n\nkind: of interesting"))

    def test_what_is_wrong_with_one_is_said_section_by_section(self):
        problems = library.validate({"manifest": library.FORMAT, "kind": "Bad Name", "domain": "radio",
                                     "extra": 1, "verbs": {"fly": {"operation": "GET /x"},
                                                           "provision": {"operation": "create it"},
                                                           "read_devices": {"operation": "GET /d", "view": {"id": "x"}}}})
        text = " | ".join(problems)
        for needle in ("kind:", "domain:", "extra:", "recognise:", "verbs.fly", "'create it'", "view: needs rows"):
            self.assertIn(needle, text)

    def test_the_guide_says_in_words_what_the_manifest_says_in_data(self):
        text = library.guide(library.read(os.path.join(SHIPPED, "optical.yaml")))
        self.assertIn("To provision", text)
        self.assertIn("At most 4 per hour", text)
        self.assertIn("service-feasibility-check", text)
        self.assertNotIn(library.FORMAT, text)        # or the guide would be read as a manifest


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_installing_puts_the_manifest_and_its_guide_in_the_corpus(self):
        got = library.install(EXAMPLE, self.dir.name)
        self.assertTrue(got["ok"])
        self.assertEqual(sorted(os.listdir(self.dir.name)),
                         ["controller-example_metro.guide.md", "controller-example_metro.yaml"])
        self.assertEqual([m["kind"] for m in library.all_manifests(self.dir.name)], ["example_metro"])

    def test_a_malformed_one_is_not_installed(self):
        bad = os.path.join(self.dir.name, "bad.yaml")
        with open(bad, "w") as fh:
            fh.write("manifest: agentic-mcp.controller/v1\nkind: x\n")
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target, True)
        got = library.install(bad, target)
        self.assertFalse(got["ok"])
        self.assertIn("recognise", got["problems"][0])
        self.assertEqual(os.listdir(target), [])

    def test_a_file_uploaded_to_the_corpus_is_recognised_where_it_lies(self):
        target = os.path.join(self.dir.name, "anything.yaml")
        shutil.copy(EXAMPLE, target)
        found = library.registered(target)
        self.assertEqual((found["kind"], found["ok"]), ("example_metro", True))
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, found["guide"])))
        paper = os.path.join(self.dir.name, "paper.md")
        with open(paper, "w") as fh:
            fh.write("# A paper\n")
        self.assertIsNone(library.registered(paper))

    def test_the_newest_manifest_of_a_kind_is_the_one_used(self):
        library.install(EXAMPLE, self.dir.name)
        newer = os.path.join(self.dir.name, "mine.yaml")
        with open(EXAMPLE) as fh:
            text = fh.read().replace('version: "1"', 'version: "2"')
        with open(newer, "w") as fh:
            fh.write(text)
        os.utime(newer, (2_000_000_000, 2_000_000_000))
        self.assertEqual(library.get("example_metro", self.dir.name)["version"], "2")


class TemplateTests(unittest.TestCase):
    def test_arguments_and_computed_values_are_filled_in(self):
        got = _fill({"dpid": {"$int": "$dpid"}, "vid": {"$add": ["$vlan", 4096]},
                     "id": {"$concat": ["delete-", "$name"]}, "who": "$name", "fixed": 7},
                    {"dpid": "12", "vlan": "102", "name": "LP3"})
        self.assertEqual(got, {"dpid": 12, "vid": 4198, "id": "delete-LP3", "who": "LP3", "fixed": 7})

    def test_a_path_goes_through_lists_on_the_way(self):
        data = {"net": {"nodes": [{"id": "a", "info": {"type": "rdm"}}, {"id": "b"}]}}
        self.assertEqual(pick(data, "net.nodes.id"), ["a", "b"])
        self.assertEqual(pick(data, "net.nodes[0].info.type"), "rdm")
        self.assertIsNone(pick(data, "net.nothing.here"))

    def test_a_view_makes_plain_rows_of_any_shape(self):
        data = {"devices": [{"device_id": {"device_uuid": {"uuid": "u1"}}, "name": "R1", "extra": 1}]}
        rows = view_rows({"rows": "devices", "id": "device_id.device_uuid.uuid", "name": "name"}, data)
        self.assertEqual(rows, [{"id": "u1", "name": "R1"}])
        self.assertEqual(view_rows({"rows": "", "id": "dpid"}, [{"dpid": "01"}]), [{"id": "01"}])


class ManifestDrivenControllerTests(unittest.TestCase):
    """The example controller, onboarded from its manifest in a corpus."""

    @classmethod
    def setUpClass(cls):
        cls.box = example_controller.Controller().start()
        cls.old = os.environ["CONTROLLER_MANIFESTS_DIR"]
        cls.corpus = tempfile.mkdtemp()
        os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.corpus
        library.install(EXAMPLE, cls.corpus)

    @classmethod
    def tearDownClass(cls):
        cls.box.stop()
        os.environ["CONTROLLER_MANIFESTS_DIR"] = cls.old
        shutil.rmtree(cls.corpus, True)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.box.services.clear()
        del self.box.requests[:]
        self.desk = Desk(data=self.dir.name, env={}, background=False)
        self.m = self.desk.onboard("metro", self.box.url, label="Metro")
        self.seat = self.desk.seat("metro")

    def call(self, tool, **args):
        got = asyncio.run(self.seat.local.call_tool(tool, args))
        return got.content[0].text, got.isError

    def test_it_is_recognised_as_its_kind(self):
        self.assertEqual(self.m["family"], "example_metro")
        self.assertEqual(self.m["domain"]["stated_by"], "manifest example_metro")
        self.assertIn("A service needs name, a_end and z_end", " ".join(self.m["quirks"]))

    def test_a_stated_read_is_still_tried_before_it_is_trusted(self):
        self.assertEqual(self.m["verbs"]["read_devices"]["status"], "verified")
        self.assertEqual(self.m["verbs"]["read_devices"]["checked"]["rows"], 3)

    def test_a_stated_write_needs_no_confirmation_of_its_mapping(self):
        entry = self.m["verbs"]["provision"]
        self.assertEqual(entry["status"], "declared")
        self.assertTrue(entry["confirmed"])
        self.assertEqual(entry["confirmed_by"], "manifest example_metro v1")

    def test_onboarding_from_a_manifest_still_writes_nothing(self):
        self.assertEqual({m for m, _ in self.box.requests}, {"GET"})

    def test_reads_come_back_as_plain_rows(self):
        text, _ = self.call("metro_list_devices")
        self.assertEqual(json.loads(text)["devices"][0], {"id": "sw1", "name": "metro-1", "status": "up"})

    def test_a_write_takes_the_arguments_the_manifest_names(self):
        tool = self.seat.tools["metro_create_service"]
        self.assertEqual(tool.schema["required"], ["name", "a_end", "z_end", "vlan"])
        self.assertEqual(tool.schema["properties"]["a_end"]["description"], "the switch at one end")

    def test_a_write_still_needs_an_approver(self):
        self.desk.approved_by("")
        _, error = self.call("metro_create_service", name="s", a_end="sw1", z_end="sw2", vlan="7")
        self.assertTrue(error)
        self.assertEqual(self.box.services, {})

    def test_the_whole_life_of_a_service_from_the_manifest_alone(self):
        self.desk.approved_by("root")
        self.call("metro_create_service", name="s", a_end="sw1", z_end="sw2", vlan="7")
        self.assertEqual(self.box.services["s"]["vlan"], 7)            # "7" sent as the integer the body asks for
        self.call("metro_update_service", name="s", a_end="sw1", z_end="sw3")
        self.assertEqual(self.box.services["s"]["z_end"], "sw3")
        text, _ = self.call("metro_list_services")
        self.assertEqual(json.loads(text)["services"][0]["status"], "up")
        self.call("metro_delete_service", name="s")
        self.assertEqual(self.box.services, {})

    def test_the_dry_run_catches_a_body_the_controller_would_refuse(self):
        got = self.seat.session.dry_run("provision", name="s", a_end="sw1", z_end="sw2", vlan="x")
        self.assertFalse(got["ok"])       # {$int: x} cannot be made, said before anything is sent
        self.assertFalse(got["sent"])

    def test_the_kinds_limit_per_hour_is_kept(self):
        self.m["verbs"]["provision"]["policy"] = {"max_per_hour": 2}
        manifest.save(self.m, data=self.dir.name)
        self.desk.close("metro")
        self.seat = self.desk.seat("metro")
        self.desk.approved_by("root")
        for n in ("a", "b"):
            self.call("metro_create_service", name=n, a_end="sw1", z_end="sw2", vlan="7")
        text, error = self.call("metro_create_service", name="c", a_end="sw1", z_end="sw2", vlan="7")
        self.assertTrue(error)
        self.assertIn("at most 2", text)
        self.assertEqual(sorted(self.box.services), ["a", "b"])


class StepsTests(unittest.TestCase):
    """A verb that is more than one request, as a switch controller's VLAN mapping is."""

    def setUp(self):
        self.sent = []
        test = self

        class Http:
            def request(self, method, url, json=None, **kw):
                test.sent.append((method, url, json))

                class R:
                    status_code, headers, text = (500 if test.fail_at == len(test.sent) else 200), {}, ""
                return R()
        self.fail_at = 0
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        from agentic_mcp import discover
        from agentic_mcp.session import Session
        m = {"key": "switch", "label": "Switch", "base_url": "http://r:1", "family": "switch", "defaults": {}, "verbs": {}}
        manifest.declare(m, next(f for f in discover.families() if f["family"] == "switch"), [])
        self.entry = m["verbs"]["provision"]
        self.session = Session(m, env={}, http=Http(), audit=os.path.join(self.dir.name, "audit.jsonl"))
        self.args = dict(dpid="14549250", client_port="1", uplink_port="3", vlan_id="102")

    def test_both_flows_of_a_vlan_mapping_are_built_from_four_arguments(self):
        got = self.session.dry_run("provision", **self.args)
        self.assertTrue(got["ok"])
        up, down = [r["body"] for r in got["requests"]]
        self.assertEqual(up["match"], {"in_port": 1})
        self.assertEqual(up["actions"][1], {"type": "SET_FIELD", "field": "vlan_vid", "value": 4198})
        self.assertEqual(down["match"], {"in_port": 3, "vlan_vid": 4198})
        self.assertEqual(self.sent, [])

    def test_a_datapath_id_is_taken_in_either_spelling(self):
        hexed = self.session.dry_run("provision", **{**self.args, "dpid": "0000000000de0102"})
        self.assertEqual(hexed["requests"][0]["body"]["dpid"], 14549250)

    def test_they_are_sent_in_order_once_approved(self):
        got = self.session.write("provision", approved_by="root", **self.args)
        self.assertTrue(got["ok"])
        self.assertEqual(len(self.sent), 2)

    def test_a_failure_stops_the_rest_and_says_what_was_and_was_not_sent(self):
        self.fail_at = 1
        got = self.session.write("provision", approved_by="root", **self.args)
        self.assertFalse(got["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(got["not_sent"]), 1)

    def test_a_missing_argument_is_named_before_anything_is_sent(self):
        got = self.session.dry_run("provision", dpid="1", client_port="1", uplink_port="3")
        self.assertIn("vlan_id", got["error"])


if __name__ == "__main__":
    unittest.main()


GUIDE = os.path.join(os.path.dirname(EXAMPLE), "metro-user-guide.md")


class UserGuideTests(unittest.TestCase):
    """A controller nobody wrote a manifest for and that publishes no API
    document: its user guide is uploaded, and that is enough to onboard it."""

    def setUp(self):
        self.box = example_controller.Controller(describes_itself=False).start()
        self.addCleanup(self.box.stop)
        self.old = os.environ["CONTROLLER_MANIFESTS_DIR"]
        self.folder, self.data = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.environ["CONTROLLER_MANIFESTS_DIR"] = self.folder
        self.addCleanup(os.environ.__setitem__, "CONTROLLER_MANIFESTS_DIR", self.old)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(shutil.rmtree, self.data, True)
        self.desk = Desk(data=self.data, env={}, background=False)
        with open(GUIDE, "rb") as fh:
            self.guide = fh.read()

    def test_a_manifest_in_the_standard_form_is_drafted_from_the_guide(self):
        got = library.draft_from_guide(self.guide.decode(), "Metro Guide", "Metro", "metro-user-guide.md")
        self.assertTrue(got["ok"], got)
        doc = got["manifest"]
        self.assertEqual((doc["manifest"], doc["kind"], doc["version"]), (library.FORMAT, "metro_guide", "draft"))
        self.assertEqual(library.validate(doc), [])
        self.assertEqual((got["operations"], got["reads"], got["writes"]), (11, 8, 3))
        ops = {(o["method"], o["path"]): o for o in doc["operations"]}
        self.assertEqual(ops[("GET", "/api/switches/{id}")]["summary"], "One switch")
        self.assertTrue(ops[("POST", "/api/services")]["writes"])
        self.assertNotIn("verbs", doc)                                  # a guide is not a person's word for a verb
        self.assertEqual(library.parse(got["yaml"]), doc)                # what is kept reads back the same

    def test_a_document_that_names_no_operation_drafts_nothing_and_says_what_would(self):
        got = library.draft_from_guide("The controller is installed with apt and started with systemd.", "quiet")
        self.assertFalse(got["ok"])
        self.assertIn("a method stands beside a path", got["problems"][0])

    def test_an_api_document_uploaded_as_the_guide_is_read_as_one(self):
        got = library.draft_from_guide(json.dumps(example_controller.openapi()), "metro_api")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["read_as"], "an API document")
        self.assertIn({"method": "DELETE", "path": "/api/services/{name}", "summary": "Remove a service",
                       "writes": True}, got["manifest"]["operations"])

    def test_a_draft_does_not_take_the_place_of_a_manifest_a_person_wrote(self):
        with open(EXAMPLE) as fh:
            self.desk.install_manifest(fh.read().replace("kind: example_metro", "kind: metro_guide"), "m.yaml")
        got = self.desk.install_guide(self.guide, "metro_guide")
        self.assertFalse(got["ok"])
        self.assertIn("is kept", got["problems"][0])
        self.assertTrue(self.desk.install_guide(self.guide, "metro_guide", replace=True)["ok"])

    def test_a_draft_claims_no_controller_that_was_not_said_to_be_of_its_kind(self):
        self.desk.install_guide(self.guide, "aaa_guide")              # its mark is a path the example also has
        open_box = example_controller.Controller().start()            # this one publishes its API
        self.addCleanup(open_box.stop)
        found = self.desk.onboard("open", open_box.url)
        self.assertEqual(found["family"], "")
        self.assertEqual(found["verbs"]["read_devices"]["status"], "verified")

    def test_the_controller_is_onboarded_from_it_and_a_write_still_waits_for_a_person(self):
        kept = self.desk.install_guide(self.guide, "metro_guide", "Metro", "metro-user-guide.md")
        self.assertTrue(kept["ok"] and kept["draft"], kept)
        self.assertIn("metro_guide", [k["kind"] for k in self.desk.kinds()])
        nothing = self.desk.onboard("silent", self.box.url)             # which kind it is was not said
        self.assertIn("publishes no API description", nothing["api"]["problem"])
        found = self.desk.onboard("metro", self.box.url, kind="metro_guide")
        self.assertEqual(found["family"], "metro_guide")
        self.assertTrue(found["conformance"]["verdict"].startswith("accepted"), found["conformance"]["verdict"])
        self.assertEqual(found["verbs"]["read_devices"]["status"], "verified")
        self.assertEqual(found["verbs"]["provision"]["status"], "needs confirmation")
        self.assertEqual(len(self.desk.read("metro", "read_devices")["data"]["switches"]), 3)
        body = {"name": "demo", "a_end": "sw1", "z_end": "sw3", "vlan": 20}
        refused = self.desk.write("metro", "provision", body, approved_by="ada")
        self.assertFalse(refused["ok"])
        self.assertEqual(self.box.services, {})                         # nothing was sent on a guide's word
        self.desk.confirm("metro", "provision", "ada")
        self.assertTrue(self.desk.dry_run("metro", "provision", body)["ok"])
        self.assertEqual(self.box.services, {})                         # nor by a dry run
        self.assertTrue(self.desk.write("metro", "provision", body, approved_by="ada")["ok"])
        self.assertEqual(self.box.services["demo"]["z_end"], "sw3")


class StandardFormTests(unittest.TestCase):
    """The form is published as a schema and a template; both have to say what
    the code that reads a manifest says."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(library.__file__), "manifest.schema.json")) as fh:
            self.schema = json.load(fh)
        self.files = sorted(glob.glob(os.path.join(SHIPPED, "*.yaml")) + glob.glob(os.path.join(os.path.dirname(EXAMPLE), "*.yaml")))

    def test_the_schema_names_the_contracts_verbs_and_the_sections_a_manifest_has(self):
        from agentic_mcp.verbs import VERBS
        self.assertEqual(sorted(self.schema["properties"]["verbs"]["propertyNames"]["enum"]), sorted(VERBS))
        self.assertEqual(set(self.schema["properties"]), set(library.SECTIONS))
        self.assertEqual(set(self.schema["properties"]["verbs"]["additionalProperties"]["properties"]),
                         set(library.VERB_KEYS))
        self.assertEqual(tuple(self.schema["properties"]["manifest"]["enum"]), library.FORMATS)

    def test_every_manifest_here_and_the_template_fit_the_schema_and_the_checker(self):
        import jsonschema
        self.assertGreaterEqual(len(self.files), 4)   # two fixtures, the example and the template
        for path in self.files:
            doc = library.read(path)
            self.assertIsNotNone(doc, path)
            wrong = [e.message for e in jsonschema.Draft7Validator(self.schema).iter_errors(doc)]
            self.assertEqual((wrong, library.validate(doc)), ([], []), path)

    def test_what_the_checker_refuses_the_schema_refuses(self):
        import jsonschema
        bad = {"manifest": library.FORMAT, "kind": "Metro Net", "recognise": {}, "colour": "blue",
               "verbs": {"fly": {"operation": "GET /x"}, "health": {"operation": "fetch /health"}}}
        self.assertEqual(len(library.validate(bad)), 5)
        said = " ".join(e.message for e in jsonschema.Draft7Validator(self.schema).iter_errors(bad))
        for part in ("Metro Net", "colour", "fly", "fetch /health"):
            self.assertIn(part, said)

    def test_a_draft_from_a_user_guide_fits_the_schema(self):
        import jsonschema
        with open(GUIDE) as fh:
            got = library.draft_from_guide(fh.read(), "metro_guide")
        self.assertEqual(list(jsonschema.Draft7Validator(self.schema).iter_errors(got["manifest"])), [])
