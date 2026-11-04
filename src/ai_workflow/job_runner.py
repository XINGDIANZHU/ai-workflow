"""Detached, one-shot executor. Never resubmit a registered external operation."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, build_opener, ProxyHandler
import uuid


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Python readers on Windows briefly deny delete-sharing. Retrying this
        # rename repeats only the same durable bytes, never the external action.
        for attempt in range(30):
            try:
                os.replace(tmp, path)
                break
            except OSError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 29:
                    raise
                time.sleep(0.01)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def read_json(path: Path) -> dict:
    for attempt in range(30):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            # A concurrent replace can also briefly deny the Windows reader.
            if os.name != "nt" or attempt == 29:
                raise
            time.sleep(0.01)


def process_identity(pid: int) -> str | None:
    """A PID plus creation time is a query identity, never a kill instruction."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return None
            created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
            return f"{pid}:{created}"
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return f"{pid}:{stat[19]}"
    except OSError:
        return None


def save_job(folder: Path, job: dict, **updates) -> dict:
    job.update(updates)
    job["updated_at"] = time.time()
    atomic_json(folder / "job.json", job)
    return job


def execute_program(folder: Path, record: dict, job: dict):
    request, inputs = record["request"], record["request"]["input"]
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if (folder / "cancel.json").exists():
        return save_job(folder, job, status="cancelled", error=None)
    # Even a crash between this marker and Popen must not cause a blind rerun.
    atomic_json(folder / "side_effect.json", {"kind": "program", "started_at": time.time()})
    with (folder / "stdin.json").open("wb") as stream:
        stream.write(json.dumps(inputs.get("stdin"), ensure_ascii=False).encode("utf-8"))
    with (folder / "stdin.json").open("rb") as stdin, (folder / "stdout.txt").open("wb") as stdout, (folder / "stderr.txt").open("wb") as stderr:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            proc = subprocess.Popen(inputs["argv"], cwd=inputs["cwd"], stdin=stdin,
                                    stdout=stdout, stderr=stderr, env=env, shell=False, **kwargs)
        except OSError as exc:
            return save_job(folder, job, status="failed", error=f"Program could not start: {exc}")
        save_job(folder, job, status="running", pid=proc.pid, process_identity=process_identity(proc.pid))
        deadline = time.monotonic() + request["timeout_seconds"]
        forced = None
        while proc.poll() is None:
            if (folder / "cancel.json").exists():
                forced = "cancelled"
            elif time.monotonic() >= deadline:
                forced = "timeout"
            if forced:
                # Popen retains the original process handle on Windows. Never kill by a saved PID.
                proc.kill()
                proc.wait()
                break
            time.sleep(0.05)
    out = (folder / "stdout.txt").read_text(encoding="utf-8", errors="replace")
    err = (folder / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    output, outcome = {"stdout": out, "stderr": err, "exit_code": proc.returncode}, None
    if forced:
        return save_job(folder, job, status="cancelled" if forced == "cancelled" else "failed",
                        output=output, error=None if forced == "cancelled" else "Program timeout; direct child stopped")
    if proc.returncode != 0:
        return save_job(folder, job, status="failed", output=output,
                        error=f"Program exited with code {proc.returncode}")
    try:
        envelope = json.loads(out)
        if isinstance(envelope, dict) and isinstance(envelope.get("outcome"), str) and isinstance(envelope.get("output"), dict):
            output, outcome = envelope["output"], envelope["outcome"]
    except ValueError:
        pass
    return save_job(folder, job, status="completed", output=output, outcome=outcome,
                    exit_code=proc.returncode, error=None)


def http(url: str, path: str, payload=None, *, raw=False, timeout=10):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url.rstrip("/") + path, data=data,
                      headers={"Content-Type": "application/json"} if data else {})
    # Local execution endpoints should not inherit unrelated proxy settings.
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        body = response.read()
        return body if raw else json.loads(body)


def find_prompt(url: str, record: dict) -> str | None:
    queue = http(url, "/queue")
    history = http(url, "/history")
    entries = list(queue.get("queue_running", [])) + list(queue.get("queue_pending", []))
    entries += [item.get("prompt", []) for item in history.values()]
    matches = set()
    for item in entries:
        if len(item) > 3 and isinstance(item[3], dict):
            extra = item[3]
            if extra.get("ai_workflow_operation_id") == record["operation_id"] and extra.get("ai_workflow_request_hash") == record["request_hash"]:
                matches.add(str(item[1]))
    if len(matches) > 1:
        raise ValueError("Multiple external prompts share this operation identity; manual reconciliation required")
    return next(iter(matches), None)


def collect_outputs(record: dict, prompt_id: str, history: dict) -> dict:
    inputs = record["request"]["input"]
    destination = Path(inputs["output_dir"]) / hashlib.sha256(record["operation_id"].encode()).hexdigest()[:20]
    artifacts = []
    for node_id, node in history.get("outputs", {}).items():
        for kind in ("images", "gifs", "audio", "videos"):
            for index, item in enumerate(node.get(kind, [])):
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                params = {key: item[key] for key in ("filename", "subfolder", "type") if key in item}
                data = http(inputs["url"], "/view?" + urlencode(params), raw=True, timeout=30)
                filename = str(item["filename"]).replace("\\", "/").rsplit("/", 1)[-1]
                safe_node = hashlib.sha256(str(node_id).encode()).hexdigest()[:8]
                path = destination / f"{safe_node}_{kind}_{index}_{filename}"
                atomic_bytes(path, data)
                artifacts.append({"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
                                  "size_bytes": len(data), **params})
    return {"prompt_id": prompt_id, "artifacts": artifacts}


