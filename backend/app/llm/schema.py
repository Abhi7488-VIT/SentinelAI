"""Turn a Pydantic model into a conservative JSON schema for provider-side structured output.

Provider-side constrained decoding supports a subset of JSON Schema. To stay inside it, `$ref`s
are inlined and value constraints (lengths, patterns, numeric bounds, item counts) are dropped;
every object gets `additionalProperties: false` and lists all its properties as required.
The dropped constraints are NOT lost: the client always validates the output against the full
Pydantic model afterwards, and a violation counts as invalid output (bounded retry).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_DROP = {
    "title",
    "default",
    "examples",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "format",
}


def provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs: dict[str, Any] = raw.pop("$defs", {})
    out = _walk(raw, defs, depth=0)
    if not isinstance(out, dict):
        raise TypeError("model schema is not an object")
    return out


def _walk(node: Any, defs: dict[str, Any], depth: int) -> Any:
    if depth > 32:
        raise ValueError("schema nesting too deep (recursive model?)")
    if isinstance(node, list):
        return [_walk(n, defs, depth + 1) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _walk(defs[name], defs, depth + 1)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP:
            continue
        if key == "properties":
            out[key] = {k: _walk(v, defs, depth + 1) for k, v in value.items()}
        else:
            out[key] = _walk(value, defs, depth + 1)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out
