"""
  python -m agentic_mcp onboard <key> --url URL [--kind K] [--user-env V --password-env V] [--spec S]
  python -m agentic_mcp serve [--port 8400]          run the agentic MCP as its own service
  python -m agentic_mcp ask "list the devices on onos"
  python -m agentic_mcp install <manifest file>...   put controller manifests in the corpus
  python -m agentic_mcp learn <kind> <user guide>    draft a manifest from a guide or API document
  python -m agentic_mcp kinds                        the kinds there is a manifest for
  python -m agentic_mcp list
  python -m agentic_mcp show [<key>]
  python -m agentic_mcp read <key> <verb> [name=value ...]
  python -m agentic_mcp tools <key>
  python -m agentic_mcp call <key> <tool> ['{"json": "arguments"}']
  python -m agentic_mcp confirm <key> <verb> --by NAME [--operation "POST /path"]
  python -m agentic_mcp draft <key> <verb> <argument>...   the model drafts a body template
  python -m agentic_mcp remove <key>           forget a controller
  python -m agentic_mcp pending <key>          writes queued for approval
  python -m agentic_mcp approve <key> <id>     approve one and send it
  python -m agentic_mcp reject <key> <id>

Credentials are read from the environment, under the variable names given at
onboarding (controllers.env is loaded when it is there).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import contract, manifest as M
from .desk import Desk
from .session import Session, rows


def _load_env() -> None:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controllers.env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    name, value = line.split("=", 1)
                    os.environ.setdefault(name.strip(), value.strip().strip("'\""))
    except OSError:
        pass


def main(argv=None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(prog="python -m agentic_mcp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    on = sub.add_parser("onboard")
    on.add_argument("key")
    on.add_argument("--url", required=True)
    on.add_argument("--label", default="")
    on.add_argument("--user-env", default="")
    on.add_argument("--password-env", default="")
    on.add_argument("--spec", default="")
    on.add_argument("--kind", default="", help="the controller manifest it is of, when it cannot be recognised")
    on.add_argument("--docs", action="append", default=[], help="a documentation page to read operations from")
    sub.add_parser("list")
    sub.add_parser("kinds")
    sv = sub.add_parser("serve", help="run the agentic MCP as a service of its own")
    sv.add_argument("--host", default=os.getenv("AGENTIC_MCP_BIND", "127.0.0.1"))
    sv.add_argument("--port", type=int, default=int(os.getenv("AGENTIC_MCP_PORT", "8400")))
    ak = sub.add_parser("ask", help="ask the agentic MCP in words")
    ak.add_argument("text")
    ak.add_argument("--confirm", action="store_true")
    ak.add_argument("--by", default="")
    ins = sub.add_parser("install")
    ins.add_argument("files", nargs="+")
    ins.add_argument("--if-missing", action="store_true",
                     help="leave a kind alone when the corpus already has a manifest for it")
    ln = sub.add_parser("learn", help="draft a controller manifest from its user guide")
    ln.add_argument("kind")
    ln.add_argument("file")
    ln.add_argument("--title", default="")
    ln.add_argument("--replace", action="store_true", help="put the draft in place of a manifest a person wrote")
    ln.add_argument("--print", action="store_true", dest="show", help="print the draft and keep nothing")
    sh = sub.add_parser("show")
    sh.add_argument("key", nargs="?")
    rd = sub.add_parser("read")
    rd.add_argument("key")
    rd.add_argument("verb")
    rd.add_argument("params", nargs="*")
    tl = sub.add_parser("tools")
    tl.add_argument("key")
    cl = sub.add_parser("call")
    cl.add_argument("key")
    cl.add_argument("tool")
    cl.add_argument("arguments", nargs="?", default="")
    for name in ("pending", "approve", "reject"):
        q = sub.add_parser(name)
        q.add_argument("key")
        if name != "pending":
            q.add_argument("id")
    dr = sub.add_parser("draft")
    dr.add_argument("key")
    dr.add_argument("verb")
    dr.add_argument("arguments", nargs="+", help="the argument names the body should take")
    dr.add_argument("--save", action="store_true", help="keep the draft as the verb's template")
    rm = sub.add_parser("remove")
    rm.add_argument("key")
    cf = sub.add_parser("confirm")
    cf.add_argument("key")
    cf.add_argument("verb")
    cf.add_argument("--by", required=True)
    cf.add_argument("--operation", default="")
    args = ap.parse_args(argv)

    if args.cmd == "onboard":
        found = Desk(background=False).onboard(
            args.key, args.url, label=args.label, user_env=args.user_env,
            password_env=args.password_env, spec=args.spec, kind=args.kind, docs=args.docs)
        if found.get("kept"):
            print(f"{args.key}: {found['kept']}; kept the manifest it had")
        print(M.summary(found))
        print(contract.text(found["conformance"]))
        return 0
    if args.cmd == "serve":
        from .service import serve
        serve(args.host, args.port)
        return 0
    if args.cmd == "ask":
        from . import agent
        from .desk import desk as the_desk
        got = agent.ask(the_desk(), args.text, confirm=args.confirm, approved_by=args.by)
        print(got.get("answer", ""))
        u = got.get("understood") or {}
        print(f"\n(understood as {u.get('action')} on {u.get('controller')}, by {u.get('by')}, {u.get('ms')} ms)")
        return 0 if got.get("ok") else 1
    if args.cmd == "kinds":
        from . import library
        for doc in library.all_manifests():
            problems = library.validate(doc)
            print(f"{doc['kind']:<14} {doc.get('title', ''):<26} verbs {len(doc.get('verbs') or {}):<3} "
                  f"tools {len(doc.get('tools') or []):<3} " + ("ok" if not problems else "; ".join(problems))
                  + f"   {os.path.relpath(doc['_file'])}")
        return 0
    if args.cmd == "install":
        from . import library
        failed = 0
        for path in args.files:
            doc = library.read(path)
            if args.if_missing and doc and library.get(doc.get("kind", "")):
                continue
            got = library.install(path)
            if got["ok"]:
                print(f"{got['kind']}: manifest in the corpus ({os.path.relpath(got['file'])}), "
                      f"{len(got['verbs'])} verbs, {got['tools']} tools; guide written for the retriever")
            else:
                failed = 1
                print(f"{path}: not installed: " + "; ".join(got["problems"]), file=sys.stderr)
        return failed
    if args.cmd == "learn":
        from . import library
        with open(args.file, "rb") as fh:
            data = fh.read()
        name = os.path.basename(args.file)
        if args.show:
            got = library.draft_from_guide(library.text_of(data, name), args.kind, args.title, name)
        else:
            got = library.install_guide(data, args.kind, args.title, name, replace=args.replace)
        if not got.get("ok"):
            print(f"{args.file}: no manifest drafted: " + "; ".join(got.get("problems") or []), file=sys.stderr)
            return 1
        if args.show:
            print(got["yaml"])
            return 0
        print(f"{got['kind']}: draft manifest kept ({os.path.relpath(got['file'])}): {got['operations']} operations "
              f"read out of {got['read_as']}, {got['reads']} that read and {got['writes']} that change something.\n"
              f"Next: python -m agentic_mcp onboard <key> --url <its address> --kind {got['kind']}")
        return 0
    if args.cmd == "list":
        for key in M.keys():
            found = M.load(key) or {}
            report = contract.check(found)
            print(f"{found.get('label', key):<13} {found.get('base_url', ''):<36} "
                  f"{(found.get('domain') or {}).get('kind', '?'):<11} {report['verdict']}")
        return 0
    if args.cmd == "show":
        for key in ([args.key] if args.key else M.keys()):
            found = M.load(key)
            print(M.summary(found) if found else f"{key}: not onboarded")
            if found:
                print(contract.text(contract.check(found)))
        return 0
    if args.cmd == "remove":
        gone = [n for n in os.listdir(M.DATA) if n.startswith(args.key + ".") and n.endswith(".json")]
        for name in gone:
            os.remove(os.path.join(M.DATA, name))
        print(f"{args.key}: removed" if gone else f"{args.key}: not onboarded")
        return 0
    if args.cmd == "draft":
        from . import advisor
        found = M.load(args.key)
        entry = ((found or {}).get("verbs") or {}).get(args.verb) or {}
        if not entry.get("operation"):
            print(f"{args.key} has no operation for {args.verb}", file=sys.stderr)
            return 1
        got = advisor.draft_template(entry["operation"], args.arguments)
        print(json.dumps(got, indent=1))
        if not got.get("ok"):
            return 1
        trial = Session({**found, "verbs": {args.verb: {**entry, "template": got["template"]}}}).dry_run(
            args.verb, **{a: f"<{a}>" for a in args.arguments})
        print("dry run with placeholder values:", "fits the published schema" if trial.get("ok")
              else "does NOT fit: " + "; ".join(trial.get("problems") or [trial.get("error", "")]))
        if args.save:
            M.set_template(found, args.verb, got["template"], by=got["by"])
            M.save(found)
            print(f"kept as the template of {args.verb}. It is unconfirmed: read it, then "
                  f"python -m agentic_mcp confirm {args.key} {args.verb} --by <you>")
        return 0
    if args.cmd in ("pending", "approve", "reject"):
        desk = Desk(background=False)
        seat = desk.seat(args.key)
        if args.cmd == "pending":
            rows = desk.queue.pending(args.key)
            print("\n".join(f"{r['id']}  {r['action']}  {r.get('target') or ''}  {r.get('summary') or ''}"
                            for r in rows) or f"nothing queued for {args.key}")
            return 0
        if args.cmd == "reject":
            print(json.dumps(desk.queue.reject(args.id, by="operator (cli)")))
            return 0
        print(json.dumps(seat.session.apply_queued(args.id, by="operator (cli)"), indent=1, default=str))
        return 0
    if args.cmd == "tools":
        seat = Desk(background=False).seat(args.key)
        for tool in seat.tools.values():
            print(f"{'write' if tool.write else 'read ':<5} {tool.name}")
        return 0
    if args.cmd == "call":
        import asyncio
        seat = Desk(background=False).seat(args.key)
        params = json.loads(args.arguments) if args.arguments else {}
        got = asyncio.run(seat.local.call_tool(args.tool, params))
        print(got.content[0].text)
        return 1 if got.isError else 0
    found = M.load(args.key)
    if not found:
        print(f"{args.key}: not onboarded", file=sys.stderr)
        return 1
    if args.cmd == "read":
        params = dict(p.split("=", 1) for p in args.params)
        got = Session(found).read(args.verb, **params)
        data = got.pop("data", None)
        print(json.dumps(got), f"rows={len(rows(data))}")
        print(json.dumps(data)[:600])
        return 0 if got.get("ok") else 1
    if args.cmd == "confirm":
        try:
            with open(os.path.join(M.DATA, f"{args.key}.operations.json")) as fh:
                operations = json.load(fh)
        except (OSError, ValueError):
            operations = []
        M.confirm(found, args.verb, args.by, args.operation, operations)
        M.save(found)
        print(M.summary(found))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
