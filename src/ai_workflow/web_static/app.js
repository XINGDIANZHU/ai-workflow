"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const main = $("#main");
const dialog = $("#dialog");
const titles = {"methodology-execution":"方法论 · 任务执行", "learning-mini":"讲解与练习", "experience-processing":"经验提炼与维护", "existing-artifact-review":"已有产物审阅", "picture-process-review":"图像处理与审阅"};
const nodeTitles = {request:"接收需求", requirements:"明确要求", prepare:"准备依据与能力", design:"形成方案", evaluate:"评估方案", plan:"拆分任务", execute_phase1:"执行第一阶段", verify_phase1:"验证第一阶段", repair_phase1:"修正第一阶段", execute_phase2:"执行第二阶段", verify_phase2:"验证第二阶段", repair_phase2:"修正第二阶段", lesson:"讲解与出题", practice:"练习与答复", review:"产物审阅", decision:"等待决定", extract:"提炼经验", process:"处理经验", settle:"验证与结案"};
const statuses = {running:"运行中", waiting:"待答复", unclaimed:"待领取", pending:"待执行", completed:"已完成", succeeded:"已完成", failed:"失败", paused:"已暂停", cancelled:"已取消", cancelling:"取消中", skipped:"已跳过", unknown:"状态待确认", processing:"处理中", closed:"已结案", superseded:"历史执行", unavailable:"读取失败", error:"读取失败"};
const kindNames = {agent:"Agent", program:"程序", comfyui:"ComfyUI", tasks:"任务列表", subflow:"子流程"};
const reasons = {resource_busy:"资源占用中", concurrency_limit:"达到并发上限", call_limit:"达到派发次数上限", cost_limit:"达到消耗上限", unbounded_cost:"执行未声明消耗上限", other_phase:"属于其他阶段", dependency_failed:"前置节点失败", dependency_skipped:"前置节点已跳过", condition_not_selected:"条件未命中"};
const S = {page:"tasks", catalog:{workflows:[],nodes:[]}, overview:null, run:null, inspection:null, inspectionRevision:null, inspectionError:null, selectedRun:null, selectedNode:null, taskFilter:"all", purposeFilter:"all", search:"", detailTab:"artifacts", backlogFilter:"active", owner:readSetting("workbench-owner", "web-user"), editor:null, overviewBusy:false, runBusy:false, runSequence:0, renderedSnapshot:null, catalogBusy:false, connected:false, lastError:null, renderPending:false};
let toastTimer;
let serial = 0;

function readSetting(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } }
function writeSetting(key, value) { try { localStorage.setItem(key, value); } catch { /* Settings are optional. */ } }
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function json(value) { return value === undefined ? "尚无数据" : JSON.stringify(value, null, 2); }
function display(value) { return typeof value === "string" ? value : json(value); }
function el(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2), value);
    else if (["value","checked","disabled","hidden","selected"].includes(key)) element[key] = value;
    else element.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}
function button(label, fn, cls = "", disabled = false) { return el("button", {type:"button", class:`button ${cls}`, onclick:fn, disabled}, label); }
function field(label, input, hint = "", cls = "") {
  if (!input.id) input.id = `field-${++serial}`;
  return el("div", {class:`field ${cls}`}, el("label", {for:input.id}, label), input, hint && el("div", {class:"field-hint"}, hint));
}
function badge(status, override) { return el("span", {class:`badge ${statuses[status] ? status : "pending"}`}, override || statuses[status] || status || "未知"); }
function empty(title, message, action) { return el("div", {class:"empty-state"}, el("div", {class:"empty-symbol", "aria-hidden":"true"}, "◇"), el("h3", {}, title), el("p", {}, message), action); }
function dataBox(title, value, full = false) { return el("section", {class:`data-box ${full ? "full" : ""}`}, el("div", {class:"data-box-title"}, title), el("pre", {}, display(value))); }
function notice(message, type = "") { return el("div", {class:`notice ${type}`}, message); }
function workflowTitle(workflow, fallback) { return workflow?.title || titles[workflow?.id] || fallback || workflow?.id || "未命名流程"; }
function nodeTitle(id, spec, workflow) { return spec?.title || spec?.name || (workflow?.id==='methodology-execution' ? nodeTitles[id] : null) || nodeDef(spec).title || (titles[workflow?.id] ? nodeTitles[id] : null) || id; }
function inspectionNodeTitle(run, node) { return node.title && node.title !== node.node_id ? node.title : nodeTitle(node.node_id, run.workflow?.nodes?.[node.node_id], run.workflow); }
function summaryNodeTitle(summary, node) { const workflow=S.catalog.workflows.find(x=>x.workflow.id===summary.workflow_id)?.workflow || {id:summary.workflow_id};return inspectionNodeTitle({workflow},node); }
function nodeDef(spec = {}) { const ref = S.catalog.nodes.find(n => n.key === spec.node)?.definition || {}; return {...ref, ...spec}; }
function nodeChoices() { return [...S.catalog.nodes].sort((a,b)=>Number(!!b.definition.io_contract)-Number(!!a.definition.io_contract)); }
function nodeChoiceLabel(n) { return `${n.definition.title ? n.definition.title+' · ' : ''}${n.key}${n.definition.io_contract ? "" : " · 旧版宽松契约"}`; }
function nodeContractPanel(def) {
  const c=def.io_contract;
  if(!c)return notice("旧版节点：输入输出未全面校验。新流程建议选择带契约的节点。","info");
  return el("details",{class:"full-width"},el("summary",{class:"details-toggle"},`输入输出契约 · v${c.version}（只读）`),
    el("p",{class:"field-hint"},"必需输入："+(c.input.required||[]).join("、")),
    dataBox("输入：字段类型、含义及可选项",c.input),dataBox("输出：按结果分支校验",c.outputs),
    el("p",{class:"field-hint"},"等待须说明原因、问题和恢复条件；错误须提供 code。此契约随节点版本复用，不能在编排中覆盖。"));
}
function readableNodeContract(def) {
  const c=def.io_contract;
  const section=(label,schema)=>el('details',{class:'node-contract-section'},el('summary',{},label+' · '+Object.keys(schema.properties||{}).length+' 个字段'),
    schema.description&&el('p',{class:'field-hint'},schema.description),
    ...WorkflowGraph.fields(schema).map(f=>el('div',{class:'contract-field'},el('div',{},el('code',{},f.path),el('span',{class:'chip'},f.required?'必填':'可选')),el('p',{},f.schema.description || '按下列类型交接'),el('small',{},WorkflowGraph.describe(f.schema)))));
  return el('div',{class:'readable-contract'},el('h3',{},'节点功能'),el('p',{},def.description || def.instruction?.split('\n').at(-1) || '尚未提供功能说明'),
    el('details',{},el('summary',{},'完整职责与执行要求'),el('p',{class:'instruction-text'},def.instruction || '由对应执行器执行')),
    c&&section('查看入参说明',c.input),c&&Object.entries(c.outputs).map(([outcome,schema])=>section('查看出参：'+outcome,schema)),
    nodeContractPanel(def));
}
function pageIntro(eyebrow, title, description, actions) { return el("div", {class:"page-intro"}, el("div", {}, el("div", {class:"eyebrow"}, eyebrow), el("h1", {}, title), el("p", {class:"subtext"}, description)), actions); }
function toast(message, error = false) { const box = $("#toast"); box.textContent = message; box.className = `toast ${error ? "error" : ""}`; box.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => box.hidden = true, 6500); }
function showError(error, target = $("#global-alert")) { target.textContent = error.message || String(error); target.hidden = false; if (target !== $("#global-alert")) target.className = "inline-error"; }
function setConnection(ok, message) { S.connected = ok; const c = $("#connection"); c.className = `connection ${ok ? "" : "offline"}`; $("span", c).textContent = message || (ok ? "本地服务已连接" : "连接中断"); }
function isEditing() { const a = document.activeElement; return !!a && ((a.matches("input,textarea,select") && a.type !== "search") || a.isContentEditable || dialog.open); }
function canRender() { return !isEditing() && !(S.page === "workflows" && S.editor) && !$("form[data-dirty='true']", main); }

async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {method:body === undefined ? "GET" : "POST", headers:body === undefined ? {Accept:"application/json"} : {Accept:"application/json", "Content-Type":"application/json"}, body:body === undefined ? undefined : JSON.stringify(body), cache:"no-store"});
  } catch { const error = new Error("无法连接本地服务。请确认工作台服务正在运行，然后点击刷新。"); error.network = true; throw error; }
  let result;
  try { result = await response.json(); } catch { throw new Error(`服务返回无法读取的内容（${response.status}）。`); }
  if (!response.ok) {
    const detail = result.detail ? `\n${display(result.detail).slice(0,1800)}` : "";
    const error = new Error((response.status === 409 ? `操作冲突：${result.error || "状态已变化"}。请刷新并核对最新状态后重新操作；此次操作未自动重放。` : result.error || `请求失败（${response.status}）`) + detail);
    error.status = response.status; error.detail = result.detail; throw error;
  }
  return result;
}
async function loadCatalog() {
  if (S.catalogBusy) return;
  S.catalogBusy = true;
  try { S.catalog = await api("/api/catalog"); return true; }
  catch (error) { showError(error); return false; }
  finally { S.catalogBusy = false; }
}
async function loadOverview({render = true} = {}) {
  if (S.overviewBusy) return;
  S.overviewBusy = true;
  try {
    S.overview = await api("/api/overview");
    const errors = S.overview.errors || [];
    setConnection(true, errors.length ? "已连接 · 部分读取失败" : undefined);
    S.lastError = null;
    $("#global-alert").hidden = !errors.length;
    if (errors.length) showError(new Error(`部分运行读取失败：${errors.map(e => typeof e === "string" ? e : `${e.run_id || ""} ${e.error || display(e)}`).join("；")}`));
    $("#last-refresh").textContent = `最近同步 ${new Date().toLocaleTimeString("zh-CN",{hour12:false})}`;
    $("#task-count").textContent = (S.overview.runs || []).filter(r => !r.root || r.root === r.run_id).length;
    $("#backlog-count").textContent = Object.values(S.overview.backlog || {}).filter(x => x.status !== "closed").length;
    if (render && canRender()) renderPage(); else S.renderPending = true;
  } catch (error) { S.lastError = error; setConnection(false); showError(error); if (!S.overview && render && canRender()) renderPage(); }
  finally { S.overviewBusy = false; }
}
async function loadRun(id, {render = true, force = false} = {}) {
  if (!id || (S.runBusy && !force)) return;
  const requested = id;
  const sequence = ++S.runSequence;
  S.runBusy = true;
  try {
    const snapshot = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (S.selectedRun !== requested || sequence !== S.runSequence) return;
    S.run = snapshot;
    if (force || S.inspection?.run_id !== id || S.inspectionRevision !== snapshot.revision) {
      try {
        const inspection = await api(`/api/runs/${encodeURIComponent(id)}/inspection`);
        if (S.selectedRun !== requested || sequence !== S.runSequence) return;
        S.inspection = inspection; S.inspectionRevision = snapshot.revision; S.inspectionError = null;
      } catch (error) {
        if (S.selectedRun !== requested || sequence !== S.runSequence) return;
        S.inspectionError = error.message; S.inspection = null; S.inspectionRevision = null;
      }
    }
    if (!S.selectedNode || !snapshot.workflow?.nodes?.[S.selectedNode]) S.selectedNode = Object.keys(snapshot.steps || {}).find(k => ["waiting","running","unknown"].includes(snapshot.steps[k].status)) || Object.keys(snapshot.workflow?.nodes || {})[0];
    if (render && S.page === "tasks" && canRender()) { if(force || JSON.stringify(snapshot) !== S.renderedSnapshot || S.inspectionError) renderTaskDetail(); } else S.renderPending = true;
  } catch (error) { if (S.selectedRun === requested) { showError(error); if (error.network || error.status >= 500) setConnection(false); } }
  finally { if(sequence === S.runSequence) S.runBusy = false; }
}
async function refreshAll() {
  const b = $("#refresh"); b.disabled = true;
  try {
    await Promise.all([loadCatalog(), loadOverview({render:false}), S.selectedRun ? loadRun(S.selectedRun,{render:false,force:true}) : Promise.resolve()]);
    if(!(S.page === "workflows" && S.editor)&&!dialog.open){
      const drafts=[...main.querySelectorAll("form[data-draft-key]")].map(form=>({key:form.dataset.draftKey,dirty:form.dataset.dirty,fields:[...form.querySelectorAll("[data-draft-field]")].map(input=>({key:input.dataset.draftField,value:input.value,checked:input.checked}))}));
      renderPage();
      for(const draft of drafts){const form=[...main.querySelectorAll("form[data-draft-key]")].find(form=>form.dataset.draftKey===draft.key);if(!form)continue;for(const saved of draft.fields){const input=[...form.querySelectorAll("[data-draft-field]")].find(input=>input.dataset.draftField===saved.key);if(input){input.value=saved.value;if(input.type==="checkbox")input.checked=saved.checked;}}if(draft.dirty)form.dataset.dirty=draft.dirty;}
      if(drafts.some(d=>d.dirty))toast("状态已刷新，未提交的内容已保留。请核对最新状态后再提交。");
    }else toast("数据已刷新。正在填写的内容已保留。");
  }
  finally { b.disabled = false; }
}
function navigate() {
  const [path,query=""] = location.hash.slice(1).split("?"), parts = path.split("/");
  const targetNode = new URLSearchParams(query).get("node");
  const page = ["tasks","workflows","backlog"].includes(parts[0]) ? parts[0] : "tasks";
  S.page = page;
  document.querySelectorAll("[data-nav]").forEach(a => { const active = a.dataset.nav === page; a.classList.toggle("active", active); active ? a.setAttribute("aria-current","page") : a.removeAttribute("aria-current"); });
  $("#page-label").textContent = {tasks:"任务",workflows:"流程库",backlog:"经验待办"}[page];
  if (page === "tasks" && parts.length > 1) { let id; try { id = decodeURIComponent(parts.slice(1).join("/")); } catch { id = null; } if (id && S.selectedRun !== id) { S.selectedRun = id; S.run = null; S.inspection = null; S.inspectionRevision = null; S.inspectionError = null; S.selectedNode = targetNode; loadRun(id,{force:true}); } else if (id && targetNode && S.run?.workflow.nodes[targetNode]) S.selectedNode = targetNode; }
  renderPage();
}
function openRun(id) {
  if (location.hash === `#tasks/${encodeURIComponent(id)}`) { S.selectedRun = id; loadRun(id); return; }
  location.hash = `tasks/${encodeURIComponent(id)}`;
}
function renderPage() {
  S.renderPending = false;
  if (S.page === "tasks") renderTasks();
  else if (S.page === "workflows") S.editor ? renderEditor() : renderWorkflows();
  else renderBacklog();
}

