"""Deterministic business transitions, hosted and persisted exclusively by Restate.

These functions only transform JSON data. They do not execute nodes, persist
state, schedule timers, claim resources, or admit spending. The durable caller
must save a dispatch request and reserve its declared resources/cost before I/O.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import Any

from .contracts import ContractError, canonical, references, resolved_bindings, resolve, validate_output, validate_workflow


_ACTIVE = {"running", "waiting", "unknown"}
_DONE = {"completed", "skipped", "failed", "cancelled"}


def _step(generation: int = 0) -> dict:
    return {"status": "pending", "generation": generation, "attempt": 0,
            "execution_id": None, "output": None, "outcome": None}


def _touch(state: dict, event: str, **details: Any) -> None:
    state["revision"] += 1
    state["history"].append({"event": event, "revision": state["revision"], **deepcopy(details)})


def new_run(run_id: str, workflow: dict, inputs: Any, limits: dict | None = None,
            parent: Any = None) -> dict:
    if not isinstance(run_id, str) or not run_id:
        raise ContractError("run_id must be a nonempty string")
    definition = validate_workflow(workflow)
    canonical(inputs)
    if limits is not None and not isinstance(limits, dict):
        raise ContractError("limits must be an object")
    policy = deepcopy(limits or {})
    canonical(policy)
    attempts = policy.setdefault("max_attempts", 3)
    if type(attempts) is not int or attempts < 1:
        raise ContractError("max_attempts must be a positive integer")
    parallel = policy.setdefault("max_parallel", 4)
    if type(parallel) is not int or parallel < 1:
        raise ContractError("max_parallel must be a positive integer")
    calls = policy.get("max_calls", 100)
    if type(calls) is not int or calls < 0:
        raise ContractError("max_calls must be a nonnegative integer")
    cost = policy.get("max_cost")
    if cost is not None and (type(cost) not in (int, float) or cost < 0):
        raise ContractError("max_cost must be a nonnegative number or null")
    if "phase" in policy and (type(policy["phase"]) is not int or policy["phase"] not in {1, 2}):
        raise ContractError("limits.phase must be 1 or 2")
    return {"run_id": run_id, "workflow": definition, "input": deepcopy(inputs),
            "variables": deepcopy(definition["variables"]), "limits": policy,
            "parent": deepcopy(parent), "status": "running", "revision": 0,
            "steps": {node_id: _step() for node_id in definition["nodes"]},
            "messages": [], "reports": {}, "history": [], "route_rounds": {},
            "pending_routes": [], "cancel_requested": False}


def _descendants(workflow: dict, roots: set[str]) -> set[str]:
    affected = set(roots)
    changed = True
    while changed:
        changed = False
        for node_id, node in workflow["nodes"].items():
            if node_id not in affected and affected.intersection(node["depends"]):
                affected.add(node_id)
                changed = True
    return affected


def _active(step: dict) -> bool:
    return step["status"] in _ACTIVE and step["execution_id"] is not None


def _refresh(state: dict) -> None:
    if state["status"] == "paused":
        return
    steps = list(state["steps"].values())
    if state["cancel_requested"]:
        status = "cancelling" if any(_active(step) for step in steps) else "cancelled"
    elif any(step["status"] == "running" for step in steps):
        status = "running"
    elif any(step["status"] in {"waiting", "unknown"} for step in steps) or state["pending_routes"]:
        status = "waiting"
    elif any(step["status"] == "failed" for step in steps):
        if state["workflow"]["failure_policy"] == "continue" and any(step["status"] == "pending" for step in steps):
            status = "running"
        else:
            status = "failed"
    elif all(step["status"] in {"completed", "skipped"} for step in steps):
        status = "succeeded"
    else:
        status = "running"
    if status != state["status"]:
        state["status"] = status
        _touch(state, "status", status=status)


def _reset(state: dict, affected: set[str], reason: str) -> None:
    for node_id in sorted(affected):
        old = state["steps"][node_id]
        state["history"].append({"event": "reset", "node_id": node_id,
                                 "reason": reason, "step": deepcopy(old)})
        state["steps"][node_id] = _step(old["generation"] + 1)


def _apply_routes(state: dict) -> None:
    # A rollback waits for conflicting actual executions to finish. It never
    # erases their identities or assumes that a cancellation request stopped them.
    for pending in list(state["pending_routes"]):
        affected = set(pending["affected"])
        if any(_active(state["steps"][node_id]) for node_id in affected):
            continue
        state["variables"].update(deepcopy(pending["set"]))
        state["variable_sources"] = [s for s in state.get("variable_sources", [])
                                     if (s.get("input_path") or [None])[0] not in pending["set"]]
        state["variable_sources"].extend(deepcopy(pending.get("sources", [])))
        _reset(state, affected, "route_limit" if pending.get("limit_reached") else "route")
        state["pending_routes"].remove(pending)
        _touch(state, "route", source=pending["source"], affected=sorted(affected),
               round=pending["round"], counter_key=pending.get("counter_key"),
               limit_reached=pending.get("limit_reached", False))


def ready(state: dict) -> list[str]:
    """Return dispatchable node IDs, recording skips and terminal run state."""
    if state["status"] == "paused" or state["cancel_requested"]:
        _refresh(state)
        return []
    _apply_routes(state)
    if any(step["status"] == "failed" for step in state["steps"].values()) and state["workflow"]["failure_policy"] == "stop":
        _refresh(state)
        return []
    blocked = {node for route in state["pending_routes"] for node in route["affected"]}
    candidates: list[str] = []
    changed = True
    while changed:
        changed = False
        candidates = []
        for node_id, node in state["workflow"]["nodes"].items():
            step = state["steps"][node_id]
            if step["status"] != "pending" or node_id in blocked:
                continue
            phase = state["variables"].get("phase", state["limits"].get("phase"))
            reason = "other_phase" if phase is not None and "phase" in node and node["phase"] != phase else None
            if reason is None:
                deps = [state["steps"][dep] for dep in node["depends"]]
                if any(dep["status"] not in _DONE for dep in deps):
                    continue
                for dep_id in node["depends"]:
                    dep = state["steps"][dep_id]
                    actual = "skipped" if dep["status"] == "skipped" else dep.get("outcome")
                    if dep["status"] in {"failed", "cancelled"}:
                        reason = "dependency_failed"
                        break
                    if dep_id in node["when"]:
                        if actual not in node["when"][dep_id]:
                            reason = "condition_not_selected"
                            break
                    elif dep["status"] == "skipped":
                        reason = "dependency_skipped"
                        break
            if reason is not None:
                step.update(status="skipped", skip_reason=reason)
                _touch(state, "skipped", node_id=node_id, reason=reason)
                changed = True
                continue
            try:
                local_input = step["request"]["input"] if "request" in step else resolve(node["input"], state)
                if any(not isinstance(local_input, dict) or key not in local_input for key in node.get("required_inputs", [])):
                    raise ContractError(f"{node_id}: missing required input")
            except ContractError as exc:
                step.update(status="failed", error={"code": "input_resolution", "message": str(exc)})
                _touch(state, "input_error", node_id=node_id, error=str(exc))
                changed = True
                continue
            candidates.append(node_id)
    if any(step["status"] == "failed" for step in state["steps"].values()) and state["workflow"]["failure_policy"] == "stop":
        candidates = []
    _refresh(state)
    return candidates


def step_artifacts(state: dict, node_id: str) -> list:
    """Accepted receipts, including runs recorded before step-level receipts."""
    step = state["steps"][node_id]
    if "artifacts" in step:
        return deepcopy(step["artifacts"])
    for event in reversed(state.get("history", [])):
        report = event.get("report", {})
        if (event.get("result", {}).get("accepted") and report.get("kind") == "completed"
                and report.get("execution_id") == step.get("execution_id")
                and report.get("attempt", 1) == step.get("attempt")):
            return deepcopy(report.get("artifacts", []))
    return []


def _supplied_artifacts(artifacts: list, value: Any, source_path: list) -> list:
    def matches(item, artifact, in_field):
        if isinstance(item, str):
            return in_field and item == artifact.get("path")
        if isinstance(item, dict):
            if item.get("path") == artifact.get("path"):
                if item.get("sha256") or item.get("snapshot"):
                    # Forwarding changes receipt identity, not the pinned bytes.
                    return in_field and (item.get("sha256") or item["snapshot"]) == artifact.get("sha256")
                if item.get("artifact_id"):
                    return in_field and item["artifact_id"] in {artifact.get("artifact_id"), (artifact.get("origin") or {}).get("artifact_id")}
                return in_field
            return any(matches(child, artifact, in_field) for child in item.values())
        if isinstance(item, list):
            return any(matches(child, artifact, in_field) for child in item)
        return False
    result = []
    for artifact in artifacts:
        field = artifact.get("output_location") or artifact.get("field", "").split(".")
        related = field[:len(source_path)] == source_path or source_path[:len(field)] == field
        if matches(value, artifact, related):
            result.append(deepcopy(artifact))
    return result


def input_sources(state: dict, spec: Any) -> list:
    """Carry exact input bindings across run boundaries without changing input values."""
    sources = []
    for binding in resolved_bindings(spec, state):
        ref, location = binding["reference"], binding["input_path"]
        parts = ref.split(".")
        value = resolve({"$ref": ref}, state)
        if parts[0] == "steps":
            node_id = parts[1]
            step = state["steps"][node_id]
            sources.append({**binding, "run_id": state["run_id"], "node_id": node_id,
                            "execution_id": step.get("execution_id"), "attempt": step.get("attempt"),
                            "generation": step.get("generation"), "source_path": parts[3:],
                            "artifacts": _supplied_artifacts(step_artifacts(state, node_id), value, parts[3:])})
        elif parts[0] in {"input", "vars"}:
            selected = parts[1:]
            for inherited in state.get("input_sources" if parts[0] == "input" else "variable_sources", []):
                bound = list(map(str, inherited.get("input_path", [])))
                if bound[:len(selected)] == selected:
                    target = location + inherited["input_path"][len(selected):]
                    source_path = inherited.get("source_path", [])
                elif selected[:len(bound)] == bound:
                    target = location
                    source_path = inherited.get("source_path", []) + selected[len(bound):]
                else:
                    continue
                sources.append({**deepcopy(inherited), "reference": ref, "input_path": target, "source_path": source_path,
                                "artifacts": _supplied_artifacts(inherited.get("artifacts", []), value, source_path)})
    return sources


def dispatch(state: dict, node_id: str) -> dict:
    """Record one dispatch. The caller performs atomic resource/budget admission."""
    if node_id not in ready(state):
        raise ContractError(f"node {node_id!r} is not ready")
    node = state["workflow"]["nodes"][node_id]
    step = state["steps"][node_id]
    if "request" not in step:
        identity = canonical([state["run_id"], node_id, step["generation"]])
        step["execution_id"] = "exe-" + sha256(identity.encode("utf-8")).hexdigest()[:32]
        step["request"] = {"execution_id": step["execution_id"], "node_id": node_id,
                           "run_id": state["run_id"], "input": resolve(node["input"], state),
                           "kind": node["kind"], "version": node["version"],
                           "resources": deepcopy(node["resources"]), "cost": node["cost"],
                           "revision": state["revision"], "node_spec": deepcopy(node),
                           "limits": deepcopy(state["limits"]), "parent": deepcopy(state["parent"])}
        step["request"]["input_sources"] = input_sources(state, node["input"])
    step["attempt"] += 1
    step["status"] = "running"
    request = deepcopy(step["request"])
    request["attempt"] = step["attempt"]
    if "reply" in step:
        request["reply"] = deepcopy(step["reply"])
    _touch(state, "dispatch", node_id=node_id, execution_id=step["execution_id"], attempt=step["attempt"])
    _refresh(state)
    return request


def _remember_report(state: dict, payload: dict, result: dict) -> dict:
    state["reports"][payload["report_id"]] = {"content": canonical(payload), "result": deepcopy(result)}
    _touch(state, "report", report=payload, result=result)
    return deepcopy(result)


def report(state: dict, report_dict: dict) -> dict:
    """Accept a fact once; stale executions/attempts cannot replace current work."""
    payload = deepcopy(report_dict)
    content = canonical(payload)
    if not isinstance(payload, dict):
        raise ContractError("report must be an object")
    for key in ("report_id", "execution_id"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ContractError(f"report.{key} must be a nonempty string")
    report_id = payload["report_id"]
    if report_id in state["reports"]:
        previous = state["reports"][report_id]
        if previous["content"] != content:
            raise ContractError("report_id is already associated with different content")
        return deepcopy(previous["result"])
    kind = payload.get("kind")
    if kind not in {"progress", "waiting", "completed", "error"}:
        raise ContractError(f"unsupported report kind: {kind}")
    match = next(((key, step) for key, step in state["steps"].items()
                  if step["execution_id"] == payload["execution_id"]), None)
    if match is None:
        return _remember_report(state, payload, {"accepted": False, "reason": "stale_execution", "status": state["status"]})
    node_id, step = match
    if step["attempt"] > 1 and "attempt" not in payload:
        raise ContractError("attempt is required after a technical retry")
    if payload.get("attempt", 1) != step["attempt"]:
        return _remember_report(state, payload, {"accepted": False, "reason": "stale_attempt", "node_id": node_id, "status": step["status"]})
    if step["status"] not in _ACTIVE:
        return _remember_report(state, payload, {"accepted": False, "reason": "execution_finished", "node_id": node_id, "status": step["status"]})
    node = state["workflow"]["nodes"][node_id]
    if kind == "completed":
        outcome = payload.get("outcome", "success")
        if outcome not in node["outcomes"]:
            raise ContractError(f"undeclared outcome {outcome!r} for {node_id}")
        validate_output(node, outcome, payload.get("output"))
        # Resolve route assignments against the accepted result before changing
        # live state, so invalid route inputs cannot partially commit a report.
        route = node["routes"].get(outcome)
        view = {**state, "steps": {**state["steps"], node_id: {**step, "status": "completed",
                "output": payload.get("output"), "artifacts": payload.get("artifacts", [])}}}
        scope = resolve(route["counter_key"], view) if route and "counter_key" in route else None
        route_key = "scoped:" + canonical(scope) if scope is not None else f"{node_id}:{outcome}"
        route_count = state["route_rounds"].get(route_key, 0)
        counted = bool(route and route["max_rounds"] is not None)
        limit_reached = bool(counted and route_count >= route["max_rounds"])
        selected_route = route.get("on_limit") if limit_reached else route
        assignments = {}
        assignment_sources = []
        if selected_route and not state["cancel_requested"]:
            assignments = resolve(selected_route["set"], view)
            assignment_sources = input_sources(view, selected_route["set"])
        step.update(status="completed", outcome=outcome, output=deepcopy(payload.get("output")),
                    artifacts=deepcopy(payload.get("artifacts", [])))
        step.pop("waiting", None)
        if route and not state["cancel_requested"]:
            if selected_route is None:
                step.update(status="failed", error={"code": "route_limit", "rounds": route_count})
            else:
                next_count = (route_count if limit_reached else route_count + 1) if counted else None
                if counted:
                    state["route_rounds"][route_key] = next_count
                affected = _descendants(state["workflow"], set(selected_route["reset"]))
                state["pending_routes"].append({"source": node_id, "affected": sorted(affected),
                                                 "set": assignments, "sources": assignment_sources, "round": next_count,
                                                  "counter_key": scope,
                                                 "limit_reached": limit_reached})
    elif kind == "waiting":
        if step["status"] != "unknown":
            step["status"] = "waiting"
        step["waiting"] = deepcopy(payload.get("waiting", payload.get("output", {})))
    elif kind == "progress":
        step["progress"] = deepcopy(payload.get("output", payload.get("progress")))
    else:
        error = deepcopy(payload.get("error", {"code": "execution_error"}))
        step["error"] = error
        code = error.get("code") if isinstance(error, dict) else None
        unknown = payload.get("unknown") is True or payload.get("outcome") == "unknown" or code in {"unknown", "submission_unknown"}
        if unknown:
            step["status"] = "unknown"
        elif state["cancel_requested"] and code == "cancelled":
            step["status"] = "cancelled"
        elif (payload.get("temporary") is True and payload.get("safe_retry") is True
              and step["attempt"] < state["limits"]["max_attempts"] and not state["cancel_requested"]):
            step["status"] = "pending"
        else:
            step["status"] = "failed"
    if "message" in payload:
        state["messages"].append({"author": payload.get("author", node_id), "text": deepcopy(payload["message"]),
                                  "related": node_id, "report_id": report_id})
    result = {"accepted": True, "reason": kind, "node_id": node_id, "status": step["status"]}
    _remember_report(state, payload, result)
    ready(state)
    return deepcopy(result)


def _check_revision(state: dict, data: dict) -> None:
    if type(data.get("expected_revision")) is not int or data["expected_revision"] != state["revision"]:
        raise ContractError(f"revision conflict: expected {data.get('expected_revision')}, current {state['revision']}")


def _changed_keys(before: dict, after: dict) -> set[str]:
    return {key for key in set(before) | set(after) if key not in before or key not in after or before[key] != after[key]}


def _revise(state: dict, data: dict) -> list[str]:
    old = state["workflow"]
    candidate = validate_workflow(data.get("workflow", old))
    if (candidate["id"], candidate["version"], set(candidate["nodes"])) != (old["id"], old["version"], set(old["nodes"])):
        raise ContractError("live revision cannot change workflow identity, version, or node set")
    for node_id, node in candidate["nodes"].items():
        for key in (set(node) | set(old["nodes"][node_id])) - {"input"}:
            if node.get(key) != old["nodes"][node_id].get(key):
                raise ContractError(f"live revision cannot change {node_id}.{key}; use a new definition")
    new_input = deepcopy(data.get("input", state["input"]))
    if not isinstance(data.get("variables", {}), dict):
        raise ContractError("variables must be an object")
    new_vars = {**state["variables"], **data.get("variables", {})}
    if "phase" in new_vars and (type(new_vars["phase"]) is not int or new_vars["phase"] not in {1, 2}):
        raise ContractError("variables.phase must be 1 or 2 when specified")
    canonical(new_input)
    canonical(new_vars)
    explicit = data.get("affected", [])
    if not isinstance(explicit, list) or any(node not in old["nodes"] for node in explicit):
        raise ContractError("affected must list existing node IDs")
    roots = set(explicit)
    input_keys = _changed_keys(state["input"], new_input) if isinstance(state["input"], dict) and isinstance(new_input, dict) else ({"*"} if state["input"] != new_input else set())
    variable_keys = _changed_keys(state["variables"], new_vars)
    for node_id, node in candidate["nodes"].items():
        if node != old["nodes"][node_id]:
            roots.add(node_id)
        for ref in references(node["input"]):
            parts = ref.split(".")
            changes = input_keys if parts[0] == "input" else variable_keys if parts[0] == "vars" else set()
            if changes and (len(parts) == 1 or "*" in changes or parts[1] in changes):
                roots.add(node_id)
        if "phase" in variable_keys and "phase" in node:
            roots.add(node_id)
    affected = _descendants(candidate, roots)
    if any(_active(state["steps"][node]) for node in affected):
        raise ContractError("revision conflicts with in-flight work; reconcile it first")
    if any(affected.intersection(route["affected"]) for route in state["pending_routes"]):
        raise ContractError("revision conflicts with a pending rollback")
    _touch(state, "revision", old_input=state["input"], old_variables=state["variables"], affected=sorted(affected))
    state["workflow"] = candidate
    state["input"] = new_input
    state["variables"] = new_vars
    for key, changed in (("input_sources", input_keys), ("variable_sources", variable_keys)):
        state[key] = [s for s in state.get(key, []) if not changed or
                      ("*" not in changed and s.get("input_path") and s["input_path"][0] not in changed)]
    _reset(state, affected, "revision")
    return sorted(affected)


def control(state: dict, action: str, data: dict | None = None) -> dict:
    """Apply a version-checked user/entrypoint action, without external I/O."""
    data = deepcopy(data or {})
    canonical(data)
    _check_revision(state, data)
    result: dict = {"accepted": True, "action": action}
    if action == "pause":
        if state["status"] in {"succeeded", "failed", "cancelled", "cancelling"}:
            raise ContractError("cannot pause a terminal or cancelling run")
        state["status"] = "paused"
    elif action == "resume":
        if state["status"] != "paused":
            raise ContractError("run is not paused")
        state["status"] = "running"
    elif action == "cancel":
        if state["status"] in {"succeeded", "failed"}:
            raise ContractError("cannot cancel an already finished run")
        state["cancel_requested"] = True
        state["status"] = "cancelling"
        state["pending_routes"] = []
        active_ids = []
        for step in state["steps"].values():
            if _active(step):
                step["cancel_requested"] = True
                active_ids.append(step["execution_id"])
            elif step["status"] not in _DONE:
                step["status"] = "cancelled"
        result["cancel_executions"] = active_ids
    elif action == "message":
        if not isinstance(data.get("author"), str) or not isinstance(data.get("text"), str):
            raise ContractError("message requires author and text")
        state["messages"].append({key: deepcopy(data[key]) for key in ("author", "text", "related") if key in data})
    elif action == "revise":
        if state["cancel_requested"]:
            raise ContractError("cannot revise a cancelled run")
        result["affected"] = _revise(state, data)
    elif action in {"reply", "wait"}:
        node_id = data.get("node_id")
        if node_id not in state["steps"]:
            raise ContractError("unknown control node_id")
        step = state["steps"][node_id]
        if action == "wait":
            if step["status"] != "pending" or step["execution_id"] is not None:
                raise ContractError("pre-execution wait requires an undispatched node")
            step.update(status="waiting", waiting=deepcopy(data.get("waiting", {})))
        else:
            if step["status"] != "waiting" or state["cancel_requested"]:
                raise ContractError("node is not awaiting a reply")
            step["reply"] = deepcopy(data.get("reply"))
            step.pop("waiting", None)
            step["status"] = "running" if step["execution_id"] else "pending"
            if step["execution_id"]:
                result["resume"] = {**deepcopy(step["request"]), "attempt": step["attempt"], "reply": deepcopy(step["reply"])}
    else:
        raise ContractError(f"unsupported control action: {action}")
    identity = {}
    if action in {"reply", "wait"}:
        identity = {"node_id": node_id, "execution_id": step.get("execution_id"), "attempt": step.get("attempt")}
    _touch(state, "control", action=action, data=data, **identity)
    ready(state)
    result.update(status=state["status"], revision=state["revision"])
    return result
