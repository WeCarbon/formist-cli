"""A minimal YAML subset, just enough for ``decisions/*.md`` front matter.

``fm`` must stay stdlib-only (no PyYAML — ``formist_cli.cli``'s module
docstring), yet local mode has to read and write the exact same front
matter bytes `nod` does (contract §6.3): block mappings, block sequences
of scalars or of mappings, and one level of nested mapping (``answer``,
``question_defaults``, ``partial_answer``). That is the whole shape
``core/serialize.ts``/``core/parse.ts`` ever put there — no YAML anchors,
no multi-document streams, no flow collections, no multi-line scalars.
This module implements exactly that shape, matching the output of the
``yaml`` npm package's default ``stringify(value, { lineWidth: 0,
defaultKeyType: 'PLAIN' })`` byte for byte for the values noddit writes,
rather than being a general YAML 1.2 parser.

Scalar typing follows YAML's "core" schema (the one the ``yaml`` npm
package resolves with by default): unquoted ``null``/``true``/``false``/
int/float read back as that type; anything else, quoted or not, is a
string. Dates are never special-cased — an ISO timestamp round-trips as
plain text, exactly as `nod`'s own output proves it does (the `--json`
``created``/``updated_at`` fields match the front matter byte for byte).
"""

from __future__ import annotations

import re

_INDENT = 2

_NULL_WORDS = {"null", "Null", "NULL", "~", ""}
_TRUE_WORDS = {"true", "True", "TRUE"}
_FALSE_WORDS = {"false", "False", "FALSE"}
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")

#: Characters that may never start an unquoted (plain) scalar in this
#: subset — a conservative superset of YAML's own indicator characters.
_LEADING_FORBIDDEN = set("-?:,[]{}#&*!|>'\"%@`")


def _looks_typed(s: str) -> bool:
    """Whether the bare (unquoted) text ``s`` would resolve to something
    other than itself under the core schema, and so must be quoted to stay
    a string."""
    return s in _NULL_WORDS or s in _TRUE_WORDS or s in _FALSE_WORDS or bool(_INT_RE.match(s)) or bool(_FLOAT_RE.match(s))


def needs_quoting(s: str) -> bool:
    if s == "":
        return True
    if s != s.strip():
        return True
    if "\n" in s or "\t" in s or "\r" in s:
        return True
    if _looks_typed(s):
        return True
    if s[0] in _LEADING_FORBIDDEN:
        return True
    if ": " in s or s.endswith(":"):
        return True
    if " #" in s:
        return True
    return False


