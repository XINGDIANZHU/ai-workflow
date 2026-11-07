"""Directory isolation, discovery and fixed artifact versions without live services."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ai_workflow import workspaces
from ai_workflow.artifacts import capture_report, read_artifact
from ai_workflow.client import Client


def test_allocation_is_stable_isolated_and_child_stays_under_parent(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_WORKFLOW_HOME', str(tmp_path))
    settings = workspaces.validate_root(str(tmp_path / '我的任务 空间'))
    a = workspaces.allocate('../同一标题:A', 'root-A', settings)
    b = workspaces.allocate('../同一标题/B', 'root-B', settings)
    assert a['path'] != b['path']
    assert Path(a['path']).parent == tmp_path / '我的任务 空间'
    assert workspaces.allocate('../同一标题:A', 'root-A', settings) == a
    changed = workspaces.validate_root(str(tmp_path / 'new-default'))
    child = workspaces.allocate('child', 'root-A', changed, parent=a)
    assert Path(child['path']).parent == Path(a['path']) / 'subtasks'
    marker = Path(a['path']) / '.workflow-task.json'
    assert json.loads(marker.read_text(encoding='utf-8'))['run_id'] == '../同一标题:A'
    marker.write_text('{"run_id":"somebody-else"}', encoding='utf-8')
    with pytest.raises(ValueError, match='其他运行'):
        workspaces.allocate('../同一标题:A', 'root-A', settings)


def test_directory_setting_checks_actual_location_and_host_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_WORKFLOW_HOME', str(tmp_path))
    mounted = tmp_path / 'mounted'
    monkeypatch.setenv('AI_WORKFLOW_PATH_MAP', json.dumps({'Q:/tasks': str(mounted)}))
    settings = workspaces.validate_root('Q:\\tasks\\中文 文件')
    assert settings['default_task_root'] == 'Q:/tasks/中文 文件'
    workspace = workspaces.allocate('one', 'one', settings)
    assert workspace['path'].startswith('Q:/tasks/中文 文件/')
    assert Path(workspace['server_path']).is_dir()
    for bad in ('relative/path', '', 'https://example.com/folder', str(tmp_path / 'bad#anchor')):
        with pytest.raises(ValueError):
            workspaces.validate_root(bad)
    blocked = tmp_path / 'file'
    blocked.write_text('file', encoding='utf-8')
    with pytest.raises(OSError):
        workspaces.validate_root(str(blocked))


def test_discovery_filters_unfinished_runs_and_directories_without_claiming():
    state = {'workflow': {'id': 'flow'}, 'steps': {}, 'revision': 1, 'status': 'waiting',
             'input': {'title': '学习计划'}, 'workspace': {'path': 'D:/tasks/one', 'server_path': '/mnt/d/tasks/one'}}
    states = {rid: {**deepcopy(state), 'run_id': rid, 'status': status}
              for rid, status in [('one', 'waiting'), ('done', 'succeeded'), ('cancel', 'cancelled'), ('failed', 'failed')]}
    states['failed']['workspace'] = None  # Old runs stay discoverable without inventing a directory.

    class Source(Client):
        def ledger(self):
            return {'runs': {rid: {} for rid in states}, 'backlog': {}}

        def read(self, rid):
            return states[rid]

    source = Source()
    result = source.overview(query='学习', unfinished_only=True)
    assert {t['run_id'] for t in result['tasks']} == {'one', 'failed'}
    result = source.overview(unfinished_only=True, directory='d:\\tasks\\one\\draft.md')
    assert [t['run_id'] for t in result['tasks']] == ['one']
    assert source.overview(directory='D:/tasks/one-other')['tasks'] == []
    assert source.overview(directory='D:/tasks/one/../two')['tasks'] == []
    assert source.overview(query='does-not-exist')['tasks'] == []
    assert len(result['runs']) == 4  # Preserve the pre-existing registry contract.


def test_relative_artifact_is_read_from_task_directory_and_remains_pinned(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_WORKFLOW_HOME', str(tmp_path))
    workspace = workspaces.allocate('one', 'one', workspaces.default_settings())
    (tmp_path / 'result.md').write_text('wrong project file', encoding='utf-8')
    source = Path(workspace['path']) / 'result.md'
    source.write_text('actual task artifact', encoding='utf-8')
    report = {'kind': 'completed', 'report_id': 'done', 'execution_id': 'exec',
              'output': {'documents': ['result.md']}}
    captured = capture_report(tmp_path, 'one', report, {'artifact_fields': ['documents']}, workspace)
    receipt = captured['output']['documents'][0]
    assert receipt['path'] == workspace['path'] + '/result.md'
    source.write_text('later edit', encoding='utf-8')
    assert read_artifact(tmp_path, receipt)[0] == b'actual task artifact'
    assert report['output']['documents'] == ['result.md']
