#!/usr/bin/env python3
"""
swagger_to_http.py - Convert a Swagger / OpenAPI JSON file into a .http file.

The output file is named from the spec's info block:

    {info.title}_{info.version}.http      e.g.  WebApplication1_API_v1.http

The generated file works with the VS Code "REST Client" extension,
Visual Studio 2022 (17.8+) and JetBrains Rider / IntelliJ HTTP Client.

Usage:
    python swagger_to_http.py swagger.json
    python swagger_to_http.py swagger.json -o ./out --base-url https://localhost:5001

Only the Python standard library is used (Python 3.8+).
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

STRING_FORMATS = {
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "time": "12:00:00",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.com",
    "uri": "https://example.com",
    "url": "https://example.com",
    "hostname": "example.com",
    "ipv4": "127.0.0.1",
    "ipv6": "::1",
    "byte": "c3RyaW5n",
}

BOUNDARY = "----SwaggerToHttpBoundary"


# --------------------------------------------------------------------------- #
# Spec helpers
# --------------------------------------------------------------------------- #
def load_spec(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Error: file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"Error: {path} is not valid JSON ({e})")


def safe_name(text):
    """Make a string safe to use in a file name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text).strip()).strip("_") or "api"


def var_name(text):
    """Make a string safe to use as a .http variable name."""
    return re.sub(r"\W", "_", str(text))


def resolve(spec, node):
    """Follow local $ref pointers ('#/components/schemas/X')."""
    seen = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen or not ref.startswith("#/"):
            return {}
        seen.add(ref)
        target = spec
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return {}
        node = target
    return node if isinstance(node, dict) else {}


def example_value(spec, schema, stack=()):
    """Build an example value from a JSON schema."""
    if not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if ref:
        if ref in stack:  # recursive schema
            return None
        stack = stack + (ref,)
        schema = resolve(spec, schema)
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("allOf"):
        merged = {}
        for sub in schema["allOf"]:
            value = example_value(spec, sub, stack)
            if isinstance(value, dict):
                merged.update(value)
        return merged
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return example_value(spec, schema[key][0], stack)

    stype = schema.get("type")
    if stype is None and "properties" in schema:
        stype = "object"
    if stype == "object":
        result = {}
        for name, prop in (schema.get("properties") or {}).items():
            if resolve(spec, prop).get("readOnly"):
                continue
            result[name] = example_value(spec, prop, stack)
        return result
    if stype == "array":
        item = example_value(spec, schema.get("items", {}), stack)
        return [] if item is None else [item]
    if stype == "string":
        return STRING_FORMATS.get(schema.get("format"), "string")
    if stype == "integer":
        return schema.get("minimum", 0)
    if stype == "number":
        return schema.get("minimum", 0.0)
    if stype == "boolean":
        return False
    return None


