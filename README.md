<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.png"><img src="docs/logo.png" alt="Agentic MCP: one contract, every controller" width="560"></picture></p>

**One service every SDN controller is onboarded to, and that an agent talks to in order to reach them.** It
replaces a hand-written MCP server per controller: you add a controller by giving it a document about the
controller, not by writing code.

```
   your agent (MCP)      an operator, a script (HTTP)
            └──────────┬──────────┘
                 agentic MCP          learns each controller, checks it against one contract
              ┌────┬───┴────┬───┐
           ONOS  Ryu  TeraFlowSDN  TransportPCE  …
```

- **Learns a controller** from its own API (OpenAPI, RESTCONF), from a manifest, or from its user guide.
- **One contract:** 18 verbs (read devices, read links, provision, teardown, …) mapped onto each controller's
  operations, with a verdict on what each one covers.
- **Safe writes:** a person confirms what each write is, every write is first a dry run, and nothing is sent
  without the name of who approved it. Everything is in the audit log.
- **Three ways in:** MCP tools, verbs over HTTP, and requests in plain words.

## Quick start

```bash
pip install -r requirements.txt
python run_example_controller.py 9100 &                    # a small controller to try it on
python -m agentic_mcp serve --port 8400 &

curl -X POST 'localhost:8400/manifests?name=metro.yaml' --data-binary @manifests/example/metro.yaml
curl -X POST localhost:8400/controllers -H 'content-type: application/json' -d '{"key": "metro", "url": "http://127.0.0.1:9100/api"}'
curl -X POST localhost:8400/ask -H 'content-type: application/json' -d '{"text": "list the devices on metro"}'
```

## Learn more

- [The full guide](docs/GUIDE.md): installing it where it runs, adding controllers and verbs, the HTTP and MCP
  interfaces, plugging in an agent, the contract, settings, scaling out and the layout.
- [The manifest format](MANIFEST_FORMAT.md), and [an agent of your own](examples/ollama_agent.py).
- Tests: `python -m unittest discover -s tests -t .`