def reconcile_comfy(folder: Path, record: dict, job: dict) -> dict:
    """Reconcile by identity, including acceptance before prompt_id was saved."""
    url = record["request"]["input"]["url"]
    prompt_id = job.get("prompt_id") or find_prompt(url, record)
    if not prompt_id:
        return save_job(folder, job, status="unknown", error="No matching prompt found in ComfyUI queue/history; not resubmitted")
    job["prompt_id"] = prompt_id
    history = http(url, "/history/" + quote(prompt_id, safe="")).get(prompt_id)
    if history:
        status = history.get("status", {})
        messages = status.get("messages", [])
        if status.get("status_str") == "error" or any(m[0] in {"execution_error", "execution_interrupted"} for m in messages if m):
            return save_job(folder, job, status="failed", error="ComfyUI execution failed", external_status=status)
        if status.get("completed") is True and status.get("status_str") == "success":
            output = collect_outputs(record, prompt_id, history)
            return save_job(folder, job, status="completed", output=output, outcome="generated", error=None)
    queue = http(url, "/queue")
    for key, state in (("queue_running", "running"), ("queue_pending", "queued")):
        if any(len(item) > 1 and str(item[1]) == prompt_id for item in queue.get(key, [])):
            return save_job(folder, job, status=state, error=None)
    return save_job(folder, job, status="unknown", error="Prompt not terminal in history and absent from queue; not resubmitted")


def execute_comfy(folder: Path, record: dict, job: dict):
    request, inputs = record["request"], record["request"]["input"]
    url = inputs["url"]
    # These calls precede the side-effect marker; failure here is known unsubmitted.
    http(url, "/system_stats")
    queue = http(url, "/queue")
    save_job(folder, job, queue_at_submit={k: len(queue.get(k, [])) for k in ("queue_running", "queue_pending")})
    schemas = {}
    for name in {node["class_type"] for node in inputs["workflow"].values()}:
        schema = http(url, "/object_info/" + quote(name, safe=""))
        if name not in schema:
            raise ValueError(f"ComfyUI node is not installed: {name}")
        schemas[name] = schema[name]
    for node_id, node in inputs["workflow"].items():
        required = schemas[node["class_type"]].get("input", {}).get("required", {})
        for key, description in required.items():
            if key not in node["inputs"]:
                raise ValueError(f"ComfyUI node {node_id} is missing required input {key}")
            value = node["inputs"][key]
            linked = isinstance(value, list) and len(value) == 2 and isinstance(value[1], int) and str(value[0]) in inputs["workflow"]
            if not linked and description and isinstance(description[0], list) and value not in description[0]:
                raise ValueError(f"ComfyUI node {node_id} input {key} is not an installed choice")
    atomic_json(folder / "side_effect.json", {"kind": "comfyui", "started_at": time.time(),
                "operation_id": record["operation_id"], "request_hash": record["request_hash"]})
    try:
        response = http(url, "/prompt", {"prompt": inputs["workflow"],
                    "client_id": "ai-workflow-" + record["request_hash"][:20],
                    "extra_data": {"ai_workflow_operation_id": record["operation_id"],
                                   "ai_workflow_request_hash": record["request_hash"]}})
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            return save_job(folder, job, status="unknown", error="Submission response has no prompt_id; query original identity")
        save_job(folder, job, status="queued", prompt_id=str(prompt_id))
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            return save_job(folder, job, status="failed", error=f"ComfyUI rejected submission: HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        return save_job(folder, job, status="unknown", error=f"ComfyUI submission uncertain: {exc}")
    except Exception as exc:
        return save_job(folder, job, status="unknown", error=f"ComfyUI submission uncertain: {exc}")
    deadline = time.monotonic() + request["timeout_seconds"]
    while time.monotonic() < deadline:
        try:
            job = reconcile_comfy(folder, record, job)
        except Exception as exc:
            save_job(folder, job, status="unknown", error=f"ComfyUI query failed: {exc}")
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.25)
    return save_job(folder, job, status="unknown", error="Observation timeout; external job may still run; GET reconciles without resubmission")


def main():
    folder = Path(sys.argv[1]).resolve()
    try:
        # Exclusive process admission survives bridge and runner crashes.
        with (folder / "runner.claim").open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return
    record, job = read_json(folder / "request.json"), read_json(folder / "job.json")
    save_job(folder, job, runner_pid=os.getpid(), runner_identity=process_identity(os.getpid()), status="running")
    try:
        if record["request"]["kind"] == "program":
            execute_program(folder, record, job)
        else:
            execute_comfy(folder, record, job)
    except Exception as exc:
        save_job(folder, job, status="unknown" if (folder / "side_effect.json").exists() else "failed", error=f"Executor error: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