def param_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(param_text(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def param_example(spec, p):
    if "example" in p:
        return p["example"]
    if p.get("examples"):
        first = resolve(spec, next(iter(p["examples"].values())))
        if "value" in first:
            return first["value"]
    return example_value(spec, p.get("schema", p))  # Swagger 2 keeps the type on p


def collect_params(spec, path_item, op):
    merged = {}
    for raw in (path_item.get("parameters") or []) + (op.get("parameters") or []):
        p = resolve(spec, raw)
        if "name" in p and "in" in p:
            merged[(p["name"], p["in"])] = p  # operation level overrides path level
    return list(merged.values())


def iter_operations(spec):
    for path, item in (spec.get("paths") or {}).items():
        item = resolve(spec, item)
        for method in HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                yield path, method, op, collect_params(spec, item, op)


def get_base_url(spec):
    servers = spec.get("servers")
    if servers and isinstance(servers[0], dict):
        url = servers[0].get("url", "/")
        for name, var in (servers[0].get("variables") or {}).items():
            url = url.replace("{" + name + "}", str(var.get("default", "")))
        if url.startswith("/"):
            url = "http://localhost" + url
        return url.rstrip("/")
    if spec.get("host"):  # Swagger 2.0
        scheme = (spec.get("schemes") or ["https"])[0]
        return (scheme + "://" + spec["host"] + spec.get("basePath", "")).rstrip("/")
    return "http://localhost"


def pick_content_type(content):
    if "application/json" in content:
        return "application/json"
    for ctype in content:
        if "json" in ctype:
            return ctype
    return next(iter(content))


def is_file(schema):
    return schema.get("format") == "binary" or schema.get("type") == "file"


def get_body(spec, op, params):
    """Return {'content_type', 'example', 'files'} or None."""
    if op.get("requestBody"):
        rb = resolve(spec, op["requestBody"])
        content = rb.get("content") or {}
        if content:
            ctype = pick_content_type(content)
            media = content[ctype] or {}
            schema = resolve(spec, media.get("schema", {}))
            if "example" in media:
                example = media["example"]
            elif media.get("examples"):
                first = resolve(spec, next(iter(media["examples"].values())))
                example = first.get("value")
            else:
                example = example_value(spec, media.get("schema", {}))
            files = {n for n, s in (schema.get("properties") or {}).items()
                     if is_file(resolve(spec, s))}
            return {"content_type": ctype, "example": example, "files": files}
        return None

    consumes = op.get("consumes") or spec.get("consumes") or []
    body_param = next((p for p in params if p.get("in") == "body"), None)
    if body_param:  # Swagger 2.0
        return {"content_type": consumes[0] if consumes else "application/json",
                "example": example_value(spec, body_param.get("schema", {})),
                "files": set()}
    form = [p for p in params if p.get("in") == "formData"]
    if form:  # Swagger 2.0
        files = {p["name"] for p in form if p.get("type") == "file"}
        multipart = bool(files) or "multipart/form-data" in consumes
        return {"content_type": "multipart/form-data" if multipart
                else "application/x-www-form-urlencoded",
                "example": {p["name"]: example_value(spec, p) for p in form},
                "files": files}
    return None


def get_accept(spec, op):
    types = []
    for code, resp in (op.get("responses") or {}).items():
        if str(code).startswith("2") or code == "default":
            types += list((resolve(spec, resp).get("content") or {}).keys())
    if not types:
        types = list(op.get("produces") or spec.get("produces") or [])
    if not types:
        return None
    return "application/json" if "application/json" in types else types[0]


def get_auth(spec, op):
    """Return the first applicable security scheme as a simple dict, or None."""
    reqs = op["security"] if "security" in op else spec.get("security")
    if not reqs:
        return None
    schemes = (spec.get("components") or {}).get("securitySchemes") \
        or spec.get("securityDefinitions") or {}
    for req in reqs:
        for name in req:
            s = resolve(spec, schemes.get(name))
            stype = s.get("type")
            if stype == "http" and s.get("scheme", "").lower() == "basic" or stype == "basic":
                return {"type": "basic"}
            if stype in ("http", "oauth2", "openIdConnect"):
                return {"type": "bearer"}
            if stype == "apiKey":
                return {"type": "apikey", "in": s.get("in", "header"),
                        "name": s.get("name", "X-API-Key")}
    return None


# --------------------------------------------------------------------------- #
# .http generation
# --------------------------------------------------------------------------- #
def multipart_body(fields, files):
    parts = []
    for name, value in fields.items():
        if name in files:
            parts.append(
                f'--{BOUNDARY}\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{name}.bin"\nContent-Type: application/octet-stream\n\n'
                f'< ./{name}.bin')
        else:
            parts.append(f'--{BOUNDARY}\nContent-Disposition: form-data; '
                         f'name="{name}"\n\n{param_text(value)}')
    return "\n".join(parts) + f"\n--{BOUNDARY}--"


def build_http(spec, base_url):
    info = spec.get("info", {})
    title = info.get("title", "API")
    version = info.get("version", "")
    variables = {"baseUrl": base_url}
    blocks = []

    for path, method, op, params in iter_operations(spec):
        name = (op.get("summary") or op.get("operationId")
                or f"{method.upper()} {path}").strip().splitlines()[0]
        lines = [f"### {name}"]
        if op.get("operationId"):
            lines.append(f"# @name {var_name(op['operationId'])}")
        if op.get("description"):
            lines.append("# " + op["description"].strip().splitlines()[0])

        # Path parameters become file-level variables.
        for p in params:
            if p["in"] == "path":
                value = param_text(param_example(spec, p)) or "1"
                variables.setdefault(var_name(p["name"]), value)

        def path_var(match):
            variables.setdefault(var_name(match.group(1)), "1")
            return "{{" + var_name(match.group(1)) + "}}"

        url_path = re.sub(r"\{([^}]+)\}", path_var, path)

        # Query parameters: required ones inline, optional ones as a comment.
        query, optional = [], []
        for p in params:
            if p["in"] != "query":
                continue
            pair = quote(p["name"], safe="") + "=" + quote(param_text(param_example(spec, p)), safe="")
            (query if p.get("required") else optional).append(pair)

        # Headers
        headers = []
        accept = get_accept(spec, op)
        if accept:
            headers.append(("Accept", accept))
        for p in params:
            if p["in"] == "header":
                headers.append((p["name"], param_text(param_example(spec, p))))

        auth = get_auth(spec, op)
        if auth:
            if auth["type"] == "bearer":
                variables.setdefault("token", "<your-token>")
                headers.append(("Authorization", "Bearer {{token}}"))
            elif auth["type"] == "basic":
                variables.setdefault("basicAuth", "<base64 of username:password>")
                headers.append(("Authorization", "Basic {{basicAuth}}"))
            elif auth["in"] == "header":
                variables.setdefault("apiKey", "<your-api-key>")
                headers.append((auth["name"], "{{apiKey}}"))
            elif auth["in"] == "query":
                variables.setdefault("apiKey", "<your-api-key>")
                query.append(quote(auth["name"], safe="") + "={{apiKey}}")

        # Body
        body_text = None
        body = get_body(spec, op, params)
        if body:
            ctype, example = body["content_type"], body["example"]
            if "json" in ctype:
                headers.append(("Content-Type", ctype))
                body_text = json.dumps(example, indent=2, ensure_ascii=False)
            elif ctype == "application/x-www-form-urlencoded":
                headers.append(("Content-Type", ctype))
                fields = example if isinstance(example, dict) else {}
                body_text = "&".join(
                    quote(k, safe="") + "=" + quote(param_text(v), safe="")
                    for k, v in fields.items())
            elif ctype.startswith("multipart/"):
                headers.append(("Content-Type", f"multipart/form-data; boundary={BOUNDARY}"))
                fields = example if isinstance(example, dict) else {}
                body_text = multipart_body(fields, body["files"])
            else:
                headers.append(("Content-Type", ctype))
                body_text = example if isinstance(example, str) else json.dumps(example, ensure_ascii=False)

        if optional:
            lines.append("# Optional query parameters: " + "&".join(optional))
        request_line = method.upper() + " {{baseUrl}}" + url_path
        if query:
            request_line += "?" + "&".join(query)
        lines.append(request_line)
        lines += [f"{k}: {v}" for k, v in headers]
        if body_text is not None:
            lines += ["", body_text]
        blocks.append("\n".join(lines))

    header = [f"# {title} (version {version})", "# Generated by swagger_to_http.py", ""]
    header += [f"@{k} = {v}" for k, v in variables.items()]
    return "\n".join(header) + "\n\n" + "\n\n".join(blocks) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Convert Swagger/OpenAPI JSON into a .http file.")
    parser.add_argument("input", help="Path to the swagger/openapi JSON file")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Folder for the .http file (default: same folder as the input file)")
    parser.add_argument("--base-url", help="Override the base URL (default: taken from the spec's servers/host)")
    args = parser.parse_args()

    spec = load_spec(args.input)
    if "paths" not in spec:
        sys.exit("Error: no 'paths' found - is this a Swagger/OpenAPI document?")

    info = spec.get("info", {})
    file_name = f"{safe_name(info.get('title', 'api'))}_{safe_name(info.get('version', ''))}.http"
    out_dir = Path(args.output_dir) if args.output_dir else Path(args.input).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / file_name

    out_path.write_text(build_http(spec, (args.base_url or get_base_url(spec)).rstrip("/")),
                        encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
