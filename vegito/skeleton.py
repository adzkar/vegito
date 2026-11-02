"""Generate a new request .yml matching the collection's existing file style."""
from __future__ import annotations

import json
import re
from http import HTTPStatus

from .common import schema_path_to_bruno

_SETTINGS = """\
settings:
  encodeUrl: true
  timeout: 0
  followRedirects: true
  maxRedirects: 5
  forwardAuthorizationHeader: false"""


def safe_filename(name: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', "_", name).strip() or "request"


def _body_template(op: dict) -> dict:
    """Top-level request body properties mapped to empty strings."""
    return {k: "" for k, v in op["body"].items()
            if "." not in k and not k.endswith("[]")}


def _response_template(op: dict) -> tuple[str, str, dict]:
    """(status, statusText, top-level-fields) of the first 2xx response."""
    for status in sorted(op["responses"]):
        if status.startswith("2"):
            fields = {k: "" for k in op["responses"][status]
                      if "." not in k and not k.endswith("[]")}
            try:
                phrase = HTTPStatus(int(status)).phrase
            except ValueError:
                phrase = "OK"
            return status, phrase, fields
    return "200", "OK", {}


def _yaml_scalar(key: str, value: str, indent: str = "") -> str:
    """Render ``key: value`` quoting/-blocking only when needed."""
    if "\n" in value:
        block = "\n".join(indent + "  " + line for line in value.splitlines())
        return f"{indent}{key}: |-\n{block}"
    if re.search(r"(^[\s!&*\[\]{},#|>@`\"'])|(: )|( #)|(:$)", value):
        return f"{indent}{key}: {json.dumps(value)}"
    return f"{indent}{key}: {value}"


def _param_lines(op: dict, indent: str) -> list[str]:
    """`params:` entries — path params first, then query params."""
    lines = []
    pad, item = indent + "  ", indent + "    "
    for name in op["path_params"]:
        lines += [f"{pad}- name: {name}", f'{item}value: ""',
                  f"{item}type: path"]
    for name, info in op["query"].items():
        lines += [f"{pad}- name: {name}", f'{item}value: ""',
                  f"{item}type: query"]
        if info.get("description"):
            lines.append(_yaml_scalar("description", info["description"], item))
        lines.append(f"{item}disabled: true")
    return lines


def _header_lines(op: dict, indent: str) -> list[str]:
    lines = []
    pad, item = indent + "  ", indent + "    "
    for info in op["headers"].values():
        lines += [f"{pad}- name: {info['name']}", f'{item}value: ""']
        if info.get("description"):
            lines.append(_yaml_scalar("description", info["description"], item))
    return lines


def _request_lines(op: dict, indent: str, url: str,
                   url_first: bool = False) -> list[str]:
    """A request-shaped block: `http:` body (method first) or an example's
    `request:` (url first, matching the collection's generated examples)."""
    head = ([f'{indent}url: "{url}"',
             f"{indent}method: {op['method'].upper()}"] if url_first else
            [f"{indent}method: {op['method'].upper()}",
             f'{indent}url: "{url}"'])
    body_tpl = _body_template(op)
    lines = list(head)
    headers = _header_lines(op, indent)
    if headers:
        lines.append(f"{indent}headers:")
        lines += headers
    params = _param_lines(op, indent)
    if params:
        lines.append(f"{indent}params:")
        lines += params
    if body_tpl:
        data = json.dumps(body_tpl, indent=2)
        lines += [f"{indent}body:", f"{indent}  type: json",
                  f"{indent}  data: |-"]
        lines += [indent + "    " + line for line in data.splitlines()]
    return lines


def render_request_file(op: dict, seq: int) -> str:
    """Full .yml text for a new request, mirroring the collection's style."""
    url = "{{baseUrl}}" + schema_path_to_bruno(op["path"])
    status, phrase, resp_tpl = _response_template(op)
    parts: list[str] = []

    info = ["info:", f"  name: {op['operation_id']}", "  type: http",
            f"  seq: {seq}"]
    if op["tags"]:
        info += ["  tags:", f"    - {op['tags'][0]}"]
    parts.append("\n".join(info))

    http_lines = ["http:"] + _request_lines(op, "  ", url)
    if op["auth_required"]:
        http_lines += ["  auth:", "    type: bearer",
                       '    token: "{{token}}"']
    parts.append("\n".join(http_lines))

    parts.append(_SETTINGS)

    status_name = f"{status} Response"
    ex = ["examples:", f"  - name: {status_name}", "    request:"]
    ex += _request_lines(op, "      ", url, url_first=True)
    ex += ["    response:", f"      status: {status}",
           f"      statusText: {phrase}", "      headers:",
           "        - name: Content-Type",
           "          value: application/json"]
    if resp_tpl:
        data = json.dumps(resp_tpl, indent=2)
        ex += ["      body:", "        type: json", "        data: |-"]
        ex += ["          " + line for line in data.splitlines()]
    parts.append("\n".join(ex))

    if op["description"]:
        parts.append(_yaml_scalar("docs", op["description"]))

    return "\n\n".join(parts) + "\n"
