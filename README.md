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

## Installing it where it will run

The quick start runs everything on one machine. A deployment beside real
controllers (a lab, a NOC, a site with no development tools) needs only Python
3.10 or newer and network access to the controllers. A model is optional (see
[The agent inside it](#the-agent-inside-it)).

```bash
git clone https://github.com/maconair0/agentic-mcp.git /opt/agentic-mcp
cd /opt/agentic-mcp
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp controllers.env.example controllers.env     # the controllers' credentials: names and values
chmod 600 controllers.env                      # only this file ever holds them

export AGENTIC_MCP_DATA=/var/lib/agentic-mcp   # manifests, onboarded controllers, queue, audit log
python -m agentic_mcp install manifests/*.yaml # the kinds you will onboard (or your own manifests)
python -m agentic_mcp onboard core --url http://10.0.0.5:8181/onos/v1 \
       --user-env CORE_USER --password-env CORE_PASSWORD
python -m agentic_mcp show core                # what it serves, verified, and what it lacks
```

**Before it is reachable from another machine**, set these, all in its
environment:

| Setting | Why |
|---|---|
| `AGENTIC_MCP_BIND=0.0.0.0` | it listens on 127.0.0.1 unless told otherwise |
| `AGENTIC_MCP_TOKEN=<a long secret>` | without it, anyone who reaches the port can use it; with it, every non-read call and the MCP endpoint need `Authorization: Bearer <secret>` |
| `AGENTIC_MCP_DATA=/var/lib/agentic-mcp` | keep its state off the code's folder, on storage that is backed up (the audit log and the approval queue are records) |
| `AGENTIC_MCP_PORT` | if 8400 is taken |

It serves plain HTTP. Put TLS in front of it with any reverse proxy (nginx,
Caddy, a load balancer) when the traffic leaves the machine.

**As a service** (systemd), so it starts with the machine and comes back if it
stops:

```ini
# /etc/systemd/system/agentic-mcp.service
[Unit]
Description=Agentic MCP
After=network-online.target

[Service]
User=agentic-mcp
WorkingDirectory=/opt/agentic-mcp
Environment=AGENTIC_MCP_DATA=/var/lib/agentic-mcp
Environment=AGENTIC_MCP_BIND=0.0.0.0
EnvironmentFile=/etc/agentic-mcp/service.env        # AGENTIC_MCP_TOKEN=... (mode 600)
ExecStart=/opt/agentic-mcp/.venv/bin/python -m agentic_mcp serve --port 8400
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now agentic-mcp
curl -s localhost:8400/health          # the controllers it holds, and their sessions
```

**Agents reach it** at `http://<host>:8400/mcp/sse` (MCP), over HTTP, or from
Python with `RemoteDesk("http://<host>:8400", token="<secret>")`.

**Upgrading** is `git pull` and a restart. The state under `AGENTIC_MCP_DATA`
(manifests, onboarded controllers, queue, audit log) is kept as it is.

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

## Adding a verb

A verb is defined in one place: its entry in `agentic_mcp/contract.json`, under
the kind of work it belongs to. Its tool, the words a request uses for it,
what it means and its examples are all read from there. Here is
`read_power`, the optical power at each port, added to `resources`:

```json
"read_power": {
  "why": "optical power at each port",
  "methods": ["GET"],
  "for": {"/power": 6, "pm/current": 5, "optical-power": 5},
  "against": ["threshold", "config"],
  "tool": "list_power",
  "help": "optical power levels at the ports",
  "words": "\\b(power|dbm|launch|received)\\b",
  "words_rank": 9.5,
  "examples": ["how strong is the light at the ports"]
}
```

| Field | What it does |
|---|---|
| `why` | what the verb is for, in reports |
| `methods` | the HTTP methods an operation serving it may use |
| `for`, `against` | path fragments that count for or against an operation being this verb, with weights. Onboarding ranks a controller's operations with them |
| `writes` | `true` for a verb that changes the network. It then waits for a person's confirmation on every controller, and every use is a dry run first |
| `one` | `true` when it reads or changes one named thing (needs an id) |
| `tool` | its tool name: every controller serves it as `<key>_<tool>` |
| `help` | what it means, for the small model choosing a verb from words |
| `words`, `words_rank` | a regular expression read by rule before any model; lower ranks are tried first |
| `examples` | requests in words that mean it, shown to the small model |

Then:

```bash
python -m agentic_mcp sync-schema          # writes the new verb into manifest.schema.json
python -m unittest discover -s tests -t .  # the contract's own checks
```

From that moment:

- every controller onboarded (or onboarded again) is searched for an operation
  that serves it, and a read is tried before it counts;
- a manifest may state it (`verbs: read_power: operation: GET /pm/current/power`);
- it is served as a tool over MCP and HTTP;
- a request in words can reach it;
- conformance reports it, as present or missing.

To give a deployment verbs of its own without changing the installed package,
point `AGENTIC_MCP_CONTRACT` at a copy of `contract.json` with them added.

## Scaling out

One instance holds any number of controllers side by side (see
[Staying up](#staying-up-and-knowing-what-it-holds)). Run more than one when
the controllers belong to different owners, sites or network domains, so that
each instance holds only its own controllers and only their credentials:

```bash
# site A: its optical controllers
AGENTIC_MCP_DATA=/var/lib/agentic-mcp/site-a  AGENTIC_MCP_ENV_FILE=/etc/agentic-mcp/site-a.env \
  python -m agentic_mcp serve --port 8401 &
# site B: its packet controllers
AGENTIC_MCP_DATA=/var/lib/agentic-mcp/site-b  AGENTIC_MCP_ENV_FILE=/etc/agentic-mcp/site-b.env \
  python -m agentic_mcp serve --port 8402 &
```

- **Its own folder** (`AGENTIC_MCP_DATA`): its manifests, its onboarded
  controllers, its queue and its audit log. Nothing is shared.
- **Its own credentials** (`AGENTIC_MCP_ENV_FILE`, mode 600): only the
  settings of its own controllers. One instance cannot sign in to another's
  controllers even by mistake.
- **Its own token and port**, and its own contract if it needs other verbs
  (`AGENTIC_MCP_CONTRACT`).
- **Agents use each instance as its own service.** An orchestrator that spans
  several keeps one `RemoteDesk` (or one MCP connection) per instance. Where
  each instance belongs to an agent of its own domain, those agents talk to
  each other rather than to each other's instances. In the edgeLM project they
  do this over A2A, each agent with its own instance.
- **Per instance, the load is bounded:**
  - `AGENTIC_MCP_PER_CONTROLLER` (requests in flight to one controller, 24)
    keeps a slow controller from taking the others down;
  - `AGENTIC_MCP_THREADS` and `AGENTIC_MCP_CONNECTIONS` size the rest;
  - `GET /sessions` and `GET /health` say how each controller is doing, for
    monitoring.

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
| `GET /sessions`, `GET /sessions/<key>` | every controller's session: up, degraded or down, since when, requests, errors, answer times |
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

## Staying up, and knowing what it holds

Every request to a controller passes one place, which keeps that controller's
session on record: its state, since when, how many requests and errors, how
long it takes to answer. `python -m agentic_mcp sessions` or `GET /sessions`
shows it; it is kept on disk and every change of state is logged.

- A controller that does not answer four times in a row is not asked again for
  a while (20 s, doubling to 5 minutes). Callers are told at once; the
  controller is not hammered while it is down; one request after the pause
  decides.
- Any number of controllers are held side by side. Each has its own session,
  its own connections and its own share of requests in flight (24;
  `AGENTIC_MCP_PER_CONTROLLER`). A controller that hangs fills only its own
  share: its further callers are told at once, and the other controllers'
  callers are served as before. The 30 s look, the topology and a question
  put to several controllers ask each on a thread of its own.
- A read that got no answer is asked once more. A write never is: it may have
  arrived.
- Every controller is looked at every 30 s whether or not anybody uses it.
- An error inside a request is that request's answer, with a reference to find
  it in the log. It is never the service going down.
- The approval queue survives a damaged file: the last good copy is used and
  the damaged one kept aside.

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

To extend it, add a verb or a kind of work to `agentic_mcp/contract.json`: it
is the only place a verb is defined (see [Adding a verb](#adding-a-verb)), and
`python -m agentic_mcp sync-schema` brings the published schema in line.

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
| `AGENTIC_MCP_PER_CONTROLLER` | requests that may be on their way to one controller at once (24) |
| `AGENTIC_MCP_THREADS` | requests served at once in all (enough for every controller's share, at least 64) |
| `AGENTIC_MCP_ENV_FILE` | the credentials file it reads at start (`controllers.env` beside the code); give each instance only its own controllers' |
| `AGENTIC_MCP_CONTRACT` | a contract file to use instead of the packaged `contract.json`, for verbs of your own |

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

About 140 tests, a few seconds, no network: the example controller runs in the
test process.

## Layout

```
agentic_mcp/
  contract.json          the verbs every controller is asked in: the one place a verb is defined
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
