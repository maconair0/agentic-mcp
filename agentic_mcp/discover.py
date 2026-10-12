"""
What a controller says about its own API.

Three sources, tried in this order, and all that answer are merged:

  OpenAPI / Swagger   a document listing every path, method and parameter. It
                      is looked for at the usual addresses and behind the
                      controller's documentation page.
  RESTCONF            `/.well-known/host-meta` gives the root; `operations`
                      under it lists every RPC and the YANG library lists every
                      module.
  a manifest          for a controller that publishes neither: the controller
                      manifest of its kind (library.py), written by a person or
                      drafted from its user guide, lists its operations or
                      names the documentation page to read them from. The kind
                      is recognised by one address that only it answers, or is
                      named at onboarding.

The third is the limit of "on the fly": a controller that describes nothing
cannot be learned from itself. It needs a manifest, an API document or a
documentation page handed over.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

# Where an API document usually is, relative to the address given.
SPEC_PATHS = ("openapi/api/v3/single", "openapi.json", "swagger.json", "v3/api-docs",
              "v2/api-docs", "api-docs", "docs/swagger.json")
DOC_PAGES = ("docs/index.html", "docs/", "swagger-ui/index.html")
_SPEC_LINK = re.compile(r"""["']([^"'\s]*(?:swagger|openapi|api-docs)[^"'\s]*\.json)["']""", re.I)
_PARAM = re.compile(r"\{([^{}]+)\}")
# An RPC is sent with POST whether it reads or writes; these names read.
_READING_RPC = re.compile(r"(^|[:/])(get|list|show|read|query)[-_]", re.I)

# fetch(method, url, body) -> (status, content type, text). 0 when nothing answered.
Fetch = Callable[[str, str, Optional[Dict[str, Any]]], Tuple[int, str, str]]


