"""
The tools a controller's session offers, and the three places they come from.

  a recipe   one tool is one request: a method, a path with the arguments in
             it, and a rule for the body. It is data (a controller manifest), so a
             controller whose tools are all of this kind needs no code at all.
  several    a recipe of more than one request: several reads handed back as
             one answer, or a write that is checked and queued for a person's
             approval (queue.py). Data too.
  a verb     for a controller nobody has written anything for: one tool per
             verb the manifest maps, named `<key>_list_devices` and so on.

A tool takes named arguments and answers (text, is it an error). The text is
what the caller reads.
"""
from __future__ import annotations

import inspect
import json
import re
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

_PARAM = re.compile(r"\{([^{}]+)\}")
_PY_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean",
             dict: "object", list: "array"}


class Tool:
    def __init__(self, name: str, fn: Callable[..., Any], description: str = "",
                 schema: Optional[Dict[str, Any]] = None, write: bool = False, verb: str = ""):
        self.name = name
        self.fn = fn
        self.description = description or ""
        self.schema = schema or schema_of(fn)
        self.write = write
        self.verb = verb


def schema_of(fn: Callable[..., Any]) -> Dict[str, Any]:
    """A JSON schema of a function's arguments, from its signature."""
    props, required = {}, []
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        params = []
    for p in params:
        if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        kind = p.annotation if isinstance(p.annotation, type) else getattr(p.annotation, "__origin__", None)
        entry: Dict[str, Any] = {"title": p.name.replace("_", " ").title()}
        if kind in _PY_TYPES:
            entry["type"] = _PY_TYPES[kind]
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
        else:
            entry["default"] = p.default
        props[p.name] = entry
    out: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


class Collector:
    """Stands where an MCP server object stood: `@collector.tool()` keeps the
    function instead of publishing it."""

    def __init__(self):
        self.fns: Dict[str, Callable[..., Any]] = {}

    def tool(self, *_, **__):
        def keep(fn):
            self.fns[fn.__name__] = fn
            return fn
        return keep


