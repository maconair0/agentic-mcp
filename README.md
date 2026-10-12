# Agentic MCP

One service that every SDN controller is onboarded to, and that an agent talks
to in order to reach them. It replaces a hand-written MCP server per
controller: a controller is added by uploading a document about it, not by
writing code.

```
        your agent            an operator, a script
            │ MCP over SSE          │ HTTP
            └──────────┬────────────┘
                ┌──────┴───────┐    its own small model, for requests in words
                │ agentic MCP  │    its own manifests, approval queue, audit log
                └─┬───┬───┬──┬─┘
          REST    │   │   │  │      one session each; no controller reaches another
        TeraFlowSDN ONOS Ryu TransportPCE  ... yours
```

## How it works

1. **It learns what a kind of controller is from a document.** Either the
   controller publishes its own API (OpenAPI, Swagger, RESTCONF), or you upload
   a **controller manifest** in the [standard format](MANIFEST_FORMAT.md), or
   you upload the controller's **user guide** and a manifest is drafted from it.
2. **It onboards a controller from its address.** It reads the API, works out
   whether the controller is optical, packet or multi-layer, and maps a fixed
   set of 18 verbs (read devices, read links, read services, compute path,
   provision, teardown, ...) onto the controller's operations.
3. **It tries every read** on the controller. A read counts as verified only
   when it answered.
4. **It checks the result against a contract** (`agentic_mcp/contract.json`).
   A controller is accepted when every required kind of work has a verb it can
   serve; the report names what is missing.
5. **It serves the controller** as tools over MCP, as verbs over HTTP, and to
   requests in words.

A write is never sent because a name matched or a guide said so. A person
confirms which operation a write verb is, every write is first a dry run, and a
write is sent only with the name of who approved it. Every write, sent or
refused, is in the audit log.

## Quick start

Python 3.10 or newer.

```bash
pip install -r requirements.txt

python run_example_controller.py 9100 &          # a small controller to try it on
python -m agentic_mcp serve --port 8400 &
```

Give it the example controller's manifest, onboard the controller, ask:

```bash
curl -X POST 'localhost:8400/manifests?name=metro.yaml' --data-binary @manifests/example/metro.yaml

curl -X POST localhost:8400/controllers -H 'content-type: application/json' \
     -d '{"key": "metro", "url": "http://127.0.0.1:9100/api"}'

curl -X POST localhost:8400/ask -H 'content-type: application/json' \
     -d '{"text": "list the devices on metro"}'
```

```
metro: 3 devices
  metro-1  sw1  up
  metro-2  sw2  up
  metro-3  sw3  up
```

A change asked for in words is answered with a dry run:

```bash
curl -X POST localhost:8400/ask -H 'content-type: application/json' \
     -d '{"text": "on metro create service name=demo a_end=sw1 z_end=sw3 vlan=20"}'
```

```
metro, dry run of provision:
  POST http://127.0.0.1:9100/api/services  {"name": "demo", "a_end": "sw1", "z_end": "sw3", "vlan": 20}
  Nothing was sent. Ask again with confirm and who approved it to send it.
```

Add `"confirm": true, "approved_by": "ada"` to the same request and it is sent.

## Adding your controller

There are three ways, from least to most work. All of them end with the same
`onboard`.

### 1. It describes itself

If the controller publishes an OpenAPI or Swagger document, or speaks RESTCONF,
onboarding needs only its address:

```bash
python -m agentic_mcp onboard north --url http://10.0.0.5:8181/onos/v1 \
       --user-env NORTH_USER --password-env NORTH_PASSWORD
```

Credentials are never typed into a request or stored. Put them in
`controllers.env` (see `controllers.env.example`) and name the two settings.

Reads work at once. Each write verb waits until you confirm which operation it
is: `python -m agentic_mcp confirm north provision --by ada`.

### 2. You upload its user guide

For a controller that publishes nothing a program can read, upload the part of
its guide that lists the API: text, Markdown, HTML, an OpenAPI file, or a PDF
(with `pip install pypdf`).

```bash
python -m agentic_mcp learn metro_guide manifests/example/metro-user-guide.md
# or: curl -X POST 'localhost:8400/guides?kind=metro_guide' --data-binary @guide.html

python -m agentic_mcp onboard quiet --url http://127.0.0.1:9101/api --kind metro_guide
```

A manifest in the standard format is drafted from it and kept. An operation is
found where a method stands beside a path (`GET /api/switches`, or a table with
Method and URI). The draft lists operations and states no verb, so nothing is
trusted because a guide said it: reads are tried on the controller, and every
write waits for your confirmation and takes the body you give it.

`python -m agentic_mcp learn <kind> <file> --print` shows the draft without
keeping it. It is the starting point for way 3.

### 3. You write its manifest

A manifest states each verb outright: the operation, its arguments, the body to
send, how to read the answer as plain rows, and a limit per hour. This is what
makes a controller fully usable, and stating a verb is the confirmation of it.

```bash
cp manifests/example/template.yaml mykind.yaml       # fill it in
python -m agentic_mcp install mykind.yaml            # checked, then kept
python -m agentic_mcp onboard north --url http://10.0.0.5:8080/api
```

The format is described in [MANIFEST_FORMAT.md](MANIFEST_FORMAT.md) and, for a
program, in `agentic_mcp/manifest.schema.json`. `manifests/` holds the
manifests of four real controllers (TeraFlowSDN, ONOS, Ryu, TransportPCE), to
use (`python -m agentic_mcp install manifests/*.yaml`) or to read as examples.
None of the four has any code here.

