"""The browser graph model must produce a saved, executable workflow definition."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ai_workflow import engine
from ai_workflow.loader import load_workflow
from ai_workflow.web import Workbench

ROOT=Path(__file__).resolve().parents[1]


def test_canvas_connection_survives_save_reload_and_actual_engine_handoff(tmp_path):
    if not shutil.which('node'):
        pytest.skip('Node.js required for the shared browser graph model')
    script="""
const G=require('./src/ai_workflow/web_static/graph-editor.js');
const def=require('./definitions/nodes/requirements.capture@1.json');
let wf={id:'canvas-acceptance',version:'1',nodes:{
 first:{node:'requirements.capture@1',input:{request:'a concrete need'}},
 second:{node:'requirements.capture@1',input:{}}},ui:{positions:{first:{x:55,y:80},second:{x:450,y:100}}}};
wf=G.connect(wf,()=>def,{kind:'data',from:'first',to:'second',outcome:'success',sourcePath:'request',path:'request'});
process.stdout.write(JSON.stringify(wf));
"""
    built=subprocess.run(['node','-e',script],cwd=ROOT,check=True,capture_output=True,text=True,encoding='utf-8')
    workflow=json.loads(built.stdout)
    (tmp_path/'definitions/nodes').mkdir(parents=True)
    shutil.copyfile(ROOT/'definitions/nodes/requirements.capture@1.json',tmp_path/'definitions/nodes/requirements.capture@1.json')
    app=Workbench(tmp_path)
    try:
        app.save({'key':'custom/canvas-acceptance','workflow':workflow})
        restored=app.catalog()['workflows'][0]['workflow']
        assert restored==workflow
    finally:
        app.close()
    loaded=load_workflow(tmp_path/'definitions/custom/canvas-acceptance.json',tmp_path/'definitions/nodes')
    state=engine.new_run('canvas-fixture',loaded,{})
    first=engine.dispatch(state,'first')
    engine.report(state,{'kind':'completed','outcome':'success','output':{'request':'the verified need'},
                        'execution_id':first['execution_id'],'attempt':first['attempt'],'report_id':'first-complete'})
    second=engine.dispatch(state,'second')
    assert second['input']=={'request':'the verified need'}
    assert state['workflow']['ui']['positions']==workflow['ui']['positions']
