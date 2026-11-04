"""Executor integration checks; ComfyUI simulation is not live-adapter acceptance."""

import base64
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_workflow.bridge import ConflictError, JobStore, canonical, capabilities, validate_request
from ai_workflow.job_runner import atomic_json, read_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "program_job.py"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-test-", dir=ROOT / ".local")
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root / "jobs")

    def tearDown(self):
        self.temp.cleanup()

    def request(self, name="中文.txt", **options):
        return {"kind": "program", "input": {"argv": [sys.executable, str(FIXTURE)],
                "cwd": str(self.root), "stdin": {"output_path": name, "text": "真实中文内容", **options}},
                "timeout_seconds": 5}

    def wait(self, operation, states=("completed", "failed", "cancelled"), timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.get(operation)
            if job["status"] in states:
                return job
            time.sleep(0.05)
        self.fail(f"Job did not reach {states}: {job}")

    def test_real_artifact_duplicate_and_business_outcome(self):
        request = self.request(outcome="needs_revision")
        self.store.submit("text-1", request)
        job = self.wait("text-1")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["outcome"], "needs_revision")
        self.assertEqual(Path(job["output"]["path"]).read_text(encoding="utf-8"), "真实中文内容")
        self.assertEqual(job["output"]["sha256"], hashlib.sha256("真实中文内容".encode()).hexdigest())
        duplicate = self.store.submit("text-1", request)
        self.assertEqual(duplicate["pid"], job["pid"])
        self.assertEqual(duplicate["output"], job["output"])
        with self.assertRaises(ConflictError):
            self.store.submit("text-1", self.request(text="different"))
        self.store.submit("failure", self.request("never.txt", fail=True))
        failed = self.wait("failure")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["output"]["exit_code"], 7)
        self.assertFalse((self.root / "never.txt").exists())

    def test_cancel_and_timeout_stop_owned_direct_child(self):
        self.store.submit("cancel", self.request("cancelled.txt", delay_seconds=4))
        deadline = time.monotonic() + 5
        while not self.store.get("cancel").get("pid") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.store.cancel("cancel")
        self.assertEqual(self.wait("cancel")["status"], "cancelled")
        request = self.request("timeout.txt", delay_seconds=4)
        request["timeout_seconds"] = 0.15
        self.store.submit("timeout", request)
        self.assertEqual(self.wait("timeout")["status"], "failed")
        self.assertFalse((self.root / "cancelled.txt").exists())
        self.assertFalse((self.root / "timeout.txt").exists())

    def test_missing_runner_identity_never_restarts_program(self):
        request = validate_request(self.request())
        folder = self.store.directory("orphan")
        folder.mkdir()
        atomic_json(folder / "request.json", {"operation_id": "orphan", "request": request,
                    "request_hash": hashlib.sha256(canonical(request)).hexdigest(), "created_at": 0})
        atomic_json(folder / "job.json", {"operation_id": "orphan", "status": "running", "output": {},
                    "error": None, "runner_pid": os.getpid(), "runner_identity": "wrong-creation-time",
                    "capabilities": capabilities("program")})
        atomic_json(folder / "side_effect.json", {"started_at": 0})
        self.assertEqual(self.store.get("orphan")["status"], "unknown")
        self.assertEqual(self.store.submit("orphan", self.request())["status"], "unknown")
        self.assertFalse((folder / "runner.claim").exists())
        self.assertFalse((self.root / "中文.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows file sharing regression")
    def test_windows_receipt_read_and_replace_tolerate_short_sharing_lock(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        path = self.root / "receipt.json"
        atomic_json(path, {"version": 1})

        def briefly_lock(share):
            handle = kernel.CreateFileW(str(path), 0x80000000, share, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            timer = threading.Timer(0.08, lambda: kernel.CloseHandle(handle))
            timer.start()
            return timer

        timer = briefly_lock(3)  # Readers allowed, delete/replace temporarily denied.
        atomic_json(path, {"version": 2})
        timer.join()
        timer = briefly_lock(0)  # Reproduce the observed transient reader denial.
        self.assertEqual(read_json(path), {"version": 2})
        timer.join()

    def test_bridge_process_restart_preserves_real_runner(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUTF8"] = "1"

        def start():
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            process = subprocess.Popen([sys.executable, "-m", "ai_workflow.bridge", "--port", "0",
                "--data-dir", str(self.root / "jobs")], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", **kwargs)
            details = json.loads(process.stdout.readline())
            return process, f"http://127.0.0.1:{details['port']}"

        def call(url, body=None):
            data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            with urlopen(Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=5) as response:
                return json.load(response)

        process, url = start()
        try:
            request = self.request(delay_seconds=1.5)
            call(url + "/jobs/restart", request)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                running = call(url + "/jobs/restart")
                if running.get("pid"):
                    break
                time.sleep(0.05)
            self.assertIn("pid", running)
            process.kill()
            process.communicate(timeout=5)
            process, url = start()
            job = self.wait("restart")
            restored = call(url + "/jobs/restart", request)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(restored["pid"], running["pid"])
            self.assertEqual(restored["runner_pid"], running["runner_pid"])
            self.assertEqual(Path(restored["output"]["path"]).read_text(encoding="utf-8"), "真实中文内容")
            with self.assertRaises(HTTPError) as exc:
                call(url + "/jobs/restart", self.request(text="different"))
            self.assertEqual(exc.exception.code, 409)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_comfy_lost_ack_reconciles_without_second_submission(self):
        history, submissions = {}, []
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1kAAAAASUVORK5CYII=")

        class FakeComfy(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, value, raw=False):
                data = value if raw else json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/system_stats":
                    self.reply({"system": {}})
                elif self.path.startswith("/object_info/"):
                    self.reply({"SaveImage": {"input": {}}})
                elif self.path == "/queue":
                    self.reply({"queue_running": [], "queue_pending": []})
                elif self.path == "/history":
                    self.reply(history)
                elif self.path.startswith("/history/"):
                    pid = self.path.rsplit("/", 1)[-1]
                    self.reply({pid: history[pid]} if pid in history else {})
                elif self.path.startswith("/view?"):
                    self.reply(png, raw=True)
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                submissions.append(body)
                prompt_id = "accepted-prompt"
                history[prompt_id] = {"prompt": [0, prompt_id, body["prompt"], body["extra_data"], ["1"]],
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {"1": {"images": [{"filename": "pixel.png", "subfolder": "", "type": "output"}]}}}
                self.reply({})  # Acceptance occurred, but the acknowledgement lost its ID.

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = {"kind": "comfyui", "input": {"url": f"http://127.0.0.1:{server.server_port}",
                "workflow": {"1": {"class_type": "SaveImage", "inputs": {}}},
                "output_dir": str(self.root / "artifacts")}, "timeout_seconds": 3}
            self.store.submit("comfy-ack", request)
            job = self.wait("comfy-ack")
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["prompt_id"], "accepted-prompt")
            self.assertEqual(Path(job["output"]["artifacts"][0]["path"]).read_bytes(), png)
            self.assertEqual(self.store.submit("comfy-ack", request)["status"], "completed")
            self.assertEqual(len(submissions), 1)
            # If the external history disappears, a new orphan receipt remains unknown.
            history.clear()
            folder = self.store.directory("comfy-unknown")
            folder.mkdir()
            normalized = validate_request(request)
            atomic_json(folder / "request.json", {"operation_id": "comfy-unknown", "request": normalized,
                "request_hash": hashlib.sha256(canonical(normalized)).hexdigest(), "created_at": 0})
            atomic_json(folder / "job.json", {"operation_id": "comfy-unknown", "status": "unknown",
                "output": {}, "error": "lost acknowledgement", "capabilities": capabilities("comfyui")})
            self.assertEqual(self.store.submit("comfy-unknown", request)["status"], "unknown")
            self.assertEqual(len(submissions), 1)
            with self.assertRaises(NotImplementedError):
                self.store.cancel("comfy-unknown")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
