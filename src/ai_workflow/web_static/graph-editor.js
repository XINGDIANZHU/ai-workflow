/* Workflow definitions are the only graph model. No second execution state. */
(function (root) {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value));
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value || {}, key);
  const safePath = path => !path || path.split('.').every(key => /^[\w-]+$/.test(key) && !['__proto__','constructor','prototype'].includes(key));
  function fields(schema, prefix = '', depth = 0, required = true) {
    if (!schema || typeof schema !== 'object' || depth > 4) return [];
    return Object.entries(schema.properties || {}).flatMap(([key, value]) => {
      const path = prefix ? prefix + '.' + key : key;
      const needed = required && (schema.required || []).includes(key);
      return [{path, schema:value, required:needed}, ...fields(value, path, depth + 1, needed)];
    });
  }
  function typeNames(schema) {
    if (schema === false) return [];
    if (!schema || schema === true) return ['any'];
    if (schema.anyOf || schema.oneOf) return [...new Set((schema.anyOf || schema.oneOf).flatMap(typeNames))];
    if (schema.type) return Array.isArray(schema.type) ? schema.type : [schema.type];
    if (own(schema,'const')) return [schema.const === null ? 'null' : typeof schema.const === 'object' ? 'object' : typeof schema.const];
    return ['any'];
  }
  function describe(schema) {
    const names = {object:'对象',array:'列表',string:'文字',integer:'整数',number:'数值',boolean:'是/否',null:'空值',any:'任意值'};
    const hints = typeNames(schema).map(t => names[t] || t);
    if (schema?.items) hints.push('元素：' + typeNames(schema.items).map(t => names[t] || t).join(' / '));
    if (schema?.enum) hints.push('限定：' + schema.enum.map(x=>JSON.stringify(x)).join('、'));
    if (own(schema,'const')) hints.push('固定：' + JSON.stringify(schema.const));
    if (schema?.minItems) hints.push(`至少 ${schema.minItems} 项`);
    if (schema?.pattern === '\\S' || schema?.minLength || schema?.minProperties) hints.push('不可为空');
    return hints.join(' · ') || '不接受值';
  }
  function at(schema, path) {
    return path ? path.split('.').reduce((s,k)=>s?.properties?.[k], schema) : schema;
  }
  function compatible(source, target) {
    const a=typeNames(source), b=typeNames(target);
    return a.includes('any') || b.includes('any') || a.some(t=>b.includes(t) || (t==='integer'&&b.includes('number')) || (t==='number'&&b.includes('integer')));
  }
  function references(value, path = '') {
    if (!value || typeof value !== 'object') return [];
    if (typeof value.$ref === 'string') {
      const m=/^steps\.([\w-]+)\.output(?:\.(.*))?$/.exec(value.$ref);
      return m ? [{from:m[1],sourcePath:m[2] || '',path,reference:value.$ref}] : [];
    }
    return Object.entries(value).flatMap(([key,child])=>references(child,path?path+'.'+key:key));
  }
  function edges(workflow, definition) {
    const result=[];
    for (const [to,spec] of Object.entries(workflow.nodes)) {
      for (const from of spec.depends || []) result.push({kind:'control',from,to,outcome:(spec.when?.[from] || []).join('|')});
      for (const ref of references(spec.input)) {
        const outcomes=spec.when?.[ref.from] || definition(workflow.nodes[ref.from] || {}).outcomes || ['success'];
        result.push({kind:'data',to,...ref,outcome:outcomes.length===1?outcomes[0]:''});
      }
      for (const [outcome,route] of Object.entries(spec.routes || {})) {
        for (const target of route.reset || []) result.push({kind:'route',from:to,to:target,outcome});
        for (const target of route.on_limit?.reset || []) result.push({kind:'route',from:to,to:target,outcome:outcome+' · 达到上限'});
      }
    }
    return result;
  }
  function get(value,path) {
    return path ? path.split('.').reduce((v,k)=>v?.[k],value) : value;
  }
  function put(value,path,item) {
    if (!path) return item;
    if (!safePath(path)) throw new Error('此字段名需通过高级 JSON 配置。');
    if (!value || typeof value !== 'object' || Array.isArray(value) || value.$ref) throw new Error('当前整个入参已有绑定，请先断开“全部入参”的连接。');
    const parts=path.split('.'); let current=value;
    for (const key of parts.slice(0,-1)) {
      if (current[key]?.$ref || (current[key] !== undefined && (current[key]===null || typeof current[key] !== 'object'))) throw new Error('上层字段已有值或连接，请先清除。');
      current=current[key] ||= {};
    }
    if (item === undefined) delete current[parts.at(-1)]; else current[parts.at(-1)]=item;
    return value;
  }
  function connect(workflow, definition, link) {
    const wf=copy(workflow), {from,to,kind,outcome='',sourcePath='',path=''}=link;
    if (!wf.nodes[from] || !wf.nodes[to] || from===to) throw new Error('请选择两个不同的节点。');
    if (!['data','control'].includes(kind)) throw new Error('请选择执行或参数连接。');
    const source=definition(wf.nodes[from]), target=definition(wf.nodes[to]);
    const seen=new Set();
    function reaches(id) { if(id===to)return true;if(seen.has(id))return false;seen.add(id);return (wf.nodes[id]?.depends || []).some(reaches); }
    if (reaches(from)) throw new Error('这条连接会形成依赖环。返工循环请使用有次数上限的回退规则。');
    if (outcome && !(source.outcomes || ['success']).includes(outcome)) throw new Error('输出结果分支不存在。');
    const gates=wf.nodes[to].when?.[from];
    if (outcome && gates && (gates.length!==1 || gates[0]!==outcome)) throw new Error('此处已有不同的分支条件，请先在高级 JSON 中调整，避免连线改变原有分支。');
    if (kind==='data') {
      if (!safePath(sourcePath) || !safePath(path)) throw new Error('此字段名需通过高级 JSON 配置。');
      const output=at(source.io_contract?.outputs?.[outcome],sourcePath);
      const input=at(target.io_contract?.input,path);
      if (!source.io_contract || !target.io_contract || output===undefined || input===undefined) throw new Error('请选择节点契约中声明的输入和输出。旧版节点可使用执行依赖或高级 JSON。');
      if (!compatible(output,input)) throw new Error(`类型不匹配：${describe(output)} → ${describe(input)}。`);
      const current=get(wf.nodes[to].input,path), reference='steps.'+from+'.output'+(sourcePath?'.'+sourcePath:'');
      if (current?.$ref===reference) return wf;
      if (current!==undefined && current!=='' && !(current && typeof current==='object' && !Object.keys(current).length)) throw new Error('此入参已有值或连线，请先清除原值或断开连线，再连接。');
      wf.nodes[to].input=put(wf.nodes[to].input || {},path,{$ref:reference});
    }
    wf.nodes[to].depends=[...new Set([...(wf.nodes[to].depends || []),from])];
    if (outcome && (source.outcomes || []).length>1) wf.nodes[to].when={...wf.nodes[to].when,[from]:[outcome]};
    return wf;
  }
  function disconnect(workflow, edge) {
    const wf=copy(workflow), spec=wf.nodes[edge.to];
    if (!spec) throw new Error('目标节点已经不存在。');
    if (edge.kind==='route') throw new Error('有界回退规则请在高级 JSON 中修改。');
    if (edge.kind==='data') {
      const expected='steps.'+edge.from+'.output'+(edge.sourcePath?'.'+edge.sourcePath:'');
      if (get(spec.input,edge.path)?.$ref!==expected) throw new Error('连接已变化，请重新选择。');
      spec.input=put(spec.input,edge.path,undefined) || {};
    } else {
      if (references(spec.input).some(r=>r.from===edge.from)) throw new Error('还有参数引用此上游节点，请先断开参数连接。');
      spec.depends=(spec.depends || []).filter(id=>id!==edge.from);
      if (spec.when) delete spec.when[edge.from];
    }
    return wf;
  }
  function layout(workflow) {
    const columns={}, counts={};
    function level(id,visiting=new Set()) {
      if (own(columns,id))return columns[id];
      if(visiting.has(id))return 0;
      const next=new Set([...visiting,id]);
      return columns[id]=Math.max(0,...(workflow.nodes[id]?.depends || []).map(x=>level(x,next)+1));
    }
    return Object.fromEntries(Object.keys(workflow.nodes).map(id=>{const col=level(id),row=counts[col] || 0;counts[col]=row+1;return [id,{x:36+col*374,y:36+row*660}];}));
  }
  function createCanvas(options) {
    const {workflow,definition,title,selected,onSelect,onConnect,onEdge,onMove,onError,el}=options;
    const viewport=el('div',{class:'flow-viewport',tabindex:0,'aria-label':'流程画布，拖动标题移动节点，拖动端口创建连接'});
    const spacer=el('div',{class:'flow-spacer'}), canvas=el('div',{class:'flow-canvas'});
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('flow-wires');
    canvas.append(svg);spacer.append(canvas);viewport.append(spacer);
    const automatic=layout(workflow), positions={}, cards=new Map(), ports=[];
    let zoom=options.zoom || 1, active=null;
    const key=(node,side,kind,path='',outcome='')=>JSON.stringify([node,side,kind,path,outcome]);
    const anchors=new Map();
    function port(node,side,kind,path='',outcome='') {
      const b=el('button',{type:'button',class:'flow-port '+kind,'aria-label':`${node} ${side==='in'?'输入':'输出'} ${kind==='control'?'执行':path || '全部参数'}${outcome?' · '+outcome:''}`,title:side==='out'?'拖到下游输入端口；也可依次点击两端':'从上游输出端口拖入'});
      const data={node,side,kind,path,outcome};b.dataset.port=String(ports.length);ports.push({element:b,data});anchors.set(key(node,side,kind,path,outcome),b);
      b.addEventListener('pointerdown',event=>{if(side!=='out')return;event.preventDefault();event.stopPropagation();beginWire(event,data,b);});
      b.addEventListener('click',event=>{event.stopPropagation();if(suppressClick){suppressClick=false;return;}if(side==='out'){pending=data;hint.textContent='已选输出，请点击下游节点的输入端口。';}else if(pending){finish(pending,data);pending=null;}});
      return b;
    }
    for (const [id,spec] of Object.entries(workflow.nodes)) {
      const stored=workflow.ui?.positions?.[id];positions[id]=stored&&Number.isFinite(stored.x)&&Number.isFinite(stored.y)?{x:Math.max(8,Math.min(stored.x,20000)),y:Math.max(8,Math.min(stored.y,20000))}:automatic[id];
      const def=definition(spec), contract=def.io_contract;
      const header=el('button',{type:'button',class:'flow-node-header','aria-label':'移动或选择节点 '+id},el('strong',{},title(id,spec)),el('small',{},id+' · '+(spec.node || def.kind || '节点')));
      const card=el('section',{class:'flow-card'+(id===selected?' selected':''),'data-node':id,style:`left:${positions[id].x}px;top:${positions[id].y}px`},header,
        el('p',{class:'flow-purpose'},def.description || '选择节点查看执行职责'),
        el('div',{class:'flow-control-row'},el('span',{},port(id,'in','control'),'执行'),el('span',{},'完成',port(id,'out','control'))));
      header.addEventListener('pointerdown',event=>beginMove(event,id,card,header));
      header.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();onSelect(id);}});
      if (contract) {
        const inputs=el('div',{class:'flow-port-column'},el('small',{class:'flow-column-title'},'入参'));
        inputs.append(el('div',{class:'flow-port-row'},port(id,'in','data',''),'全部入参'));
        for (const [name,schema] of Object.entries(contract.input.properties || {})) {
          const nested=fields(schema,name).filter(x=>safePath(x.path));
          const row=el('div',{class:'flow-port-row',title:(schema.description || '')+' · '+describe(schema)},port(id,'in','data',name),el('span',{},name+(contract.input.required?.includes(name)?' *':'')));
          if(nested.length) {
            const selector=el('select',{'aria-label':id+' 入参字段 '+name},el('option',{value:name},name+'（整体）'),...nested.map(f=>el('option',{value:f.path},f.path)));
            selector.addEventListener('change',()=>{const old=row.querySelector('.flow-port'), idx=Number(old.dataset.port);anchors.delete(key(id,'in','data',ports[idx].data.path));ports[idx].data.path=selector.value;old.setAttribute('aria-label',id+' 输入 '+selector.value);anchors.set(key(id,'in','data',selector.value),old);draw();});row.append(selector);
          }
          inputs.append(row);
        }
        const outputs=el('div',{class:'flow-port-column output'},el('small',{class:'flow-column-title'},'出参 / 结果分支'));
        for(const [outcome,schema] of Object.entries(contract.outputs)) {
          const b=port(id,'out','data','',outcome);
          const selector=el('select',{'aria-label':id+' 输出字段 '+outcome},el('option',{value:''},'全部输出'),...fields(schema).filter(f=>safePath(f.path)).map(f=>el('option',{value:f.path},f.path+' · '+describe(f.schema))));
          selector.addEventListener('change',()=>{const data=ports[Number(b.dataset.port)].data;anchors.delete(key(id,'out','data',data.path,outcome));data.path=selector.value;b.setAttribute('aria-label',id+' 输出 '+(data.path||'全部参数')+' · '+outcome);anchors.set(key(id,'out','data',data.path,outcome),b);draw();});
          outputs.append(el('div',{class:'flow-output-row'},el('div',{class:'flow-port-row'},el('span',{},outcome),b),selector));
        }
        card.append(el('div',{class:'flow-ports'},inputs,outputs));
      } else card.append(el('p',{class:'flow-purpose'},'旧版宽松契约 · 参数连接使用高级 JSON'));
      canvas.append(card);cards.set(id,card);
    }
    let pending=null,suppressClick=false;
    const hint=el('p',{class:'flow-hint',role:'status'},'拖动标题移动节点；绿色端口传参数，灰色端口连接执行顺序。也可点击两端连接。');
    function resize() {
      const width=Math.max(800,...[...cards].map(([id,c])=>positions[id].x+c.offsetWidth+100));
      const height=Math.max(540,...[...cards].map(([id,c])=>positions[id].y+c.offsetHeight+100));
      canvas.style.width=width+'px';canvas.style.height=height+'px';canvas.style.transform=`scale(${zoom})`;spacer.style.width=width*zoom+'px';spacer.style.height=height*zoom+'px';
      svg.setAttribute('width',width);svg.setAttribute('height',height);
    }
    function point(element) {const r=element.getBoundingClientRect(),base=canvas.getBoundingClientRect();return {x:(r.left+r.width/2-base.left)/zoom,y:(r.top+r.height/2-base.top)/zoom};}
    function anchor(id,side,kind,path,outcome) {
      return anchors.get(key(id,side,kind,path,outcome)) || ports.find(p=>p.data.node===id&&p.data.side===side&&p.data.kind===kind&&(side==='in'?p.data.path.split('.')[0]===path.split('.')[0]:p.data.outcome===outcome))?.element || cards.get(id)?.querySelector('.flow-node-header');
    }
    function curve(a,b) {const bend=Math.max(65,Math.abs(b.x-a.x)*.45);return `M ${a.x} ${a.y} C ${a.x+bend} ${a.y}, ${b.x-bend} ${b.y}, ${b.x} ${b.y}`;}
    function pathElement(a,b,cls) {const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',curve(a,b));p.setAttribute('class',cls);return p;}
    function draw() {
      resize();svg.replaceChildren();
      for(const edge of edges(workflow,definition)) {
        const a=anchor(edge.from,'out',edge.kind==='data'?'data':'control',edge.sourcePath || '',edge.outcome || ''),b=anchor(edge.to,'in',edge.kind==='data'?'data':'control',edge.path || '','');
        if(!a || !b)continue;
        const p=pathElement(point(a),point(b),'flow-wire '+edge.kind),hit=pathElement(point(a),point(b),'flow-wire-hit');
        const label=edge.kind==='data'?`${edge.from}.${edge.sourcePath || '全部输出'} → ${edge.to}.${edge.path || '全部入参'}`:`${edge.from} → ${edge.to}${edge.outcome?' · '+edge.outcome:''}`;
        const tip=document.createElementNS(svg.namespaceURI,'title');tip.textContent=label;hit.append(tip);hit.setAttribute('role','button');hit.setAttribute('tabindex','0');hit.setAttribute('aria-label','查看连接 '+label);
        hit.addEventListener('click',()=>onEdge(edge));hit.addEventListener('keydown',e=>{if(e.key==='Enter')onEdge(edge);});svg.append(p,hit);
      }
    }
    function finish(source,target) {
      if(source.kind==='control'&&target.kind==='data')return onError(new Error('执行端口不能连接数据入参，请选择绿色输出端口。'));
      if(source.side!=='out'||target.side!=='in')return onError(new Error('请从输出端口连接到输入端口。'));
      onConnect({from:source.node,to:target.node,kind:target.kind,outcome:source.outcome,sourcePath:source.path,path:target.path});
    }
    function track(event,move,up) {
      const cancel=()=>{cleanup();draw();};
      const escape=e=>{if(e.key==='Escape')cancel();};
      const cleanup=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',done);window.removeEventListener('pointercancel',cancel);window.removeEventListener('keydown',escape);active=null;};
      const done=e=>{cleanup();up(e);};
      active=cleanup;window.addEventListener('pointermove',move);window.addEventListener('pointerup',done,{once:true});window.addEventListener('pointercancel',cancel,{once:true});window.addEventListener('keydown',escape);
    }
    function beginWire(event,data,element) {
      if(active)active();const start={x:event.clientX,y:event.clientY};
      const preview=pathElement(point(element),point(element),'flow-wire preview');svg.append(preview);
      track(event,e=>{const base=canvas.getBoundingClientRect();preview.setAttribute('d',curve(point(element),{x:(e.clientX-base.left)/zoom,y:(e.clientY-base.top)/zoom}));},e=>{
        preview.remove();const moved=Math.hypot(e.clientX-start.x,e.clientY-start.y)>5;
        if(!moved)return;
        suppressClick=true;setTimeout(()=>{suppressClick=false;},0);const target=document.elementFromPoint(e.clientX,e.clientY)?.closest('.flow-port');
        if(target&&canvas.contains(target))finish(data,ports[Number(target.dataset.port)].data);
        else hint.textContent='未连接：请把输出端口拖到下游节点的输入端口。';
      });
    }
    function beginMove(event,id,card,header) {
      if(event.button!==0)return;event.preventDefault();if(active)active();
      const initial={...positions[id]},start={x:event.clientX,y:event.clientY};let moved=false;
      track(event,e=>{const dx=(e.clientX-start.x)/zoom,dy=(e.clientY-start.y)/zoom;moved ||= Math.hypot(dx,dy)>4;if(!moved)return;positions[id]={x:Math.max(8,Math.min(20000,initial.x+dx)),y:Math.max(8,Math.min(20000,initial.y+dy))};card.style.left=positions[id].x+'px';card.style.top=positions[id].y+'px';draw();},()=>{if(moved)onMove(id,positions[id]);else onSelect(id);});
    }
    const controls=el('div',{class:'flow-controls'},...[[.2,'＋ 放大'],[-.2,'－ 缩小']].map(([step,label])=>el('button',{type:'button',class:'button small',onclick:()=>{zoom=Math.min(1.4,Math.max(.4,zoom+step));options.onZoom?.(zoom);draw();}},label)),el('button',{type:'button',class:'button small',onclick:()=>{options.onArrange();}},'自动排列'));
    const wrapper=el('div',{class:'flow-editor'},controls,hint,viewport);
    requestAnimationFrame(()=>{draw();viewport.scrollLeft=options.scroll?.x || 0;viewport.scrollTop=options.scroll?.y || 0;});
    viewport.addEventListener('scroll',()=>options.onScroll?.({x:viewport.scrollLeft,y:viewport.scrollTop}));
    return wrapper;
  }
  const api={fields,describe,compatible,edges,connect,disconnect,layout,createCanvas};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.WorkflowGraph=api;
})(typeof globalThis==='object'?globalThis:this);
