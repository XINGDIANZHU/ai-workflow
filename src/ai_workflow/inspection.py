"""Read-only task views derived from authoritative run snapshots and reports.

No inferred dates, adopted evidence, or second mutable task ledger are created.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from .artifacts import artifact_candidates, inspect_artifact


def _request_text(inputs):
    """Read only explicit request fields, including the parent handoff shape."""
    if not isinstance(inputs, dict):
        return None
    context = inputs.get("context", {})
    candidates = [inputs.get("request"), context.get("request") if isinstance(context, dict) else None]
    for value in candidates:
        if isinstance(value, dict):
            value = value.get("request")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _title(state):
    inputs = state.get("input", {})
    if isinstance(inputs, dict):
        for value in (inputs.get("title"), _request_text(inputs), inputs.get("requirements")):
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:160]
    workflow = state.get("workflow", {})
    return workflow.get("title") or workflow.get("id") or state.get("run_id", "任务")


def _node_title(state, node_id):
    node = state.get("workflow", {}).get("nodes", {}).get(node_id, {})
    return node.get("title") or node_id


def _request(state, node_id):
    step = state.get("steps", {}).get(node_id, {})
    for key, request in reversed(list(state.get("requests", {}).items())):
        if (request.get("node_id") == node_id and request.get("execution_id") == step.get("execution_id")
                and request.get("attempt") == step.get("attempt")):
            return request, state.get("assignments", {}).get(key)
    return step.get("request", {}), step.get("assignment")


def summarize(state):
    steps = state.get("steps", {})
    workflow = state.get("workflow", {})
    current = []
    unclaimed = 0
    for node_id, step in steps.items():
        request, owner = _request(state, node_id)
        status = step.get("status", "pending")
        if status == "running" and request and not request.get("child_run") and not owner:
            unclaimed += 1
            status = "unclaimed"
        # Error/waiting fields are retained as evidence after recovery. They are
        # not current blockers once the step has advanced or ended.
        blocker = (state.get("admission", {}).get(node_id) if status == "pending" else
                   step.get("waiting") if status == "waiting" else
                   step.get("error") if status in {"unknown", "failed"} else None)
        # A cancelled child may leave a failed node in a cancelled run. Keep
        # that history, without presenting it as work still awaiting resolution.
        if state.get("status") not in {"succeeded", "cancelled"} and (
                status in {"running", "waiting", "unknown", "unclaimed", "failed"} or blocker):
            current.append({"node_id": node_id, "title": _node_title(state, node_id), "status": status,
                            "owner": owner, "progress": step.get("progress"), "blocker": blocker})
    inputs = state.get("input", {})
    meta = inputs.get("_meta", {}) if isinstance(inputs, dict) else {}
    purpose = meta.get("purpose", "unknown") if isinstance(meta, dict) else "unknown"
    return {"run_id": state.get("run_id"), "root": state.get("root", state.get("run_id")),
            "workspace": deepcopy(state.get("workspace")),
            "status": state.get("status"), "revision": state.get("revision"), "title": _title(state),
            "workflow_id": workflow.get("id", ""), "workflow_title": workflow.get("title") or workflow.get("id", ""),
            "total": len(steps), "completed": sum(s.get("status") == "completed" for s in steps.values()),
            "skipped": sum(s.get("status") == "skipped" for s in steps.values()),
            "waiting": sum(s.get("status") in {"waiting", "unknown"} for s in steps.values()),
            "unclaimed": unclaimed, "current_nodes": current, "purpose": purpose if purpose in {"task", "test"} else "unknown",
            "created_at": state.get("created_at"), "updated_at": state.get("updated_at")}


def _references(value):
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str):
            yield value["$ref"]
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _waiting_identity(history):
    """Recover an old reply's occurrence only from its still-open wait."""
    for previous in reversed(history):
        if previous["event"] == "report":
            if previous["accepted"] is not True:
                continue
            if previous["kind"] == "waiting":
                return previous["execution_id"], previous["attempt"]
            if previous["kind"] in {"completed", "error"}:
                break
        elif previous.get("action") == "wait":
            return previous["execution_id"], previous["attempt"]
        elif previous["event"] in {"reset", "dispatch"} or previous.get("action") == "reply":
            break
    return None, None


