#!/usr/bin/env python3
"""Validate router-agent protocol messages against the JSON Schemas in docs/protocol/ (docs/protocol.md).

A deliberately small validator for the subset of JSON Schema (draft 2020-12) that these schemas use, so the check needs no
third-party package. It refuses a schema keyword it does not implement instead of silently skipping it.

Usage: python test/protocol_schema.py <schema file name> <message.json> [--strict]
  e.g. python test/protocol_schema.py heartbeat.schema.json captured-heartbeat.json --strict

--strict also refuses fields the schema does not name (receivers ignore unknown fields, so this is a check for senders
that claim to speak exactly this version)."""
import json
import os
import re
import sys

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "protocol")

# Keywords the validator enforces, and annotations it reads or ignores. Anything else in a schema is an error.
CHECKED = {"type", "properties", "required", "additionalProperties", "items", "enum", "const", "minimum", "maximum",
           "maxLength", "pattern", "$ref", "$defs"}
ANNOTATIONS = {"$schema", "$comment", "title", "description", "examples", "x-since-agent", "x-since-router",
               "x-ignored-by-receiver", "x-legacy", "x-open"}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
VERSION = re.compile(r"baseline|\d+\.\d+\.\d+")


def load_schemas(directory=SCHEMA_DIR):
    """{file name: schema} of every *.schema.json in the directory."""
    out = {}
    for name in sorted(os.listdir(directory)):
        if name.endswith(".schema.json"):
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                out[name] = json.load(f)
    return out


def resolve(schemas, current, ref):
    """(file name, sub-schema) for a $ref: "#/$defs/x", "other.schema.json" or "other.schema.json#/$defs/x"; None if unknown."""
    file, _, pointer = ref.partition("#")
    file = file or current
    node = schemas.get(file)
    for part in [p for p in pointer.split("/") if p]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return (file, node) if isinstance(node, dict) else None


def type_matches(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    return isinstance(value, (int, float))   # number


def errors(schemas, file, schema, value, path="$", strict=False):
    """List of problems of value against schema (from schema file `file`); empty when it conforms."""
    out = []
    if "$ref" in schema:
        target = resolve(schemas, file, schema["$ref"])
        if target is None:
            return [f"{path}: unresolvable $ref {schema['$ref']}"]
        out += errors(schemas, target[0], target[1], value, path, strict)
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(type_matches(value, t) for t in names):
            return out + [f"{path}: expected {'/'.join(names)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} is above {schema['maximum']}")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            out += errors(schemas, file, schema["items"], item, f"{path}[{i}]", strict)
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        out += [f"{path}: missing required field {k!r}" for k in schema.get("required") or [] if k not in value]
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                out += errors(schemas, file, props[k], v, f"{path}.{k}", strict)
            elif isinstance(extra, dict):
                out += errors(schemas, file, extra, v, f"{path}.{k}", strict)
            elif extra is False or (strict and "properties" in schema and not schema.get("x-open")):
                out.append(f"{path}: field {k!r} is not in the schema")
    return out


def validate(name, value, strict=False, schemas=None):
    """Problems of a message against the schema file `name` (e.g. "heartbeat.schema.json")."""
    schemas = schemas if schemas is not None else load_schemas()
    if name not in schemas:
        return [f"no schema {name}"]
    return errors(schemas, name, schemas[name], value, strict=strict)


def schema_problems(schemas):
    """Meta-check: only supported keywords, resolvable $refs, valid types, a description on every property, version
    annotations in the right form, and every example valid against its schema (strictly)."""
    out = []

    def walk(file, node, path):
        if not isinstance(node, dict):
            out.append(f"{file} {path}: not an object")
            return
        for k in node:
            if k not in CHECKED and k not in ANNOTATIONS:
                out.append(f"{file} {path}: unsupported keyword {k!r}")
        types = node.get("type")
        for t in (types if isinstance(types, list) else [types] if types is not None else []):
            if t not in TYPES:
                out.append(f"{file} {path}: unknown type {t!r}")
        for k in ("x-since-agent", "x-since-router"):
            if k in node and not (isinstance(node[k], str) and VERSION.fullmatch(node[k])):
                out.append(f"{file} {path}: {k} must be 'baseline' or a version, got {node[k]!r}")
        if "$ref" in node and resolve(schemas, file, node["$ref"]) is None:
            out.append(f"{file} {path}: unresolvable $ref {node['$ref']}")
        if "pattern" in node:
            re.compile(node["pattern"])   # a broken pattern is a programming error in the schema
        for k in node.get("required") or []:
            if k not in (node.get("properties") or {}):
                out.append(f"{file} {path}: required field {k!r} is not a property")
        for k, sub in (node.get("properties") or {}).items():
            if not (isinstance(sub, dict) and (sub.get("description") or "$ref" in sub)):
                out.append(f"{file} {path}.{k}: property without description")
            walk(file, sub, f"{path}.{k}")
        for k, sub in (node.get("$defs") or {}).items():
            walk(file, sub, f"{path}.$defs.{k}")
        for k in ("items", "additionalProperties"):
            if isinstance(node.get(k), dict):
                walk(file, node[k], f"{path}.{k}")

    for file, schema in schemas.items():
        walk(file, schema, "$")
        if not schema.get("title") or not schema.get("description"):
            out.append(f"{file}: title and description are required")
        if not schema.get("examples"):
            out.append(f"{file}: at least one example is required")
        for i, example in enumerate(schema.get("examples") or []):
            out += [f"{file} example {i}: {e}" for e in errors(schemas, file, schema, example, strict=True)]
    return out


def main(argv):
    args = [a for a in argv if a != "--strict"]
    if len(args) != 2:
        print(__doc__)
        return 2
    with open(args[1], encoding="utf-8") as f:
        text = f.read()
    try:
        message = json.loads(text)
    except ValueError:   # the file is untrusted input; whether it parses only the parser can tell
        print(f"{args[1]}: not JSON")
        return 1
    problems = validate(args[0], message, strict="--strict" in argv)
    for p in problems:
        print(p)
    print(f"{args[1]}: {'conforms to' if not problems else 'does not conform to'} {args[0]}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