def _coerce(schema: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Arguments as the schema types them: "5" for an integer becomes 5."""
    props = (schema or {}).get("properties") or {}
    out = {}
    for name, value in (args or {}).items():
        kind = (props.get(name) or {}).get("type")
        if isinstance(value, str):
            try:
                if kind == "integer":
                    value = int(value)
                elif kind == "number":
                    value = float(value)
                elif kind == "boolean":
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                elif kind in ("object", "array") and value.strip()[:1] in "{[":
                    value = json.loads(value)
            except ValueError:
                pass
        out[name] = value
    return out


class LocalSession:
    """What a caller in the same process talks to in place of an MCP session.

    It has the two calls that client makes (`list_tools`, `call_tool`) and
    answers them from the tools held here, in the same shapes.
    """

    def __init__(self, tools: Dict[str, Tool]):
        self.tools = tools

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=t.name, description=t.description, inputSchema=t.schema)
            for t in self.tools.values()])

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None):
        tool = self.tools.get(name)
        if tool is None:
            return _answer(f"Unknown tool: {name}", True)
        try:
            got = tool.fn(**_coerce(tool.schema, arguments or {}))
            if inspect.isawaitable(got):
                got = await got
        except Exception as exc:  # noqa: BLE001 - a tool that fails is an answer
            return _answer(f"Error executing tool {name}: {exc}", True)
        if isinstance(got, tuple):
            return _answer(str(got[0]), bool(got[1]))
        return _answer(got if isinstance(got, str) else json.dumps(got, indent=1, default=str), False)


def _answer(text: str, is_error: bool):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=is_error)


# ----- recipes -----

def _fill(template: Any, args: Dict[str, Any]) -> Any:
    """A body template with "$name" standing for an argument.

    A few one-key forms compute a value: {"$int": x}, {"$str": x},
    {"$add": [a, b]} and {"$concat": [a, b, ...]}.
    """
    if isinstance(template, str) and template.startswith("$"):
        return args.get(template[1:])
    if isinstance(template, dict):
        if len(template) == 1:
            (fn, value), = template.items()
            if fn == "$int":
                return int(_fill(value, args))
            if fn == "$num":
                return number(_fill(value, args))
            if fn == "$str":
                return str(_fill(value, args))
            if fn == "$add":
                return sum(int(_fill(v, args)) for v in value)
            if fn == "$concat":
                return "".join(str(_fill(v, args)) for v in value)
        return {k: _fill(v, args) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, args) for v in template]
    return template


def number(value: Any) -> int:
    """An id as a number: 14549250, "14549250", "0x00de0102" or "0000000000de0102"."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    if len(text) == 16 or any(c in "abcdef" for c in text):
        return int(text, 16)
    return int(text)


def found(data: Any, key: str) -> List[Any]:
    """Every value under a key of this name, wherever it is and whatever its prefix."""
    out: List[Any] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key or k.endswith(":" + key):
                out.append(v)
            out += found(v, key)
    elif isinstance(data, list):
        for item in data:
            out += found(item, key)
    return out


def unmet(expect: Dict[str, Any], data: Any) -> str:
    """Why an answer is not what the manifest expects of it, or ""."""
    if not expect:
        return ""
    if "at" in expect:
        got = pick(data, expect["at"])
        values = [str(v) for v in (got if isinstance(got, list) else [] if got is None else [got])]
    else:
        values = [str(v) for v in found(data, expect["find"]) if not isinstance(v, (dict, list))]
    if not values:
        return expect.get("say", "") + " (no answer)" if expect.get("required") else ""
    allowed = [str(v) for v in (expect["in"] if "in" in expect else [expect.get("all_equal")])]
    wrong = sorted({v for v in values if v not in allowed})
    return f"{expect.get('say', 'unexpected answer')} ({', '.join(wrong)})" if wrong else ""


def checked(rules: Dict[str, Any], args: Dict[str, Any]) -> None:
    """Raise ValueError when the arguments break what the manifest says of them."""
    for name, (low, high) in (rules.get("between") or {}).items():
        if name in args and not (low <= int(args[name]) <= high):
            raise ValueError(f"{name} must be {low}..{high}")
    for name, allowed in (rules.get("keys") or {}).items():
        unknown = sorted(set(args.get(name) or {}) - set(allowed)) if isinstance(args.get(name), dict) else []
        if unknown:
            raise ValueError(f"unknown {name} field(s) {unknown}; the controller would ignore them")
    for name, rule in (rules.get("each") or {}).items():
        for row in args.get(name) or []:
            if str((row or {}).get(rule["field"], "")).upper() not in rule["one_of"]:
                raise ValueError(f"unknown {name} {rule['field']} {(row or {}).get(rule['field'])!r}")
    for names in rules.get("distinct") or []:
        seen = [str(args.get(n)) for n in names if n in args]
        if len(set(seen)) < len(seen):
            raise ValueError(" and ".join(names) + " are the same")


def pick(data: Any, path: str) -> Any:
    """A value out of an answer by a dotted path: `network.nodes`, `a.b[0].c`.

    A list met on the way is gone through: `links.src` of a list of links is
    the list of their `src`.
    """
    if path in ("", "."):
        return data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if data is None:
            return None
        if part.startswith("["):
            index = int(part[1:-1])
            data = data[index] if isinstance(data, list) and len(data) > index else None
        elif isinstance(data, list):
            found = [d.get(part) for d in data if isinstance(d, dict) and part in d]
            data = [x for f in found for x in (f if isinstance(f, list) else [f])]
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def view_rows(view: Dict[str, Any], data: Any) -> List[Dict[str, Any]]:
    """An answer as plain rows, by a view: where the list is, and which field is which."""
    rows = pick(data, str(view.get("rows", "")))
    if isinstance(rows, dict):
        rows = list(rows.values()) if all(isinstance(v, (dict, list)) for v in rows.values()) else [rows]
        rows = [x for r in rows for x in (r if isinstance(r, list) else [r])]
    fields = {k: v for k, v in view.items() if k != "rows"}
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            out.append({"id": row})
            continue
        if not fields:
            out.append(row)
            continue
        made = {name: pick(row, str(path)) for name, path in fields.items()}
        out.append({k: v for k, v in made.items() if v is not None})
    return out


def placeholders(template: Any) -> List[str]:
    """The argument names a body template asks for."""
    if isinstance(template, str) and template.startswith("$") and not template.startswith(("$int", "$num", "$str", "$add", "$concat")):
        return [template[1:]]
    out: List[str] = []
    for value in (template.values() if isinstance(template, dict) else template
                  if isinstance(template, list) else []):
        out += [p for p in placeholders(value) if p not in out]
    return out


def body_of(rule: Any, args: Dict[str, Any]) -> Any:
    if not rule:
        return None
    if "arg" in rule and "wrap" in rule:
        return {rule["wrap"]: args[rule["arg"]]}
    if "arg" in rule:
        return args[rule["arg"]]
    if "template" in rule:
        return _fill(rule["template"], args)
    return None


def _ensure_uuid(body: Any, path: str) -> Any:
    """The body with a fresh uuid at `path` when it has none there."""
    import copy
    import uuid
    if not isinstance(body, dict):
        return body
    out = copy.deepcopy(body)
    box = out.get("data") if isinstance(out.get("data"), dict) else out
    *parents, leaf = path.split(".")
    for part in parents:
        if not isinstance(box.get(part), dict):
            box[part] = {}
        box = box[part]
    if not isinstance(box.get(leaf), str) or not box[leaf].strip():
        box[leaf] = str(uuid.uuid4())
    return out


def _text_line(line: str, scope: Dict[str, Any]) -> str:
    """`{count:a.b}`, `{a.b|default}` and `{flag?yes:no}` filled in from answers."""
    def fill(m):
        expr = m.group(1)
        if expr.startswith("count:"):
            got = pick(scope, expr[6:])
            return str(len(got) if isinstance(got, (list, dict)) else 0)
        if "?" in expr:
            name, rest = expr.split("?", 1)
            yes, _, no = rest.partition(":")
            return yes if pick(scope, name) else no
        name, _, default = expr.partition("|")
        got = pick(scope, name)
        return str(default if got in (None, "") else got)
    return re.sub(r"\{([^{}]+)\}", fill, line)


def _shape(reply: Dict[str, Any], got: Dict[str, Any], args: Optional[Dict[str, Any]] = None) -> Any:
    """The answer of a tool that is several requests, as its manifest lays it out."""
    if "const" in reply:
        return reply["const"]
    if "collect" in reply:
        # One list out of the answers of a `foreach` step, with a count beside it.
        rule = reply["collect"]
        out: Dict[str, Any] = {name: (args or {}).get(name) for name in rule.get("echo") or []}
        if rule.get("count_of"):
            counted = pick(got, rule["count_of"])
            out[rule.get("count_as", "count")] = len(counted) if isinstance(counted, list) else 0
        rows: List[Any] = []
        for answer in (got.get(rule["from"]) or {}).values():
            found_ = pick(answer, rule["field"]) if isinstance(answer, dict) else None
            rows += [r for r in (found_ if isinstance(found_, list) else []) if isinstance(r, dict)]
        out[rule.get("as", rule["field"])] = rows
        return out
    if "pluck" in reply:
        rule = reply["pluck"]
        rows = pick(got, rule["from"])
        return {rule["as"]: [r[rule["field"]] for r in rows or [] if isinstance(r, dict) and rule["field"] in r]}
    if "text" in reply:
        lines = []
        for line in reply["text"]:
            if isinstance(line, dict):
                for row in pick(got, line["each"]) or []:
                    lines.append(_text_line(line["line"], row if isinstance(row, dict) else {"value": row}))
            else:
                lines.append(_text_line(line, got))
        return "\n".join(lines)
    return got


def from_recipe(recipe: Dict[str, Any], session: Any, style: str = "json") -> Tool:
    """One tool from a manifest: one request, or a few with a shaped answer."""
    steps = recipe.get("steps") or ([recipe] if recipe.get("path") else [])
    paths = [p for st in steps for p in _PARAM.findall(st["path"]) if p != "item"]
    schema = recipe.get("schema") or {"type": "object", "properties": {
        p: {"type": "string"} for p in dict.fromkeys(paths)}}
    defaults = {name: prop["default"] for name, prop in (schema.get("properties") or {}).items()
                if isinstance(prop, dict) and "default" in prop}
    defaults.update(recipe.get("defaults") or {})
    reply = REPLIES.get(style, _reply_json)

    def send(step: Dict[str, Any], args: Dict[str, Any]):
        for name, chars in (step.get("strip") or {}).items():
            if name in args:
                args = {**args, name: str(args[name]).lstrip(chars)}
        safe = "" if step.get("quote") else "/:=,@+"
        path = _PARAM.sub(lambda m: quote(str(args[m.group(1)]), safe=safe), step["path"])
        query = {k: (str(args[v]).lower() if isinstance(args[v], bool) else args[v])
                 for k, v in (step.get("query") or {}).items() if v in args}
        body = body_of(step.get("body"), args)
        if (step.get("body") or {}).get("ensure_uuid"):
            body = _ensure_uuid(body, step["body"]["ensure_uuid"])
        url = session.host + path
        return url, session.fetch(step["method"].upper(), url, body, params=query or None,
                                  **({"timeout": float(step["timeout"])} if step.get("timeout") else {}))

    def _unsettled(rules: List[Dict[str, Any]], data: Any) -> str:
        """Why the controller is still busy with a write approved here, or ""."""
        import time as _time
        done = session.queue.applied(session.manifest["key"])
        for rule in rules:
            last = next((i for i in reversed(done) if i.get("action") == rule["action"]), None)
            if last is None or _time.time() - float(last.get("applied") or 0) > float(rule["for_s"]):
                continue
            listed = str(last.get("target")) in [str(v) for v in found(data, rule.get("named_by", "name"))]
            if rule.get("until") == "listed" and not listed:
                return rule["say"].format(target=last.get("target"))
            if rule.get("until") == "gone" and listed:
                return rule["say"].format(target=last.get("target"))
        return ""

    def requests_of(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The requests this tool would send, for a tool that queues them."""
        if recipe.get("verb") and not steps:
            return session.requests_for(recipe["verb"], None, **args)
        out = []
        for step in steps:
            safe = "" if step.get("quote") else "/:=,@+"
            path = _PARAM.sub(lambda m: quote(str(args[m.group(1)]), safe=safe), step["path"])
            out.append({"method": step["method"].upper(), "url": session.host + path,
                        "body": body_of(step.get("body"), args), "operation": f"{step['method'].upper()} {step['path']}",
                        **({"timeout": step["timeout"]} if step.get("timeout") else {}),
                        **({"expect": step["expect"]} if step.get("expect") else {})})
        return out

    def run(**given) -> Tuple[str, bool]:
        args = {**(session.manifest.get("defaults") or {}), **defaults, **given}
        if recipe.get("approve"):
            # The operator's token, held by the host that put a person in front
            # of this change. It is a credential, not something a model picks.
            import hmac
            want = session.env.get(recipe["approve"].get("token_env", ""), "")
            if not want:
                return json.dumps({"ok": False, "error": "disabled", "detail":
                                   f"no {recipe['approve'].get('token_env')} is configured; approve with "
                                   f"python -m agentic_mcp approve"}, indent=1), False
            if not hmac.compare_digest(str(args.get("approver_token", "")), want):
                return json.dumps({"ok": False, "error": "refused", "detail": "approver token mismatch"},
                                  indent=1), False
            return json.dumps(session.apply_queued(str(args.get("approval_id", "")), by="approver token"),
                              indent=1, default=str), False
        missing = [p for p in paths if p not in args and not recipe.get("verb")]
        missing += [r for r in schema.get("required") or [] if r not in args]
        if missing:
            raise ValueError("missing argument(s): " + ", ".join(dict.fromkeys(missing)))
        try:
            for name, how in (recipe.get("convert") or {}).items():
                if name in args and how == "number":
                    args[name] = number(args[name])
            checked(recipe.get("check") or {}, args)
            if recipe.get("queue"):
                fmt = lambda t: str(t or "").format(**args)  # noqa: E731
                return json.dumps(session.queue_write(recipe["name"], requests_of(args), fmt(recipe.get("summary")),
                                                      target=fmt(recipe.get("target")), verb=recipe.get("verb", "")),
                                  indent=1, default=str), False
        except (ValueError, KeyError, LookupError) as exc:
            return json.dumps({"ok": False, "error": "invalid", "detail": str(exc)}, indent=1), False
        if "steps" not in recipe:
            url, (status, kind, text) = send(recipe, args)
            return reply(recipe, args, url, status, kind, text)
        got: Dict[str, Any] = {}
        for i, step in enumerate(steps):
            name = step.get("as", f"step{i + 1}")
            if step.get("foreach"):
                # One request per item of an earlier answer, kept by item.
                each: Dict[str, Any] = {}
                for item in (pick(got, step["foreach"]) or [])[:200]:
                    value = number(item) if step.get("item") == "number" else item
                    _, (status, kind, text) = send(step, {**args, "item": value})
                    if 200 <= status < 300:
                        each[str(item)] = _loads(text)
                got[name] = each
                continue
            url, (status, kind, text) = send(step, args)
            if not 200 <= status < 300:
                # An answer that means "nothing there yet" is an empty answer.
                if step.get("optional") or (step.get("empty_if") and step["empty_if"] in (text or "")):
                    got[name] = None
                    continue
                return reply(recipe, args, url, status, kind, text)
            got[name] = _loads(text)
            why = unmet(step.get("expect") or {}, got[name]) or _unsettled(step.get("quiet_after") or [], got[name])
            if why:
                # The tool stops here: what follows would act on a controller
                # that is not in the state this step was there to establish.
                return json.dumps({"ok": False, "error": step.get("error", "busy"), "operation": recipe["name"],
                                   "detail": why}, indent=2), False
        if (recipe.get("reply") or {}).get("raw"):
            # What the controller said, untouched, for a reader that knows it.
            return json.dumps({"ok": True, "raw": got}, default=str), False
        shaped = _shape(recipe.get("reply") or {}, got, args)
        return (shaped if isinstance(shaped, str) else json.dumps(shaped, indent=2, default=str)), False

    return Tool(recipe["name"], run, recipe.get("description", ""), schema,
                write=bool(recipe.get("write", bool(recipe.get("queue") or recipe.get("approve"))
                                      or any(st["method"].upper() != "GET" for st in steps))),
                verb=recipe.get("verb", ""))


def _loads(text: str) -> Any:
    try:
        return json.loads(text) if text and text.strip() else None
    except ValueError:
        return None


def _reply_json(recipe, args, url, status, kind, text) -> Tuple[str, bool]:
    if 200 <= status < 300:
        data = _loads(text)
        return json.dumps(data if data is not None else {"ok": True, "status": status}, indent=1), False
    return json.dumps({"ok": False, "status": status, "error": (text or "no answer")[:500]}, indent=1), True


def _reply_envelope(recipe, args, url, status, kind, text) -> Tuple[str, bool]:
    """Every answer is a JSON object, a failure too (with the URL and the status)."""
    if not status:
        data: Any = {"error": text, "status": "error", "url": url}
    elif status >= 400:
        data = {"error": f"HTTP {status}: {text}", "status": "error", "url": url}
    elif not text:
        data = {"status": "ok", "http_status": status}
    elif "application/json" in kind:
        data = _loads(text)
    else:
        data = {"status": "ok", "body": text}
    return json.dumps(data, indent=2, default=str), False


def _reply_repr(recipe, args, url, status, kind, text) -> Tuple[str, bool]:
    """The answer as a plain printed value, and a sentence beginning "Error"
    when the controller refused."""
    if 200 <= status < 300:
        data = _loads(text)
        result = data if data is not None else {}
        if recipe.get("reply") == "text":
            return recipe.get("ok_text", "{result}").format(result=result, **args), False
        return str(result), False
    error = (f"API error: {status} - {text}" if status
             else f"Error connecting to the controller: {text}")
    return recipe.get("error_text", "Error: {error}").format(error=error, **args), False


REPLIES = {"json": _reply_json, "envelope": _reply_envelope, "repr": _reply_repr}


# ----- verbs, for a controller with no family -----

# The tool each verb is served under (<controller key>_<tool>), as the
# contract names it: a verb added to contract.json gets its tool from there.
from .verbs import VERBS as _VERBS
VERB_TOOLS = {verb: rule["tool"] for verb, rule in _VERBS.items()}
# The roles a planner looks for, and the verb behind each.
ROLES = {"devices": "read_devices", "links": "read_links", "services": "read_services",
         "path": "compute_path", "provision": "provision", "teardown": "teardown"}


def from_verbs(session: Any, approver: Callable[[], str]) -> Dict[str, Tool]:
    """One tool per verb the manifest maps, and one that says which is which."""
    key = session.manifest["key"]
    label = session.manifest.get("label", key)
    out: Dict[str, Tool] = {}
    for verb, entry in (session.manifest.get("verbs") or {}).items():
        op = entry.get("operation")
        if not op:
            continue
        name = f"{key}_{VERB_TOOLS[verb]}"
        props = {p: {"type": "string", "description": f"{p} in {op['path']}"}
                 for p in op["params"] if p not in (session.manifest.get("defaults") or {})}
        required = list(props)
        described = entry.get("arguments") or {}
        templates = [entry.get("template")] + [st.get("template") for st in entry.get("steps") or []]
        if any(t is not None for t in templates):
            for t in templates:
                for p in placeholders(t):
                    if p not in props:
                        props[p] = {"type": "string",
                                    "description": str(described.get(p) or f"{p}, placed in the request body")}
                        required.append(p)
        elif op.get("body"):
            props["body"] = {"type": "object", "description": "the request body, as the controller's "
                             "API describes it" + (": " + json.dumps(op["body_schema"])[:600]
                                                   if op.get("body_schema") else "")}
        for p, text in described.items():
            props.setdefault(p, {"type": "string"})["description"] = str(text)
        for name in (entry.get("query") or {}).values():
            props.setdefault(name, {"type": "string", "description": f"{name}, sent in the query"})
        schema = {"type": "object", "properties": props, "required": required}
        about = f"{label}: {entry.get('why') or verb.replace('_', ' ')} ({op['id']})"

        def run(_verb=verb, _write=bool(op["writes"]), **given):
            body = given.pop("body", None)
            if _write:
                got = session.write(_verb, body, approved_by=approver(), **given)
                return json.dumps(got, indent=1, default=str), not got.get("ok")
            got = session.read(_verb, body, **given)
            if got.get("ok") and "rows" in got:
                # The manifest says how to read this answer: plain rows.
                return json.dumps({VERB_TOOLS[_verb].split("_", 1)[1]: got["rows"]}, indent=1), False
            if got.get("ok"):
                # A read answers with the controller's own data, as a tool
                # written for that controller would.
                return json.dumps(got.get("data") if got.get("data") is not None else {}, indent=1), False
            return json.dumps(got, indent=1, default=str), True

        out[name] = Tool(name, run, about, schema, write=bool(op["writes"]), verb=verb)

    def capabilities() -> str:
        roles = {}
        for role, verb in ROLES.items():
            name = f"{key}_{VERB_TOOLS[verb]}"
            if name in out:
                roles[role] = name
        return json.dumps({"controller": key, "domain": (session.manifest.get("domain") or {}).get("kind"),
                           "roles": roles})

    out[f"{key}_capabilities"] = Tool(
        f"{key}_capabilities", capabilities,
        f"{label}: which of its tools reads devices, links and services, computes a path, "
        f"provisions and tears down", {"type": "object", "properties": {}})
    return out
