"""Start/stop this checkout's backend; deployment paths stay in .local, not code."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.local/deployment.json'
HTTP = build_opener(ProxyHandler({}))


def config():
    return json.loads(CONFIG.read_text(encoding='utf-8-sig')) if CONFIG.exists() else {}


def request(url, data=None):
    req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                  headers={'Content-Type': 'application/json'})
    with HTTP.open(req, timeout=3) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def wait_for(check, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise RuntimeError('Service did not become ready; inspect .local/logs')


def owned(service, cfg):
    """Verify command identity each time; never trust a stale PID file."""
    signatures = ([str(ROOT / 'ops/restate.toml').encode(), b'--config-file']
                  if service == 'restate' else
                  [cfg.get('python_bin', str(ROOT / '.venv/bin/python')).encode(),
                   b'ai_workflow.server'])
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            if all(s in args for s in signatures):
                result.append(int(entry.name))
        except (OSError, ProcessLookupError):
            pass
    return result


def backend(action, cfg):
    admin = cfg.get('admin_url', 'http://127.0.0.1:29070')
    endpoint = cfg.get('endpoint_url', 'http://127.0.0.1:29080')
    if action in ('stop', 'restart'):
        for service in ('endpoint', 'restate'):
            for pid in owned(service, cfg):
                try:
                    os.kill(pid, signal.SIGTERM)
                    # Restate SDK 1.2 owns SIGTERM for draining its requests;
                    # Hypercorn retains SIGINT to finish the server shutdown.
                    if service == 'endpoint':
                        os.kill(pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            wait_for(lambda: not owned(service, cfg), 12)
    if action in ('start', 'restart'):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), AI_WORKFLOW_HOME=str(ROOT),
                   PYTHONUNBUFFERED='1', AI_WORKFLOW_PATH_MAP=json.dumps(cfg.get('path_map', {})))
        env['RESTATE_BIN'] = cfg.get('restate_bin', os.getenv('RESTATE_BIN', 'restate-server'))
        env['PYTHON_BIN'] = cfg.get('python_bin', str(ROOT / '.venv/bin/python'))
        env['AI_WORKFLOW_ENDPOINT_BIND'] = endpoint.split('://', 1)[1].rstrip('/')
        logs = ROOT / '.local/logs'
        logs.mkdir(parents=True, exist_ok=True)
        for service, url in (('restate', admin + '/health'), ('endpoint', endpoint + '/discover')):
            child = None
            if not owned(service, cfg):
                with (logs / (service + '.log')).open('ab') as log:
                    child = subprocess.Popen(['sh', str(ROOT / 'ops' / (service + '.sh'))], cwd=ROOT,
                                     env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
            wait_for(lambda: HTTP.open(url, timeout=2).close() is None and bool(owned(service, cfg)))
            if child:
                time.sleep(.2)
                if child.poll() is not None:
                    raise RuntimeError(service + ' exited during startup; inspect .local/logs')
        deployments = request(admin + '/deployments').get('deployments', [])
        if not any(d.get('http', {}).get('uri', '').rstrip('/') == endpoint.rstrip('/') for d in deployments):
            request(admin + '/deployments', {'uri': endpoint})
    return {service: owned(service, cfg) for service in ('restate', 'endpoint')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status'])
    args = parser.parse_args()
    cfg = config()
    if os.name == 'nt':
        if not all(cfg.get(k) for k in ('wsl_distribution', 'linux_root', 'python_bin')):
            raise SystemExit('Configure .local/deployment.json first; see docs/WORKBENCH.md')
        result = subprocess.run(['wsl.exe', '-d', cfg['wsl_distribution'], '--', cfg['python_bin'],
                                 cfg['linux_root'] + '/ops/manage.py', args.action],
                                capture_output=True, timeout=80,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise SystemExit(result.stderr.decode('utf-8', errors='replace'))
        print(result.stdout.decode('utf-8').strip())
    else:
        print(json.dumps(backend(args.action, cfg)))


if __name__ == '__main__':
    main()
