"""Opt-in release acceptance against real local services. Creates labelled test runs.

Agent reasoning is a fixture; actual processes, files, MCP and durable state are real.
Set WORKFLOW_LIVE=1. WORKFLOW_CRASH=1 additionally restarts this checkout's backend.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from ai_workflow.client import Client
from ai_workflow.bridge import JobStore
from ai_workflow.worker import cycle
from test_definitions import fixture_output

pytestmark = pytest.mark.skipif(os.getenv('WORKFLOW_LIVE') != '1', reason='opt-in real backend acceptance')
ROOT = Path(__file__).resolve().parents[1]
WEB = os.getenv('AI_WORKFLOW_WEB', 'http://127.0.0.1:28100')


def wait(call, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = call()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('transition was not observed')


def test_full_methodology_documents_controls_recovery_and_mcp():
    client = Client()
    rid = 'release-' + uuid.uuid4().hex[:8]
    folder = ROOT / '.local/evidence' / rid
    folder.mkdir(parents=True)
    document = folder / '需求与方案.md'
    store = JobStore(folder / 'jobs')
    runs = [rid]
    reports = []
    actor = 'release-fixture-A'
    with httpx.Client(base_url=WEB, timeout=30, trust_env=False) as web:
        workflow = next(w['workflow'] for w in web.get('/api/catalog').json()['workflows']
                        if w['key'] == 'builtin/methodology-execution')
        inputs = {'title': '新版发布验收：文档、两阶段执行与恢复',
                  'request': '发布验收夹具：两阶段分别生成一个文件，核验交接、恢复和最终归档。',
                  '_meta': {'purpose': 'test'}}
        started = web.post('/api/runs', json={'run_id': rid, 'workflow': workflow,
                                            'input': inputs, 'limits': {'max_calls': 40}})
        started.raise_for_status()

        def claim(run, node):
            return wait(lambda: next((j for j in client.claim(run, actor)['jobs'] if j['node_id'] == node), None))

        def finish(run, node, output=None, outcome='success', job=None):
            job = job or claim(run, node)
            state = client.read(run)
            body = fixture_output(state, job, output, outcome)
            if node in ('requirements', 'design', 'plan'):
                document.write_text('# ' + node + '\n\n本文件是发布验收夹具，核对真实版本归档与交接。\n', encoding='utf-8')
                body['documents'] = [{'path': str(document), 'title': node + ' 文档'}]
            report = {'report_id': job['execution_id'] + ':completed', 'execution_id': job['execution_id'],
                      'attempt': job['attempt'], 'worker_id': actor, 'kind': 'completed',
                      'outcome': outcome, 'output': body}
            if node == 'requirements':
                with pytest.raises(httpx.HTTPStatusError) as rejected:
                    client.report(run, {**report, 'output': {}})
                assert rejected.value.response.status_code == 409
            assert client.report(run, report)['accepted']
            assert client.report(run, report)['accepted']
            reports.append({'run_id': run, 'job': job, 'report': report})
            return job

        try:
            finish(rid, 'request', {'request': inputs['request']})
            finish(rid, 'requirements', {'requirements': '两阶段文件与可追溯交接', 'acceptance': ['两个文件均可读取']})
            first_receipt = client.read(rid)['steps']['requirements']['output']['documents'][0]
            finish(rid, 'prepare', {'capabilities': ['program', 'agent'], 'evidence': ['本机执行器与共享目录实测']})
            job = claim(rid, 'design')
            client.report(rid, {'report_id': job['execution_id'] + ':waiting', 'execution_id': job['execution_id'],
                'attempt': job['attempt'], 'kind': 'waiting',
                'waiting': {'reason': '验收中断恢复', 'question': '测试预设：重启后继续？', 'resume_condition': '收到测试答复'}})
            before = client.read(rid)
            if os.getenv('WORKFLOW_CRASH') == '1':
                # Execute a fixed helper in the configured backend, using the same ownership checks as stop.
                cfg = json.loads((ROOT / '.local/deployment.json').read_text(encoding='utf-8-sig'))
                script = ('import os,signal,sys;sys.path.insert(0,sys.argv[1]+"/ops");'
                          'import manage;c=manage.config();'
                          '[os.kill(p,signal.SIGKILL) for s in ("endpoint","restate") for p in manage.owned(s,c)]')
                if os.name == 'nt':
                    command = ['wsl.exe', '-d', cfg['wsl_distribution'], '--', cfg['python_bin'],
                               '-c', script, cfg['linux_root']]
                else:
                    command = [sys.executable, '-c', script, str(ROOT)]
                subprocess.run(command, check=True, capture_output=True, timeout=15)
                subprocess.run([sys.executable, str(ROOT / 'ops/manage.py'), 'start'],
                               check=True, capture_output=True, timeout=80)
            restored = client.read(rid)
            assert restored['steps']['design']['status'] == 'waiting'
            assert restored['steps']['design']['execution_id'] == job['execution_id']
            assert restored['reports'] == before['reports']
            assert restored['assignments'] == before['assignments']
            client.control(rid, 'handoff', {'expected_revision': restored['revision'], 'old_stopped': True,
                                          'old_worker': actor, 'new_worker': 'release-fixture-B'})
            actor = 'release-fixture-B'
            client.control(rid, 'reply', {'expected_revision': client.read(rid)['revision'],
                                         'node_id': 'design', 'reply': '发布验收预设答复：继续'})
            same = claim(rid, 'design')
            assert same['execution_id'] == job['execution_id']
            finish(rid, 'design', {'plan_id': rid, 'plan': '生成并归档两个文件',
                                   'stage1': {'criteria': ['第一文件可读']},
                                   'stage2': {'criteria': ['第二文件可读、第一文件保留']}}, job=same)
            assert client.read(rid)['steps']['requirements']['output']['documents'][0] == first_receipt
            finish(rid, 'evaluate', {'assessment': '发布测试可执行'}, 'passed')
            tasks = {}
            for phase in (1, 2):
                path = folder / f'阶段{phase}.md'
                tasks[f'phase{phase}_tasks'] = {'write': {
                    'kind': 'program', 'version': '1', 'phase': phase, 'depends': [], 'outcomes': ['success'],
                    'artifact_fields': ['documents'], 'io_contract': {'version': '1', 'input': {'type': 'object'},
                        'outputs': {'success': {'type': 'object', 'required': ['documents'],
                                               'properties': {'documents': {'type': 'array', 'items': {'type': ['string', 'object']}}}}}},
                    'input': {'argv': [sys.executable, str(ROOT / 'examples/program_job.py')], 'cwd': str(ROOT),
                              'stdin': {'output_path': str(path), 'text': f'# 阶段 {phase}\n\n真实程序生成的验收文件。',
                                        'outcome': 'success', 'register_document': True}}}}
            finish(rid, 'plan', tasks)

            def execute(phase):
                child = wait(lambda: next((r.get('child_run') for r in client.read(rid)['requests'].values()
                                          if r['node_id'] == f'execute_phase{phase}'), None))
                runs.append(child)
                wait(lambda: (cycle(client, store, 'release-real-program', [child]),
                              client.read(child)['status'] == 'succeeded')[1])
                assert (folder / f'阶段{phase}.md').read_text(encoding='utf-8').startswith('# 阶段')
            execute(1)
            verify1 = claim(rid, 'verify_phase1')
            client.control(rid, 'pause', {'expected_revision': client.read(rid)['revision']})
            finish(rid, 'verify_phase1', outcome='passed', job=verify1)
            assert client.read(rid)['steps']['execute_phase2']['status'] == 'pending'
            client.control(rid, 'resume', {'expected_revision': client.read(rid)['revision']})
            execute(2)
            finish(rid, 'verify_phase2', {'issue_id': rid + '-repair', 'defects': ['测试返工路由']}, 'repair')
            repair = claim(rid, 'repair_phase2')
            finish(rid, 'repair_phase2', {'artifacts': deepcopy(repair['input']['artifacts']),
                                         'changes': ['验收夹具确认文件无需修改']}, job=repair)
            finish(rid, 'verify_phase2', outcome='passed')
            finish(rid, 'deliver')
            state = wait(lambda: (s if (s := client.read(rid))['status'] == 'succeeded' else None))
            detail = web.get(f'/api/runs/{rid}/inspection').json()
            docs = [a for a in detail['artifacts'] if a['node_id'] in ('requirements', 'design', 'plan')]
            assert len({a['sha256'] for a in docs}) == 3
            for artifact in docs:
                response = web.get(f"/api/runs/{rid}/artifacts/{artifact['artifact_id']}/content")
                response.raise_for_status()
                assert artifact['node_id'].encode() in response.content
            assert any(e['event'] == 'handoff' for e in state['history'])
            assert not [h for h in client.ledger()['holds'].values()
                        if h['request']['root'] == rid and h['status'] != 'settled']

            async def mcp_read():
                params = StdioServerParameters(command=sys.executable, args=['-X', 'utf8', '-m', 'ai_workflow.mcp_server'],
                    cwd=str(ROOT), env={**os.environ, 'AI_WORKFLOW_HOME': str(ROOT), 'AI_WORKFLOW_URL': client.url})
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        assert len(listed.tools) == 7
                        result = await session.call_tool('workflow_read', {'run_id': rid})
                        assert not result.isError
                        data = result.structuredContent or json.loads(next(c.text for c in result.content if c.type == 'text'))
                        assert data['status'] == 'succeeded' and data['run_id'] == rid
            asyncio.run(mcp_read())
            (folder / 'result.json').write_text(json.dumps({'run_id': rid, 'state': state, 'inspection': detail,
                'reports': reports, 'crash_recovery': os.getenv('WORKFLOW_CRASH') == '1'}, ensure_ascii=False, indent=2), encoding='utf-8')
            (folder.parent / 'release-latest.json').write_text(json.dumps({'run_id': rid, 'folder': str(folder)}), encoding='utf-8')
        finally:
            # Keep evidence, but leave no active test execution behind.
            for run in reversed(runs):
                state = client.read(run)
                if state['status'] not in ('succeeded', 'failed', 'cancelled'):
                    client.control(run, 'cancel', {'expected_revision': state['revision']})
