"""Ensure the configured backend and web client are ready, then open the workbench."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
URL = 'http://127.0.0.1:28100'
HTTP = build_opener(ProxyHandler({}))


def available():
    try:
        with HTTP.open(URL + '/api/catalog', timeout=2) as response:
            catalog = json.load(response)
        return isinstance(catalog.get('workflows'), list) and isinstance(catalog.get('nodes'), list)
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / 'ops/manage.py'), 'start'], cwd=ROOT, check=True,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if not available():
        logpath = ROOT / '.local/logs/web.log'
        logpath.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, AI_WORKFLOW_HOME=str(ROOT), AI_WORKFLOW_URL='http://127.0.0.1:28080')
        with logpath.open('ab') as log:
            subprocess.Popen([sys.executable, '-m', 'ai_workflow.web', '--root', str(ROOT),
                              '--url', env['AI_WORKFLOW_URL'], '--port', '28100'], cwd=ROOT,
                             env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                             start_new_session=os.name != 'nt')
        for _ in range(50):
            if available():
                break
            time.sleep(.2)
        else:
            raise SystemExit('Web client did not start; inspect .local/logs/web.log')
    with HTTP.open(URL + '/api/overview', timeout=15) as response:
        overview = json.load(response)
    if overview.get('errors'):
        raise SystemExit('Backend is unavailable: ' + json.dumps(overview['errors'], ensure_ascii=False))
    if not args.no_browser:
        webbrowser.open(URL)
    print(URL)


if __name__ == '__main__':
    main()