function renderTasks() {
  const focusedSearch=document.activeElement?.matches("#run-search") ? {start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd} : null;
  const oldGraph=$("#run-detail .graph-scroll"), oldList=$("#run-list");
  const scroll={graphX:oldGraph?.scrollLeft||0,graphY:oldGraph?.scrollTop||0,listX:oldList?.scrollLeft||0,listY:oldList?.scrollTop||0,run:$("#run-detail")?.dataset.runId};
  const runs = S.overview?.runs || [];
  const roots = runs.filter(r => !r.root || r.root === r.run_id);
  const stats = [["▤", roots.length, "全部任务"], ["↗", roots.filter(r => !["succeeded","cancelled"].includes(r.status)).length, "未完成目标"], ["◷", runs.reduce((n,r) => n + (r.waiting || 0),0), "等待答复节点"], ["✓", roots.filter(r => r.status === "succeeded").length, "已完成任务"]];
  const search = el("input", {id:"run-search",type:"search", placeholder:"搜索任务名称或 ID", "aria-label":"搜索任务", value:S.search, oninput:e => { S.search = e.target.value; renderRunList(); }});
  const filters = el("div", {class:"filter-pills"}, ...[["all","全部"],["unfinished","未完成"],["active","进行中"],["waiting","待处理"],["succeeded","已完成"],["failed","失败"],["cancelled","已取消"]].map(([key,label]) => button(label,()=>{S.taskFilter=key; renderTasks();},`filter-pill ${S.taskFilter === key ? "active" : ""}`)));
  const purpose = el("select", {"aria-label":"任务用途",value:S.purposeFilter,onchange:e=>{S.purposeFilter=e.target.value;renderRunList();}},...[["all","全部用途"],["task","实际任务"],["test","测试 / 验收"],["unknown","历史记录 · 未标注用途"]].map(([value,label])=>el("option",{value,selected:S.purposeFilter===value},label)));
  main.replaceChildren(pageIntro("YOUR WORK IN MOTION", "任务", "从一个明确的需求开始，让每一步进展都有据可查。"),
    el("div",{class:"summary-strip"},...stats.map(([symbol,value,label]) => el("div",{class:"summary-item"},el("span",{class:"stat-icon","aria-hidden":"true"},symbol),el("div",{},el("div",{class:"summary-value"},S.overview ? value : "—"),el("div",{class:"summary-label"},label))))),
    el("div",{class:"task-layout"},el("aside",{class:"panel run-sidebar","aria-label":"任务列表"},el("div",{class:"list-filter"},search,filters,purpose,el("p",{class:"filter-hint"},"失败仍属未完成；已取消单独列出。历史用途未标注时保留显示。")),el("div",{class:"run-list",id:"run-list"})),el("div",{class:"run-detail",id:"run-detail"})));
  renderRunList(); renderTaskDetail();
  const graph=$("#run-detail .graph-scroll"),list=$("#run-list");
  if(graph&&scroll.run===S.selectedRun){graph.scrollLeft=scroll.graphX;graph.scrollTop=scroll.graphY;}
  if(list){list.scrollLeft=scroll.listX;list.scrollTop=scroll.listY;}
  if(focusedSearch){search.focus({preventScroll:true});search.setSelectionRange(focusedSearch.start,focusedSearch.end);}
}
function renderRunList() {
  const target = $("#run-list"); if (!target) return;
  let runs = [...(S.overview?.runs || [])];
  const match = S.search.toLowerCase().trim();
  runs = runs.filter(r => !match || `${r.run_id} ${r.title || ""} ${display(r.request || "")} ${r.workflow_title || titles[r.workflow_id] || ""} ${r.workflow_id || ""}`.toLowerCase().includes(match));
  if (S.purposeFilter !== "all") runs = runs.filter(r => (r.purpose || "unknown") === S.purposeFilter);
  if (S.taskFilter === "unfinished") runs = runs.filter(r => !["succeeded","cancelled"].includes(r.status));
  if (S.taskFilter === "active") runs = runs.filter(r => ["running","waiting","paused","cancelling"].includes(r.status));
  if (S.taskFilter === "waiting") runs = runs.filter(r => r.waiting || r.unclaimed || r.status === "waiting" || r.error);
  if (["succeeded","failed","cancelled"].includes(S.taskFilter)) runs = runs.filter(r => r.status === S.taskFilter);
  const orderTime=r=>{const v=r.updated_at??r.created_at;if(v==null)return 0;return typeof v==="number"?(v<1e12?v*1000:v):(Date.parse(v)||0);};
  runs.sort((a,b) => (((b.root === b.run_id || !b.root) ? 1 : 0) - ((a.root === a.run_id || !a.root) ? 1 : 0)) || orderTime(b)-orderTime(a));
  if (!runs.length) { target.replaceChildren(empty(S.overview ? "还没有匹配的任务" : "等待服务连接", S.overview ? "启动一个流程，或调整搜索和筛选条件。" : "连接成功后会显示已有任务。")); return; }
  target.replaceChildren(...runs.map(r => {
    const child = r.root && r.root !== r.run_id;
    return el("button",{type:"button",class:`run-card ${r.run_id === S.selectedRun ? "selected" : ""}`,onclick:()=>openRun(r.run_id),"aria-label":`查看${child ? "子流程" : "任务"} ${r.run_id}`,"aria-pressed":r.run_id === S.selectedRun},
      el("div",{class:"run-card-top"},el("h3",{title:r.title || r.run_id},`${child ? "↳ " : ""}${r.title || r.workflow_title || r.workflow_id || r.run_id}`),badge(r.error ? "error" : r.status)),
      el("div",{class:"run-workflow"},r.workflow_title || titles[r.workflow_id] || r.workflow_id || "流程未记录", " · ",({task:"实际任务",test:"测试 / 验收",unknown:"用途未标注"}[r.purpose] || "用途未标注")),
      el("div",{class:"run-id mono",title:r.run_id},r.run_id),
      (r.current_nodes || []).length ? el("div",{class:"run-current"},"当前：",r.current_nodes.map(n=>`${summaryNodeTitle(r,n)} · ${statuses[n.status] || n.status}${n.blocker ? `（${display(n.blocker)}）` : ""}`).join("；")) : null,
      el("div",{class:"run-card-meta"},el("span",{},child ? "子流程" : r.unclaimed ? `${r.unclaimed} 个待领取` : r.waiting ? `${r.waiting} 个待答复` : `修订 ${r.revision ?? "—"}`),el("span",{},`完成 ${r.completed ?? "—"} / ${r.total ?? "—"}`)),
      el("div",{class:"run-count-note"},`跳过 ${r.skipped ?? 0} 个节点 · 节点数量不代表工作量`),
      el("div",{class:"progress-track"},el("div",{class:"progress-fill",style:`width:${r.total ? Math.min(100,100 * (r.completed || 0)/r.total) : 0}%`})));
  }));
}
function currentRequest(run, id) { const step = run.steps?.[id]; if (!step?.execution_id) return {}; return run.requests?.[`${step.execution_id}:${step.attempt}`] || step.request || {}; }
function assignedTo(run, id) { const step = run.steps?.[id]; return step?.execution_id ? run.assignments?.[`${step.execution_id}:${step.attempt}`] || step.assignment : null; }
function visibleStatus(run, id) { const step = run.steps?.[id] || {}; const spec = run.workflow?.nodes?.[id] || {}; if (step.status === "running" && !["tasks","subflow"].includes(spec.kind) && !assignedTo(run,id)) return "unclaimed"; return step.status || "pending"; }
function inspectionFor(run) { return S.inspection?.run_id === run.run_id ? S.inspection : null; }
function inspectedNode(run,id) { return inspectionFor(run)?.nodes?.find(n=>n.node_id===id); }
function dateLabel(value) { if(value===undefined || value===null || value==="")return "未记录"; const d=new Date(typeof value==="number"&&value<1e12?value*1000:value);return Number.isNaN(d.getTime())?String(value):d.toLocaleString("zh-CN",{hour12:false}); }
function summaryText(value) {
  if(value===null||value===undefined)return "尚无回报";
  if(typeof value==="string")return value;
  if(typeof value!=="object")return String(value);
  const fields=["summary","verdict","observation","assessment","requirements","plan","description","message","result","checks","defects","unverified"];
  const labels={summary:"摘要",verdict:"验证结论",observation:"实际观察",assessment:"评估",requirements:"需求",plan:"方案",description:"说明",message:"回报",result:"结果",checks:"检查项",defects:"发现的问题",unverified:"未验证范围"};
  const lines=fields.filter(k=>value[k]!==undefined).map(k=>`${labels[k]}：${typeof value[k]==="string"?value[k]:display(value[k])}`);
  return lines.length?lines.join("\n"):display(value);
}
function readableResult(label,value) {
  return el("section",{class:"result-summary"},el("div",{class:"data-box-title"},label),el("div",{class:"result-prose"},summaryText(value)),value && typeof value==="object" ? el("details",{},el("summary",{class:"details-toggle"},"完整记录"),el("pre",{},json(value))) : null);
}
function selectNode(id) { if(!S.run?.workflow.nodes[id])return;S.selectedNode=id;renderTaskDetail();$(".node-inspector")?.scrollIntoView({block:"nearest"}); }
function nodeReference(run,source,label) {
  const title=label || inspectionNodeTitle(run,source);
  return source.run_id && source.run_id!==run.run_id
    ? el("a",{class:"run-link",href:`#tasks/${encodeURIComponent(source.run_id)}?node=${encodeURIComponent(source.node_id||"")}`},title)
    : button(title,()=>selectNode(source.node_id),"text-button");
}
function artifactStatus(a) { return ({archived:"已归档版本",legacy:"历史引用 · 当前文件",missing:"文件缺失",changed:"文件已变化",unavailable:"无法读取"}[a.status] || "待核验"); }
function artifactCards(run,items,compact=false) {
  if(!items.length)return el("p",{class:"muted small"},"尚未登记成果。节点回报中的文档、文件和交付物将在这里展示。");
  return el("div",{class:`artifact-grid ${compact?"compact":""}`},...items.map(a=>el("article",{class:"artifact-card"},
    el("div",{class:"artifact-card-top"},el("h3",{},a.title || a.path?.split(/[\\/]/).pop() || "成果"),el("span",{class:`artifact-state ${a.status||""}`},artifactStatus(a))),
    el("p",{class:"artifact-path",title:a.path},a.path || "已保存内容"),
    el("div",{class:"meta-grid"},a.node_id && button(nodeTitle(a.node_id,run.workflow.nodes[a.node_id],run.workflow),()=>selectNode(a.node_id),"text-button"),el("span",{},`尝试 ${a.attempt ?? "未记录"}`),el("span",{},dateLabel(a.at))),
    a.sha256 && el("div",{class:"artifact-version mono",title:a.sha256},`内容版本 ${a.sha256.slice(0,12)}`),
    a.origin && el("div",{class:"artifact-usage"},"原始交付：",nodeReference(run,a.origin,`${a.origin.node_id || "来源节点"} · ${a.origin.run_id}`)),
    a.status==="legacy" && el("p",{class:"field-hint"},"未保存当时的文件快照；当前内容不代表历史版本。"),
    a.detail && el("p",{class:"field-hint"},display(a.detail)),
    a.deliveries?.length ? el("details",{class:"artifact-usage"},el("summary",{class:"details-toggle"},`已交接到 ${a.deliveries.length} 次节点执行`),...a.deliveries.map(d=>el("div",{},button(nodeTitle(d.node_id,run.workflow.nodes[d.node_id],run.workflow),()=>selectNode(d.node_id),"text-button"),` · 尝试 ${d.attempt ?? "未记录"}`,el("div",{class:"artifact-version mono"},d.execution_id||"执行身份未记录")))) : a.used_by?.length ? el("div",{class:"artifact-usage"},"已交接至：",...a.used_by.map(id=>button(nodeTitle(id,run.workflow.nodes[id],run.workflow),()=>selectNode(id),"text-button"))) : null,
    el("div",{class:"button-row"},button("查阅成果",()=>openArtifact(run,a),"small",["missing","changed","unavailable"].includes(a.status))))));
}
function documentBody(text,markdown) {
  if(!markdown)return el("pre",{class:"document-text"},text);
  const article=el("article",{class:"document-prose"});let fence=false,code=[],paragraph=[];
  const flush=()=>{if(paragraph.length){article.append(el("p",{},paragraph.join("\n")));paragraph=[];}};
  for(const line of text.replace(/\r\n/g,"\n").split("\n")){
    if(/^\s*```/.test(line)){flush();if(fence){article.append(el("pre",{},code.join("\n")));code=[];}fence=!fence;continue;}
    if(fence){code.push(line);continue;}
    const heading=line.match(/^(#{1,6})\s+(.+)$/), bullet=line.match(/^\s*(?:[-*]|\d+[.)])\s+(.+)$/);
    if(heading){flush();article.append(el(`h${Math.min(6,heading[1].length+1)}`,{},heading[2]));}
    else if(bullet){flush();article.append(el("p",{class:"document-bullet"},"• ",bullet[1]));}
    else if(!line.trim())flush();else paragraph.push(line);
  }
  flush();if(code.length)article.append(el("pre",{},code.join("\n")));return article;
}
async function openArtifact(run,a) {
  let body;
  showDialog(a.title || "查看成果",`${artifactStatus(a)} · ${a.path || ""}`,target=>{body=target;body.append(el("p",{class:"muted"},"正在读取成果…"));});
  const prefix=`/api/runs/${encodeURIComponent(run.run_id)}/artifacts/${encodeURIComponent(a.artifact_id)}`;
  try{
    const data=await api(prefix);if(!dialog.open||!body.isConnected)return;
    const artifact=data.artifact||a,media=artifact.media_type||"",url=data.preview_url;
    body.replaceChildren();
    if(artifact.status==="legacy")body.append(notice("这是历史引用指向的当前文件，未保存当时快照。","warning"));
    if(typeof data.text==="string")body.append(documentBody(data.text, /markdown/.test(media)||/\.md(?:#.*)?$/i.test(artifact.path||"")));
    else if(url){
      const parsed=new URL(url,location.href);if(parsed.origin!==location.origin||!parsed.pathname.startsWith(prefix+"/"))throw new Error("成果预览地址无效。");
      if(media.startsWith("image/")&&!media.includes("svg"))body.append(el("img",{src:parsed.href,alt:artifact.title||"成果预览",class:"artifact-image"}));
      else if(media.startsWith("audio/"))body.append(el("audio",{src:parsed.href,controls:true,preload:"metadata",class:"artifact-media"}));
      else if(media.startsWith("video/"))body.append(el("video",{src:parsed.href,controls:true,preload:"metadata",class:"artifact-media"}));
      else body.append(el("p",{class:"subtext"},"此格式请下载后使用对应软件查看。"));
    }else body.append(el("p",{class:"subtext"},"此格式请下载查看。"));
    body.append(el("div",{class:"form-actions"},el("a",{class:"button small",href:prefix+"/content",download:""},"下载此版本"),button("关闭",()=>dialog.close(),"small")));
  }catch(error){if(body.isConnected)body.replaceChildren(notice(error.message,"error"));}
}
function renderNodeHistory(run,node) {
  const events=node?.history||[];
  if(!events.length)return el("p",{class:"muted small"},"尚无节点执行记录。");
  const labels={dispatch:"派发任务",claim:"领取任务",handoff:"交接负责人",reset:"返工 / 重新执行",report:"执行回报",progress:"更新进展",waiting:"等待答复",wait:"等待答复",reply:"收到答复",completed:"交付成果",error:"执行错误",skipped:"跳过节点"};
  return el("div",{class:"node-history"},...events.slice().reverse().map(h=>el("article",{class:`history-entry ${h.accepted===false?"rejected":""}`},
    el("div",{class:"history-entry-top"},el("strong",{},labels[h.kind]||labels[h.action]||labels[h.event]||h.event),h.outcome&&el("span",{class:"chip"},h.outcome),h.accepted===false&&el("span",{class:"artifact-state changed"},"未采纳的回报")),
    el("div",{class:"meta-grid"},el("span",{},dateLabel(h.at)),h.attempt!=null&&el("span",{},`第 ${h.attempt} 次尝试`),h.owner&&el("span",{},`执行者 ${h.owner}`),h.revision!=null&&el("span",{},`修订 ${h.revision}`)),
    h.event==="handoff" && el("p",{class:"result-prose"},`负责人：${h.old_worker || "未记录"} → ${h.new_worker || "未记录"}`),
    h.action==="reply" ? readableResult("当时收到的答复",h.reply) : h.kind==="progress" ? readableResult("当时的进展",h.progress ?? h.output) : h.kind==="waiting"||h.action==="wait" ? readableResult("当时等待答复的问题",h.waiting ?? h.output) : h.output!==undefined&&h.output!==null ? readableResult("执行结果",h.output) : null,
    h.error ? readableResult("问题",h.error) : null,
    h.correction && el("p",{class:"field-hint"},h.correction),
    el("details",{},el("summary",{class:"details-toggle"},"原始记录"),el("pre",{},json(h.raw || h))))));
}
function renderSources(run,node) {
  const sources=node?.sources||[];
  return el("section",{class:"source-section"},el("h4",{},"上游依据与成果交接"),sources.length?el("div",{class:"source-list"},...sources.map(source=>el("div",{class:"source-item"},
    nodeReference(run,source),
    source.run_id && source.run_id!==run.run_id && el("span",{class:"mono source-reference"},`来源任务 ${source.run_id}`),
    el("span",{class:"chip"},source.relation==="delivered"?"已交接到本次输入":"声明依赖"),
    source.reference&&el("span",{class:"mono source-reference"},source.reference),
    ...(source.artifact_ids||[]).map(id=>{const a=inspectionFor(run)?.artifacts?.find(x=>x.artifact_id===id);return a?button(a.title||"查阅引用成果",()=>openArtifact(run,a),"small"):null;}),
    ...(source.artifacts||[]).map(a=>el("div",{},button(a.title||"查阅引用成果",()=>openArtifact({run_id:source.run_id},a),"small",["missing","changed","unavailable"].includes(a.status)),a.sha256&&el("span",{class:"artifact-version mono"},` · 版本 ${a.sha256.slice(0,12)}`),a.origin&&el("div",{},"原始交付：",nodeReference(run,a.origin,`${a.origin.node_id || "来源节点"} · ${a.origin.run_id}`))))
  ))):el("p",{class:"muted small"},"没有记录上游节点引用；原始输入见下方。"),el("p",{class:"field-hint"},"已交接表示成果进入了本次输入，不等于执行者已阅读或采用。实际使用以执行回报为准。"));
}
function renderTaskDetail() {
  const target = $("#run-detail"); if (!target) return;
  const oldGraph=$(".graph-scroll",target), graphScroll={x:oldGraph?.scrollLeft||0,y:oldGraph?.scrollTop||0,run:target.dataset.runId};
  const run = S.run;
  if (!S.selectedRun) { target.replaceChildren(el("div",{class:"panel"},empty("选择一个任务，查看每一步", "左侧是本地已登记的真实运行。你也可以从流程库启动新的任务。",button("新建任务",()=>openStart(),"primary")))); return; }
  if (!run || run.run_id !== S.selectedRun) { target.replaceChildren(el("div",{class:"panel"},empty("正在读取任务", S.selectedRun))); return; }
  const revision = run.revision;
  const inspection=inspectionFor(run), summary=S.overview?.runs?.find(r=>r.run_id===run.run_id);
  const terminal = ["succeeded","failed","cancelled","cancelling"].includes(run.status);
  const waiting = Object.keys(run.steps || {}).filter(id => run.steps[id].status === "waiting");
  const unclaimed = Object.keys(run.steps || {}).filter(id => visibleStatus(run,id) === "unclaimed");
  const notices = [];
  if(S.inspectionError)notices.push(notice(`成果与履历读取失败：${S.inspectionError}。刷新可重试。`,"error"));
  if (waiting.length) notices.push(notice(`${waiting.length} 个节点正在等待答复。选择节点可查看问题并继续。`,"warning"));
  if (unclaimed.length) { const agentCount = unclaimed.filter(id=>run.workflow.nodes[id].kind === "agent").length; notices.push(notice(agentCount ? `${agentCount} 个 Agent 节点已派发、尚未领取。需要活动的 Agent 通过现有入口领取；本页不会自动唤起 Codex。` : `${unclaimed.length} 个节点已派发、等待执行器领取。`,"info")); }
  for (const [id,block] of Object.entries(run.admission || {})) if(run.steps[id]?.status==="pending") notices.push(notice(`${nodeTitle(id,run.workflow.nodes[id],run.workflow)}：${reasons[block.reason] || block.reason}${block.resource ? ` · ${block.resource}` : ""}`,"warning"));
  if (run.error || run.dispatch_error) notices.push(notice(display(run.error || run.dispatch_error),"error"));
  if (run.cancel_requested && run.status !== "cancelled") notices.push(notice("取消请求已记录。仍在执行的工作需要执行器确认停止，当前尚未完成取消。","warning"));
  const graphPanel = el("section",{class:"panel"},
    el("div",{class:"detail-heading"},el("div",{},el("div",{class:"detail-title-row"},el("h2",{},inspection?.title || summary?.title || workflowTitle(run.workflow)),badge(run.status)),el("div",{class:"detail-meta"},el("span",{},inspection?.workflow_title || workflowTitle(run.workflow)),el("span",{class:"mono"},run.run_id),el("span",{},`修订 ${revision}`),el("span",{},`流程 v${run.workflow.version}`),run.parent?.run_id && el("a",{href:`#tasks/${encodeURIComponent(run.parent.run_id)}`},"↰ 返回父任务")),el("div",{class:"detail-meta"},el("span",{},`开始 ${dateLabel(inspection?.created_at)}`),el("span",{},`最近更新 ${dateLabel(inspection?.updated_at)}`),inspection?.phase!=null&&el("span",{},`阶段 ${inspection.phase}`)))),
    el("div",{class:"task-request"},el("strong",{},"任务需求"),el("p",{},summaryText(inspection?.request ?? run.input?.request ?? run.input))),
    el("div",{class:"task-request"},el("strong",{},"任务目录"),el("p",{class:"mono"},run.workspace?.path || "旧任务未分配目录；保留原工作位置"),run.workspace && el("p",{class:"field-hint"},"领取和换对话续接使用此目录。成果回报后可在下方查看固定版本。")),
    summary?.current_nodes?.length ? el("div",{class:"current-work"},el("strong",{},"当前工作"),...summary.current_nodes.map(n=>el("div",{},button(inspectionNodeTitle(run,n),()=>selectNode(n.node_id),"text-button"),badge(n.status),n.owner&&el("span",{},`执行者 ${n.owner}`),n.progress&&el("span",{},summaryText(n.progress)),n.blocker&&el("span",{class:"current-blocker"},`阻塞：${display(n.blocker)}`)))) : null,
    el("div",{class:"detail-toolbar"},run.status === "paused" ? button("继续执行",()=>controlRun(run,"resume",{}),"primary small") : button("暂停",()=>controlRun(run,"pause",{}),"small",terminal),button("取消任务",()=>openCancel(run),"small danger",terminal),el("a",{class:"button small push",href:`/api/runs/${encodeURIComponent(run.run_id)}/export`,download:""},"导出快照")),
    notices.length ? el("div",{class:"detail-notices"},...notices) : null,
    el("div",{class:"graph-label"},el("span",{},"执行路径"),el("div",{class:"graph-legend"},el("span",{},el("i",{class:"legend-line"}),"依赖"),el("span",{},el("i",{class:"legend-line route"}),"条件回退"),el("span",{},"点击节点查看详情"))),
    renderGraph(run.workflow,run,S.selectedNode,id => { S.selectedNode=id; renderTaskDetail(); }),
    renderNodeInspector(run));
  const tabs = [["artifacts","成果文档"],["assignments","任务分配"],["history","节点履历"],["messages","协作消息"],["inputs","输入与预算"]];
  const lower = el("section",{class:"panel below-panel"},el("div",{class:"tabs",role:"tablist","aria-label":"任务详情"},...tabs.map(([key,label])=>el("button",{type:"button",class:`tab ${S.detailTab === key ? "active" : ""}`,role:"tab","aria-selected":S.detailTab === key,onclick:()=>{S.detailTab=key;renderTaskDetail();}},label))),el("div",{class:"panel-body"},renderRunTab(run)));
  target.replaceChildren(graphPanel,lower);
  target.dataset.runId=run.run_id;
  S.renderedSnapshot=JSON.stringify(run);
  const newGraph=$(".graph-scroll",target);if(newGraph&&graphScroll.run===run.run_id){newGraph.scrollLeft=graphScroll.x;newGraph.scrollTop=graphScroll.y;}
  renderRunList();
}
function renderNodeInspector(run) {
  const id = S.selectedNode, spec = run.workflow.nodes[id];
  if (!spec) return el("div");
  const step = run.steps[id] || {}, request = currentRequest(run,id), owner = assignedTo(run,id), inspected=inspectedNode(run,id);
  const content = el("div",{class:"node-inspector"},el("div",{class:"inspector-title"},el("h3",{},nodeTitle(id,spec,run.workflow)),el("span",{class:"chip mono"},id),badge(visibleStatus(run,id))),
    el("div",{class:"meta-grid"},el("span",{},"类型 ",el("strong",{},kindNames[spec.kind] || spec.kind)),el("span",{},"尝试 ",el("strong",{},step.attempt || 0)),el("span",{},"执行者 ",el("strong",{},owner || (["tasks","subflow"].includes(spec.kind) ? "子流程调度" : "尚未领取"))),step.outcome && el("span",{},"结果 ",el("strong",{},step.outcome))),
    request.child_run || step.output?.child_run ? el("p",{class:"subtext"},el("a",{class:"run-link",href:`#tasks/${encodeURIComponent(request.child_run || step.output.child_run)}`},`↗ 查看子流程 ${request.child_run || step.output.child_run}`)) : null);
  if (step.skip_reason) content.append(notice(reasons[step.skip_reason] || step.skip_reason));
  if (step.status === "waiting") content.append(renderReply(run,id,step));
  const purpose=request.input?.purpose || spec.input?.purpose || inspected?.instruction || spec.instruction;
  if(purpose)content.append(el("details",{class:"node-purpose"},el("summary",{class:"details-toggle"},"节点职责与执行要求"),el("p",{class:"result-prose"},purpose)));
  content.append(el("div",{class:"meta-grid"},el("span",{},`开始 ${dateLabel(inspected?.started_at)}`),el("span",{},`结束 ${dateLabel(inspected?.completed_at)}`)),renderSources(run,inspected));
  const ended=["completed","skipped","cancelled"].includes(step.status);
  content.append(el("div",{class:"inspector-grid"},readableResult("已回报成果",step.output),step.progress && readableResult(ended?"最近回报进展":"当前执行进展",step.progress),step.error && readableResult(["failed","unknown"].includes(step.status)?"执行问题":"历史问题",step.error)));
  const artifacts=inspectionFor(run)?.artifacts || [],ids=new Set(inspected?.artifacts || []);
  content.append(el("section",{class:"node-artifacts"},el("h4",{},"此节点的成果"),artifactCards(run,artifacts.filter(a=>ids.has(a.artifact_id)||a.node_id===id),true)));
  content.append(el("details",{},el("summary",{class:"details-toggle"},"查看节点执行与返工历史"),renderNodeHistory(run,inspected)));
  content.append(el("details",{},el("summary",{class:"details-toggle"},request.input !== undefined ? "查看本次实际接收的完整输入" : "查看输入定义 · 尚未派发"),dataBox("输入",request.input ?? spec.input ?? {})));
  if (spec.instruction || Object.keys(spec.when || {}).length || Object.keys(spec.routes || {}).length) content.append(el("details",{},el("summary",{class:"details-toggle"},"查看节点指令、条件与回退定义"),dataBox("节点完整定义",spec)));
  return content;
}
function renderReply(run,id,step) {
  const text = el("textarea",{placeholder:"填写对这个问题的答复…",required:true,"data-draft-field":"text"});
  const asJSON = el("input",{type:"checkbox","data-draft-field":"json"});
  const error = el("div",{hidden:true});
  const form = el("form",{class:"form-section","data-draft-key":`reply:${run.run_id}:${id}`,onsubmit:async e=>{
    e.preventDefault(); error.hidden=true;
    try { const reply = asJSON.checked ? JSON.parse(text.value) : text.value; if (reply === "") throw new Error("请填写答复。"); const ok = await controlRun(run,"reply",{node_id:id,reply},error); if(ok) toast("答复已提交。节点将沿原执行身份继续。"); } catch(err) { showError(err,error); }
  }},dataBox("等待中的问题",step.waiting || {}),field("你的答复",text),el("label",{class:"check-chip"},asJSON,"将答复解析为 JSON"),error,el("div",{class:"button-row"},el("button",{type:"submit",class:"button primary small"},"提交答复")));
  return el("div",{class:"stack",style:"margin-bottom:18px"},form);
}
function renderRunTab(run) {
  const inspection=inspectionFor(run);
  if (["artifacts","assignments","history"].includes(S.detailTab) && !inspection) return notice(S.inspectionError || "正在读取成果与履历…",S.inspectionError?"error":"");
  if (S.detailTab === "artifacts") return el("div",{class:"stack"},el("p",{class:"subtext"},"需求、方案、计划和交付物按来源节点汇总。同一成果可从节点详情或此处查阅，内容版本随回报保留。"),artifactCards(run,inspection.artifacts||[]));
  if (S.detailTab === "assignments") {
    const assignments=inspection.assignments||[],children=inspection.children||[];
    return el("div",{class:"stack"},el("p",{class:"subtext"},"查看每项任务交给谁、执行到哪一步，以及分派到子流程的工作。历史执行身份保持保留。"),
      assignments.length?el("div",{class:"assignment-grid"},...assignments.map(a=>el("article",{class:"assignment-card"},el("div",{class:"artifact-card-top"},button(inspectionNodeTitle(run,a),()=>selectNode(a.node_id),"text-button"),badge(a.status)),el("p",{},`执行者：${a.worker_id || (a.child_run?"子流程调度":"尚未领取")}`),el("div",{class:"meta-grid"},el("span",{},`第 ${a.attempt ?? "未记录"} 次尝试`),el("span",{class:"mono"},a.execution_id||"尚未派发")),a.history?.length?el("details",{},el("summary",{class:"details-toggle"},"领取与负责人交接"),...a.history.map(h=>el("p",{},h.event==="handoff"?`${h.old_worker || "未记录"} → ${h.new_worker || "未记录"}`:`领取：${h.owner || "未记录"}`,` · ${dateLabel(h.at)}`))):null,a.child_run&&el("a",{class:"run-link",href:`#tasks/${encodeURIComponent(a.child_run)}`},"查看子流程任务与成果")))):el("p",{class:"muted small"},"尚未派发任务或没有保存分配信息。"),
      children.length?el("section",{class:"source-section"},el("h4",{},"子流程"),...children.map(c=>el("p",{},el("a",{class:"run-link",href:`#tasks/${encodeURIComponent(c.run_id)}`},`${nodeTitle(c.node_id,run.workflow.nodes[c.node_id],run.workflow)} → ${c.run_id}`)))):null);
  }
  if (S.detailTab === "inputs") {
    const budget = S.overview?.budgets?.[run.root || run.run_id];
    return el("div",{class:"stack"},budget && el("div",{class:"budget-grid"},...[["已派发 / 上限",`${budget.calls} / ${budget.max_calls}`],["记账消耗上界",budget.spent_bound ?? 0],["并发上限",budget.max_parallel ?? "—"]].map(([l,v])=>el("div",{class:"budget-cell"},el("span",{},l),el("strong",{},v)))),dataBox("任务输入",run.input),dataBox("运行限制",run.limits),dataBox("流程变量",run.variables),Object.keys(S.overview?.resources || {}).length ? dataBox("当前资源占用",S.overview.resources) : null);
  }
  if (S.detailTab === "history") return el("div",{class:"stack"},el("p",{class:"subtext"},"按节点查看实际回报、交付成果与历次返工。旧记录未保存的时间显示为“未记录”。"),...(inspection.nodes||[]).map(n=>el("details",{class:"node-ledger",open:n.node_id===S.selectedNode},el("summary",{},nodeTitle(n.node_id,run.workflow.nodes[n.node_id],run.workflow)," · ",statuses[n.status]||n.status,` · ${(n.history||[]).length} 条记录`),renderNodeHistory(run,n))),el("details",{},el("summary",{class:"details-toggle"},"完整流程原始记录"),el("pre",{},json(run.history||[]))));
  const author = el("input",{value:S.owner,required:true,"data-draft-field":"author"});
  const text = el("textarea",{placeholder:"记录补充要求、发现或交接信息…",required:true,"data-draft-field":"text"});
  const error = el("div",{hidden:true});
  const form = el("form",{"data-draft-key":`message:${run.run_id}`,onsubmit:async e=>{e.preventDefault();error.hidden=true;const ok = await controlRun(run,"message",{author:author.value.trim(),text:text.value},error);if(ok)toast("消息已记录。");}},field("消息",text),el("div",{class:"form-actions"},field("署名",author),el("button",{type:"submit",class:"button primary"},"发送消息")),error);
  return el("div",{class:"stack"},el("div",{class:"message-list"},...(run.messages?.length ? run.messages.map(m=>el("article",{class:"message"},el("div",{class:"message-top"},el("span",{class:"message-author"},m.author || "未知作者"),m.related && el("span",{},`关联 ${m.related}`)),el("div",{class:"message-text"},display(m.text)))) : [el("p",{class:"muted small"},"还没有协作消息。") ])),form);
}
async function controlRun(run,action,data,errorTarget) {
  try {
    const result = await api(`/api/runs/${encodeURIComponent(run.run_id)}/control`,{action,data:{...data,expected_revision:run.revision}});
    if (result.accepted === false) throw new Error(result.reason || "操作未被接受。");
    if(dialog.open) dialog.close();
    await loadRun(run.run_id,{render:false,force:true});
    if(S.page === "tasks") renderTaskDetail();
    if(!["message","reply"].includes(action)) toast({pause:"任务已暂停。已派发工作以执行器实际状态为准。",resume:"任务已继续。",cancel:"取消请求已提交，请以运行状态为准。"}[action] || "操作已提交。");
    loadOverview({render:false});
    return true;
  } catch(error) { errorTarget ? showError(error,errorTarget) : toast(error.message,true); if(error.status === 409) showError(error); return false; }
}
function openCancel(run) {
  showDialog("取消这个任务", "取消后将停止后续派发，并请求正在执行的工作停止。", body => {
    body.append(notice(`任务：${run.run_id} · 当前修订 ${run.revision}`,"warning"));
    const err=el("div",{hidden:true});
    body.append(err,el("div",{class:"form-actions"},button("返回",()=>dialog.close()),button("确认取消任务",()=>controlRun(run,"cancel",{},err),"danger")));
  });
}

function renderGraph(workflow, run, selected, onSelect) {
  const nodes = workflow.nodes || {}, ids = Object.keys(nodes), levels = new Map();
  function level(id, trail = new Set()) {
    if(levels.has(id)) return levels.get(id);
    if(trail.has(id) || !nodes[id]) return 0;
    const next=new Set(trail);next.add(id);
    const value=Math.min(ids.length, Math.max(-1,...(Array.isArray(nodes[id].depends) ? nodes[id].depends : []).filter(dep=>nodes[dep]).map(dep=>level(dep,next)))+1);
    levels.set(id,value);return value;
  }
  ids.forEach(id=>level(id));
  const groups=new Map(); ids.forEach(id=>{const l=levels.get(id);if(!groups.has(l))groups.set(l,[]);groups.get(l).push(id);});
  const positions=new Map(); const rowGap=125,colGap=260,pad=29;
  const maxRows=Math.max(1,...[...groups.values()].map(x=>x.length));
  const width=Math.max(270,(Math.max(0,...levels.values())+1)*colGap+pad*2-58),height=Math.max(190,maxRows*rowGap+pad*2+16);
  for(const [l,group] of groups) group.forEach((id,i)=>positions.set(id,{x:pad+l*colGap,y:pad+i*rowGap+(maxRows-group.length)*rowGap/2}));
  const canvas=el("div",{class:"graph-canvas",style:`width:${width}px;height:${height}px`});
  const ns="http://www.w3.org/2000/svg", svg=document.createElementNS(ns,"svg"); svg.setAttribute("width",width);svg.setAttribute("height",height);svg.setAttribute("aria-hidden","true");
  const markerId=`arrow-${++serial}`, routeId=`route-${serial}`;
  const defs=document.createElementNS(ns,"defs");
  for(const [id,color] of [[markerId,"#b8c8ac"],[routeId,"#c6ae7e"]]) {const marker=document.createElementNS(ns,"marker");for(const [k,v] of Object.entries({id,viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:5,markerHeight:5,orient:"auto-start-reverse"}))marker.setAttribute(k,v);const p=document.createElementNS(ns,"path");p.setAttribute("d","M 0 0 L 10 5 L 0 10 z");p.setAttribute("fill",color);marker.append(p);defs.append(marker);}
  svg.append(defs);
  function line(from,to,route=false,label="") {
    const a=positions.get(from), b=positions.get(to);if(!a||!b)return;
    const path=document.createElementNS(ns,"path");
    let d;
    if(route){const sy=a.y+94,ty=b.y+94,bottom=Math.max(sy,ty)+18;d=`M ${a.x+150} ${sy} C ${a.x+150} ${bottom+15},${b.x+110} ${bottom+15},${b.x+110} ${ty}`;}
    else {const sx=a.x+202,sy=a.y+47,tx=b.x,ty=b.y+47;d=`M ${sx} ${sy} C ${sx+27} ${sy},${tx-27} ${ty},${tx} ${ty}`;}
    for(const [k,v] of Object.entries({d,fill:"none",stroke:route?"#c6ae7e":"#b8c8ac","stroke-width":1.3,"marker-end":`url(#${route?routeId:markerId})`}))path.setAttribute(k,v);
    if(route)path.setAttribute("stroke-dasharray","4 4");
    if(label){const title=document.createElementNS(ns,"title");title.textContent=label;path.append(title);}svg.append(path);
  }
  ids.forEach(id=>{const spec=nodes[id];(Array.isArray(spec.depends)?spec.depends:[]).forEach(dep=>line(dep,id,false,spec.when?.[dep] ? `${dep} → ${id}，条件：${display(spec.when[dep])}` : `${dep} → ${id}`));Object.entries(spec.routes||{}).forEach(([outcome,route])=>[...(route.reset||[]),...(route.on_limit?.reset||[])].forEach(target=>line(id,target,true,`${id}：${outcome} → ${target}`)));});
  canvas.append(svg);
  ids.forEach(id=>{const spec=nodes[id],position=positions.get(id),def=nodeDef(spec), status=run ? visibleStatus(run,id) : null;
    canvas.append(el("button",{type:"button",class:`graph-node ${selected===id?"selected":""}`,style:`left:${position.x}px;top:${position.y}px`,onclick:()=>onSelect(id),"aria-label":`${nodeTitle(id,spec,workflow)}，${status ? statuses[status] || status : kindNames[def.kind] || "节点"}`,"aria-pressed":selected===id},el("div",{class:"node-top"},el("span",{class:"node-symbol","aria-hidden":"true"},["tasks","subflow"].includes(def.kind)?"◇":"▦"),status ? badge(status) : el("span",{class:"chip"},kindNames[def.kind]||def.kind||"节点")),el("div",{class:"node-title"},nodeTitle(id,spec,workflow)),el("div",{class:"node-id mono"},id)));
  });
  if(!ids.length)canvas.append(empty("添加第一个节点","选择一个已登记能力，或从空白节点开始。"));
  return el("div",{class:"graph-scroll",tabindex:0,"aria-label":"节点依赖图，可水平滚动"},canvas);
}

function renderWorkflows() {
  const workflows=S.catalog.workflows || [];
  const makeCard=entry=>{const wf=entry.workflow;return el("article",{class:"panel workflow-card"},el("div",{class:"workflow-card-top"},el("div",{class:"workflow-symbol","aria-hidden":"true"},"◇"),el("span",{class:"chip"},entry.editable?"自定义":"内置模板")),el("h2",{},workflowTitle(wf,entry.title)),el("p",{class:"subtext"},wf.description || entry.description || "组合可复用节点，将需求变成可追踪的执行过程。"),el("div",{class:"workflow-card-footer"},el("span",{},`${Object.keys(wf.nodes||{}).length} 个节点 · v${wf.version}`),el("div",{class:"button-row"},button(entry.editable?"编辑":"查看与编排",()=>openEditor(entry),"small"),button("启动",()=>openStart(entry),"small primary"))));};
  const custom=workflows.filter(w=>w.editable),builtin=workflows.filter(w=>!w.editable);
  main.replaceChildren(pageIntro("REUSABLE BY DESIGN","流程库","把有效的方法整理成流程，在需要时明确启动。",button("＋ 创建流程",()=>openEditor(),"primary")),el("div",{class:"section-caption"},el("h2",{},"内置流程"),el("span",{},`${builtin.length} 个模板`)),el("div",{class:"workflow-grid"},...builtin.map(makeCard)),el("div",{class:"section-caption"},el("h2",{},"我的流程"),el("span",{},`${custom.length} 个自定义流程`)),custom.length?el("div",{class:"workflow-grid"},...custom.map(makeCard)):el("div",{class:"panel"},empty("保存你的第一套流程","复用一个模板，或从空白流程开始编排。保存后会出现在这里。",button("创建流程",()=>openEditor()))));
}
function showDialog(title,subtitle,build) {
  dialog.replaceChildren(el("div",{class:"dialog-header"},el("div",{},el("div",{class:"eyebrow"},"WORKSPACE ACTION"),el("h2",{id:"dialog-title"},title),subtitle&&el("p",{class:"subtext"},subtitle)),button("×",()=>dialog.close(),"icon-button")));
  $(".icon-button",dialog).setAttribute("aria-label","关闭对话框");
  const body=el("div",{class:"dialog-body"});dialog.append(body);build(body);if(!dialog.open)dialog.showModal();
}
function requiredInputKeys(workflow) {
  const keys=new Map();
  function walk(value){if(Array.isArray(value))return value.forEach(walk);if(!value||typeof value!=="object")return;if(typeof value.$ref==="string"&&value.$ref.startsWith("input.")){const key=value.$ref.split(".")[1];keys.set(key,(keys.get(key)||false)||!("default"in value));}Object.values(value).forEach(walk);}
  walk(workflow.nodes);return keys;
}
function openStart(selected) {
  if(!S.catalog.workflows.length&&!selected){toast("流程库尚未加载，请先刷新连接。",true);return;}
  let entry=selected || S.catalog.workflows[0];
  const available=selected&&!S.catalog.workflows.includes(selected)?[selected,...S.catalog.workflows]:S.catalog.workflows;
  showDialog("启动一个新任务","选择流程、填写输入，点击启动后才会创建运行。",body=>{
    const select=el("select",{},...available.map((w,i)=>el("option",{value:i,selected:w===entry},workflowTitle(w.workflow,w.title))));
    const id=el("input",{value:`task-${new Date().toISOString().slice(0,10).replaceAll("-","")}-${Math.random().toString(36).slice(2,7)}`,required:true,pattern:"[A-Za-z0-9_-]+"});
    const taskTitle=el("input",{placeholder:"可选，例如：整理课程第三章的学习笔记"});
    const purpose=el("select",{},el("option",{value:"task"},"实际任务"),el("option",{value:"test"},"测试 / 验收"));
    const input=el("textarea",{class:"code-input",spellcheck:false,required:true});
    const friendly=el("div",{class:"form-section"});
    const inputHint=el("div",{class:"field-hint"});
    let friendlyControls={};
    const labels={request:"这次要完成什么",topic:"学习主题",criteria:"验收标准",artifacts:"已有产物或路径",source:"需求来源",authorization:"授权与边界",goal:"目标",description:"描述"};
    function configure(){
      entry=available[Number(select.value)];
      const keys=requiredInputKeys(entry.workflow), initial={};friendlyControls={};friendly.replaceChildren();
      for(const [key,required]of keys){if(required)initial[key]=key==="artifacts"?[]:"";if(labels[key]){const control=el("textarea",{rows:2,placeholder:labels[key],"data-input-key":key});friendlyControls[key]=control;control.addEventListener("input",()=>{try{const parsed=JSON.parse(input.value);if(parsed===null||typeof parsed!=="object"||Array.isArray(parsed))return;parsed[key]=key==="artifacts"?control.value.split(/\r?\n/).filter(Boolean):control.value;input.value=json(parsed);}catch{/* Preserve invalid JSON for correction. */}});friendly.append(field(labels[key]+(required?" *":""),control,key==="artifacts"?"每行一个路径或产物引用；复杂结构请在 JSON 中填写。":""));}}
       initial._meta={purpose:purpose.value};if(taskTitle.value.trim())initial.title=taskTitle.value.trim();input.value=json(initial);
      inputHint.textContent=keys.size?`流程引用字段：${[...keys].map(([k,required])=>`${k}${required?"（必需）":"（可选）"}`).join("、")}。最终以输入 JSON 为准。`:"这个流程未声明顶层输入引用，可按需填写 JSON。";
    }
    select.addEventListener("change",configure);
    taskTitle.addEventListener("input",()=>{try{const value=JSON.parse(input.value);value.title=taskTitle.value.trim();input.value=json(value);}catch{}});
    purpose.addEventListener("change",()=>{try{const value=JSON.parse(input.value);value._meta={...(value._meta||{}),purpose:purpose.value};input.value=json(value);}catch{}});
    input.addEventListener("input",()=>{try{const value=JSON.parse(input.value);taskTitle.value=typeof value?.title==="string"?value.title:"";purpose.value=["task","test"].includes(value?._meta?.purpose)?value._meta.purpose:"task";for(const[k,c]of Object.entries(friendlyControls)){const v=value?.[k];c.value=Array.isArray(v)?v.join("\n"):typeof v==="string"?v:"";}}catch{}});
    const parallel=el("input",{type:"number",min:1,step:1,value:4,required:true});
    const calls=el("input",{type:"number",min:0,step:1,value:100,required:true});
    const attempts=el("input",{type:"number",min:1,step:1,value:3,required:true});
    const cost=el("input",{type:"number",min:0,step:"any",placeholder:"留空表示不设置"});
    const limits=el("textarea",{class:"code-input",value:"{}",spellcheck:false});
    const err=el("div",{hidden:true});
    const submit=el("button",{type:"submit",class:"button primary"},"启动任务");
    const form=el("form",{class:"form-section",onsubmit:async e=>{e.preventDefault();err.hidden=true;submit.disabled=true;try{
      const inputs=JSON.parse(input.value), extra=JSON.parse(limits.value);
      if(!inputs||typeof inputs!=="object"||Array.isArray(inputs))throw new Error("任务输入必须是 JSON 对象。");
      if(!extra||typeof extra!=="object"||Array.isArray(extra))throw new Error("高级限制必须是 JSON 对象。");
      const policy={...extra,max_parallel:Number(parallel.value),max_calls:Number(calls.value),max_attempts:Number(attempts.value)};if(cost.value!=="")policy.max_cost=Number(cost.value);
      await api("/api/runs",{run_id:id.value.trim(),workflow:entry.workflow,input:inputs,limits:policy});
      dialog.close();toast("任务已创建。Agent 节点需要活动的 Agent 领取。");S.editor=null;openRun(id.value.trim());loadOverview({render:false});
    }catch(error){showError(error,err);}finally{submit.disabled=false;}}},
      field("选择流程",select),field("任务名称",taskTitle,"留空时从需求生成名称。"),field("用途",purpose,"仅按明确标注区分，不根据任务名称猜测。"),field("运行 ID",id,"独立运行使用不同 ID，只能包含字母、数字、短横线和下划线。"),friendly,field("输入 JSON",input),inputHint,
      el("div",{class:"field-row limits-grid"},field("最大并发",parallel),field("最大派发次数",calls),field("每节点尝试上限",attempts)),
      el("details",{},el("summary",{},"更多运行限制"),field("消耗上限",cost,"消耗按节点声明的上界记账。未声明上界的节点可能被阻止派发。"),field("高级限制 JSON",limits,"填写 phase 等其他限制；上方数值优先。")),
      notice("网页负责查看和控制流程。Agent 节点需要活动的 Agent 通过现有入口领取。","info"),err,el("div",{class:"form-actions"},button("返回",()=>dialog.close()),submit));
    body.append(form);configure();
  });
}

function openEditor(entry) {
  S.editor={workflow:entry?clone(entry.workflow):{id:"my-workflow",version:"1",title:"我的新流程",description:"",nodes:{}},sourceKey:entry?.key || null,hash:entry?.hash || null,saveKey:entry?.editable?entry.key:`custom/${entry?.workflow.id || "my-workflow"}`,selected:Object.keys(entry?.workflow.nodes||{})[0]||null,mode:"visual",advanced:null,dirty:false};
  S.page="workflows";if(location.hash!=="#workflows")location.hash="workflows";else renderEditor();
}
function editorError(err) { const target=$("#editor-feedback");if(target)showError(err,target);else toast(err.message,true); }
function syncEditor() {
  const ed=S.editor;if(!ed)return;
  if(ed.mode==="advanced"){
    const candidate=JSON.parse($("#workflow-json")?.value ?? ed.advanced ?? json(ed.workflow));
    if(!candidate||typeof candidate!=="object"||Array.isArray(candidate)||!candidate.nodes||typeof candidate.nodes!=="object"||Array.isArray(candidate.nodes))throw new Error("完整流程必须是含 nodes 对象的 JSON 对象。");
    for(const [id,node] of Object.entries(candidate.nodes)) {
      if(!node||typeof node!=="object"||Array.isArray(node))throw new Error(`节点 ${id} 必须是 JSON 对象。`);
      if(node.depends!==undefined&&(!Array.isArray(node.depends)||node.depends.some(x=>typeof x!=="string")))throw new Error(`节点 ${id} 的 depends 必须是字符串数组。`);
      for(const key of ["when","routes"])if(node[key]!==undefined&&(!node[key]||typeof node[key]!=="object"||Array.isArray(node[key])))throw new Error(`节点 ${id} 的 ${key} 必须是 JSON 对象。`);
      for(const route of Object.values(node.routes||{}))if(!route||typeof route!=="object"||!Array.isArray(route.reset)||(route.on_limit&&!Array.isArray(route.on_limit.reset)))throw new Error(`节点 ${id} 的回退规则必须包含 reset 数组。`);
    }
    ed.workflow=candidate;ed.advanced=json(candidate);
    if(!ed.workflow.nodes[ed.selected])ed.selected=Object.keys(ed.workflow.nodes)[0]||null;
  }else{
    const input=$("#node-input");if(input&&ed.selected)ed.workflow.nodes[ed.selected].input=JSON.parse(input.value);
    else if(ed.selected){
      const spec=ed.workflow.nodes[ed.selected];
      for(const box of main.querySelectorAll('[data-param]')){
        const format=box.dataset.format,value=box.value;
        if(value===''){delete spec.input[box.dataset.param];continue;}
        spec.input[box.dataset.param]=format==='json'?JSON.parse(value):format==='task'?{$ref:'input.'+value}:value;
      }
    }
  }
}
function safeEditorAction(fn) {try{syncEditor();fn();}catch(error){editorError(new Error(`请先修正 JSON：${error.message}`));}}
function graphAction(fn){try{syncEditor();fn();}catch(error){editorError(error);toast(error.message,true);}}
function connectEditor(link){graphAction(()=>{const ed=S.editor;ed.workflow=WorkflowGraph.connect(ed.workflow,nodeDef,link);ed.dirty=true;ed.selected=link.to;ed.edge=null;renderEditor();toast('已连接。完整字段约束会在校验和保存时检查。');});}
function removeEditorEdge(edge){graphAction(()=>{const ed=S.editor;ed.workflow=WorkflowGraph.disconnect(ed.workflow,edge);ed.dirty=true;ed.edge=null;renderEditor();toast(edge.kind==='data'?'已断开参数连接，执行依赖保留。':'已删除执行依赖及对应分支条件。');});}
function renderFlowCanvas(){
  const ed=S.editor;
  return WorkflowGraph.createCanvas({workflow:ed.workflow,definition:nodeDef,title:(id,spec)=>nodeTitle(id,spec,ed.workflow),selected:ed.selected,el,zoom:ed.zoom||.8,scroll:ed.scroll,
    onSelect:id=>graphAction(()=>{ed.selected=id;ed.edge=null;renderEditor();}),onConnect:connectEditor,
    onEdge:edge=>graphAction(()=>{ed.edge=edge;renderEditor();}),onError:error=>{editorError(error);toast(error.message,true);},
    onMove:(id,point)=>{ed.workflow.ui={...ed.workflow.ui,positions:{...ed.workflow.ui?.positions,[id]:point}};ed.dirty=true;},
    onZoom:zoom=>{ed.zoom=zoom;},onScroll:scroll=>{ed.scroll=scroll;},
    onArrange:()=>graphAction(()=>{ed.workflow.ui={...ed.workflow.ui,positions:WorkflowGraph.layout(ed.workflow)};ed.scroll={x:0,y:0};ed.dirty=true;renderEditor();})});
}
function renderParameterInputs(spec,def){
  const ed=S.editor;
  const raw=()=>field('节点输入 JSON',el('textarea',{id:'node-input',class:'code-input',value:json(spec.input??{}),spellcheck:false,oninput:()=>{ed.dirty=true;}}),'保留完整参数。字段连线由画布写入引用。','full-width');
  if(spec.input?.$ref&&ed.inputMode!=='json')return el('div',{class:'parameter-fields'},el('h3',{},'入参来源'),el('p',{class:'field-hint'},'全部入参由以下来源整体提供，字段要求见上方入参说明。'),el('code',{},spec.input.$ref),
    button('解除整体绑定，逐项填写',()=>graphAction(()=>{spec.input={};ed.inputMode='form';ed.dirty=true;renderEditor();}),'small'),button('高级：输入 JSON',()=>graphAction(()=>{ed.inputMode='json';renderEditor();}),'small'));
  if(!def.io_contract||ed.inputMode==='json'||spec.input?.$ref||!spec.input||Array.isArray(spec.input)||typeof spec.input!=='object')return el('div',{},button('使用参数表单',()=>graphAction(()=>{ed.inputMode='form';renderEditor();}),'small'),raw());
  return el('div',{class:'parameter-fields'},el('h3',{},'填写入参'),...Object.entries(def.io_contract.input.properties||{}).map(([name,schema])=>{
    const value=spec.input[name],required=def.io_contract.input.required?.includes(name),bound=value?.$ref;
    const clear=()=>graphAction(()=>{delete spec.input[name];ed.dirty=true;renderEditor();});
    if(bound)return el('div',{class:'parameter-field'},el('strong',{},name+(required?' *':'')),el('p',{class:'field-hint'},schema.description||WorkflowGraph.describe(schema)),el('code',{},bound),button('清除此入参',clear,'small'));
    const defaultFormat=typeof value==='string'||schema.type==='string'||schema.anyOf?.some(s=>s.type==='string')?'text':'json';
    const format=ed.formats?.[ed.selected+':'+name]||defaultFormat;
    const choice=el('select',{'aria-label':name+' 填写方式'},...[['text','文字'],['json','结构化值'],['task','启动任务时提供']].map(([v,label])=>el('option',{value:v,selected:format===v},label)));
    choice.addEventListener('change',()=>graphAction(()=>{ed.formats={...ed.formats,[ed.selected+':'+name]:choice.value};renderEditor();}));
    const box=el('textarea',{'data-param':name,'data-format':format,'aria-label':'入参 '+name,rows:2,value:value===undefined?'':format==='json'?json(value):typeof value==='string'?value:json(value),placeholder:format==='task'?'任务输入字段名，例如 request':format==='json'?'例如 ["条件一", "条件二"]':'填写内容',oninput:()=>{ed.dirty=true;}});
    return el('div',{class:'parameter-field'},field(name+(required?' *':''),box,(schema.description||'')+' · '+WorkflowGraph.describe(schema)),choice);
  }),button('高级：输入 JSON',()=>graphAction(()=>{ed.inputMode='json';renderEditor();}),'small'));
}
function renderEdgeInspector(edge){
  const label=edge.kind==='data'?'参数传递':edge.kind==='route'?'有界回退':'执行依赖';
  return el('aside',{class:'editor-inspector'},el('h3',{},label),el('p',{},`${edge.from}${edge.sourcePath?'.'+edge.sourcePath:''} → ${edge.to}${edge.path?'.'+edge.path:''}`),edge.outcome&&el('p',{},'结果条件：'+edge.outcome),
    el('p',{class:'field-hint'},edge.kind==='data'?'删除参数连接后，执行先后关系仍保留。必填入参需重新填写或连接。':edge.kind==='route'?'回退包含次数上限等规则，请在完整 JSON 中修改。':'删除前需先断开依赖它的参数连接。'),
    edge.kind!=='route'&&button('删除这条连线',()=>removeEditorEdge(edge),'danger'),button('返回节点说明',()=>{S.editor.edge=null;renderEditor();},'small'));
}
function renderEditor() {
  const ed=S.editor;if(!ed)return renderWorkflows();
  const wf=ed.workflow;
  const title=el("input",{value:wf.title || "",placeholder:"给流程起个易读的名字",oninput:e=>{wf.title=e.target.value;ed.dirty=true;}});
  const id=el("input",{value:wf.id || "",oninput:e=>{wf.id=e.target.value;ed.dirty=true;}});
  const version=el("input",{value:wf.version || "1",oninput:e=>{wf.version=e.target.value;ed.dirty=true;}});
  const description=el("textarea",{rows:2,value:wf.description || "",oninput:e=>{wf.description=e.target.value;ed.dirty=true;}});
  const modeButtons=el("div",{class:"button-row"},...[["visual","节点编排"],["advanced","完整 JSON"]].map(([mode,label])=>button(label,()=>safeEditorAction(()=>{ed.mode=mode;ed.advanced=json(ed.workflow);renderEditor();}),ed.mode===mode?"small primary":"small")));
  const toolbar=el("div",{class:"editor-toolbar"},modeButtons,button("＋ 添加节点",()=>safeEditorAction(openAddNode),"small push"),button("校验流程",()=>validateEditor(),"small"));
  let content;
  if(ed.mode==="advanced"){
    const text=el("textarea",{id:"workflow-json",class:"code-input",value:ed.advanced || json(wf),spellcheck:false,"aria-label":"完整流程 JSON",oninput:e=>{ed.advanced=e.target.value;ed.dirty=true;}});
    content=el("div",{class:"advanced-editor"},field("完整流程 JSON",text,"这里保留所有字段，包括条件 when、回退 routes、变量、资源和额外元数据。"),el("div",{class:"form-actions"},button("应用 JSON 到节点图",()=>safeEditorAction(()=>{ed.mode="visual";renderEditor();toast("完整定义已应用。保存前还会进行服务端校验。");}),"small")));
  }else content=el("div",{class:"editor-layout flow-layout"},el("div",{class:"editor-canvas"},el("details",{class:"flow-metadata"},el('summary',{},'流程名称与版本'),el("div",{class:"editor-info"},field("流程名称",title),field("版本",version),field("流程 ID",id,"稳定标识；保存文件名在页面底部设置。","wide"),field("流程说明",description,"","wide"))),renderFlowCanvas(),el("div",{class:"editor-metadata"},`${Object.keys(wf.nodes).length} 个节点`,el("span",{},"· 绿色：参数 · 灰色：依赖 · 虚线：有界回退"))),ed.edge?renderEdgeInspector(ed.edge):renderEditorInspector());
  const key=el("input",{value:ed.saveKey,"aria-label":"保存路径",oninput:e=>{ed.saveKey=e.target.value;}});
  const leave=button("← 返回流程库",()=>{
    if(ed.dirty)showDialog("离开流程编辑","当前有尚未保存的修改。",body=>body.append(el("div",{class:"form-actions"},button("继续编辑",()=>dialog.close()),button("放弃修改并返回",()=>{dialog.close();S.editor=null;renderWorkflows();},"danger"))));
    else{S.editor=null;renderWorkflows();}
  });
  const saveBar=el("div",{class:"save-bar"},field("保存到我的流程",key,"格式：custom/name；name 使用字母、数字、短横线或下划线。"),button("保存流程",()=>saveEditor(),"primary"),button("用当前定义启动",()=>safeEditorAction(()=>openStart({key:"draft",workflow:clone(ed.workflow),title:workflowTitle(ed.workflow)}))));
  main.replaceChildren(
    pageIntro("WORKFLOW STUDIO",workflowTitle(wf),ed.sourceKey?.startsWith("builtin/")?"正在编辑模板副本。保存为自己的流程后，可重复启动。":"编辑流程定义。修改不会改变已经启动的任务。",leave),
    el("section",{class:"panel"},toolbar,content,el("div",{class:"editor-help"},"点击节点查看功能与参数。选择输出分支和字段，拖到下游入参；连线自动写入依赖、结果条件与参数引用。复杂回退规则保留在完整 JSON 中。"),saveBar),
    el("div",{id:"editor-feedback",hidden:true,style:"margin-top:15px"})
  );
}
function renderEditorInspector() {
  const ed=S.editor,id=ed.selected,spec=ed.workflow.nodes[id];
  if(!spec)return el("aside",{class:"editor-inspector"},empty("选择一个节点","点击节点卡片编辑依赖与输入，或添加一个新节点。"));
  const def=nodeDef(spec);
  const title=el("input",{value:spec.title || "",placeholder:nodeTitle(id,spec,ed.workflow),oninput:e=>{spec.title=e.target.value;ed.dirty=true;}});
  const kind=el("select",{},el("option",{value:""},"自定义节点"),...nodeChoices().map(n=>el("option",{value:n.key,selected:spec.node===n.key},nodeChoiceLabel(n))));
  kind.value=spec.node || "";
  kind.addEventListener("change",()=>safeEditorAction(()=>{const reference=kind.value;if(reference){spec.node=reference;for(const key of ["kind","version","instruction","outcomes","id","io_contract","required_inputs","output_contract","artifact_fields","definition_sha256","bounded_cost"])delete spec[key];}else{const current=nodeDef(spec);delete spec.node;Object.assign(spec,{kind:current.kind || "agent",version:current.version || "1",instruction:current.instruction || "",outcomes:current.outcomes || ["success"],...(current.io_contract?{io_contract:clone(current.io_contract),required_inputs:clone(current.required_inputs||[]),artifact_fields:clone(current.artifact_fields||[])}:{})});}ed.dirty=true;renderEditor();}));
  const type=el("select",{},...Object.entries(kindNames).map(([k,v])=>el("option",{value:k,selected:def.kind===k},v)));
  type.addEventListener("change",e=>{spec.kind=e.target.value;ed.dirty=true;});
  const dependencies=el("div",{class:"dependency-list"},...Object.keys(ed.workflow.nodes).filter(n=>n!==id).map(n=>{const check=el("input",{type:"checkbox",checked:(spec.depends||[]).includes(n),"aria-label":`依赖节点 ${n}`,onchange:e=>safeEditorAction(()=>{const set=new Set(spec.depends||[]);e.target.checked?set.add(n):set.delete(n);spec.depends=[...set];ed.dirty=true;renderEditor();})});return el("label",{class:"check-chip"},check,n);}));
  const instructions=!spec.node?el("textarea",{value:spec.instruction || "",oninput:e=>{spec.instruction=e.target.value;ed.dirty=true;}}):null;
  return el("aside",{class:"editor-inspector"},el("div",{class:"inspector-title"},el("h3",{},"节点说明与设置"),el("span",{class:"chip mono"},id)),readableNodeContract(def),renderParameterInputs(spec,def),el('details',{},el('summary',{},'名称、能力与依赖设置'),el("div",{class:"form-section"},field("显示名称",title),field("可复用能力",kind),!spec.node&&field("节点类型",type),el("div",{class:"field full-width"},el("div",{class:"field-label"},"前置依赖"),dependencies.childElementCount?dependencies:el("div",{class:"field-hint"},"添加更多节点后可选择依赖。")),instructions&&field("Agent 指令",instructions,"","full-width"))),
    el('details',{},el('summary',{},'连接列表'),...WorkflowGraph.edges(ed.workflow,nodeDef).filter(e=>e.from===id||e.to===id).map(e=>button(`${e.from} → ${e.to}${e.kind==='data'?' · '+(e.sourcePath||'全部输出')+' → '+(e.path||'全部入参'):e.kind==='route'?' · 回退':' · 依赖'}`,()=>{ed.edge=e;renderEditor();},'small'))),button("删除节点",()=>safeEditorAction(()=>deleteNode(id)),"danger small"));
}
function openAddNode() {
  showDialog("添加一个节点","选择已有能力，或创建内联定义。",body=>{
    const keys=Object.keys(S.editor.workflow.nodes);let n=keys.length+1;while(keys.includes(`step_${n}`))n++;
    const id=el("input",{value:`step_${n}`,required:true,pattern:"[A-Za-z0-9_-]+"});
    const capability=el("select",{},...nodeChoices().map(n=>el("option",{value:n.key},nodeChoiceLabel(n))),el("option",{value:"inline:agent"},"空白 Agent 节点"),el("option",{value:"inline:program"},"空白程序节点"));
    const err=el("div",{hidden:true});
    const preview=el('div',{class:'node-preview'});
    const updatePreview=()=>{const def=S.catalog.nodes.find(n=>n.key===capability.value)?.definition;preview.replaceChildren(def?readableNodeContract(def):notice('内联节点的功能和契约需由你填写。'));};
    capability.addEventListener('change',updatePreview);updatePreview();
    body.append(el("form",{class:"form-section",onsubmit:e=>{e.preventDefault();const key=id.value.trim();if(S.editor.workflow.nodes[key]){showError(new Error("此节点 ID 已存在，请使用其他名称。"),err);return;}const reference=capability.value;const def=S.catalog.nodes.find(n=>n.key===reference)?.definition;const spec=reference.startsWith("inline:")?{kind:reference.split(":")[1],version:"1",input:{},depends:[],instruction:""}:{node:reference,input:{},depends:[]};S.editor.workflow.nodes[key]=spec;S.editor.selected=key;S.editor.edge=null;S.editor.dirty=true;S.editor.advanced=null;const count=Object.keys(S.editor.workflow.nodes).length;S.editor.workflow.ui={...S.editor.workflow.ui,positions:{...S.editor.workflow.ui?.positions,[key]:{x:36+(count-1)*374,y:36}}};dialog.close();renderEditor();}},field("节点 ID",id,"字母、数字、短横线或下划线；用于依赖与输出引用。"),field("节点能力",capability),preview,err,el("div",{class:"form-actions"},button("返回",()=>dialog.close()),el("button",{type:"submit",class:"button primary"},"添加节点"))));
  });
}
function deleteNode(id) {
  const wf=S.editor.workflow;
  const related=Object.entries(wf.nodes).filter(([key,node])=>key!==id&&((node.depends||[]).includes(id)||JSON.stringify(node).includes(`steps.${id}.output`)||Object.values(node.routes||{}).some(route=>[...(route.reset||[]),...(route.on_limit?.reset||[])].includes(id)))).map(([key])=>key);
  if(related.length){editorError(new Error(`节点 ${id} 仍被 ${related.join("、")} 依赖或引用。请先移除相关依赖、输入引用和回退目标，再删除。`));return;}
  delete wf.nodes[id];S.editor.selected=Object.keys(wf.nodes)[0]||null;S.editor.dirty=true;renderEditor();
}
async function validateEditor() {
  try{syncEditor();await api("/api/validate",{workflow:S.editor.workflow});const target=$("#editor-feedback");target.className="inline-success";target.textContent="校验通过：节点定义、依赖与引用符合运行契约。尚未保存或启动。";target.hidden=false;}
  catch(error){editorError(error);}
}
async function saveEditor() {
  try{
    syncEditor();const ed=S.editor,key=ed.saveKey.trim();
    if(!/^custom\/[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(key))throw new Error("保存路径应为 custom/name，name 为 1–80 个字母、数字、短横线或下划线，且以字母或数字开头。");
    const body={key,workflow:ed.workflow};
    if(ed.sourceKey===key&&ed.hash)body.expected_hash=ed.hash;
    const result=await api("/api/workflows",body);
    ed.hash=result.hash;ed.sourceKey=result.key;ed.workflow=clone(result.workflow);ed.dirty=false;ed.advanced=null;
    await loadCatalog();renderEditor();toast("流程已保存，后续可从流程库重复启动。");
  }catch(error){editorError(error);}
}

function renderBacklog() {
  const items=Object.values(S.overview?.backlog||{});
  const owner=el("input",{value:S.owner,"aria-label":"待办操作身份",oninput:e=>{S.owner=e.target.value;writeSetting("workbench-owner",S.owner);}});
  const filters=[["active","未结案"],["pending","待领取"],["processing","处理中"],["closed","已结案"],["all","全部"]];
  const filtered=items.filter(i=>S.backlogFilter==="all"||(S.backlogFilter==="active"?i.status!=="closed":i.status===S.backlogFilter));
  main.replaceChildren(pageIntro("LEARN FROM REAL WORK","经验待办","从真实任务中留下发现，带着产物与证据完成维护。",button("＋ 记录待办",openAddBacklog)),el("div",{class:"backlog-toolbar"},el("div",{class:"filter-pills"},...filters.map(([key,label])=>button(label,()=>{S.backlogFilter=key;renderBacklog();},`filter-pill ${S.backlogFilter===key?"active":""}`))),el("label",{class:"owner-field"},"操作身份",owner)),filtered.length?el("div",{class:"backlog-grid"},...filtered.map(backlogCard)):el("div",{class:"panel"},empty("这里暂时没有待办","经验来自实际任务的发现。没有值得维护的内容时，无需刻意创建。")));
}
function backlogCard(item) {
  const source=item.source||{},title=source.title||source.summary||source.description||source.problem||source.gap||source.text||item.id;
  const card=el("article",{class:"panel backlog-card"},el("div",{class:"backlog-card-top"},el("span",{class:"chip mono"},item.id),badge(item.status,item.status==="pending"?"待领取":undefined)),el("h3",{},display(title)),source.run_id&&el("a",{class:"run-link",href:`#tasks/${encodeURIComponent(source.run_id)}`},`↗ 来源任务 ${source.run_id}`),source.evidence&&el("div",{class:"backlog-source"},"依据：",display(source.evidence)),el("details",{},el("summary",{},"完整来源与上下文"),el("pre",{},json(source))),item.result&&el("div",{class:"backlog-result"},display(item.result)));
  if(item.updates?.length)card.append(el("details",{},el("summary",{},`补充记录（${item.updates.length}）`),...item.updates.map(update=>el("div",{class:"backlog-source"},el("strong",{},update.author),el("span",{class:"muted small"},` · ${update.update_id}`),el("pre",{},json(update.content))))));
  if(item.reopenings?.length)card.append(el("details",{},el("summary",{},`重新处理记录（${item.reopenings.length}）`),...item.reopenings.map(entry=>el("div",{class:"backlog-source"},el("strong",{},entry.author),el("span",{class:"muted small"},` · ${entry.reopen_id}`),el("p",{},entry.reason),el("div",{class:"muted small"},"上次结案结果"),el("pre",{},json(entry.previous_result))))));
  const actions=el("div",{class:"backlog-actions"},el("span",{class:"muted small"},item.owner?`当前负责人：${item.owner}`:item.status==="closed"?"已结案":"尚未领取"),button("补充记录",()=>openAppendBacklog(item),"small"));
  if(item.status==="closed")actions.append(button("重新处理",()=>openReopenBacklog(item),"primary small push"));
  if(item.status==="pending")actions.append(button("领取",()=>backlogAction("claim",item),"primary small push"));
  if(item.status==="processing")actions.append(button("释放",()=>backlogAction("release",item),"small push"),button("带证据结案",()=>openCloseBacklog(item),"primary small"));
  card.append(actions);return card;
}
async function backlogAction(action,item,extra={},errorTarget) {
  try{if(!S.owner.trim())throw new Error("请先填写操作身份。");const identity=["append","reopen"].includes(action)?{author:S.owner.trim()}:{owner:S.owner.trim()};const result=await api("/api/backlog",{action,data:{id:item.id,...identity,...extra}});if(S.overview)S.overview.backlog[item.id]=result;if(dialog.open)dialog.close();renderBacklog();loadOverview({render:false});toast({claim:"已领取待办。",release:"已释放待办。",close:"已记录结案结果与证据。",add:"待办已记录。",append:"补充记录已保存，原待办状态保持不变。",reopen:result.status==="pending"?"已转为待领取，原结案结果已保留在重新处理记录中。":`该重新处理请求已处理过，待办当前为${statuses[result.status]||result.status}。`}[action]);return true;}
  catch(error){errorTarget?showError(error,errorTarget):toast(error.message,true);return false;}
}
function openAppendBacklog(item) {
  showDialog("补充记录",display(item.source?.title||item.id),body=>{
    const updateId=`update-${crypto.randomUUID()}`;
    const fact=el("textarea",{placeholder:"补充观察、反证、适用条件或复用反馈。",required:true,rows:4});
    const evidence=el("textarea",{placeholder:"填写证据位置或相关任务；没有证据的推测请在上方明确标注。",rows:3});
    const err=el("div",{hidden:true});
    let submitted=null;
    const submit=el("button",{type:"submit",class:"button primary"},"保存补充记录");
    body.append(notice("补充记录保留署名，并追加到原条目；不会覆盖原始依据、改变负责人或重新打开已结案待办。"),el("form",{class:"form-section",onsubmit:async e=>{
      e.preventDefault();if(submit.disabled)return;
      if(!fact.value.trim()){showError(new Error("请填写补充内容。"),err);return;}
      if(!S.owner.trim()){showError(new Error("请先在待办页填写操作身份。"),err);return;}
      const content={fact:fact.value.trim()};if(evidence.value.trim())content.evidence=evidence.value.trim();
      const current={author:S.owner.trim(),update_id:updateId,content};
      if(submitted&&JSON.stringify(submitted)!==JSON.stringify(current)){showError(new Error("上次提交结果尚未确认。请保持原内容重试或先刷新核对；确认保存后，再另开补充记录。"),err);return;}
      submitted=current;submit.disabled=true;
      try{await backlogAction("append",item,current,err);}finally{submit.disabled=false;}
    }},field("补充内容",fact),field("证据或关联任务",evidence),err,el("div",{class:"form-actions"},button("返回",()=>dialog.close()),submit)));
  });
}
function openReopenBacklog(item) {
  showDialog("重新处理",display(item.source?.title||item.id),body=>{
    const reopenId=`reopen-${crypto.randomUUID()}`;
    const reason=el("textarea",{placeholder:"说明需要重新处理的原因，并注明反证或其他依据的位置。",required:true,rows:5});
    const err=el("div",{hidden:true});
    let submitted=null;
    const submit=el("button",{type:"submit",class:"button primary"},"转为待领取");
    body.append(notice("原结案结果会保留在历史中；重新领取、处理并核验后，再记录新的结案结果。"),el("form",{class:"form-section",onsubmit:async e=>{
      e.preventDefault();if(submit.disabled)return;
      if(!reason.value.trim()){showError(new Error("请填写重新处理的原因与依据。"),err);return;}
      if(!S.owner.trim()){showError(new Error("请先在待办页填写操作身份。"),err);return;}
      const current={author:S.owner.trim(),reopen_id:reopenId,reason:reason.value.trim()};
      if(submitted&&JSON.stringify(submitted)!==JSON.stringify(current)){showError(new Error("上次提交结果尚未确认。请保持原内容重试或先刷新核对，再决定后续操作。"),err);return;}
      submitted=current;submit.disabled=true;
      try{await backlogAction("reopen",item,current,err);}finally{submit.disabled=false;}
    }},field("原因与依据",reason),err,el("div",{class:"form-actions"},button("返回",()=>dialog.close()),submit)));
  });
}
function openCloseBacklog(item) {
  showDialog("带着证据结案",display(item.source?.title||item.id),body=>{
    const result=el("textarea",{placeholder:"说明解决了什么、产物位置与验证证据；撤销需求时说明依据和原因。",required:true,rows:6});
    const err=el("div",{hidden:true});
    body.append(notice(`当前负责人：${item.owner}。操作身份：${S.owner || "尚未填写"}。只有当前负责人可以结案。`),el("form",{class:"form-section",onsubmit:e=>{e.preventDefault();backlogAction("close",item,{result:result.value.trim()},err);}},field("结案结果与证据",result),err,el("div",{class:"form-actions"},button("返回",()=>dialog.close()),el("button",{type:"submit",class:"button primary"},"确认结案"))));
  });
}
function openAddBacklog() {
  showDialog("记录一项经验待办","只记录有依据的发现，保留来源和下一步。",body=>{
    const id=el("input",{value:`note-${Date.now().toString(36)}`,required:true});
    const title=el("input",{required:true,placeholder:"需要修复、验证或复用的具体问题"});
    const run=el("input",{placeholder:"可选：关联运行 ID"});
    const evidence=el("textarea",{required:true,placeholder:"观察到了什么，依据在哪里"});
    const next=el("textarea",{placeholder:"下一步及恢复条件"});
    const err=el("div",{hidden:true});
    body.append(el("form",{class:"form-section",onsubmit:async e=>{e.preventDefault();const data={id:id.value.trim(),title:title.value.trim(),evidence:evidence.value.trim(),next_step:next.value.trim(),author:S.owner};if(run.value.trim())data.run_id=run.value.trim();try{const result=await api("/api/backlog",{action:"add",data});if(S.overview)S.overview.backlog[data.id]=result;dialog.close();renderBacklog();loadOverview({render:false});toast("待办已记录。");}catch(error){showError(error,err);}}},field("待办 ID",id),field("问题或发现",title),field("来源任务",run),field("已有证据",evidence),field("后续处理",next),err,el("div",{class:"form-actions"},button("返回",()=>dialog.close()),el("button",{type:"submit",class:"button primary"},"记录待办"))));
  });
}

async function openTaskSettings() {
  try {
    const settings=await api('/api/settings');
    showDialog('默认任务目录','新任务在此创建独立目录；修改后已有任务仍使用原目录。',body=>{
      const path=el('input',{value:settings.default_task_root,required:true,'aria-label':'默认任务目录',placeholder:'填写绝对路径'});
      const err=el('div',{hidden:true});
      body.append(el('form',{class:'form-section',onsubmit:async e=>{
        e.preventDefault();
        try { await api('/api/settings',{default_task_root:path.value.trim()});dialog.close();toast('默认任务目录已保存。'); }
        catch(error){showError(error,err);}
      }},field('默认任务目录',path,'服务需要能够读写该目录。'),err,el('div',{class:'form-actions'},button('返回',()=>dialog.close()),el('button',{type:'submit',class:'button primary'},'保存目录'))));
    });
  } catch(error){showError(error);}
}

$("#task-settings").addEventListener("click",openTaskSettings);
$("#refresh").addEventListener("click",refreshAll);
$("#new-run").addEventListener("click",()=>openStart());
window.addEventListener("hashchange",navigate);
window.addEventListener("beforeunload",event=>{if(S.editor?.dirty){event.preventDefault();event.returnValue="";}});
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&S.page==="tasks"&&S.selectedRun)loadRun(S.selectedRun);});
main.addEventListener("input",event=>{const form=event.target.closest("form");if(form)form.dataset.dirty="true";});
setInterval(()=>{if(!document.hidden)loadOverview();},20000);
setInterval(()=>{if(!document.hidden&&S.page==="tasks"&&S.selectedRun&&!dialog.open)loadRun(S.selectedRun);},5000);
async function init() {
  navigate();
  await Promise.all([loadCatalog(),loadOverview({render:false})]);
  if(!S.selectedRun){const runs=S.overview?.runs||[];const candidate=runs.find(r=>(!r.root||r.root===r.run_id)&&["running","waiting","paused"].includes(r.status))||runs.find(r=>!r.root||r.root===r.run_id);if(candidate){S.selectedRun=candidate.run_id;await loadRun(candidate.run_id,{render:false});}}
  if(!isEditing())renderPage();
}
init();
