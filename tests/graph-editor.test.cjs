const test=require('node:test');
const assert=require('node:assert/strict');
const G=require('../src/ai_workflow/web_static/graph-editor.js');
const schema=(properties,required=Object.keys(properties))=>({type:'object',properties,required,additionalProperties:false});
const source={kind:'agent',version:'1',outcomes:['ok','retry'],io_contract:{version:'1',input:schema({question:{type:'string'}}),outputs:{ok:schema({answer:{type:'string'},detail:schema({score:{type:'integer'}})}),retry:schema({reason:{type:'string'}})}}};
const target={kind:'agent',version:'1',outcomes:['success'],io_contract:{version:'1',input:schema({text:{type:'string'}}),outputs:{success:schema({accepted:{type:'boolean'}})}}};
const definition=node=>({source,target}[node.node]||node);
const workflow=()=>({id:'canvas',version:'1',variables:{round:0},nodes:{a:{node:'source',input:{question:'hello'},routes:{retry:{reset:['a'],counter:'round',limit:2,on_limit:{action:'fail'}}}},b:{node:'target',input:{}}},ui:{positions:{a:{x:30,y:40}}}});
const link={kind:'data',from:'a',to:'b',outcome:'ok',sourcePath:'answer',path:'text'};

test('drawn parameter connection becomes runtime mapping, dependency and outcome condition',()=>{
  const old=workflow(),result=G.connect(old,definition,link);
  assert.deepEqual(result.nodes.b.input,{text:{$ref:'steps.a.output.answer'}});
  assert.deepEqual(result.nodes.b.depends,['a']);assert.deepEqual(result.nodes.b.when,{a:['ok']});
  assert.deepEqual(result.nodes.a.routes,old.nodes.a.routes);assert.deepEqual(result.ui,old.ui);assert.deepEqual(old.nodes.b.input,{});
  assert.deepEqual(JSON.parse(JSON.stringify(result)),result);
});
test('incompatible source type, self links and dependency cycles cannot change the workflow',()=>{
  const wf=workflow();assert.throws(()=>G.connect(wf,definition,{...link,sourcePath:'detail.score'}),/类型/);
  assert.throws(()=>G.connect(wf,definition,{...link,to:'a'}),/不同/);
  const connected=G.connect(wf,definition,link);
  assert.throws(()=>G.connect(connected,definition,{kind:'control',from:'b',to:'a'}),/依赖环/);
  assert.deepEqual(wf.nodes.b.input,{});
});
test('connecting never silently overwrites a value or existing branching decision',()=>{
  let wf=workflow();wf.nodes.b.input.text='user value';assert.throws(()=>G.connect(wf,definition,link),/已有值/);
  wf=workflow();wf.nodes.b.when={a:['retry']};assert.throws(()=>G.connect(wf,definition,link),/分支条件/);
});
test('disconnecting data keeps dependency; dependency cannot be removed with live data references',()=>{
  const connected=G.connect(workflow(),definition,link);
  assert.throws(()=>G.disconnect(connected,{kind:'control',from:'a',to:'b'}),/先断开/);
  const result=G.disconnect(connected,link);assert.deepEqual(result.nodes.b.input,{});assert.deepEqual(result.nodes.b.depends,['a']);
  const detached=G.disconnect(result,{kind:'control',from:'a',to:'b'});assert.deepEqual(detached.nodes.b.depends,[]);assert.deepEqual(detached.nodes.b.when,{});
});
test('existing workflow references, conditions and bounded loops are rendered without conversion',()=>{
  const wf=G.connect(workflow(),definition,link),before=JSON.stringify(wf),all=G.edges(wf,definition);
  assert.equal(all.filter(e=>e.kind==='data').length,1);assert.equal(all.find(e=>e.kind==='data').sourcePath,'answer');
  assert.equal(all.find(e=>e.kind==='route').outcome,'retry');assert.equal(JSON.stringify(wf),before);
});
test('whole-object handoff and nested output fields follow the same interface',()=>{
  const wf=workflow();wf.nodes.b={...target,input:{}};wf.nodes.b.io_contract=JSON.parse(JSON.stringify(target.io_contract));wf.nodes.b.io_contract.input=schema({score:{type:'integer'}});
  const result=G.connect(wf,definition,{...link,sourcePath:'detail',path:''});assert.deepEqual(result.nodes.b.input,{$ref:'steps.a.output.detail'});
  assert.deepEqual(G.disconnect(result,{...link,sourcePath:'detail',path:''}).nodes.b.input,{});
});
test('schema descriptions expose nested fields, requiredness, alternatives and list elements',()=>{
  const list=G.fields(source.io_contract.outputs.ok);assert(list.find(f=>f.path==='detail.score').required);
  assert.match(G.describe({type:'array',items:{type:'string'},minItems:1}),/列表.*文字.*至少 1/);
  assert.match(G.describe({anyOf:[{type:'string'},{type:'object'}]}),/文字.*对象/);
});
test('new layout uses dependencies and keeps every node reachable',()=>{
  const wf=G.connect(workflow(),definition,link),positions=G.layout(wf);assert(positions.b.x>positions.a.x);assert.equal(Object.keys(positions).length,2);
});
