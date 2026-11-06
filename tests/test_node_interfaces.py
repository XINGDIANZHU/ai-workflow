"""Public-contract checks: reusable definitions, wiring and actual handoffs."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ai_workflow import engine
from ai_workflow.artifacts import capture_report, read_artifact
from ai_workflow.client import Client
from ai_workflow.contracts import ContractError, validate_workflow
from ai_workflow.loader import load_workflow
from ai_workflow.web import Workbench, WebAPIError

ROOT = Path(__file__).resolve().parents[1]


def object_schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {},
            'required': list(properties or {}) if required is None else required, 'additionalProperties': False}


def producer_consumer(source=None, target=None):
    output = source or object_schema({'answer': {'type': 'string'}})
    expected = target or {'type': 'string', 'minLength': 1}
    return {'id': 'wiring', 'version': '1', 'nodes': {
        'a': {'kind': 'agent', 'version': '2', 'input': {}, 'outcomes': ['success'],
              'io_contract': {'version': '1', 'input': object_schema(), 'outputs': {'success': output}}},
        'b': {'kind': 'agent', 'version': '2', 'depends': ['a'], 'outcomes': ['success'],
              'input': {'question': {'$ref': 'steps.a.output.answer'}},
              'io_contract': {'version': '1', 'input': object_schema({'question': expected}),
                              'outputs': {'success': object_schema({'accepted': {'type': 'boolean'}})}}}}}


def completion(job, output, outcome='success', report_id=None):
    return {'execution_id': job['execution_id'], 'attempt': job['attempt'], 'report_id': report_id or job['execution_id'] + ':result',
            'kind': 'completed', 'outcome': outcome, 'output': output}


@pytest.mark.parametrize('mutation', ['wrong_type', 'missing_field', 'optional_field', 'unknown_input', 'bad_constant', 'bad_item'])
def test_observable_wiring_errors_are_rejected_before_execution(mutation):
    workflow = producer_consumer()
    a, b = workflow['nodes'].values()
    if mutation == 'wrong_type':
        a['io_contract']['outputs']['success']['properties']['answer'] = {'type': 'integer'}
    elif mutation == 'missing_field':
        b['input']['question']['$ref'] = 'steps.a.output.missing'
    elif mutation == 'optional_field':
        a['io_contract']['outputs']['success']['required'] = []
    elif mutation == 'unknown_input':
        b['input']['typo'] = {'$ref': 'input.anything'}
    elif mutation == 'bad_constant':
        b['input']['question'] = 42
    elif mutation == 'bad_item':
        a['io_contract']['outputs']['success']['properties']['answer'] = {'type': 'array', 'items': {'type': 'integer'}}
        b['io_contract']['input']['properties']['question'] = {'type': 'array', 'items': {'type': 'string'}}
    with pytest.raises(ContractError):
        validate_workflow(workflow)


def test_branch_dependent_handoff_requires_outcome_gate_or_missing_value_default():
    workflow = producer_consumer()
    a, b = workflow['nodes'].values()
    a['outcomes'].append('unavailable')
    a['io_contract']['outputs']['unavailable'] = object_schema({'reason': {'type': 'string'}})
    with pytest.raises(ContractError, match='unavailable'):
        validate_workflow(workflow)
    b['when'] = {'a': ['success']}
    validate_workflow(workflow)
    del b['when']
    b['input']['question']['default'] = 'explicit fallback'
    state = engine.new_run('default', workflow, {})
    job = engine.dispatch(state, 'a')
    engine.report(state, completion(job, {'reason': 'no evidence'}, 'unavailable'))
    assert engine.dispatch(state, 'b')['input']['question'] == 'explicit fallback'


def test_input_type_error_cannot_be_masked_by_reference_default():
    workflow = producer_consumer()
    workflow['nodes']['a']['io_contract']['outputs']['success']['properties']['answer'] = {'type': 'integer'}
    workflow['nodes']['b']['input']['question']['default'] = 'fallback'
    with pytest.raises(ContractError, match='incompatible'):
        validate_workflow(workflow)


def test_schema_alternatives_preserve_boolean_branches_and_exact_one_semantics():
    workflow = producer_consumer()
    node = workflow['nodes']['b']
    node['io_contract']['input']['anyOf'] = [False, True]
    validate_workflow(workflow)
    node['input'] = {'question': 'known input'}
    node['io_contract']['input']['oneOf'] = [True, True]
    with pytest.raises(ContractError, match='oneOf'):
        validate_workflow(workflow)


def test_external_inputs_checked_at_start_and_dynamic_values_checked_before_dispatch():
    workflow = producer_consumer()
    b = workflow['nodes']['b']
    b['input']['question'] = {'$ref': 'input.question'}
    with pytest.raises(ContractError, match='question'):
        engine.new_run('bad-start', workflow, {'question': 4})
    b['input']['question'] = {'$ref': 'vars.question'}
    workflow['variables'] = {'question': 4}
    state = engine.new_run('bad-dynamic', workflow, {})
    job = engine.dispatch(state, 'a')
    engine.report(state, completion(job, {'answer': 'ready'}))
    assert state['steps']['b']['status'] == 'failed'
    assert state['steps']['b']['attempt'] == 0
    assert 'question' in state['steps']['b']['error']['message']


def test_rejected_completion_does_not_advance_and_same_report_can_be_corrected():
    state = engine.new_run('typed-handoff', producer_consumer(), {})
    job = engine.dispatch(state, 'a')
    before = deepcopy(state)
    with pytest.raises(ContractError, match='answer'):
        engine.report(state, completion(job, {'answer': 4}))
    assert state == before
    engine.report(state, completion(job, {'answer': 'read this'}))
    assert engine.dispatch(state, 'b')['input'] == {'question': 'read this'}


@pytest.mark.parametrize('bad', ['unknown_version', 'incomplete_outcomes', 'remote_schema', 'misspelled_schema', 'required_mismatch'])
def test_incomplete_or_unsupported_interfaces_fail_closed(bad):
    workflow = producer_consumer()
    contract = workflow['nodes']['a']['io_contract']
    if bad == 'unknown_version': contract['version'] = '2'
    elif bad == 'incomplete_outcomes': contract['outputs'] = {}
    elif bad == 'remote_schema': contract['input']['$ref'] = 'https://example.invalid/schema'
    elif bad == 'required_mismatch': workflow['nodes']['b']['required_inputs'] = []
    else: contract['input']['requried'] = ['missing']
    with pytest.raises(ContractError):
        validate_workflow(workflow)


def test_new_wait_error_protocol_and_legacy_snapshot_are_distinct():
    state = engine.new_run('waiting', producer_consumer(), {})
    job = engine.dispatch(state, 'a')
    before = deepcopy(state)
    with pytest.raises(ContractError, match='resume_condition'):
        engine.report(state, {**completion(job, {}), 'kind': 'waiting', 'waiting': {'reason': 'missing', 'question': 'which?'}})
    assert state == before
    engine.report(state, {**completion(job, {}), 'kind': 'waiting',
                         'waiting': {'reason': 'missing', 'question': 'which?', 'resume_condition': 'user answers'}})
    resumed = engine.control(state, 'reply', {'node_id': 'a', 'reply': 'chosen', 'expected_revision': state['revision']})
    assert resumed['resume']['execution_id'] == job['execution_id']
    legacy = {'id': 'legacy', 'version': '1', 'nodes': {'old': {'kind': 'agent', 'version': '1'}}}
    old = engine.new_run('old', legacy, {})
    engine.report(old, completion(engine.dispatch(old, 'old'), None))
    assert old['status'] == 'succeeded'


@pytest.mark.parametrize('field,value', [('io_contract', {}), ('required_inputs', []), ('output_contract', {}),
                                       ('artifact_fields', []), ('bounded_cost', True), ('id', 'different')])
def test_composition_cannot_weaken_registered_interface(field, value):
    workflow = json.loads((ROOT/'definitions/workflows/existing-artifact-review.json').read_text(encoding="utf-8"))
    workflow['nodes']['review'][field] = value
    app = Workbench(ROOT)
    try:
        with pytest.raises(WebAPIError, match='流程定义无效'):
            app.expand(workflow)
    finally:
        app.close()


def test_same_review_contract_runs_in_two_flows_and_checks_the_same_bad_output():
    jobs = []
    for name in ['existing-artifact-review', 'picture-process-review']:
        workflow = load_workflow(ROOT/f'definitions/workflows/{name}.json')
        state = engine.new_run(name, workflow, {'artifacts': {'path': 'existing.png'}, 'criteria': ['visible subject'],
            'comfy_request': {'url': 'http://127.0.0.1:8188', 'workflow': {'1': {'class_type': 'Fixture', 'inputs': {}}}, 'output_dir': 'C:/fixture'}})
        if name.startswith('picture'):
            engine.report(state, completion(engine.dispatch(state, 'process'),
                {'prompt_id': 'synthetic-job', 'artifacts': [{'path': 'generated.png'}]}))
        job = engine.dispatch(state, 'review')
        jobs.append(job)
        with pytest.raises(ContractError, match='evidence'):
            engine.report(state, completion(job, {'verdict': 'passed', 'evidence': [], 'defects': [], 'unverified': [],
                                                  'artifacts': job['input']['artifacts']}, 'passed'))
        engine.report(state, completion(job, {'verdict': 'passed', 'evidence': ['synthetic inspection'],
            'defects': [], 'unverified': [], 'artifacts': job['input']['artifacts']}, 'passed'))
        assert state['status'] == 'succeeded'
    assert jobs[0]['node_spec']['io_contract'] == jobs[1]['node_spec']['io_contract']
    assert jobs[0]['node_spec']['definition_sha256'] == jobs[1]['node_spec']['definition_sha256']
    assert jobs[0]['input']['artifacts'] != jobs[1]['input']['artifacts']


def test_document_archive_and_downstream_input_share_compatible_contract(tmp_path):
    workflow = load_workflow(ROOT/'definitions/workflows/methodology-execution.json')
    state = engine.new_run('docs', workflow, {'request': 'a small task'})
    engine.report(state, completion(engine.dispatch(state, 'request'), {'request': 'a small task'}))
    document = tmp_path/'requirements.md'
    document.write_text('# Readable requirements\nReal fixture document', encoding='utf-8')
    job = engine.dispatch(state, 'requirements')
    report = completion(job, {'requirements': 'a small task', 'constraints': [], 'acceptance': ['observable result'],
                              'documents': [str(document)]}, report_id='requirements-result')
    captured = capture_report(tmp_path, state['run_id'], report, {**job['node_spec'], 'node_id': 'requirements'})
    engine.report(state, captured)
    next_job = engine.dispatch(state, 'prepare')
    receipt = next_job['input']['requirements']['documents'][0]
    document.unlink()
    assert read_artifact(tmp_path, receipt)[0].startswith(b'# Readable requirements')


def test_entrypoints_reject_invalid_inputs_before_backend_call():
    calls = []
    client = Client()
    client.call = lambda *args: calls.append(args)
    workflow = load_workflow(ROOT/'definitions/workflows/existing-artifact-review.json')
    with pytest.raises(ContractError):
        client.start('invalid', workflow, {'artifacts': {'path': 'x'}, 'criteria': 4})
    app = Workbench(ROOT, client)
    try:
        raw = json.loads((ROOT/'definitions/workflows/existing-artifact-review.json').read_text(encoding="utf-8"))
        with pytest.raises(WebAPIError) as error:
            app.post('/api/runs', {'run_id': 'invalid', 'workflow': raw, 'input': {'artifacts': 4, 'criteria': 'check'}})
        assert error.value.status == 400
    finally:
        app.close()
    assert calls == []


def test_live_revision_cannot_replace_interface_or_accept_invalid_external_input():
    workflow = producer_consumer()
    workflow['nodes']['a']['input'] = {'value': {'$ref': 'input.value'}}
    workflow['nodes']['a']['io_contract']['input'] = object_schema({'value': {'type': 'string'}})
    state = engine.new_run('revision', workflow, {'value': 'initial'})
    before = deepcopy(state)
    changed = deepcopy(workflow)
    changed['nodes']['a']['io_contract']['input']['properties']['value'] = {'type': 'integer'}
    with pytest.raises(ContractError, match='new definition'):
        engine.control(state, 'revise', {'expected_revision': 0, 'workflow': changed})
    with pytest.raises(ContractError):
        engine.control(state, 'revise', {'expected_revision': 0, 'input': {'value': 4}})
    assert state == before
