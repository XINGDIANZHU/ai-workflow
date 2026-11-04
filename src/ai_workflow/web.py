"""Loopback-only workbench: files are definitions, Restate remains the authority."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from urllib.parse import quote, unquote, urlsplit

import httpx

from .client import Client, readable
from .contracts import canonical, validate_workflow
from .loader import load_workflow
from .inspection import inspect_run, summarize
from .artifacts import read_artifact
from .settings import project_root


MAX_BODY = 1024 * 1024
DEFAULT_ROOT = project_root()
STATIC_ROOT = Path(__file__).resolve().parent / "web_static"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_CONTROLS = {"pause", "resume", "cancel", "reply", "message", "revise", "wait", "handoff"}
_BACKLOG = {"add", "append", "reopen", "claim", "release", "close"}


class WebAPIError(Exception):
    def __init__(self, status, message, detail=None):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message}
        if detail is not None:
            self.payload["detail"] = detail


def _object(value, label):
    if not isinstance(value, dict):
        raise WebAPIError(400, f"{label} 必须是 JSON 对象")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise WebAPIError(400, f"{label} 必须是非空文本，且不能包含控制字符")
    return value


def _backend_error(exc):
    if isinstance(exc, WebAPIError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:8192]
        message = detail.get("message") or detail.get("error") if isinstance(detail, dict) else detail
        return WebAPIError(response.status_code, f"后端拒绝请求：{message or response.status_code}", detail)
    if isinstance(exc, httpx.RequestError):
        return WebAPIError(503, "无法连接工作流后端，请检查后端是否运行；本次操作结果未确认", str(exc))
    return WebAPIError(502, "工作流后端返回了无法读取的数据", str(exc))


class Workbench:
    def __init__(self, root=DEFAULT_ROOT, client=None, *, overview_workers=4):
        self.root = Path(root).resolve()
        self.client = client if client is not None else Client()
        self._save_lock = threading.Lock()
        # Shared across requests: simultaneous browser polls cannot multiply fan-out.
        self._reads = ThreadPoolExecutor(max_workers=overview_workers, thread_name_prefix="workbench-read")

    def close(self):
        self._reads.shutdown(wait=True, cancel_futures=True)

    def _path(self, *parts):
        path = self.root.joinpath(*parts)
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise WebAPIError(400, "定义路径不能使用符号链接或跳出项目目录")
        return path

    def _call(self, name, *args):
        try:
            value = getattr(self.client, name)(*args)
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            return value
        except (httpx.HTTPError, ValueError) as exc:
            raise _backend_error(exc) from exc

    def expand(self, workflow):
        _object(workflow, "workflow")
        nodes = _object(workflow.get("nodes"), "workflow.nodes")
        node_root = self._path("definitions", "nodes")
        for node in nodes.values():
            _object(node, "节点")
            reference = node.get("node")
            if reference is not None:
                if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference) or ".." in reference:
                    raise WebAPIError(400, "节点引用必须是已登记的名称@版本")
                self._path("definitions", "nodes", reference + ".json")
        # Keep the existing loader as the single definition-expansion contract.
        # The temporary input is never a run or a second persistent task store.
        with tempfile.TemporaryDirectory(prefix="ai-workflow-validate-") as directory:
            path = Path(directory) / "workflow.json"
            path.write_text(canonical(workflow), encoding="utf-8")
            try:
                return validate_workflow(load_workflow(path, node_root))
            except FileNotFoundError as exc:
                raise WebAPIError(400, "引用的节点定义不存在", Path(exc.filename).name) from exc
            except (ValueError, KeyError, TypeError, RecursionError) as exc:
                raise WebAPIError(400, "流程定义无效", str(exc)) from exc

    def catalog(self):
        workflows, nodes = [], []
        for category, folder in (("builtin", "workflows"), ("custom", "custom")):
            directory = self._path("definitions", folder)
            for path in sorted(directory.glob("*.json")):
                self._path("definitions", folder, path.name)
                raw = path.read_bytes()
                workflow = _object(json.loads(raw), "流程文件")
                item = {"key": f"{category}/{path.stem}", "title": workflow.get("title") or workflow.get("id") or path.stem,
                        "description": workflow.get("description", ""), "editable": category == "custom",
                        "workflow": workflow}
                if category == "custom":
                    item["hash"] = sha256(raw).hexdigest()
                workflows.append(item)
        for path in sorted(self._path("definitions", "nodes").glob("*.json")):
            self._path("definitions", "nodes", path.name)
            nodes.append({"key": path.stem, "definition": json.loads(path.read_bytes())})
        return {"workflows": workflows, "nodes": nodes}

    def save(self, data):
        key = data.get("key")
        if not isinstance(key, str) or not key.startswith("custom/"):
            raise WebAPIError(400, "只能另存到 custom/名称；内置模板只读")
        name = key[len("custom/"):]
        if not _NAME.fullmatch(name) or name.upper() in _RESERVED:
            raise WebAPIError(400, "文件名须为 1–80 位英文字母、数字、下划线或短横线，并以字母或数字开头")
        workflow = data.get("workflow")
        self.expand(workflow)
        expected = data.get("expected_hash")
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise WebAPIError(400, "expected_hash 必须是文件的 SHA-256")
        raw = (json.dumps(workflow, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        with self._save_lock:
            path = self._path("definitions", "custom", name + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            current = sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if expected != current:
                raise WebAPIError(409, "流程文件已存在或已被修改，请刷新后重新保存")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".save-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return {"key": key, "hash": sha256(raw).hexdigest(), "workflow": workflow}

    def _summary(self, item):
        run_id, entry = item
        base = {"run_id": run_id, "root": entry.get("root", run_id), "status": "unavailable", "revision": None,
                "workflow_id": "", "title": run_id, "completed": 0, "total": 0, "waiting": 0, "unclaimed": 0}
        try:
            state = self._call("read", run_id)
            return {**base, **summarize(state)}
        except Exception as exc:
            error = _backend_error(exc)
            return {**base, "error": error.payload["error"], "error_status": error.status}

    def overview(self):
        ledger = self._call("ledger")
        runs = list(self._reads.map(self._summary, ledger.get("runs", {}).items()))
        return {"runs": runs, "budgets": ledger.get("budgets", {}), "resources": ledger.get("resources", {}),
                "backlog": ledger.get("backlog", {}),
                "errors": [{"run_id": item["run_id"], "error": item["error"], "status": item["error_status"]}
                           for item in runs if "error" in item]}

    def inspection(self, run_id):
        return inspect_run(self._call("read", run_id), self.root)

    def artifact(self, run_id, artifact_id):
        # Only an artifact registered in this run can select a file. No endpoint
        # accepts a caller supplied path, URL or storage hash.
        record = next((item for item in self.inspection(run_id)["artifacts"]
                       if item["artifact_id"] == artifact_id), None)
        if record is None:
            raise WebAPIError(404, "该任务没有此成果")
        if record.get("status") in {"changed", "missing", "unavailable"}:
            raise WebAPIError(409 if record["status"] == "changed" else 404,
                              "成果已变化，不能冒充当时版本" if record["status"] == "changed" else "成果文件当前不可读取", record)
        try:
            raw, media_type = read_artifact(self.root, record)
        except (OSError, ValueError) as exc:
            raise WebAPIError(409, "成果读取失败，请刷新核对文件状态", str(exc)) from exc
        return record, raw, media_type

    def post(self, path, data):
        if path == "/api/validate":
            return {"valid": True, "workflow": self.expand(data.get("workflow"))}
        if path == "/api/workflows":
            return self.save(data)
        if path == "/api/runs":
            run_id = _text(data.get("run_id"), "run_id")
            workflow = self.expand(data.get("workflow"))
            limits = _object(data.get("limits", {}), "limits")
            return self._call("start", run_id, workflow, data.get("input", {}), limits)
        if path == "/api/backlog":
            action = data.get("action")
            if not isinstance(action, str) or action not in _BACKLOG:
                raise WebAPIError(400, "不支持的待办操作")
            return self._call("backlog", action, _object(data.get("data", {}), "data"))
        match = re.fullmatch(r"/api/runs/([^/]+)/control", path)
        if match:
            action = data.get("action")
            if not isinstance(action, str) or action not in _CONTROLS:
                raise WebAPIError(400, "不支持的任务控制操作")
            return self._call("control", _text(unquote(match[1]), "run_id"), action,
                              _object(data.get("data", {}), "data"))
        raise WebAPIError(404, "接口不存在")


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app, port=28100, *, static_root=STATIC_ROOT):
        self.app = app
        self.static_root = Path(static_root).resolve()
        super().__init__(("127.0.0.1", port), WorkbenchHandler)

    def server_close(self):
        super().server_close()
        self.app.close()


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "LocalWorkbench/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _check_host(self, *, mutation=False):
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update({"127.0.0.1", "localhost"})
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            raise WebAPIError(403, "仅允许从本机工作台地址访问")
        if mutation:
            origins = self.headers.get_all("Origin", [])
            if origins and (len(origins) != 1 or origins[0].lower() != "http://" + hosts[0].lower()):
                raise WebAPIError(403, "拒绝跨来源操作，请从工作台页面提交")
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                raise WebAPIError(403, "拒绝跨站操作")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise WebAPIError(400, "不支持分块请求体")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise WebAPIError(411, "请求需要 Content-Length")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise WebAPIError(400, "无效的 Content-Length")
        size = int(lengths[0])
        if size > MAX_BODY:
            raise WebAPIError(413, "请求内容超过 1 MiB 上限")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or self.headers.get_content_type() != "application/json":
            raise WebAPIError(415, "请求必须使用 application/json")
        if self.headers.get_content_charset("utf-8").lower() not in {"utf-8", "utf8"}:
            raise WebAPIError(415, "JSON 必须使用 UTF-8 编码")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise WebAPIError(400, "请求内容不完整")
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            canonical(value)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise WebAPIError(400, "请求不是有效的 UTF-8 JSON", str(exc)) from exc
        return _object(value, "请求")

    def _send(self, status, value, content_type="application/json; charset=utf-8", *, headers=None):
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
               if not isinstance(value, bytes) else value)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for name, item in (headers or {}).items():
            self.send_header(name, item)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _get(self, path):
        app = self.server.app
        if path == "/api/catalog":
            return self._send(200, app.catalog())
        if path == "/api/overview":
            return self._send(200, app.overview())
        match = re.fullmatch(r"/api/runs/([^/]+)/inspection", path)
        if match:
            return self._send(200, app.inspection(_text(unquote(match[1]), "run_id")))
        match = re.fullmatch(r"/api/runs/([^/]+)/artifacts/([A-Za-z0-9_-]+)(/content)?", path)
        if match:
            run_id = _text(unquote(match[1]), "run_id")
            record, raw, mime = app.artifact(run_id, match[2])
            safe_media = mime in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
                                  "audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4", "video/mp4", "video/webm", "video/ogg"}
            content_url = f"/api/runs/{quote(run_id, safe='')}/artifacts/{match[2]}/content"
            if match[3]:
                disposition = "inline" if safe_media else "attachment"
                name = Path(record.get("path", "artifact").replace("\\", "/")).name
                return self._send(200, raw, mime if safe_media else "application/octet-stream",
                                  headers={"Content-Disposition": disposition + "; filename=artifact; filename*=UTF-8''" + quote(name, safe="")})
            result = {"artifact": record, "download_url": content_url}
            if safe_media:
                result["preview_url"] = content_url
            elif (mime.startswith("text/") or mime in {"application/json", "application/xml", "image/svg+xml"}) and len(raw) <= 2 * 1024 * 1024:
                try:
                    result["text"] = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    result["notice"] = "该文本不是 UTF-8，可下载后查看。"
            else:
                result["notice"] = "该成果可下载查看。" if len(raw) <= 2 * 1024 * 1024 else "成果超过 2 MiB 预览上限，可下载查看。"
            return self._send(200, result)
        match = re.fullmatch(r"/api/runs/([^/]+)(/export)?", path)
        if match:
            run_id = _text(unquote(match[1]), "run_id")
            state = app._call("read", run_id)
            if match[2]:
                return self._send(200, readable(state).encode("utf-8"), "text/markdown; charset=utf-8",
                                  headers={"Content-Disposition": "attachment; filename=workflow.md; filename*=UTF-8''" + quote(run_id + ".md", safe="")})
            return self._send(200, state)
        assets = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                  "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        if path not in assets:
            raise WebAPIError(404, "页面或接口不存在")
        name, mime = assets[path]
        asset = self.server.static_root / name
        if asset.resolve() != asset or not asset.is_file():
            raise WebAPIError(404, "页面资源不存在")
        self._send(200, asset.read_bytes(), mime + "; charset=utf-8")

    def _handle(self):
        try:
            self._check_host(mutation=self.command == "POST")
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise WebAPIError(400, "只支持本地相对请求路径")
            if self.command == "POST":
                self._send(200, self.server.app.post(parsed.path, self._body()))
            else:
                self._get(parsed.path)
        except WebAPIError as exc:
            self._send(exc.status, exc.payload)
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            self._send(400, {"error": "内容格式无效", "detail": str(exc)})
        except TimeoutError:
            self._send(408, {"error": "请求读取超时"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_error("workbench failure: %s", exc)
            self._send(500, {"error": "工作台发生内部错误，请查看服务日志"})

    do_GET = do_HEAD = do_POST = _handle

    def do_OPTIONS(self):
        self._send(405, {"error": "不支持此请求方法"})


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本地中文工作流工作台")
    parser.add_argument("--port", type=int, default=28100)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="项目目录（默认按模块位置定位）")
    parser.add_argument("--url", help="现有工作流后端地址")
    args = parser.parse_args(argv)
    app = Workbench(args.root, Client(args.url))
    server = WorkbenchServer(app, args.port)
    print(f"工作台：http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
