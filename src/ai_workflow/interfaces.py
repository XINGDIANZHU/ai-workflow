"""Versioned node interfaces: schema validation and conservative wiring checks.

JSON Schema validates actual values. Wiring checks reject provable mistakes;
unknown external values and open payloads are checked again at dispatch time.
No I/O, schema downloads, state storage, or execution lives in this module.
"""
from functools import lru_cache
import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class InterfaceError(ValueError):
    pass


_KEYWORDS = {
    "type", "title", "description", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "minProperties", "maxProperties", "minLength",
    "maxLength", "pattern", "enum", "const", "anyOf", "oneOf", "allOf",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "uniqueItems",
}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_WAIT = {"type": "object", "required": ["reason", "question", "resume_condition"],
         "properties": {k: {"type": "string", "pattern": r"\S"}
                        for k in ("reason", "question", "resume_condition")}}
_ERROR = {"type": "object", "required": ["code"],
          "properties": {"code": {"type": "string", "pattern": r"\S"}}}


def _keywords(schema, label):
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise InterfaceError(f"{label}: unsupported schema keys (use inline JSON Schema)")
    for key, child in schema.get("properties", {}).items():
        _keywords(child, f"{label}.{key}")
    for key in ("items", "additionalProperties"):
        if key in schema:
            _keywords(schema[key], f"{label}.{key}")
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key, []):
            _keywords(child, label)
    if set(schema.get("required", [])) - set(schema.get("properties", {})):
        raise InterfaceError(f"{label}: required fields must be described in properties")


@lru_cache(maxsize=256)
def _validator(encoded):
    schema = json.loads(encoded)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InterfaceError(f"invalid io_contract schema: {exc.message}") from exc
    _keywords(schema, "io_contract")
    return Draft202012Validator(schema)


def _schema(schema, label):
    try:
        return _validator(json.dumps(schema, sort_keys=True, ensure_ascii=False, allow_nan=False))
    except InterfaceError as exc:
        raise InterfaceError(f"{label}: {exc}") from exc


def validate_interface(node, label):
    contract = node.get("io_contract")
    if contract is None:
        return
    if (not isinstance(contract, dict) or set(contract) != {"version", "input", "outputs"}
            or contract["version"] != "1"):
        raise InterfaceError(f"{label}.io_contract: expected version 1, input and outputs")
    if not isinstance(contract["outputs"], dict) or set(contract["outputs"]) != set(node["outcomes"]):
        raise InterfaceError(f"{label}.io_contract.outputs must cover every declared outcome")
    for key, schema in [("input", contract["input"]), *contract["outputs"].items()]:
        _schema(schema, f"{label}.io_contract.{key}")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise InterfaceError(f"{label}.io_contract.{key}: root must be an object schema")
    if "required_inputs" in node and set(node["required_inputs"]) != set(contract["input"].get("required", [])):
        raise InterfaceError(f"{label}: required_inputs must match io_contract required fields")


def validate_value(value, schema, label):
    error = next(_schema(schema, label).iter_errors(value), None)
    if error:
        path = ".".join(map(str, error.absolute_path))
        # Do not echo potentially large/private field values in an error message.
        detail = ("missing " + ", ".join(k for k in error.validator_value if k not in error.instance)
                  if error.validator == "required" else f"violates {error.validator}")
        raise InterfaceError(f"{label}{'.' + path if path else ''}: {detail}")


def validate_input(node, value):
    if "io_contract" in node:
        validate_value(value, node["io_contract"]["input"], f"{node.get('id', 'node')}.input")


def validate_result(node, outcome, value):
    if "io_contract" in node:
        validate_value(value, node["io_contract"]["outputs"][outcome],
                       f"{node.get('id', 'node')}.output.{outcome}")


def validate_report(node, report):
    if "io_contract" not in node:
        return
    if report["kind"] == "waiting":
        validate_value(report.get("waiting"), _WAIT, "report.waiting")
    elif report["kind"] == "error":
        validate_value(report.get("error"), _ERROR, "report.error")


def _types(schema):
    if schema is True:
        return _TYPES
    if schema is False:
        return set()
    if "anyOf" in schema or "oneOf" in schema:
        return set().union(*(_types(s) for s in schema.get("anyOf", schema.get("oneOf"))))
    value = schema.get("type")
    types = set(value if isinstance(value, list) else [value]) if value else _TYPES
    return types | ({"integer"} if "number" in types else set())


def _at(schema, parts):
    """Return possible field schemas; False means provably absent, None optional."""
    if not parts or isinstance(schema, bool):
        return [schema]
    if "anyOf" in schema or "oneOf" in schema:
        return [s for branch in schema.get("anyOf", schema.get("oneOf")) for s in _at(branch, parts)]
    key, *rest = parts
    if "array" in _types(schema) and key.isdecimal():
        return [None, *_at(schema.get("items", True), rest)]
    if key in schema.get("properties", {}):
        result = _at(schema["properties"][key], rest)
        return result if key in schema.get("required", []) else [None, *result]
    if "object" not in _types(schema):
        return [False]
    extra = schema.get("additionalProperties", True)
    return [False] if extra is False else [True]


