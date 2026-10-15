"""A verb is defined in one place, contract.json: its tool, its words, what it
means and its examples come from there, and a verb added there alone is
served, understood, mapped at onboarding and accepted in manifests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(HERE, "agentic_mcp", "contract.json")
SCHEMA = os.path.join(HERE, "agentic_mcp", "manifest.schema.json")

NEW_VERB = {"why": "optical power at each port", "methods": ["GET"],
            "for": {"/power": 6, "pm/current": 5}, "against": ["threshold"],
            "tool": "list_power", "help": "optical power levels at the ports",
            "words": r"\b(power|dbm)\b", "words_rank": 9.5,
            "examples": ["how strong is the light at the ports"]}


class SingleSourceTests(unittest.TestCase):
    def test_every_verb_names_its_tool_in_the_contract(self):
        from agentic_mcp import agent, tools
        from agentic_mcp.verbs import VERBS
        self.assertEqual(set(tools.VERB_TOOLS), set(VERBS))
        self.assertTrue(all(rule.get("tool") for rule in VERBS.values()))
        self.assertEqual({v for v, _ in agent.WORDS} - {"onboard", "describe", "controllers"},
                         {v for v, r in VERBS.items() if r.get("words")})

    def test_the_published_schema_names_the_contracts_verbs(self):
        from agentic_mcp import library
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        copy = os.path.join(d, "schema.json")
        shutil.copy(SCHEMA, copy)
        self.assertEqual(library.sync_schema(copy), 0)        # in step with the contract as shipped

    def test_a_verb_added_to_the_contract_alone_is_served_understood_and_accepted(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        contract = json.load(open(CONTRACT))
        next(dom for dom in contract["domains"] if dom["name"] == "resources")["verbs"]["read_power"] = NEW_VERB
        path = os.path.join(d, "contract.json")
        json.dump(contract, open(path, "w"))
        schema = os.path.join(d, "schema.json")
        shutil.copy(SCHEMA, schema)
        probe = textwrap.dedent(f"""
            import json, re
            from agentic_mcp import agent, tools, library, verbs
            assert tools.VERB_TOOLS["read_power"] == "list_power"
            order = [v for v, _ in agent.WORDS]
            assert order.index("read_statistics") < order.index("read_power") < order.index("read_alarms"), order
            assert agent.HELP["read_power"] == "optical power levels at the ports"
            assert ("how strong is the light at the ports", "read_power") in agent.EXAMPLES
            hits = [v for v, p in agent.WORDS if re.search(p, "what is the power on the ports")]
            assert "read_power" in hits, hits
            ops = [{{"method": "GET", "path": "/api/pm/current/power", "params": [], "writes": False, "id": "x"}}]
            assert verbs.score("read_power", ops[0]) > 0
            doc = {{"manifest": library.FORMAT, "kind": "metro", "recognise": {{"operation_contains": "/api"}},
                   "verbs": {{"read_power": {{"operation": "GET /api/pm/current/power"}}}}}}
            assert library.validate(doc) == [], library.validate(doc)
            assert library.sync_schema({schema!r}) == 3
            s = json.load(open({schema!r}))
            assert "read_power" in s["properties"]["verbs"]["propertyNames"]["enum"]
            print("ok")
        """)
        got = subprocess.run([sys.executable, "-c", probe], cwd=HERE, capture_output=True, text=True,
                             env={**os.environ, "AGENTIC_MCP_CONTRACT": path})
        self.assertEqual(got.stdout.strip(), "ok", got.stderr[-2000:])


class EnvFileTests(unittest.TestCase):
    def test_an_instance_reads_the_credentials_file_it_is_given(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        env = os.path.join(d, "site-a.env")
        open(env, "w").write("SITE_A_USER=ada\n")
        probe = "import os; from agentic_mcp.__main__ import _load_env; _load_env(); print(os.environ.get('SITE_A_USER'))"
        got = subprocess.run([sys.executable, "-c", probe], cwd=HERE, capture_output=True, text=True,
                             env={**{k: v for k, v in os.environ.items() if k != "SITE_A_USER"},
                                  "AGENTIC_MCP_ENV_FILE": env})
        self.assertEqual(got.stdout.strip(), "ada", got.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
