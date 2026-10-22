const state={overview:null,selectedTask:null,selectedRun:null};
const $=id=>document.getElementById(id);
const el=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node};
const clear=node=>{while(node.firstChild)node.removeChild(node.firstChild)};
const statusText={passed:"公开测试通过",failed:"公开测试失败",public_tests_passed:"公开测试通过",public_tests_failed:"公开测试失败",rejected:"补丁被拒绝",not_run:"未验证"};

function selectTask(task){
  state.selectedTask=task;
  document.querySelectorAll(".task-card").forEach(card=>card.classList.toggle("active",card.dataset.id===task.id));
  $("selected-task-title").textContent=task.id;
  $("selected-task-suite").textContent=task.suite;
  $("selected-task-category").textContent=task.category;
  $("selected-task-note").textContent=task.origin==="BugsInPy"?"真实项目缺陷；公开触发测试来自上游回归，留出测试单独验收。":"自建开发任务；用于检查定位、修复与验收链路。";
  const matching=state.overview.runs.filter(run=>run.task_id===task.id);
  const picker=$("run-select");clear(picker);
  if(!matching.length){picker.append(el("option","","暂无该任务的运行记录"));picker.disabled=true;renderEmptyRun();return}
  picker.disabled=false;
  for(const run of matching){const option=el("option","",`${run.run_id} · ${statusText[run.validation_status]||run.validation_status}`);option.value=run.run_id;picker.append(option)}
  loadRun(matching[0].run_id);
}

function renderTasks(){
  const tasks=state.overview.tasks;$("task-count").textContent=String(tasks.length).padStart(2,"0");
  $("metric-tasks").textContent=tasks.filter(t=>t.status==="ready").length;
  $("metric-real").textContent=tasks.filter(t=>t.origin==="BugsInPy"&&t.status==="ready").length;
  $("metric-runs").textContent=state.overview.runs.length;
  $("metric-evals").textContent=state.overview.evaluations.length;
  const list=$("task-list");clear(list);
  for(const task of tasks){
    const card=el("button","task-card");card.type="button";card.dataset.id=task.id;
    const top=el("div","task-card-top");top.append(el("span","",task.id));top.append(el("small","",task.origin));card.append(top);card.append(el("p","",`${task.category} · ${task.status}`));
    card.addEventListener("click",()=>selectTask(task));list.append(card);
  }
  if(tasks.length)selectTask(tasks[0]);else list.append(el("div","empty-state","未找到可显示的任务清单。"));
}

function renderEmptyRun(){
  state.selectedRun=null;$("trace-status").textContent="等待运行记录";$("evidence-count").textContent="00";$("patch-label").textContent="NO DIFF";
  clear($("trace-steps"));$("trace-steps").append(el("div","empty-state","选择一条运行记录以查看流程。"));
  clear($("evidence-list"));$("evidence-list").append(el("div","empty-state","暂无证据。"));
  clear($("patch-view"));$("patch-view").append(el("div","empty-state","运行成功后会在这里显示统一 diff。"));
}

function addTraceStep(target,number,title,detail){
  const step=el("div","trace-step");step.append(el("small","",number));step.append(el("strong","",title));step.append(el("p","",detail));target.append(step);
}

