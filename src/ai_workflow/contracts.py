"""JSON contracts shared by the durable runtime and its execution adapters.

This module performs no I/O. Definitions are copied so a running workflow cannot
silently change when its caller edits the original definition.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re
from typing import Any


class ContractError(ValueError):
    """A request does not satisfy the supported workflow contract."""


KINDS = frozenset({"agent", "program", "comfyui", "tasks", "subflow"})
_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_OUTPUT_PATH = re.compile(r"^[\w-]+(?:\.[\w-]+)*$")
_OUTPUT_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool}


def canonical(value: Any) -> str:
    """Stable JSON content identity, rejecting non-JSON and nonfinite values."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"value must be finite JSON: {exc}") from exc


def references(value: Any) -> list[str]:
    """Validate reference objects and return their paths, including defaults."""
    found: list[str] = []
    if isinstance(value, dict):
        if "$ref" in value:
            if set(value) - {"$ref", "default"}:
                raise ContractError("reference objects allow only $ref and default")
            path = value["$ref"]
            if not isinstance(path, str):
                raise ContractError("$ref must be a string")
            parts = path.split(".")
            if any(not part for part in parts) or parts[0] not in {"input", "vars", "steps"}:
                raise ContractError(f"unsupported reference: {path}")
            if parts[0] == "steps" and (len(parts) < 3 or parts[2] != "output"):
                raise ContractError(f"step references must use steps.<id>.output: {path}")
            found.append(path)
            if "default" in value:
                found.extend(references(value["default"]))
        else:
            for item in value.values():
                found.extend(references(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(references(item))
    return found


def resolve(value: Any, state: dict) -> Any:
    """Resolve explicit JSON paths without evaluating code or attributes."""
    if isinstance(value, list):
        return [resolve(item, state) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    if "$ref" not in value:
        return {key: resolve(item, state) for key, item in value.items()}
    references(value)
    path = value["$ref"]
    parts = path.split(".")
    roots = {"input": state["input"], "vars": state["variables"], "steps": state["steps"]}
    try:
        current = roots[parts[0]]
        if parts[0] == "steps" and state["steps"][parts[1]]["status"] != "completed":
            raise KeyError(parts[1])
        for part in parts[1:]:
            if isinstance(current, dict):
                current = current[part]
            elif isinstance(current, list) and part.isdecimal():
                current = current[int(part)]
            else:
                raise KeyError(part)
        return deepcopy(current)
    except (KeyError, IndexError, TypeError) as exc:
        if "default" in value:
            return resolve(value["default"], state)
        raise ContractError(f"unresolved reference: {path}") from exc


def resolved_references(value: Any, state: dict) -> list[str]:
    """References actually selected by resolution, excluding unused defaults."""
    return [item["reference"] for item in resolved_bindings(value, state)]


def resolved_bindings(value: Any, state: dict, path: tuple = ()) -> list[dict]:
    """Selected references and where their values enter the resolved input."""
    if isinstance(value, list):
        return [ref for i, child in enumerate(value) for ref in resolved_bindings(child, state, (*path, i))]
    if not isinstance(value, dict):
        return []
    if "$ref" in value:
        try:
            resolve({"$ref": value["$ref"]}, state)
            return [{"reference": value["$ref"], "input_path": list(path)}]
        except ContractError:
            if "default" in value:
                return resolved_bindings(value["default"], state, path)
            raise
    return [ref for key, child in value.items() for ref in resolved_bindings(child, state, (*path, key))]


def _strings(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ContractError(f"{label} must be a list of nonempty strings")
    if len(value) != len(set(value)) or (nonempty and not value):
        raise ContractError(f"{label} must contain unique values" + (" and not be empty" if nonempty else ""))
    return value


def _validate_output_contract(node: dict, label: str) -> dict:
    contract = node.get("output_contract", {})
    if not isinstance(contract, dict):
        raise ContractError(f"{label}.output_contract must be an object")
    for outcome, fields in contract.items():
        if outcome not in node.get("outcomes", ["success"]) or not isinstance(fields, dict):
            raise ContractError(f"{label}.output_contract needs declared outcomes and field objects")
        for path, rule in fields.items():
            if not isinstance(path, str) or not _OUTPUT_PATH.fullmatch(path):
                raise ContractError(f"{label}.output_contract has invalid field path: {path!r}")
            if not isinstance(rule, dict) or set(rule) - {"type", "nonempty"}:
                raise ContractError(f"{label}.output_contract.{path} allows only type and nonempty")
            kind = rule.get("type")
            if not isinstance(kind, str) or kind not in {*_OUTPUT_TYPES, "value"}:
                raise ContractError(f"{label}.output_contract.{path} has unsupported type")
            if type(rule.get("nonempty", False)) is not bool:
                raise ContractError(f"{label}.output_contract.{path}.nonempty must be a boolean")
    return contract


def validate_output(node: dict, outcome: str, output: Any) -> None:
    """Check declared handoff fields, without assessing semantic quality or mutating data.

    Missing contracts/outcomes preserve the original unrestricted output shape.
    Dotted fields traverse JSON objects only; extra output fields are retained.
    """
    fields = _validate_output_contract(node, node.get("id", "node")).get(outcome, {})
    if not fields:
        return
    canonical(output)
    for path, rule in fields.items():
        value = output
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise ContractError(f"output_contract.{outcome}: missing output field {path!r}")
            value = value[part]
        kind = rule["type"]
        valid = value is not None if kind == "value" else isinstance(value, _OUTPUT_TYPES[kind])
        if not valid:
            raise ContractError(f"output field {path!r} must be {kind}")
        if rule.get("nonempty") and (
            isinstance(value, str) and not value.strip()
            or isinstance(value, (list, dict)) and not value
        ):
            raise ContractError(f"output field {path!r} must not be empty")


def validate_workflow(workflow: dict) -> dict:
    """Return a normalized, pinned workflow or raise ContractError.

    Node metadata not interpreted here (instructions, registry references,
    adapter descriptions, provenance) is preserved for the execution layer.
    Dependencies are an acyclic graph; explicit bounded routes perform rework.
    """
    if not isinstance(workflow, dict):
        raise ContractError("workflow must be an object")
    canonical(workflow)
    result = deepcopy(workflow)
    for key in ("id", "version"):
        if not isinstance(result.get(key), str) or not result[key]:
            raise ContractError(f"workflow.{key} must be a nonempty string")
    nodes = result.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ContractError("workflow.nodes must be a nonempty object")
    if not isinstance(result.setdefault("variables", {}), dict):
        raise ContractError("workflow.variables must be an object")
    if "phase" in result["variables"] and (type(result["variables"]["phase"]) is not int or result["variables"]["phase"] not in {1, 2}):
        raise ContractError("variables.phase must be 1 or 2 when specified")
    if result.setdefault("failure_policy", "stop") not in {"stop", "continue"}:
        raise ContractError("failure_policy must be stop or continue")
    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not _ID.fullmatch(node_id):
            raise ContractError(f"invalid node id: {node_id!r}")
        if not isinstance(node, dict):
            raise ContractError(f"node {node_id} must be an object")
        if node.get("kind") not in KINDS:
            raise ContractError(f"node {node_id}: unsupported kind {node.get('kind')!r}")
        if node.get("version") != "1":
            raise ContractError(f"node {node_id}: unsupported version {node.get('version')!r}")
        node.setdefault("input", {})
        _strings(node.get("artifact_fields", []), f"{node_id}.artifact_fields")
        _strings(node.setdefault("depends", []), f"{node_id}.depends")
        _strings(node.setdefault("resources", []), f"{node_id}.resources")
        _strings(node.setdefault("outcomes", ["success"]), f"{node_id}.outcomes", nonempty=True)
        if "skipped" in node["outcomes"]:
            raise ContractError("skipped is a reserved dependency status")
        _validate_output_contract(node, node_id)
        cost = node.setdefault("cost", 0)
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            raise ContractError(f"{node_id}.cost must be finite and nonnegative")
        if "phase" in node and (type(node["phase"]) is not int or node["phase"] not in {1, 2}):
            raise ContractError(f"{node_id}.phase must be 1 or 2")
        if not isinstance(node.setdefault("when", {}), dict):
            raise ContractError(f"{node_id}.when must be an object")
        if not isinstance(node.setdefault("routes", {}), dict):
            raise ContractError(f"{node_id}.routes must be an object")
        for dep in node["depends"]:
            if dep not in nodes or dep == node_id:
                raise ContractError(f"{node_id}: invalid dependency {dep}")
        for dep, outcomes in node["when"].items():
            if dep not in node["depends"]:
                raise ContractError(f"{node_id}.when refers to a non-dependency: {dep}")
            _strings(outcomes, f"{node_id}.when.{dep}", nonempty=True)
        for outcome, route in node["routes"].items():
            if outcome not in node["outcomes"] or not isinstance(route, dict):
                raise ContractError(f"{node_id}: route needs a declared outcome and object")
            if set(route) - {"reset", "set", "max_rounds", "on_limit", "counter_key"}:
                raise ContractError(f"{node_id}: unsupported route operation")
            _strings(route.get("reset"), f"{node_id}.routes.{outcome}.reset", nonempty=True)
            if any(target not in nodes for target in route["reset"]):
                raise ContractError(f"{node_id}: route reset target does not exist")
            bound = route.setdefault("max_rounds", 3)
            if bound is None:
                if "on_limit" in route or "counter_key" in route:
                    raise ContractError(f"{node_id}: uncounted routes cannot have on_limit or counter_key")
            elif type(bound) is not int or bound < 1:
                raise ContractError(f"{node_id}: max_rounds must be a positive integer or null")
            if "counter_key" in route:
                for ref in references(route["counter_key"]):
                    if ref.startswith("steps.") and ref.split(".")[1] not in nodes:
                        raise ContractError(f"{node_id}: unknown counter_key reference {ref}")
            if not isinstance(route.setdefault("set", {}), dict):
                raise ContractError(f"{node_id}: route set must be an object")
            for ref in references(route["set"]):
                if ref.startswith("steps.") and ref.split(".")[1] not in nodes:
                    raise ContractError(f"{node_id}: unknown route reference {ref}")
            if "on_limit" in route:
                fallback = route["on_limit"]
                if not isinstance(fallback, dict) or set(fallback) - {"reset", "set"}:
                    raise ContractError(f"{node_id}: on_limit allows only reset and set")
                _strings(fallback.get("reset"), f"{node_id}.on_limit.reset", nonempty=True)
                if any(target not in nodes for target in fallback["reset"]):
                    raise ContractError(f"{node_id}: on_limit reset target does not exist")
                if not isinstance(fallback.setdefault("set", {}), dict):
                    raise ContractError(f"{node_id}: on_limit set must be an object")
                for ref in references(fallback["set"]):
                    if ref.startswith("steps.") and ref.split(".")[1] not in nodes:
                        raise ContractError(f"{node_id}: unknown on_limit reference {ref}")

    visiting: set[str] = set()
    ancestors: dict[str, set[str]] = {}

    def visit(node_id: str) -> set[str]:
        if node_id in visiting:
            raise ContractError(f"dependency cycle at {node_id}")
        if node_id not in ancestors:
            visiting.add(node_id)
            parents: set[str] = set()
            for dep in nodes[node_id]["depends"]:
                parents.add(dep)
                parents.update(visit(dep))
            ancestors[node_id] = parents
            visiting.remove(node_id)
        return ancestors[node_id]

    for node_id, node in nodes.items():
        allowed = visit(node_id)
        for dep, outcomes in node["when"].items():
            if set(outcomes) - set(nodes[dep]["outcomes"]) - {"skipped"}:
                raise ContractError(f"{node_id}: condition uses undeclared result of {dep}")
        for ref in references(node["input"]):
            if ref.startswith("steps.") and ref.split(".")[1] not in allowed:
                raise ContractError(f"{node_id}: input reference requires a dependency: {ref}")
        _strings(node.get("required_inputs", []), f"{node_id}.required_inputs")
    return result
