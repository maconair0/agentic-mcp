# The controller manifest: `agentic-mcp.controller/v1`

A controller manifest is one document that says what a kind of SDN controller
is and how it is used. It is the only thing needed to add a new kind of
controller. It is written once per kind, by whoever knows the controller, in
YAML or JSON.

- Start from [manifests/example/template.yaml](manifests/example/template.yaml).
- A small complete one: [manifests/example/metro.yaml](manifests/example/metro.yaml).
- The same rules for a program: [agentic_mcp/manifest.schema.json](agentic_mcp/manifest.schema.json) (JSON Schema, draft 7).

## Using one

```bash
python -m agentic_mcp install mykind.yaml        # checks it and keeps it
curl -X POST 'localhost:8400/manifests?name=mykind.yaml' --data-binary @mykind.yaml
python -m agentic_mcp kinds                      # every kind there is a manifest for
```

The upload answers whether the manifest is well formed, and names every problem
when it is not. A newer manifest uploaded for the same kind replaces the old
one; onboard the controller again to use it.

No manifest yet? `python -m agentic_mcp learn <kind> <user guide> --print`
drafts one from the controller's user guide: the operations it names, in this
format, ready for you to add the verbs to.

## Sections

Only `manifest`, `kind` and `recognise` are required.

| Section | What it holds |
|---|---|
| `manifest` | `agentic-mcp.controller/v1` |
| `kind` | a short lower-case name for this kind of controller (`metro`, `mykind`) |
| `title`, `version`, `about` | for people. `version: draft` marks a manifest drafted from a user guide |
| `domain` | `optical`, `packet` or `multi-layer`; left out, it is worked out from the words in the API |
| `recognise` | how a controller of this kind is told from any other: `fingerprint` (an address only it answers, with JSON) or `operation_contains` (a path only its API has) |
| `api` | `openapi`: where its API document is, if not at a usual address. `docs`: documentation pages to read operations from. `style`: how its named tools answer |
| `defaults` | arguments that have a usual value, e.g. `context_uuid: admin` |
| `operations` | its operations, for a controller that publishes none: `method`, `path`, `summary`, `writes` |
| `verbs` | how each verb of the contract is done (below) |
| `tools` | named tools, each one request or a few, for a caller that uses tools by name |
| `provides` | which named tool serves which verb |
| `replay` | when to send again what was approved (below) |
| `quirks` | what its API does not say and use has shown, one sentence each |
| `limits` | limits in words, for people |
| `guide` | prose about working with it |

## A verb

The verbs are the contract's ([agentic_mcp/contract.json](agentic_mcp/contract.json)):

`read_devices` `read_device` `read_ports` `read_links` `read_hosts`
`read_services` `read_service` `compute_path` `check_feasibility`
`read_spectrum` `read_statistics` `provision` `teardown` `adjust` `reroute`
`health` `read_alarms` `configure_device`

```yaml
verbs:
  read_devices:
    operation: GET /api/switches
    view: {rows: switches, id: id, name: name, type: kind, status: status}
  provision:
    operation: POST /api/services
    arguments: {name: a name for the service, a_end: one end, z_end: the other end}
    body: {name: $name, a_end: $a_end, z_end: $z_end}
    policy: {max_per_hour: 20}
```

| Key | Meaning |
|---|---|
| `operation` | `METHOD /path`, with `{argument}` where one goes in the address |
| `steps` | several requests in order, each with its own `operation` and `body`, when one is not enough |
| `arguments` | each argument and what it is, shown to whoever is asked for it |
| `body` | the body to send, with `$argument` where one goes. `{$int: $x}`, `{$num: $x}`, `{$str: $x}`, `{$add: [$x, 4096]}` and `{$concat: [a, $x]}` compute a value |
| `query` | query parameters: `{name in the address: argument}` |
| `view` | how to read the answer as plain rows: `rows` is where the list is (`a.b`), every other key is a field and where it is in a row |
| `policy` | `max_per_hour`: writes of this verb beyond it are refused |
| `reads` | `true` for a POST that only reads (a path computation) |
| `why` | a sentence shown beside it |

**What stating a verb means.** A verb stated in a manifest takes the place of
one matched by name, and stating it is the confirmation of that mapping: a
person wrote it down. Verbs a manifest leaves out are still matched by name
from the controller's API, and a write matched that way waits for an operator's
confirmation. A manifest drafted from a user guide states no verb for exactly
this reason.

## A named tool

`tools` is for a caller that uses tools by name, such as an agent over MCP.
Each has a `name`, a `description` and a `schema` of its arguments (JSON
Schema), and one of:

| Key | Meaning |
|---|---|
| `method`, `path`, `body`, `query` | one request |
| `steps` | several requests, each `as` a name; `foreach: a.b` makes one per item of an earlier answer (`{item}` in the path) |
| `verb` | the requests of a verb stated above |
| `queue: true` | do not send: check, queue for approval and answer with an id. `summary` and `target` describe it |
| `approve: {token_env: NAME}` | approve and send one queued id, for whoever holds the token in that setting |
| `check` | rules on the arguments: `between: {vlan_id: [1, 4094]}`, `distinct: [[a, b]]`, `keys: {match: [...]}` |
| `convert: {dpid: number}` | read an argument as a number however it is written |
| `reply` | `{raw: true}` hands back what the controller said; `{const: ...}`, `{pluck: ...}`, `{collect: ...}` (one list out of a `foreach`) and `{text: [...]}` shape it |

A step can also carry `timeout`, `empty_if` (text that means "nothing there
yet"), `expect` (`{at: a.b, all_equal: inService, say: why}` or `{find: key,
in: [...]}`) and `quiet_after` (refuse while a write approved here settles).

A controller with no `tools` still has tools: one per verb it serves, named
`<key>_list_devices`, `<key>_create_service` and so on.

## Writes that wait, and putting back what was approved

```yaml
tools:
  - name: mykind_add_flow
    queue: true                              # checked and queued, never sent by this call
    summary: "flow on {dpid}"
    method: POST
    path: /flows/{dpid}
  - name: mykind_approve_request
    approve: {token_env: MYKIND_APPROVER_TOKEN}

replay: {when_new_in: health, every_s: 15}
```

A queued write is sent once, by whoever holds the token named in `approve`, or
from the command line (`python -m agentic_mcp pending|approve|reject <key>`).
`replay` sends again everything applied on the controller, oldest first, when
something new appears in that read: a switch that came back empty gets its
flows back.

## Checking one

```bash
python -m agentic_mcp install mykind.yaml     # refuses a manifest that is not well formed, and says why
python -m agentic_mcp show north              # after onboarding: each verb, its operation and how far it is trusted
```

After onboarding, each verb has one of these states:

| State | Meaning |
|---|---|
| declared | the manifest states it; a person wrote it down |
| verified | a read that was run and answered |
| mapped | a read matched by name, not run because it needs arguments |
| answered empty | a read that was run and holds nothing now |
| needs confirmation | a write matched by name; a person has to confirm it before it is used |
| confirmed | a write a person confirmed |
| missing | the controller has nothing for this verb |
| not applicable | the verb does not exist in this controller's domain |