function renderReport(report,patch){
  const initial=report.initial_public_test||{},diagnosis=report.diagnosis||{},repair=report.repair||{},validation=report.validation||{};
  $("trace-status").textContent=statusText[validation.status]||validation.status||"未验证";
  const trace=$("trace-steps");clear(trace);
  addTraceStep(trace,"01 / REPRODUCE","复现失败",`公开测试：${initial.status||"未知"} · ${Array.isArray(initial.failures)?initial.failures.length:0} 条失败记录`);
  addTraceStep(trace,"02 / LOCALIZE","定位根因",diagnosis.hypothesis?`${diagnosis.hypothesis.target_file} · ${diagnosis.hypothesis.target_symbol}`:"Agent 未提出可验证的定位假设");
  addTraceStep(trace,"03 / PATCH","生成候选",repair.candidate?repair.candidate.explanation:"没有生成候选补丁");
  addTraceStep(trace,"04 / VALIDATE","公开验证",validation.reason||"未运行公开验证");
  const evidence=$("evidence-list");clear(evidence);const items=Array.isArray(report.evidence)?report.evidence:[];$("evidence-count").textContent=String(items.length).padStart(2,"0");
  if(!items.length)evidence.append(el("div","empty-state","报告没有记录证据。"));
  for(const item of items){const row=el("div","evidence-item");row.append(el("b","",item.id));row.append(el("span","",`${item.kind}${item.file_path?` · ${item.file_path}`:""}`));row.append(el("p","",String(item.content||"").slice(0,450)));evidence.append(row)}
  const view=$("patch-view");clear(view);$("patch-label").textContent=patch?"UNIFIED DIFF":"NO DIFF";
  if(!patch)view.append(el("div","empty-state","本次运行没有已应用的补丁。"));
  else for(const line of patch.split("\n")){let cls="diff-line";if(line.startsWith("+++ ")||line.startsWith("--- "))cls+=" diff-header";else if(line.startsWith("+"))cls+=" diff-add";else if(line.startsWith("-"))cls+=" diff-remove";else if(line.startsWith("@@"))cls+=" diff-hunk";view.append(el("div",cls,line||" "))}
  $("patch-footer").textContent="此处只显示公开验证；独立隐藏验收请查看实验评测记录。";
}

async function loadRun(runId){
  state.selectedRun=runId;
  try{const [reportResponse,patchResponse]=await Promise.all([fetch(`/api/runs/${encodeURIComponent(runId)}`),fetch(`/api/runs/${encodeURIComponent(runId)}/patch`)]);if(!reportResponse.ok)throw Error("报告不可用");const report=await reportResponse.json();const patch=patchResponse.ok?await patchResponse.text():"";if(state.selectedRun!==runId)return;renderReport(report,patch)}
  catch(error){if(state.selectedRun!==runId)return;renderEmptyRun();$("trace-status").textContent=error.message}
}

function renderEvaluations(){
  const evaluations=state.overview.evaluations;const latest=evaluations.find(item=>!item.is_reference_fixture)||evaluations[0];if(!latest)return;
  $("eval-title").textContent=latest.id;
  $("eval-badge").textContent=latest.is_reference_fixture?"PIPELINE SELF-CHECK":latest.partial_run?"PILOT / PARTIAL":"MODEL EVALUATION";
  const box=$("eval-methods");clear(box);
  for(const method of latest.methods||[]){const row=el("div","eval-method");row.append(el("span","",method.method));const track=el("div","bar-track");const fill=el("div","bar-fill");fill.style.width=`${Math.max(0,Math.min(100,Number(method.success_rate||0)*100))}%`;track.append(fill);row.append(track);row.append(el("strong","",`${method.passed}/${method.total}`));box.append(row)}
  $("eval-note").textContent=latest.is_reference_fixture?"这是参考补丁或模拟回复的链路自检，不是 Agent 的成功率。当前真实任务数量仍不足以得出研究结论。":latest.partial_run?`试点只运行 ${latest.selected_task_count}/${latest.task_count} 个任务；图中的分母是完整任务集，不代表一次完整模型评测。`:`评测后端：${latest.backend}。请结合每个任务的错误与缺失提交，避免只看总成功率。`;
}

async function main(){
  try{const response=await fetch("/api/overview");if(!response.ok)throw Error("无法读取概览");state.overview=await response.json();renderTasks();renderEvaluations()}
  catch(error){$("task-list").textContent=error.message;$("eval-note").textContent="请确认 dashboard 命令与任务清单路径。"}
  $("run-select").addEventListener("change",event=>{if(event.target.value)loadRun(event.target.value)});
  $("copy-command").addEventListener("click",async()=>{if(!state.selectedTask)return;const command=`.\\.venv\\Scripts\\python.exe -m repofix.cli repair --task "${state.selectedTask.config}" --provider api --runner docker`;try{await navigator.clipboard.writeText(command);$("copy-command").textContent="已复制运行命令 ✓";setTimeout(()=>$("copy-command").textContent="复制容器运行命令 ↗",1900)}catch{$("copy-command").textContent=command}});
}
main();
