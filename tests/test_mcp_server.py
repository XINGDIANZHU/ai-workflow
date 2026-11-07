"""Onboarding must be discoverable over MCP without a backend or a host file loader."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_workflow import mcp_server
from ai_workflow.engine import dispatch, new_run
from ai_workflow.loader import load_workflow

ROOT = Path(__file__).resolve().parents[1]


def payload(result):
    assert not result.isError, result.content
    return result.structuredContent or json.loads(next(c.text for c in result.content if c.type == 'text'))


def test_stdio_discovers_guide_workflows_inputs_and_rules_without_backend(tmp_path):
    home = tmp_path / 'relocated'
    for folder in ('docs', 'definitions/nodes', 'definitions/workflows', 'methodology'):
        shutil.copytree(ROOT / folder, home / folder)

    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-X', 'utf8', '-m', 'ai_workflow.mcp_server'],
            cwd=str(tmp_path), env={**os.environ, 'AI_WORKFLOW_HOME': str(home),
                                   'AI_WORKFLOW_URL': 'http://127.0.0.1:1'})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
                initialized = await session.initialize()
                assert 'workflow_guide' in initialized.instructions
                listed = {t.name: t for t in (await session.list_tools()).tools}
                assert listed['workflow_guide'].annotations.readOnlyHint is True
                # Tool descriptions remain discoverable even when a host drops initialize.instructions.
                assert all('workflow_guide' in t.description for name, t in listed.items() if name != 'workflow_guide')
                guide = payload(await session.call_tool('workflow_guide', {}))
                assert len(guide['workflows']) == 5 and guide['unavailable_workflows'] == []
                assert all(name in guide['guide'] for name in ('workflow_start', 'workflow_claim', 'workflow_report',
                    'execution_id', 'report_id', 'expected_revision', 'waiting', 'accepted'))
                path = next(w['workflow_path'] for w in guide['workflows'] if w['id'] == 'methodology-execution')
                selected = payload(await session.call_tool('workflow_guide', {'workflow_path': path}))['workflow']
                request = next(b for b in selected['input_bindings'] if b['source'] == 'input.request')
                assert request['schema'] and request['node_id'] == 'request'
                assert selected['rules']
                for rule in selected['rules']:
                    assert sha256(rule['content'].encode('utf-8')).hexdigest() == rule['sha256'].lower()
                    assert str(home) in rule['path']
                full = payload(await session.call_tool('workflow_guide', {'detail': True}))
                assert full['guide'] == (home / 'docs/AGENT_ENTRY.md').read_text(encoding='utf-8')
                declined = payload(await session.call_tool('workflow_start', {'run_id': 'not-authorized',
                    'workflow_path': path, 'inputs': {}}))
                assert declined['error'] == 'explicit_start_required'
    asyncio.run(check())
    assert not (home / '.local').exists()


def test_start_resolves_discovered_relative_path_from_home_not_agent_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv('AI_WORKFLOW_HOME', str(ROOT))
    monkeypatch.chdir(tmp_path)
    calls = []

    class Client:
        def start(self, *args):
            calls.append(args)
            return {'accepted': True}

    monkeypatch.setattr(mcp_server, 'Client', Client)
    mcp_server.workflow_start('portable', 'definitions/workflows/existing-artifact-review.json',
                              {'artifacts': '2+2=4', 'criteria': '整数加法'}, True)
    assert len(calls) == 1 and calls[0][1]['nodes']['review']['io_contract']['version'] == '1'
    assert calls[0][0] == 'portable' and calls[0][2]['artifacts'] == '2+2=4'


def test_claim_enriches_only_transport_response_with_real_report_identity(monkeypatch):
    workflow = load_workflow(ROOT / 'definitions/workflows/existing-artifact-review.json')
    state = new_run('review', workflow, {'artifacts': '2+2=4', 'criteria': '整数加法'})
    job = dispatch(state, 'review')
    original = {'run_id': 'review', 'status': 'running', 'jobs': [job]}
    before = deepcopy(original)

    class Client:
        def claim(self, run_id, worker_id):
            assert (run_id, worker_id) == ('review', 'agent-A')
            return original

    monkeypatch.setattr(mcp_server, 'Client', Client)
    claimed = mcp_server.workflow_claim('review', 'agent-A')
    template = claimed['jobs'][0]['report_template']
    assert template['execution_id'] == job['execution_id'] and template['attempt'] == job['attempt']
    assert template['worker_id'] == 'agent-A' and template['outcome'] is None
    assert claimed['jobs'][0]['node_spec'] == job['node_spec']
    assert original == before
    original.update(status='waiting', jobs=[])
    empty = mcp_server.workflow_claim('review', 'agent-A')
    assert empty['jobs'] == [] and 'workflow_read' in empty['next_action']


def test_guide_keeps_custom_discovery_and_reports_bad_definition(monkeypatch, tmp_path):
    shutil.copytree(ROOT / 'docs', tmp_path / 'docs')
    custom = tmp_path / 'definitions/custom'
    custom.mkdir(parents=True)
    (custom / 'own.json').write_text('{"id":"own","version":"1","nodes":{}}', encoding='utf-8')
    (custom / 'broken.json').write_text('{', encoding='utf-8')
    monkeypatch.setenv('AI_WORKFLOW_HOME', str(tmp_path))
    result = mcp_server.workflow_guide()
    assert result['workflows'][0]['workflow_path'] == 'definitions/custom/own.json'
    assert result['unavailable_workflows'][0]['workflow_path'] == 'definitions/custom/broken.json'