def operation(method: str, path: str, summary: str = "", body: bool = False,
              source: str = "", name: str = "") -> Dict[str, Any]:
    method = method.upper()
    reads = method == "GET" or (method == "POST" and bool(_READING_RPC.search(name or path)))
    return {"id": f"{method} {path}", "method": method, "path": path,
            "summary": " ".join(str(summary or "").split())[:200],
            "params": _PARAM.findall(path), "body": body, "writes": not reads,
            "source": source}


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def from_openapi(doc: Dict[str, Any], source: str = "openapi") -> List[Dict[str, Any]]:
    """Every operation in an OpenAPI 3 or Swagger 2 document."""
    base = ""
    if doc.get("swagger"):
        base = str(doc.get("basePath") or "").rstrip("/")
    else:
        servers = doc.get("servers") or []
        if servers and isinstance(servers[0], dict):
            base = urlsplit(str(servers[0].get("url") or "")).path.rstrip("/")
    out = []
    for path, methods in (doc.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        full = path if base and path.startswith(base + "/") else base + path
        for method, spec in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(spec, dict):
                continue
            body = "requestBody" in spec or any(
                p.get("in") == "body" for p in spec.get("parameters") or [] if isinstance(p, dict))
            op = operation(method, full, spec.get("summary") or spec.get("description") or "",
                           body=body, source=source, name=path.rsplit("/", 1)[-1])
            schema = body_schema(doc, spec)
            if schema:
                op["body_schema"] = schema
            out.append(op)
    return out


def _resolve(doc: Dict[str, Any], node: Any, depth: int) -> Any:
    """A schema with its references followed, to a depth a person can read."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if depth <= 0:
                return {"type": "object", "description": "see " + ref.rsplit("/", 1)[-1]}
            target: Any = doc
            for part in ref[2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve(doc, target, depth - 1)
        return {k: _resolve(doc, v, depth) for k, v in node.items() if k not in ("xml", "example")}
    if isinstance(node, list):
        return [_resolve(doc, v, depth) for v in node]
    return node


def body_schema(doc: Dict[str, Any], spec: Dict[str, Any], depth: int = 3, limit: int = 6000) -> Any:
    """What an operation's request body has to look like, when the document says."""
    schema = None
    content = (spec.get("requestBody") or {}).get("content") or {}
    if "application/json" in content:
        schema = content["application/json"].get("schema")
    for p in spec.get("parameters") or []:
        if isinstance(p, dict) and p.get("in") == "body":
            schema = p.get("schema")
    if not schema:
        return None
    for d in range(depth, 0, -1):
        got = _resolve(doc, schema, d)
        if len(json.dumps(got)) <= limit:
            return got
    return None


def from_restconf_operations(root: str, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every RPC a RESTCONF server lists under `operations`."""
    rpcs = doc.get("ietf-restconf:operations") or doc.get("operations") or {}
    return [operation("POST", f"{root}/operations/{name}", body=True, source="restconf", name=name)
            for name in rpcs]


def from_family(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A family's operations: the ones it lists and the one behind each recipe."""
    out = []
    rows = list(doc.get("operations") or []) + [
        {"method": st["method"], "path": st["path"], "summary": t.get("description", ""),
         "body": bool(st.get("body"))} for t in doc.get("tools") or [] for st in t.get("steps") or [t]
        if st.get("method") and st.get("path") and "{item}" not in st["path"]]
    seen = set()
    for row in rows:
        if (row["method"], row["path"]) in seen:
            continue
        seen.add((row["method"], row["path"]))
        op = operation(row["method"], row["path"], row.get("summary", ""),
                       body=bool(row.get("body")), source=f"family:{doc.get('family', '')}")
        if "writes" in row:
            op["writes"] = bool(row["writes"])
        out.append(op)
    return out


def families() -> List[Dict[str, Any]]:
    """Every kind of controller there is a manifest for, from the library."""
    from . import library
    return library.families()


def _merge(into: List[Dict[str, Any]], more: List[Dict[str, Any]]) -> None:
    seen = {op["id"] for op in into}
    for op in more:
        if op["id"] not in seen:
            seen.add(op["id"])
            into.append(op)


def discover(base_url: str, fetch: Fetch, spec: str = "", docs: Iterable[str] = ()) -> Dict[str, Any]:
    """What this controller publishes about itself.

    `spec` is an API document handed over by the operator: an address, a file,
    or the name of a family. With it nothing else is looked for. `docs` are
    addresses of documentation pages to read operations out of, for a
    controller that publishes no document a program can read.
    """
    base = base_url.rstrip("/") + "/"
    host = "{0.scheme}://{0.netloc}/".format(urlsplit(base))
    found: Dict[str, Any] = {"styles": [], "sources": [], "operations": [], "modules": [],
                             "restconf_root": "", "defaults": {}, "tried": [], "family": ""}

    def get(url: str) -> Tuple[int, str, str]:
        status, kind, text = fetch("GET", url, None)
        found["tried"].append({"url": url, "status": status})
        return status, kind, text

    def take_spec(doc: Any, where: str) -> bool:
        if not isinstance(doc, dict):
            return False
        if doc.get("family") and (doc.get("operations") or doc.get("tools")):
            _merge(found["operations"], from_family(doc))
            found["styles"].append("manifest of kind %s" % doc["family"])
            found["defaults"].update(doc.get("defaults") or {})
            found["family"] = doc["family"]
        elif doc.get("paths"):
            _merge(found["operations"], from_openapi(doc))
            found["styles"].append("openapi %s" % (doc.get("openapi") or doc.get("swagger") or ""))
        else:
            return False
        found["sources"].append(where)
        return True

    if spec:
        named = next((f for f in families() if f.get("family") == spec), None)
        if named is not None:
            take_spec(named, f"controller manifest {spec}")
        elif os.path.exists(spec):
            with open(spec) as fh:
                take_spec(_json(fh.read()), spec)
        else:
            take_spec(_json(get(spec)[2]), spec)
        return found

    # 1. An API document, at a usual address or linked from a documentation page.
    extra = tuple(p.lstrip("/") for fam in families() for p in fam.get("spec_paths") or [])
    for start in dict.fromkeys((base, host)):
        if any(s.startswith("openapi") for s in found["styles"]):
            break
        for path in SPEC_PATHS + extra:
            status, _, text = get(urljoin(start, path))
            if status == 200 and take_spec(_json(text), urljoin(start, path)):
                break
        else:
            for page in DOC_PAGES:
                status, _, text = get(urljoin(start, page))
                link = _SPEC_LINK.search(text) if status == 200 else None
                if link:
                    url = urljoin(urljoin(start, page), link.group(1))
                    status, _, text = get(url)
                    if status == 200 and take_spec(_json(text), url):
                        break

    # 2. RESTCONF: its root, its RPCs, its modules.
    status, _, text = get(urljoin(host, ".well-known/host-meta"))
    root = re.search(r"rel=['\"]restconf['\"]\s+href=['\"]([^'\"]+)['\"]", text) if status == 200 else None
    if root:
        found["restconf_root"] = root.group(1).rstrip("/")
        status, _, text = get(urljoin(host, found["restconf_root"].lstrip("/") + "/operations"))
        ops = _json(text) if status == 200 else None
        if isinstance(ops, dict):
            _merge(found["operations"], from_restconf_operations(found["restconf_root"], ops))
            found["styles"].append("restconf")
            found["sources"].append(found["restconf_root"] + "/operations")
        for lib in ("ietf-yang-library:modules-state", "ietf-yang-library:yang-library"):
            status, _, text = get(urljoin(host, f"{found['restconf_root'].lstrip('/')}/data/{lib}"))
            names = sorted(set(re.findall(r'"name"\s*:\s*"([^"]+)"', text))) if status == 200 else []
            if names:
                found["modules"] = names
                break

    # 2b. Documentation pages: the ones handed over, and the ones a known kind
    #     names (read after the kind is recognised, below).
    def read_docs(pages: Iterable[str]) -> None:
        for page in pages:
            status, _, text = get(page)
            learned = from_docs(text) if status == 200 else []
            if learned:
                _merge(found["operations"], learned)
                found["styles"].append("documentation page")
                found["sources"].append(f"{page} ({len(learned)} operations)")

    read_docs(docs)

    # 3. Is it of a kind already known? First from what it published, and
    #    only then by asking the one address each known kind answers.
    # A manifest drafted from a user guide claims no controller by itself:
    # what it would recognise one by was chosen by a program, so a controller
    # is of that kind only when whoever onboards it says so.
    written = [fam for fam in families() if str(fam.get("version")) != "draft"]
    for fam in written:
        mark = str((fam.get("recognise") or {}).get("operation_contains") or "")
        if mark and any(mark in op["path"] for op in found["operations"]):
            found["family"] = fam["family"]
            found["defaults"].update(fam.get("defaults") or {})
            break
    if not found["family"]:
        for fam in written:
            probe = str(fam.get("fingerprint") or "")
            if not probe:
                continue
            status, kind, text = get(urljoin(host, probe.lstrip("/")))
            if status == 200 and _json(text) is not None:
                take_spec(fam, f"controller manifest {fam.get('family')} (recognised by {probe})")
                break
    known = next((f for f in families() if f.get("family") == found["family"]), {})
    read_docs(d for d in known.get("docs") or [] if d not in docs)
    return found


# ----- an API learned from its documentation page -----

_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_DOC_OP = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\b[^/\n]{0,40}?(/[A-Za-z0-9_.{}<>:/\-\[\]]{2,})"
    r"|(/[A-Za-z0-9_.{}<>:/\-\[\]]{2,})[^\n]{0,40}?\b(GET|POST|PUT|PATCH|DELETE)\b")
_KEY_VALUE = re.compile(r"\b(Method|URI|URL|Path|Endpoint)\b\s*:?\s*(\S+)", re.I)


def _plain(page: str) -> str:
    import html
    text = _TAG.sub(" ", page)
    # In a guide written as text, `/switches/<id>` is an argument and not a tag.
    text = re.sub(r"/<([A-Za-z_][\w\-]*)>", r"/{\1}", text)
    text = re.sub(r"<(br|/p|/div|/tr|/li|/h\d|/dt|/dd|/pre)[^>]*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t\r\xa0]+", " ", text)


def _doc_path(raw: str) -> str:
    path = re.sub(r"\[[^\]]*\]", "", raw)                    # an optional tail: [/<port>]
    path = re.sub(r"<([^<>]+)>", r"{\1}", path)              # <dpid> -> {dpid}
    return path.rstrip(".,;:)")


def from_docs(page: str, source: str = "docs") -> List[Dict[str, Any]]:
    """Operations named in a documentation page: a method beside a path.

    Two layouts are read. A table that says `Method GET` and `URI /stats/x` on
    neighbouring lines, and prose or a code sample with `GET /stats/x` on one
    line. A page that names its operations any other way yields nothing, and
    that is said at onboarding. Whether a POST reads or writes cannot be told
    from a page, so every non-GET is treated as a write.
    """
    lines = [l.strip() for l in _plain(page).split("\n") if l.strip()]
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(method: str, raw: str, summary: str) -> None:
        path = _doc_path(raw)
        if len(path) < 3 or (method, path) in seen or "//" in path:
            return
        seen.add((method, path))
        op = operation(method, path, summary, body=method != "GET", source=source)
        op["writes"] = method != "GET"
        out.append(op)

    title, method, uri, waiting = "", "", "", ""
    for line in lines:
        pair = _KEY_VALUE.match(line)
        if waiting:
            # A table cell on its own line: the value of the key before it.
            if waiting == "method":
                method = line.upper().strip(",")
            elif line.startswith("/"):
                uri = line.split()[0]
            waiting = ""
        elif line.lower() in ("method", "uri", "url", "path", "endpoint"):
            waiting = "method" if line.lower() == "method" else "uri"
        elif pair and pair.group(1).lower() == "method":
            method = pair.group(2).upper().strip(",")
        elif pair and pair.group(2).startswith("/"):
            uri = pair.group(2)
        else:
            hit = _DOC_OP.search(line)
            heading = line.lstrip("# ").strip()
            if hit and "://" not in line:
                # What the line says after the path is what the operation is
                # for; a line that says nothing more takes the heading above.
                said = line[hit.end():].strip(" \t-–—:|`*.") if hit.group(1) else ""
                add((hit.group(1) or hit.group(4)), hit.group(2) or hit.group(3),
                    said if len(said) > 3 else title)
            elif heading and len(heading) < 80 and heading[0].isalpha() and not heading.endswith((":", ".")):
                title = heading.rstrip(" ¶")
        if method in ("GET", "POST", "PUT", "PATCH", "DELETE") and uri:
            add(method, uri, title)
            method, uri = "", ""
    return out
