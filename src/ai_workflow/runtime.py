"""Short Restate state handlers. Executors pull requests; no external work under a lock."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import restate
from restate.exceptions import TerminalError
from . import engine, ledger
from .artifacts import capture_report
from .settings import project_root
from . import workspaces

hub = restate.VirtualObject("WorkflowHub")
runs = restate.VirtualObject("WorkflowRun")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@hub.handler()
async def change(ctx: restate.ObjectContext, command: dict) -> dict:
    state = await ctx.get("state") or ledger.empty()
    try:
        action, data = command["action"], command.get("data", {})
        if action == "task_settings":
            if "default_task_root" in data:
                def configure():
                    try:
                        return {"settings": workspaces.validate_root(data["default_task_root"])}
                    except (OSError, ValueError) as exc:
                        return {"error": str(exc)}
                configured = await ctx.run("configure-task-root", configure, max_attempts=1)
                if "error" in configured:
                    raise ValueError(configured["error"])
                state["task_settings"] = configured["settings"]
            result = state.get("task_settings") or workspaces.default_settings()
        elif action == "register":
            result = ledger.register(state, **data)
        elif action == "admit":
            result = ledger.admit(state, data)
        elif action == "settle":
            result = ledger.settle(state, **data)
        elif action.startswith("backlog_"):
            result = ledger.backlog(state, action[8:], data)
        else:
            raise ValueError("unknown ledger operation")
    except (ValueError, KeyError, TypeError) as exc:
        raise TerminalError(str(exc), status_code=409) from exc
    ctx.set("state", state)
    return result


@hub.handler(kind="shared")
async def read(ctx: restate.ObjectSharedContext) -> dict:
    return await ctx.get("state") or ledger.empty()


async def update_hub(ctx, action, data):
    return await ctx.object_call(change, key="local", arg={"action": action, "data": data})


async def save_run(ctx, state, since):
    """Timestamp only newly observed transitions; old records stay undated."""
    events = state.get("history", [])[since:]
    if events or since == -1:
        at = datetime.fromtimestamp(await ctx.time(), timezone.utc).isoformat()
        state["updated_at"] = at
        if since == -1:
            state["created_at"] = at
        for event in events:
            event.setdefault("at", at)
            node_id = event.get("node_id") or event.get("result", {}).get("node_id")
            step = state.get("steps", {}).get(node_id)
            if step and event["event"] == "dispatch":
                step.setdefault("started_at", at)
            if step and event["event"] == "report" and event.get("result", {}).get("accepted"):
                if step["status"] in ("completed", "failed", "cancelled"):
                    step["completed_at"] = at
    ctx.set("state", state)


@runs.handler()
async def start(ctx: restate.ObjectContext, request: dict) -> dict:
    old = await ctx.get("state")
    fingerprint = digest(request)
    if old:
        if old["start_hash"] != fingerprint:
            raise TerminalError("run identity reused with different request", status_code=409)
        return {"run_id": ctx.key(), "status": old["status"], "replayed": True, "workspace": old.get("workspace")}
    if request.get("explicit_start") is not True:
        raise TerminalError("explicit workflow activation is required", status_code=400)
    try:
        state = engine.new_run(ctx.key(), request["workflow"], request.get("input", {}),
                               limits=request.get("limits"), parent=request.get("parent"))
    except (ValueError, KeyError, TypeError) as exc:
        raise TerminalError(str(exc), status_code=400) from exc
    state.update(start_hash=fingerprint, requests={}, assignments={}, admission={},
                 input_sources=deepcopy(request.get("input_sources", [])),
                 root=request.get("root", ctx.key()), parent=request.get("parent"))
    settings = await update_hub(ctx, "task_settings", {})
    def create_workspace():
        try:
            return {"workspace": workspaces.allocate(ctx.key(), state["root"], settings,
                                                      request.get("parent_workspace"))}
        except (OSError, ValueError) as exc:
            return {"error": str(exc)}
    allocated = await ctx.run("create-task-workspace", create_workspace, max_attempts=1)
    if "error" in allocated:
        raise TerminalError("无法创建任务目录：" + allocated["error"], status_code=409)
    state["workspace"] = allocated["workspace"]
    await update_hub(ctx, "register", {"run_id": ctx.key(), "root": state["root"],
                                       "limits": state.get("limits", {})})
    await save_run(ctx, state, -1)
    ctx.object_send(tick, key=ctx.key(), arg=None)
    return {"run_id": ctx.key(), "status": state["status"], "workspace": state["workspace"]}


@runs.handler(kind="shared")
async def snapshot(ctx: restate.ObjectSharedContext) -> dict:
    state = await ctx.get("state")
    if state is None:
        raise TerminalError("unknown run", status_code=404)
    return state


def request_key(request):
    return f"{request['execution_id']}:{request['attempt']}"


async def notify_parent(ctx, state):
    parent = state.get("parent")
    if not parent or state.get("parent_notified"):
        return
    if state["status"] not in ("succeeded", "failed", "cancelled"):
        return
    if any(s["status"] in ("running", "waiting", "unknown") for s in state["steps"].values()):
        return
    outputs = {k: {"status": v["status"], "outcome": v.get("outcome"),
                   "output": v.get("output"), "error": v.get("error"),
                   "artifacts": engine.step_artifacts(state, k)} for k, v in state["steps"].items()}
    failure_outcome = parent.get("failure_outcome") if state["status"] == "failed" else None
    result_available = state["status"] == "succeeded" or failure_outcome is not None
    report = {"report_id": f"child:{ctx.key()}:final", "execution_id": parent["execution_id"],
              "attempt": parent["attempt"], "kind": "completed" if result_available else "error",
              "outcome": failure_outcome or "success", "output": {"child_run": ctx.key(), "status": state["status"], "steps": outputs}}
    if state["status"] != "succeeded":
        report["error"] = {"code": "child_failed", "status": state["status"]}
    ctx.object_send(receive, key=parent["run_id"], arg=report)
    state["parent_notified"] = True


@runs.handler()
async def tick(ctx: restate.ObjectContext) -> dict:
    state = await ctx.get("state")
    if not state:
        return {"status": "absent"}
    since = len(state["history"])
    if state["status"] in ("paused", "cancelling", "cancelled", "succeeded", "failed"):
        await notify_parent(ctx, state)
        ctx.set("state", state)
        return {"status": state["status"]}
    try:
        candidates = engine.ready(state)
        for node_id in candidates:
            trial = deepcopy(state)
            request = engine.dispatch(trial, node_id)
            key = request_key(request)
            admission = {"admission_id": key, "root": state["root"],
                         "resources": request.get("resources", []), "cost": request.get("cost", 0),
                         "occupies_slot": request["kind"] not in ("tasks", "subflow"),
                         "bounded_cost": request["node_spec"].get("bounded_cost", request.get("cost", 0) == 0)}
            granted = await update_hub(ctx, "admit", admission)
            if not granted["accepted"]:
                state["admission"][node_id] = granted
                if granted["reason"] in ("resource_busy", "concurrency_limit"):
                    ctx.object_send(tick, key=ctx.key(), arg=None, send_delay=timedelta(seconds=2))
                continue
            state = trial
            state["admission"].pop(node_id, None)
            state["requests"][key] = request
            state["assignments"][key] = None
            if request["kind"] in ("tasks", "subflow"):
                inp = request["input"]
                workflow = deepcopy(inp.get("workflow"))
                if request["kind"] == "tasks":
                    tasks = inp["tasks"]
                    if isinstance(tasks, list) and len({t["id"] for t in tasks}) != len(tasks):
                        raise ValueError("duplicate dynamic task identity")
                    nodes = tasks if isinstance(tasks, dict) else {t["id"]: t for t in tasks}
                    workflow = {"id": "task-list", "version": "1", "nodes": nodes,
                                "variables": {"phase": inp.get("phase", state["variables"].get("phase"))}}
                    if workflow["variables"]["phase"] is None:
                        workflow["variables"] = {}
                # Validate before the one-way start so a bad child cannot silently strand its parent.
                from .contracts import validate_workflow
                workflow = validate_workflow(workflow)
                child_id = "child-" + digest(key)[:24]
                state["requests"][key]["child_run"] = child_id
                ctx.object_send(start, key=child_id, arg={"explicit_start": True,
                    "workflow": workflow, "input": inp.get("input", {}),
                    "input_sources": [{**source, "input_path": source["input_path"][1:]}
                                      for source in request.get("input_sources", [])
                                      if source.get("input_path", [])[:1] == ["input"]],
                    "root": state["root"], "limits": state.get("limits", {}),
                    "parent_workspace": state.get("workspace"),
                    "parent": {"run_id": ctx.key(), "execution_id": request["execution_id"],
                               "attempt": request["attempt"],
                               "failure_outcome": "failed" if "failed" in request["node_spec"]["outcomes"] else None}})
        engine.ready(state)
    except (ValueError, KeyError, TypeError) as exc:
        state["controller_error"] = str(exc)
        if "request" in locals() and "key" in locals() and key in state["requests"]:
            error = {"code": "invalid_plan", "message": str(exc)}
            handled = request["kind"] in ("tasks", "subflow") and "failed" in request["node_spec"]["outcomes"]
            report = {"report_id": key + ":invalid-plan", "execution_id": request["execution_id"],
                "attempt": request["attempt"], "kind": "error", "error": error}
            if handled:
                report.update(kind="completed", outcome="failed", output={"child_run": None,
                    "status": "failed", "steps": {}, "error": error})
            engine.report(state, report)
            await update_hub(ctx, "settle", {"admission_id": key, "actual_cost": 0})
            if handled:
                # Continue the declared assessment route; the rejected plan is
                # already terminal and must never be submitted as child work.
                ctx.object_send(tick, key=ctx.key(), arg=None)
        else:
            state["status"] = "waiting"
    await notify_parent(ctx, state)
    await save_run(ctx, state, since)
    return {"status": state["status"], "ready": candidates if "candidates" in locals() else []}


@runs.handler()
async def claim(ctx: restate.ObjectContext, request: dict) -> dict:
    state = await ctx.get("state")
    if not state:
        raise TerminalError("unknown run", status_code=404)
    since = len(state["history"])
    worker = request.get("worker_id")
    if not worker:
        raise TerminalError("worker identity required", status_code=400)
    jobs = []
    for key, job in state["requests"].items():
        step = state["steps"][job["node_id"]]
        current = step.get("execution_id") == job["execution_id"] and step.get("attempt") == job["attempt"]
        if not current or step["status"] not in ("running", "waiting", "unknown"):
            continue
        if job["kind"] not in request.get("kinds", ["agent"]):
            continue
        assigned = state["assignments"].get(key)
        if assigned not in (None, worker):
            continue
        if state["status"] in ("paused", "cancelling", "cancelled", "failed") and assigned is None:
            continue
        state["assignments"][key] = worker
        if assigned != worker:
            state["revision"] += 1
            state["history"].append({"event": "claim", "node_id": job["node_id"],
                "worker_id": worker, "execution_id": job["execution_id"], "attempt": job["attempt"],
                "revision": state["revision"]})
        jobs.append({**job, "step_status": step["status"], "reply": step.get("reply"),
                     "run_status": state["status"], "worker_id": worker, "workspace": state.get("workspace")})
    await save_run(ctx, state, since)
    return {"run_id": ctx.key(), "status": state["status"], "workspace": state.get("workspace"), "jobs": jobs}


@runs.handler()
async def receive(ctx: restate.ObjectContext, report: dict) -> dict:
    state = await ctx.get("state")
    if not state:
        raise TerminalError("unknown run", status_code=404)
    since = len(state["history"])
    try:
        original_hash = digest(report)
        report_id = report.get("report_id")
        previous = state.get("report_sources", {}).get(report_id)
        if previous:
            if previous != original_hash:
                raise ValueError("report_id is already associated with different content")
            return state["reports"][report_id]["result"]
        if report_id in state["reports"]:
            return engine.report(state, report)
        # Reject stale/invalid reports before reading files or capturing content.
        trial = engine.report(deepcopy(state), report)
        if trial.get("accepted") and report.get("kind") == "completed":
            node = {**state["workflow"]["nodes"][trial["node_id"]], "node_id": trial["node_id"]}
            def capture():
                try:
                    return {"report": capture_report(project_root(), ctx.key(), report, node, state.get("workspace"))}
                except (OSError, ValueError) as exc:
                    return {"error": str(exc)}
            captured = await ctx.run("capture-artifacts", capture, max_attempts=1)
            if "error" in captured:
                raise ValueError(captured["error"])
            report = captured["report"]
        result = engine.report(state, report)
        state.setdefault("report_sources", {})[report_id] = original_hash
        key = f"{report['execution_id']}:{report.get('attempt', 1)}"
        if result.get("accepted") and report["kind"] in ("completed", "error"):
            unknown = report.get("unknown", False) or any(
                x.get("execution_id") == report["execution_id"] and x["status"] == "unknown"
                for x in state["steps"].values())
            await update_hub(ctx, "settle", {"admission_id": key,
                "actual_cost": report.get("actual_cost"), "unknown": unknown})
        for item in report.get("discoveries", []):
            await update_hub(ctx, "backlog_add", {**item, "run_id": ctx.key()})
    except (ValueError, KeyError, TypeError) as exc:
        raise TerminalError(str(exc), status_code=409) from exc
    await save_run(ctx, state, since)
    ctx.object_send(tick, key=ctx.key(), arg=None)
    return result


@runs.handler()
async def control(ctx: restate.ObjectContext, command: dict) -> dict:
    state = await ctx.get("state")
    if not state:
        raise TerminalError("unknown run", status_code=404)
    since = len(state["history"])
    action, data = command["action"], command.get("data", {})
    try:
        if action == "handoff":
            if data.get("expected_revision") != state["revision"] or data.get("old_stopped") is not True:
                raise ValueError("handoff requires current revision and verified stopped executor")
            if not data.get("old_worker") or not data.get("new_worker"):
                raise ValueError("handoff requires old and new worker identities")
            state["revision"] += 1
            for key, owner in state["assignments"].items():
                job = state["requests"].get(key, {})
                step = state["steps"].get(job.get("node_id"), {})
                current = (step.get("execution_id") == job.get("execution_id")
                           and step.get("attempt") == job.get("attempt"))
                if owner == data["old_worker"] and current and step.get("status") in ("running", "waiting", "unknown"):
                    state["assignments"][key] = data["new_worker"]
                    state["history"].append({"event": "handoff", "revision": state["revision"],
                        "node_id": job["node_id"], "execution_id": job["execution_id"], "attempt": job["attempt"],
                        "old_worker": data["old_worker"], "new_worker": data["new_worker"]})
            result = {"accepted": True, "revision": state["revision"]}
        else:
            result = engine.control(state, action, data)
        if action == "cancel":
            for key, job in state["requests"].items():
                step = state["steps"][job["node_id"]]
                if (not job.get("child_run") and state["assignments"].get(key) is None
                        and step.get("execution_id") == job["execution_id"]
                        and step["status"] in ("running", "waiting")):
                    engine.report(state, {"report_id": key + ":cancel-before-claim",
                        "execution_id": job["execution_id"], "attempt": job["attempt"],
                        "kind": "error", "error": {"code": "cancelled", "detail": "not claimed"}})
                    await update_hub(ctx, "settle", {"admission_id": key, "actual_cost": 0})
        if action in ("pause", "resume", "cancel"):
            for job in state["requests"].values():
                if job.get("child_run"):
                    ctx.object_send(parent_control, key=job["child_run"],
                                    arg={"action": action, "parent_run": ctx.key()})
    except (ValueError, KeyError, TypeError) as exc:
        raise TerminalError(str(exc), status_code=409) from exc
    await save_run(ctx, state, since)
    if action in ("resume", "reply", "revise", "handoff"):
        ctx.object_send(tick, key=ctx.key(), arg=None)
    return result


@runs.handler()
async def parent_control(ctx: restate.ObjectContext, command: dict) -> dict:
    state = await ctx.get("state")
    if not state or (state.get("parent") or {}).get("run_id") != command["parent_run"]:
        raise TerminalError("parent association mismatch", status_code=409)
    since = len(state["history"])
    try:
        result = engine.control(state, command["action"], {"expected_revision": state["revision"]})
    except (ValueError, KeyError, TypeError) as exc:
        raise TerminalError(str(exc), status_code=409) from exc
    for job in state["requests"].values():
        if job.get("child_run"):
            ctx.object_send(parent_control, key=job["child_run"],
                            arg={"action": command["action"], "parent_run": ctx.key()})
    if command["action"] == "cancel":
        for key, job in state["requests"].items():
            step = state["steps"][job["node_id"]]
            if (not job.get("child_run") and state["assignments"].get(key) is None
                    and step.get("execution_id") == job["execution_id"]
                    and step["status"] in ("running", "waiting")):
                engine.report(state, {"report_id": key + ":parent-cancel-before-claim",
                    "execution_id": job["execution_id"], "attempt": job["attempt"],
                    "kind": "error", "error": {"code": "cancelled", "detail": "not claimed"}})
                await update_hub(ctx, "settle", {"admission_id": key, "actual_cost": 0})
    await notify_parent(ctx, state)
    await save_run(ctx, state, since)
    if command["action"] == "resume":
        ctx.object_send(tick, key=ctx.key(), arg=None)
    return result


app = restate.app([hub, runs])
