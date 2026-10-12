"""
The agentic MCP: one session per SDN controller, learned from the controller.

Each controller used to sit behind its own MCP server, written by hand for that
controller. Adding a controller meant writing another server and then teaching
whatever used it that server's tool names. This replaces the servers: given an
address and credentials it reads what the controller publishes about its own
API (or the controller manifest or user guide uploaded for its kind), works out
which domain the controller is in, and maps a fixed set of verbs (read devices,
read links, read services, compute a path, read spectrum, provision, adjust,
tear down) onto the controller's operations.

  contract.json the verbs every controller is asked in, by domain of work
  library.py    controller manifests: the standard form, checked, drafted
  discover.py   what the controller says about its own API
  verbs.py      the domain, and which operation serves which verb
  manifest.py   the record of one onboarded controller, built and stored
  session.py    the session: reads run, writes need a confirmed mapping and an
                approver
  tools.py      named tools made from a manifest's recipes
  queue.py      writes waiting for a person, and what was applied
  desk.py       every controller's seat; no seat can reach another
  agent.py      a request in words: rules first, a small model for the rest
  service.py    all of it as one service, over HTTP and over MCP
"""