## Talking to it

**MCP:** `http://<host>:8400/mcp/sse`. One server holds every controller's
tools, plus `desk_controllers`, `desk_kinds`, `desk_onboard` and `desk_ask`. A
write tool takes `approved_by`. This needs the `mcp` package, version 1.x.

**HTTP:**

| Address | |
|---|---|
| `GET /health`, `GET /topology` | itself, and every controller it holds |
| `GET /manifests`, `POST /manifests` | the kinds it knows; upload a manifest (the body is the YAML or JSON) |
| `POST /guides?kind=K` | upload a user guide; a manifest is drafted for kind K |
| `GET /manifests/<kind>/guide` | a kind's manifest as prose |
| `GET /controllers`, `POST /controllers` | the controllers; onboard one (`key`, `url`, `kind`, `user_env`, `password_env`, `docs`) |
| `GET /controllers/<key>`, `DELETE` | what was learned about one; forget it |
| `GET /controllers/<key>/tools`, `POST .../call` | its tools; call one (`tool`, `arguments`, `approved_by`) |
| `POST /controllers/<key>/read`, `dry-run`, `write`, `confirm` | by verb (`verb`, `arguments`, `body`, `approved_by`) |
| `GET /queue` | writes waiting for a person's approval |
| `POST /ask` | a request in words: `{"text": "list the devices on onos"}` |

**From Python:**

```python
from agentic_mcp.client import RemoteDesk
desk = RemoteDesk("http://localhost:8400")
desk.read("metro", "read_links")["data"]
```

**Command line:** `python -m agentic_mcp --help` (`serve`, `onboard`, `learn`,
`install`, `kinds`, `list`, `show`, `tools`, `call`, `read`, `confirm`,
`pending`, `approve`, `reject`, `ask`, `remove`).

## The agent inside it

A request in words has to become a controller, a verb and arguments. A request
that says all three plainly is read by rule, in a few milliseconds and with no
model. A small model is asked only for the rest ("anything complaining on the
optical core"), and only to choose from the onboarded controllers and the
contract's verbs. What it answers is checked against those lists before
anything is done, so a wrong answer is a request not understood and never a
call nobody asked for. It never decides a write.

The model is served by [Ollama](https://ollama.com). The default,
`qwen2.5:3b-instruct-q4_K_M` (1.9 GB), got 14 of 16 loosely worded requests
right in about 1.8 seconds on a CPU. Without a model everything else works;
only loosely worded requests go unanswered.

## The contract

| Kind of work | Required | Verbs |
|---|---|---|
| inventory | yes | read devices, read device, read ports |
| topology | yes | read links, read hosts |
| services | yes | read services, read service |
| path computation | no | compute path, check feasibility |
| resources | no | read spectrum, read statistics |
| provisioning | yes | provision, teardown |
| adjustment | no | adjust, reroute |
| health | yes | health, read alarms |
| device configuration | no | configure device |

To extend it, add a verb or a kind of work to `agentic_mcp/contract.json`.
Nothing else needs to change for it to be checked.

## Settings

| Setting | Meaning |
|---|---|
| `AGENTIC_MCP_PORT`, `AGENTIC_MCP_BIND` | where it listens (8400, 127.0.0.1) |
| `AGENTIC_MCP_DATA` | its folder: manifests, onboarded controllers, queue, audit log (`agentic_mcp/data`) |
| `AGENTIC_MCP_TOKEN` | when set, anything that is not a read of the service needs `Authorization: Bearer <token>`, and so does the MCP endpoint |
| `AGENTIC_MCP_MODEL` | the model for requests in words |
| `AGENTIC_MCP_MODEL_URL` | the Ollama that serves it; another machine works |
| `AGENTIC_MCP_MODEL_GPU=0` | keep the model on the CPU |
| `AGENTIC_MCP_TIMEOUT` | seconds to wait for a controller (20) |
| `AGENTIC_MCP_CONNECTIONS` | connections kept to each controller (32) |

## What it does not do

- It signs in to a controller with basic authentication or none. A controller
  that needs a token or OAuth is not supported yet.
- From a user guide it learns which operations exist, not what their bodies
  look like. Those come from you, or from a manifest.
- The MCP endpoint is written for `mcp` 1.x. With 2.x installed the service
  starts and serves HTTP only, and `/health` says so.
- With no `AGENTIC_MCP_TOKEN` set, anyone who can reach the port can use it. It
  listens on 127.0.0.1 unless told otherwise.

## Tests

```bash
python -m unittest discover -s tests -t .
```

About 100 tests, a few seconds, no network: the example controller runs in the
test process.

## Layout

```
agentic_mcp/
  contract.json          the verbs every controller is asked in
  manifest.schema.json   the manifest format, for a program
  library.py             manifests: read, checked, drafted from a user guide
  discover.py            what a controller says about its own API
  verbs.py               the domain, and which operation serves which verb
  manifest.py            the record of one onboarded controller
  session.py             reads, dry runs, writes, limits, the audit log
  tools.py               named tools made from a manifest
  queue.py               writes waiting for a person, and what was applied
  desk.py                every controller's seat
  agent.py               requests in words
  service.py             HTTP and MCP
  client.py              the service, from another Python process
manifests/               four real controllers, an example, a template
tests/
```