def inspect_run(state, root):
    """Expand a run into human-facing nodes, immutable report history and artifacts."""
    state = deepcopy(state)
    specs = state.get("workflow", {}).get("nodes", {})
    steps = state.get("steps", {})
    histories = {node_id: [] for node_id in steps}
    artifacts = {}
    identities = {}

    def add_artifacts(node_id, output, *, execution_id=None, attempt=None, report_id=None, at=None, captured=None):
        candidates = captured if captured else artifact_candidates(output)
        ids = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate.get("path"):
                continue
            record = {**{key: value for key, value in candidate.items() if not key.startswith("_")},
                      "node_id": node_id, "used_by": []}
            for field, value in (("execution_id", execution_id), ("attempt", attempt), ("report_id", report_id), ("at", at)):
                if value is not None or field not in record:
                    record[field] = value
            key = (node_id, record.get("execution_id"), record["path"], record.get("sha256"), record.get("artifact_id"))
            if not record.get("artifact_id"):
                fields = 4 if record.get("sha256") else 3
                known = next((identity for previous, identity in identities.items() if previous[:fields] == key[:fields]), None)
                if known:
                    ids.append(known)
                    continue
            if key in identities:
                ids.append(identities[key])
                continue
            identity = record.get("artifact_id") or sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:32]
            # Artifact IDs are scoped to a node occurrence, even when an upstream
            # captured artifact is included again in a downstream deliverable.
            if identity in artifacts:
                identity = sha256((identity + json.dumps(key, ensure_ascii=False)).encode()).hexdigest()[:32]
            record["artifact_id"] = identity
            record.setdefault("title", Path(record["path"].replace("\\", "/")).name)
            try:
                record = inspect_artifact(root, record)
            except (OSError, ValueError) as exc:
                record.update(status="unavailable", detail=str(exc))
            artifacts[identity] = record
            identities[key] = identity
            ids.append(identity)
        return ids

    for entry in state.get("history", []):
        report = entry.get("report", {})
        result = entry.get("result", {})
        action = entry.get("action") if entry.get("event") == "control" else None
        control_data = entry.get("data", {}) if action in {"reply", "wait"} else {}
        node_id = entry.get("node_id") or result.get("node_id") or control_data.get("node_id")
        if node_id not in histories:
            continue
        step = entry.get("step", {})
        execution_id = report.get("execution_id") or entry.get("execution_id") or step.get("execution_id")
        attempt = report.get("attempt", entry.get("attempt", step.get("attempt")))
        if action == "reply" and "execution_id" not in entry:
            execution_id, attempt = _waiting_identity(histories[node_id])
        if attempt is None and execution_id:
            attempt = next((previous["attempt"] for previous in reversed(histories[node_id])
                            if previous.get("execution_id") == execution_id and previous.get("attempt") is not None), None)
            if attempt is None:
                matching = {job.get("attempt") for job in state.get("requests", {}).values()
                            if job.get("execution_id") == execution_id and job.get("attempt") is not None}
                if len(matching) == 1:
                    attempt = matching.pop()
        owner = report.get("worker_id") or entry.get("worker_id") or entry.get("new_worker") or entry.get("owner")
        item = {"event": entry.get("event"), "at": entry.get("at"), "revision": entry.get("revision"),
                "kind": report.get("kind"), "outcome": report.get("outcome", step.get("outcome")),
                "output": report.get("output", step.get("output")), "error": report.get("error", step.get("error")),
                "progress": report.get("progress"), "waiting": report.get("waiting", control_data.get("waiting")),
                "action": action, "reply": control_data.get("reply"),
                "old_worker": entry.get("old_worker"), "new_worker": entry.get("new_worker"),
                "owner": owner, "execution_id": execution_id, "attempt": attempt,
                "accepted": result.get("accepted"), "reason": entry.get("reason", result.get("reason")),
                "raw": deepcopy(entry)}
        output = report.get("output")
        # Old notify_parent reports attached this exact error even on success.
        # Correct only that identifiable producer bug; raw evidence stays intact.
        if (result.get("accepted") is True and report.get("kind") == "completed" and report.get("outcome") == "success"
                and isinstance(output, dict) and output.get("child_run") and output.get("status") == "succeeded"
                and report.get("report_id") == f"child:{output['child_run']}:final"
                and item["error"] == {"code": "child_failed", "status": "succeeded"}):
            item["error"] = None
            item["correction"] = "旧版汇总误附了失败标签；子流程实际成功，原始记录保留供核对。"
        histories[node_id].append(item)
        if entry.get("event") == "report" and result.get("accepted") is True:
            item["artifacts"] = add_artifacts(node_id, report.get("output"), execution_id=execution_id,
                attempt=attempt, report_id=report.get("report_id"), at=entry.get("at"), captured=report.get("artifacts"))
        elif entry.get("event") == "reset":
            item["artifacts"] = add_artifacts(node_id, step.get("output"), execution_id=execution_id, attempt=attempt,
                                              captured=step.get("artifacts"))

    # Finish collecting outputs before matching any downstream references;
    # serialized object order is not an execution order.
    for node_id, step in steps.items():
        add_artifacts(node_id, step.get("output"), execution_id=step.get("execution_id"), attempt=step.get("attempt"),
                      captured=step.get("artifacts"))

    def matches_receipt(candidate, artifact):
        if candidate.get("artifact_id"):
            # Legacy outputs can reuse an upstream ID. add_artifacts scopes
            # those IDs to node occurrences; retain that exact mapping too.
            key = (artifact["node_id"], artifact.get("execution_id"), artifact["path"],
                   artifact.get("sha256"), candidate["artifact_id"])
            return (candidate["artifact_id"] == artifact["artifact_id"]
                    or identities.get(key) == artifact["artifact_id"])
        return (candidate.get("path") == artifact.get("path") and candidate.get("sha256")
                and candidate["sha256"] == artifact.get("sha256"))

    nodes, assignments, children = [], [], []
    for node_id, step in steps.items():
        spec = specs.get(node_id, {})
        request, owner = _request(state, node_id)
        sources = []
        recorded_sources = request.get("input_sources", [])
        for recorded_source in recorded_sources:
            source_id = recorded_source.get("node_id")
            source_run = recorded_source.get("run_id") or state.get("run_id")
            local = source_run == state.get("run_id")
            receipts = recorded_source.get("artifacts", [])
            input_candidates = receipts if "artifacts" in recorded_source else list(artifact_candidates(request.get("input")))
            used = []
            for identity, artifact in artifacts.items():
                if not local or artifact["node_id"] != source_id:
                    continue
                if recorded_source.get("execution_id") != artifact.get("execution_id"):
                    continue
                for candidate in input_candidates:
                    if matches_receipt(candidate, artifact):
                        if identity not in used:
                            used.append(identity)
                        if node_id not in artifact["used_by"]:
                            artifact["used_by"].append(node_id)
            external = []
            if not local:
                for receipt in receipts:
                    if isinstance(receipt, dict) and receipt.get("artifact_id"):
                        external.append(inspect_artifact(root, {**receipt, "run_id": source_run}))
            sources.append({"run_id": source_run, "node_id": source_id,
                            "title": _node_title(state, source_id) if local else recorded_source.get("title") or source_id,
                            "reference": recorded_source.get("reference"), "input_path": recorded_source.get("input_path"),
                            "execution_id": recorded_source.get("execution_id"), "attempt": recorded_source.get("attempt"),
                            "artifact_ids": used, "artifacts": external, "relation": "delivered"})
        recorded_references = {source.get("reference") for source in recorded_sources}
        for reference in dict.fromkeys(_references(request.get("node_spec", spec).get("input", {}))):
            parts = reference.split(".")
            if reference in recorded_references or len(parts) < 3 or parts[0] != "steps":
                continue
            sources.append({"run_id": state.get("run_id"), "node_id": parts[1], "title": _node_title(state, parts[1]),
                            "reference": reference, "execution_id": None, "artifact_ids": [], "relation": "declared"})
        referenced = {x["node_id"] for x in sources if x.get("run_id") == state.get("run_id")}
        for source_id in spec.get("depends", []):
            if source_id not in referenced:
                sources.append({"run_id": state.get("run_id"), "node_id": source_id, "title": _node_title(state, source_id), "reference": None,
                                "execution_id": None, "artifact_ids": [], "relation": "declared"})
        child_run = request.get("child_run") or (step.get("output") or {}).get("child_run") if isinstance(step.get("output"), dict) else request.get("child_run")
        nodes.append({"node_id": node_id, "title": _node_title(state, node_id), "status": step.get("status"),
                      "instruction": spec.get("instruction") or (spec.get("input", {}).get("purpose") if isinstance(spec.get("input"), dict) else None),
                      "input": request.get("input", spec.get("input")), "output": step.get("output"),
                      "progress": step.get("progress"), "error": step.get("error"), "owner": owner,
                      "attempt": step.get("attempt"), "generation": step.get("generation"), "child_run": child_run,
                      "started_at": step.get("started_at"), "completed_at": step.get("completed_at"),
                      "history": histories[node_id], "sources": sources,
                      "artifacts": [key for key, value in artifacts.items() if value["node_id"] == node_id]})
    for key, request in state.get("requests", {}).items():
        node_id = request.get("node_id")
        step = steps.get(node_id, {})
        current = (request.get("execution_id") == step.get("execution_id") and request.get("attempt") == step.get("attempt"))
        assignments.append({"node_id": node_id, "title": _node_title(state, node_id),
                            "worker_id": state.get("assignments", {}).get(key), "execution_id": request.get("execution_id"),
                            "attempt": request.get("attempt"), "status": step.get("status") if current else "superseded",
                            "child_run": request.get("child_run"),
                            "history": [h for h in histories.get(node_id, [])
                                        if h["event"] in {"claim", "handoff"}
                                        and h["execution_id"] == request.get("execution_id")
                                        and h["attempt"] == request.get("attempt")]})
        if request.get("child_run"):
            children.append({"run_id": request["child_run"], "node_id": node_id})
        # Old dispatched inputs remain meaningful after a downstream rework.
        # Keep the exact version's delivery relationship, not only the latest
        # node attempt. This records transport, never an assertion of adoption.
        supplied = list(artifact_candidates(request.get("input")))
        for source in request.get("input_sources", []):
            if source.get("run_id", state.get("run_id")) != state.get("run_id"):
                continue
            candidates = source["artifacts"] if "artifacts" in source else supplied
            for artifact in artifacts.values():
                if (artifact["node_id"] != source.get("node_id")
                        or artifact.get("execution_id") != source.get("execution_id")):
                    continue
                if any(matches_receipt(item, artifact) for item in candidates):
                    if node_id not in artifact["used_by"]:
                        artifact["used_by"].append(node_id)
                    delivery = {"node_id": node_id, "execution_id": request.get("execution_id"),
                                "attempt": request.get("attempt"), "relation": "delivered"}
                    if delivery not in artifact.setdefault("deliveries", []):
                        artifact["deliveries"].append(delivery)
    inputs = state.get("input", {})
    request = _request_text(inputs) or (inputs.get("request", inputs) if isinstance(inputs, dict) else inputs)
    return {**summarize(state), "request": request,
            "phase": state.get("variables", {}).get("phase", state.get("limits", {}).get("phase")),
            "nodes": nodes, "artifacts": list(artifacts.values()), "assignments": assignments,
            "history": state.get("history", []), "children": children}
