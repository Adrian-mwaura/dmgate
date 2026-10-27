"""A small JSON Schema checker for the subset the LLM layer uses.

Guided decoding should already keep model output inside the schema; this checks it again, because
not every server enforces every keyword. Supported: type, enum, const, properties, required,
additionalProperties (false), items, minItems, maxItems, minLength, maxLength, minimum, maximum,
anyOf.
"""

from __future__ import annotations

from typing import Any

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Every way `value` breaks `schema`; empty when it conforms."""
    out: list[str] = []
    if "anyOf" in schema:
        if all(errors(value, option, path) for option in schema["anyOf"]):
            out.append(f"{path}: matches none of the allowed shapes")
        return out
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_TYPES[k](value) for k in kinds):
            return [f"{path}: expected {kind}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: expected {schema['const']!r}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            out.append(f"{path}: string length {len(value)} out of range")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            out.append(f"{path}: {value} out of range")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            out.append(f"{path}: {len(value)} items out of range")
        if "items" in schema:
            for i, item in enumerate(value):
                out += errors(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        out += [f"{path}: missing {name}" for name in schema.get("required", ()) if name not in value]
        if schema.get("additionalProperties") is False:
            out += [f"{path}: unexpected {name}" for name in value if name not in properties]
        for name, sub in properties.items():
            if name in value:
                out += errors(value[name], sub, f"{path}.{name}")
    return out
