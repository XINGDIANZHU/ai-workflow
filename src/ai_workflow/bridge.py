"""Local execution bridge. Receipts belong to executors, never to scheduling.

This endpoint executes arbitrary programs and must only be reachable by the local
trusted runtime. The caller owns authorization, paths, budgets and resource locks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .job_runner import atomic_json, process_identity, read_json, reconcile_comfy


TERMINAL = {"completed", "failed", "cancelled"}


class ConflictError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def capabilities(kind: str) -> dict:
    return {"submit": True, "query": True, "background": True,
            "resume": "query_original_only", "cancel": kind == "program",
            "cancel_scope": "direct_child_only" if kind == "program" else None,
            "automatic_resubmit": False}


def validate_request(request: dict) -> dict:
    if not isinstance(request, dict) or request.get("kind") not in {"program", "comfyui"}:
        raise ValueError("kind must be program or comfyui")
    if set(request) - {"kind", "input", "timeout_seconds"}:
        raise ValueError("unknown request fields")
    data = request.get("input")
    if not isinstance(data, dict):
        raise ValueError("input must be an object")
    timeout = request.get("timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    if request["kind"] == "program":
        argv = data.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\0" in x for x in argv):
            raise ValueError("input.argv must be a nonempty list of nonempty strings")
        if not isinstance(data.get("cwd"), str) or not Path(data["cwd"]).is_absolute():
            raise ValueError("input.cwd must be an absolute path")
    else:
        url = urlsplit(data.get("url", ""))
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise ValueError("input.url must be an HTTP(S) endpoint without credentials")
        graph = data.get("workflow")
        if not isinstance(graph, dict) or not graph or any(
            not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), dict) for node in graph.values()
        ):
            raise ValueError("input.workflow must be an API-format ComfyUI graph")
        if not isinstance(data.get("output_dir"), str) or not Path(data["output_dir"]).is_absolute():
            raise ValueError("input.output_dir must be an absolute path")
    normalized = {"kind": request["kind"], "input": data, "timeout_seconds": float(timeout)}
    canonical(normalized)
    return normalized


class JobStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def directory(self, operation_id: str) -> Path:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", operation_id):
            raise ValueError("operation_id must be 1-200 ASCII letters, numbers, _, ., : or -")
        return self.data_dir / hashlib.sha256(operation_id.encode()).hexdigest()

    def submit(self, operation_id: str, request: dict) -> dict:
        request = validate_request(request)
        folder = self.directory(operation_id)
        fingerprint = hashlib.sha256(canonical(request)).hexdigest()
        if not folder.exists() and request["kind"] == "program" and not Path(request["input"]["cwd"]).is_dir():
            raise ValueError("input.cwd does not exist")
        try:
            folder.mkdir()
        except FileExistsError:
            try:
                existing = read_json(folder / "request.json")
            except (FileNotFoundError, ValueError):
                # A crash while recording admission is not permission to repeat it.
                return self._unknown(operation_id, "Admission receipt is incomplete; manual reconciliation required")
            if existing["request_hash"] != fingerprint:
                raise ConflictError("operation_id already belongs to different content")
            return self.get(operation_id)
        record = {"operation_id": operation_id, "request": request,
                  "request_hash": fingerprint, "created_at": time.time()}
        atomic_json(folder / "request.json", record)
        atomic_json(folder / "job.json", {"operation_id": operation_id, "status": "queued",
                    "output": {}, "error": None, "created_at": record["created_at"],
                    "capabilities": capabilities(request["kind"])})
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            with (folder / "runner.log").open("ab") as log:
                runner = subprocess.Popen([sys.executable, "-m", "ai_workflow.job_runner", str(folder)],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 env=env, close_fds=True, **kwargs)
                # Reap the process when this host survives; a host exit leaves it independent.
                def reap():
                    code = runner.wait()
                    try:
                        current = read_json(folder / "job.json")
                        if current["status"] not in TERMINAL:
                            atomic_json(folder / "runner_exit.json", {"exit_code": code, "finished_at": time.time()})
                    except (OSError, ValueError):
                        pass  # Receipts may have been archived after the terminal result.
                threading.Thread(target=reap, daemon=True).start()
        except OSError as exc:
            job = read_json(folder / "job.json")
            job.update(status="failed", error=f"Runner could not start: {exc}")
            atomic_json(folder / "job.json", job)
        return self.get(operation_id)

    @staticmethod
    def _unknown(operation_id: str, error: str) -> dict:
        return {"operation_id": operation_id, "status": "unknown", "output": {},
                "error": error, "capabilities": {"submit": False, "query": True, "cancel": False}}

    def get(self, operation_id: str) -> dict:
        folder = self.directory(operation_id)
        if not folder.exists():
            raise FileNotFoundError(operation_id)
        try:
            record, job = read_json(folder / "request.json"), read_json(folder / "job.json")
        except (FileNotFoundError, ValueError):
            return self._unknown(operation_id, "Incomplete executor receipt; no automatic resubmission")
        if job["status"] in TERMINAL:
            return job
        identity = job.get("runner_identity")
        alive = identity is not None and process_identity(job["runner_pid"]) == identity
        if alive:
            return job
        # The admission call may return before the detached runner records its identity.
        if not identity and time.time() - record["created_at"] < 3:
            return job
        if record["request"]["kind"] == "comfyui":
            try:
                return reconcile_comfy(folder, record, job)
            except Exception as exc:
                job.update(status="unknown", error=f"ComfyUI reconciliation unavailable: {exc}")
                return job
        job.update(status="unknown", error=job.get("error") or "Runner is absent or its process identity cannot be confirmed; execution is not repeated")
        if (folder / "runner_exit.json").exists():
            job["runner_exit"] = read_json(folder / "runner_exit.json")
        return job

    def cancel(self, operation_id: str) -> dict:
        job = self.get(operation_id)
        if job["status"] in TERMINAL:
            return job
        if not job["capabilities"].get("cancel"):
            raise NotImplementedError("This job cannot be safely cancelled by this adapter")
        # Only the live runner acts on this flag using its original Popen handle.
        atomic_json(self.directory(operation_id) / "cancel.json", {"requested_at": time.time()})
        job["cancel_requested"] = True
        return job


def make_server(host: str, port: int, data_dir: str | Path) -> ThreadingHTTPServer:
    store = JobStore(data_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, code: int, body: dict):
            data = canonical(body)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def dispatch(self):
            path = unquote(urlsplit(self.path).path)
            if self.command == "GET" and path == "/health":
                return self.respond(200, {"status": "ok", "protocol_version": 1,
                    "capabilities": {kind: capabilities(kind) for kind in ("program", "comfyui")}})
            match = re.fullmatch(r"/jobs/([^/]+)(/cancel)?", path)
            if not match:
                return self.respond(404, {"error": "Unknown endpoint"})
            operation_id, cancel = match.groups()
            if self.command == "GET" and not cancel:
                return self.respond(200, store.get(operation_id))
            if self.command == "POST" and cancel:
                return self.respond(202, store.cancel(operation_id))
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 8 * 1024 * 1024:
                    raise ValueError("Request body must be between 1 byte and 8 MiB")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                existed = store.directory(operation_id).exists()
                job = store.submit(operation_id, request)
                return self.respond(200 if existed else 202, job)
            return self.respond(405, {"error": "Method not allowed"})

        def handle_request(self):
            try:
                self.dispatch()
            except ConflictError as exc:
                self.respond(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})
            except FileNotFoundError as exc:
                self.respond(404, {"error": str(exc)})
            except NotImplementedError as exc:
                self.respond(501, {"error": str(exc)})
            except Exception as exc:
                self.respond(500, {"error": f"Executor receipt or query error: {exc}"})

        do_GET = handle_request
        do_POST = handle_request

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19090)
    from .settings import project_root
    parser.add_argument("--data-dir", default=str(project_root() / ".local/jobs"))
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.data_dir)
    print(json.dumps({"host": args.host, "port": server.server_port,
                      "data_dir": str(Path(args.data_dir).resolve())}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
