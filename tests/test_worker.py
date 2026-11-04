"""Adapter receipts must be accepted by the actual node/result contract."""
import pytest
from ai_workflow import engine
from ai_workflow.worker import cycle


@pytest.mark.parametrize('kind,receipt_outcome,declared',[
    ('comfyui','generated','success'),('program',None,'success'),
    ('program','needs_revision','needs_revision')])
def test_collected_receipt_enters_real_contract_without_losing_business_result(kind,receipt_outcome,declared):
    state=engine.new_run('receipt-contract',{'id':'receipt','version':'1','nodes':{
        'operation':{'kind':kind,'version':'1','input':{},'outcomes':[declared]}}},{})
    request=engine.dispatch(state,'operation')
    class Client:
        def claim(self,*args):
            return {'jobs':[{**request,'run_status':'running','step_status':'running'}]}
        def report(self,rid,value):
            assert engine.report(state,value)['accepted']
    class Store:
        def submit(self,*args):
            return {'status':'completed','outcome':receipt_outcome,'output':{'artifact':'actual-receipt-path'}}
    assert cycle(Client(),Store(),'executor',['receipt-contract']) == 1
    assert state['status']=='succeeded'
    assert state['steps']['operation']['outcome']==declared
    assert state['steps']['operation']['output']=={'artifact':'actual-receipt-path'}