def _compatible(source, target, label):
    if target is False:
        raise InterfaceError(f"{label}: destination disallows this field")
    if source is True or target is True:
        return
    if source is False or target is False or not _types(source) & _types(target):
        raise InterfaceError(f"{label}: incompatible source and input types")
    if isinstance(source, dict) and ("anyOf" in source or "oneOf" in source):
        for branch in source.get("anyOf", source.get("oneOf")):
            _compatible(branch, target, label)
        return
    if isinstance(target, dict) and ("anyOf" in target or "oneOf" in target):
        for branch in target.get("anyOf", target.get("oneOf")):
            try:
                _compatible(source, branch, label)
                return
            except InterfaceError:
                pass
        raise InterfaceError(f"{label}: no compatible input alternative")
    source_enum = source.get("enum", [source["const"]] if "const" in source else None)
    if source_enum is not None and not any(Draft202012Validator(target).is_valid(v) for v in source_enum):
        raise InterfaceError(f"{label}: incompatible enum values")
    for key in target.get("required", []):
        candidates = _at(source, [key])
        if candidates == [False]:
            raise InterfaceError(f"{label}: source cannot supply required field {key}")
        for candidate in candidates:
            if candidate is not None and candidate is not False:
                _compatible(candidate, target.get("properties", {}).get(key, True), label + "." + key)
    if "array" in _types(source) & _types(target) and "items" in source and "items" in target:
        _compatible(source["items"], target["items"], label + "[]")


def _partial(value, schema, label, workflow, node, inputs):
    if schema is False:
        return validate_value(value, schema, label)
    if isinstance(value, dict) and "$ref" in value:
        parts = value["$ref"].split(".")
        if parts[0] == "input" and inputs is not None:
            actual = inputs
            try:
                for part in parts[1:]:
                    actual = actual[int(part)] if isinstance(actual, list) else actual[part]
            except (KeyError, IndexError, TypeError, ValueError):
                if "default" not in value:
                    raise InterfaceError(f"{label}: unresolved reference {value['$ref']}")
                return _partial(value["default"], schema, label, workflow, node, inputs)
            return validate_value(actual, schema, label)
        if parts[0] == "steps":
            source = workflow["nodes"][parts[1]]
            outputs = source.get("io_contract", {}).get("outputs", {})
            outcomes = node.get("when", {}).get(parts[1], source.get("outcomes", []))
            for outcome in outcomes:
                if outcome not in outputs:
                    continue  # Legacy source or skipped dependency: runtime check remains required.
                for candidate in _at(outputs[outcome], parts[3:]):
                    if candidate is False or candidate is None:
                        if "default" not in value:
                            raise InterfaceError(f"{label}: {value['$ref']} is not guaranteed for {outcome}")
                    else:
                        _compatible(candidate, schema, f"{label} <- {value['$ref']} ({outcome})")
        if "default" in value:
            _partial(value["default"], schema, label + ".default", workflow, node, inputs)
        return
    if isinstance(schema, bool):
        return validate_value(value, schema, label)
    if not _contains_reference(value):
        return validate_value(value, schema, label)
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            _partial(value, {k: v for k, v in schema.items() if k != keyword},
                     label, workflow, node, inputs)
            for branch in schema[keyword]:
                try:
                    _partial(value, branch, label, workflow, node, inputs)
                    return
                except InterfaceError:
                    pass
            raise InterfaceError(f"{label}: no compatible schema alternative")
    for branch in schema.get("allOf", []):
        _partial(value, branch, label, workflow, node, inputs)
    shape = {k: v for k, v in schema.items() if k not in {"properties", "items", "allOf"}}
    if "properties" in schema:
        shape["properties"] = {key: True for key in schema["properties"]}
    shape.pop("additionalProperties", None)
    validate_value(value, shape, label)
    if isinstance(value, dict):
        for key, child in value.items():
            sub = schema.get("properties", {}).get(key, schema.get("additionalProperties", True))
            _partial(child, sub, label + "." + key, workflow, node, inputs)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _partial(child, schema.get("items", True), f"{label}[{i}]", workflow, node, inputs)


def _contains_reference(value):
    if isinstance(value, dict):
        return "$ref" in value or any(_contains_reference(x) for x in value.values())
    return isinstance(value, list) and any(_contains_reference(x) for x in value)


def validate_connections(workflow, inputs=None):
    for key, node in workflow["nodes"].items():
        if "io_contract" in node:
            _partial(node["input"], node["io_contract"]["input"], f"{key}.input", workflow, node, inputs)