def _quote(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{out}"'


def scalar_repr(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    s = str(value)
    return _quote(s) if needs_quoting(s) else s


def _is_scalar(value) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _render_kv(key: str, value, indent: int) -> list[str]:
    pad = " " * indent
    if _is_scalar(value):
        return [f"{pad}{key}: {scalar_repr(value)}"]
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        if all(_is_scalar(item) for item in value):
            lines = [f"{pad}{key}:"]
            for item in value:
                lines.append(f"{pad}  - {scalar_repr(item)}")
            return lines
        lines = [f"{pad}{key}:"]
        for item in value:
            if not isinstance(item, dict):
                raise TypeError(f"mixed sequence under {key!r}: {item!r}")
            item_lines = _render_mapping(item, indent + 4)
            first = item_lines[0][indent + 4:]
            item_lines[0] = f"{pad}  - {first}"
            lines.extend(item_lines)
        return lines
    if isinstance(value, dict):
        lines = [f"{pad}{key}:"]
        lines.extend(_render_mapping(value, indent + _INDENT))
        return lines
    raise TypeError(f"unsupported front-matter value for {key!r}: {value!r}")


def _render_mapping(mapping: dict, indent: int) -> list[str]:
    lines: list[str] = []
    for key, value in mapping.items():
        lines.extend(_render_kv(key, value, indent))
    return lines


def dump(mapping: dict) -> str:
    """The YAML block for an ordered top-level mapping — no ``---`` fences.

    Key order is exactly the dict's own iteration order; the caller
    (``task_serialize``) is responsible for ordering it the way
    ``serialize.ts``'s ``KEY_ORDER`` does.
    """
    lines = _render_mapping(mapping, 0)
    return "\n".join(lines) + "\n" if lines else ""


class YamlError(ValueError):
    """Front matter that doesn't fit this subset."""


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


_KEY_RE = re.compile(r"^([^:\n]+):(.*)$")


def _split_key_rest(content: str) -> tuple[str, str | None]:
    """``"key: value"`` -> ``("key", "value")``; ``"key:"`` -> ``("key", None)``
    (a nested block follows). Raises if ``content`` isn't a key line at all."""
    if content[:1] in ('"', "'"):
        # A quoted scalar (e.g. a sequence item `- "a: b"`) is never a key:
        # keys are always plain in this subset (`defaultKeyType: 'PLAIN'`),
        # so a leading quote means this line is a scalar value, not a
        # mapping entry, even if it happens to contain ": " inside the quotes.
        raise YamlError(f"not a mapping entry: {content!r}")
    m = _KEY_RE.match(content)
    if not m:
        raise YamlError(f"not a mapping entry: {content!r}")
    key, after = m.group(1), m.group(2)
    if after == "":
        return key, None
    if after[:1] != " ":
        # A colon with no following space is not a mapping indicator (e.g. a
        # scalar like "a:b") — this line was not a key line after all.
        raise YamlError(f"not a mapping entry: {content!r}")
    return key, after[1:]


def _unquote(raw: str) -> str:
    if raw.startswith('"'):
        if not raw.endswith('"') or len(raw) < 2:
            raise YamlError(f"unterminated double-quoted scalar: {raw!r}")
        body = raw[1:-1]
        out = []
        i = 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    if raw.startswith("'"):
        if not raw.endswith("'") or len(raw) < 2:
            raise YamlError(f"unterminated single-quoted scalar: {raw!r}")
        return raw[1:-1].replace("''", "'")
    return raw


def resolve_scalar(raw: str) -> object:
    """A scalar's value, typed per the core schema — unless quoted, which
    always stays a string regardless of what it looks like."""
    raw = raw.strip()
    if raw[:1] in ('"', "'"):
        return _unquote(raw)
    if raw in _NULL_WORDS:
        return None
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    if _INT_RE.match(raw):
        return int(raw)
    if _FLOAT_RE.match(raw):
        return float(raw)
    return raw


def _is_seq_line(lines: list[str], i: int, indent: int) -> bool:
    if i >= len(lines) or lines[i].strip() == "":
        return False
    if _leading_spaces(lines[i]) != indent:
        return False
    return lines[i][indent:].startswith("- ") or lines[i][indent:].rstrip() == "-"


def _is_map_line(lines: list[str], i: int, indent: int) -> bool:
    if i >= len(lines) or lines[i].strip() == "":
        return False
    if _leading_spaces(lines[i]) != indent:
        return False
    try:
        _split_key_rest(lines[i][indent:])
    except YamlError:
        return False
    return True


def _parse_mapping(lines: list[str], i: int, indent: int) -> tuple[dict, int]:
    result: dict = {}
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        cur = _leading_spaces(line)
        if cur < indent:
            break
        if cur > indent:
            raise YamlError(f"unexpected indent at line {i + 1}: {line!r}")
        key, after = _split_key_rest(line[indent:])
        if after is None:
            if _is_seq_line(lines, i + 1, indent + _INDENT):
                value, i = _parse_sequence(lines, i + 1, indent + _INDENT)
            elif _is_map_line(lines, i + 1, indent + _INDENT):
                value, i = _parse_mapping(lines, i + 1, indent + _INDENT)
            else:
                value, i = None, i + 1
        else:
            value, i = resolve_scalar(after), i + 1
        result[key] = value
    return result, i


def _parse_sequence(lines: list[str], i: int, indent: int) -> tuple[list, int]:
    items: list = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        cur = _leading_spaces(line)
        if cur < indent:
            break
        if cur > indent:
            raise YamlError(f"unexpected indent at line {i + 1}: {line!r}")
        content = line[indent:]
        if not content.startswith("- ") and content.rstrip() != "-":
            break
        after_dash = content[2:] if content.startswith("- ") else ""
        try:
            _split_key_rest(after_dash)  # probe only: is this a "- key: ..." line?
            is_mapping_item = True
        except YamlError:
            is_mapping_item = False
        if not is_mapping_item:
            items.append(resolve_scalar(after_dash))
            i += 1
            continue
        # Rewrite this one line so the first key lines up at indent+2, the
        # same column every later sibling key of this item already sits at
        # (``- `` is exactly two characters, same width as the two extra
        # spaces it stands in for) — then an ordinary mapping parse handles
        # the first key and every key after it uniformly.
        field_indent = indent + _INDENT
        lines[i] = (" " * field_indent) + after_dash
        item, i = _parse_mapping(lines, i, field_indent)
        items.append(item)
    return items, i


def load(text: str) -> dict:
    """Parse a front-matter block (no ``---`` fences) into nested
    dict/list/scalar values."""
    lines = text.split("\n")
    # A trailing blank line from the split is not a content line.
    while lines and lines[-1] == "":
        lines.pop()
    result, i = _parse_mapping(lines, 0, 0)
    if i < len(lines):
        raise YamlError(f"unconsumed front matter at line {i + 1}: {lines[i]!r}")
    return result
