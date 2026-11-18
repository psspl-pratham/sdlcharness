'use strict';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label = value => String(value ?? '').replaceAll('_', ' ');
const pretty = value => esc(JSON.stringify(value, null, 2));
const time = value => value ? new Date(value).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
const date = value => value ? new Date(value + 'T12:00:00').toLocaleDateString([], {day:'numeric',month:'short',year:'numeric'}) : '—';
const list = values => `<ul class="list">${(values || []).map(v => `<li>${esc(v)}</li>`).join('')}</ul>`;
const badge = (value, text) => `<span class="status ${esc(value)}">${esc(text || label(value))}</span>`;
const link = value => /^https?:\/\//i.test(value || '') ? esc(value) : '#';
const ROLES = {
    product_manager: ['Product manager', 'Understands the idea and writes user stories.'],
    architect: ['Architect', 'Designs the app, its data, and its API.'],
    tech_lead: ['Tech lead', 'Breaks the scope into tasks and dependencies.'],
    developer: ['Developer', 'Writes code and tests for one task at a time.'],
    qa: ['QA engineer', 'Repairs observed failures and verifies the test suite.'],
    scrum_master: ['Scrum master', 'Summarizes the measured delivery results.'],
};
const STAGES = {
    intake: ['Understand the idea','Product manager','The original request becomes a product brief.','brief'],
    requirements: ['Define the scope','Product manager','User stories describe what people can do and how to verify it.','requirements'],
    architecture: ['Design the app','Architect','Choose the structure, data model, pages, and API contracts.','architecture'],
    breakdown: ['Create the backlog','Tech lead','Turn the scope into tasks with estimates, files, and dependencies.','backlog'],
    sprint_plan: ['Plan the sprints','Planning engine','Code schedules the backlog using dependencies and team capacity.','sprint_plan'],
    build: ['Build the app','Developer + QA','Generate one task, run real tests, repair failures, and commit passing work.','build'],
    qa: ['Verify the release','QA engineer','Compile and test the complete app before release review.','qa'],
    deploy: ['Start the service','Release engine','Build a Docker image, start it, and check the running service.','deployment'],
    retro: ['Review the outcome','Scrum master','Summarize actual tasks, tests, and deployment results.','retro'],
};
let selected = new URLSearchParams(location.search).get('project') || localStorage.getItem('harness-project');
let current = null, events = [], tab = 'overview', selectedStage = null, signature = '', refreshing = false;
let sourceFiles = [], sourceFile = '', sourceText = '', sourceLoading = false;
const drafts = {};

async function api(path, method = 'GET', body) {
    const response = await fetch(path, {method, headers:{'Content-Type':'application/json','X-Harness-Request':'1'}, ...(body ? {body:JSON.stringify(body)} : {})});
    if (!response.ok) {
        const error = await response.json();
        throw Error(typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail));
    }
    return response.json();
}
function notice(error) { $('notice').textContent = error?.message || ''; $('notice').hidden = !error; }
function taskFor(id) { return current?.artifacts.backlog?.tasks.find(t => t.id === id); }
function roleName(role) { return ROLES[role]?.[0] || label(role); }
function activeStage() {
    const entries = Object.entries(current.stages);
    return entries.find(([,s]) => ['failed','awaiting_approval','running'].includes(s.status))?.[0]
        || entries.filter(([,s]) => s.status === 'done').at(-1)?.[0] || entries[0]?.[0];
}
function latestCheck() { return events.findLast(e => ['quality.quick_check','quality.checked'].includes(e.type)); }
function taskState(task) {
    return current.status === 'failed' && latestCheck()?.data.task === task.id && !latestCheck().data.green ? 'blocked' : task.status;
}
function taskLabel(task) { return taskState(task) === 'blocked' ? 'Failed check' : label(task.status); }
function checkText(data) {
    if (data.dependency_errors?.length) return `${data.dependency_errors.length} dependency issue(s) · tests did not run`;
    if (data.compile_errors?.length) return `${data.compile_errors.length} compilation error(s)`;
    return `${data.passed || 0} passed · ${data.failed || 0} failed · ${data.errors || 0} errors${data.skipped ? ` · ${data.skipped} skipped` : ''}`;
}
function requestInFlight() {
    if (current?.status !== 'running') return null;
    const event = events.findLast(e => ['agent.requested','agent.started','agent.call','quality.started','quality.quick_check','quality.checked'].includes(e.type));
    return event && ['agent.requested','agent.started','quality.started'].includes(event.type) ? event : null;
}

async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
        const projects = await api('/api/projects');
        if (!projects.some(p => p.id === selected)) selected = projects[0]?.id;
        $('projects').innerHTML = projects.length ? projects.map(p => `<button class="project ${p.id===selected?'selected':''}" data-project="${esc(p.id)}">${esc(p.idea.slice(0,95))}<small>${esc(label(p.status))} · ${esc(p.id)}</small></button>`).join('') : '<p class="muted small">No projects yet.</p>';
        if (selected) {
            const id = selected;
            const [project, log] = await Promise.all([api(`/api/projects/${id}`), api(`/api/projects/${id}/events?limit=1000`)]);
            if (selected !== id) return;
            current = project; events = log;
            const next = JSON.stringify([project,log]);
            if (next !== signature) { signature = next; render(); }
        }
        $('connection').textContent = `Connected · updated ${time(new Date().toISOString())}`;
    } catch (error) { $('connection').textContent = 'Connection interrupted'; notice(error); }
    finally { refreshing = false; }
}
function choose(id) {
    selected = id; localStorage.setItem('harness-project', id);
    selectedStage = null; tab = 'overview'; signature = ''; sourceFiles = []; sourceFile = ''; sourceText = '';
    refresh();
}
function changeTab(value) { tab = value; render(); if (tab === 'code') loadFiles(); }
async function action(name, body) {
    notice();
    try { await api(`/api/projects/${selected}/${name}`, 'POST', body); signature = ''; selectedStage = null; await refresh(); }
    catch (error) { notice(error); }
}

function eventInfo(event) {
    const d = event.data || {}, task = taskFor(d.task?.split(':').at(-1));
    const taskName = task ? `${task.id} · ${task.title}` : d.task || '';
    const stage = STAGES[event.stage]?.[0] || label(event.stage);
    let title = event.message || label(event.type), body = '', tone = '';
    switch (event.type) {
        case 'project.created': title = 'Project created'; body = event.message; break;
        case 'stage.started': title = `${stage} started`; body = `${STAGES[event.stage]?.[1] || 'Harness'} · stage attempt ${d.attempt}`; break;
        case 'stage.completed': title = `${stage} completed`; body = 'The output was saved for the next stage.'; break;
        case 'stage.failed': title = `${stage} ${d.retrying ? 'failed; another attempt will follow' : 'stopped'}`; body = `${d.retrying ? 'Retrying this stage.' : 'Later stages remain stopped.'} ${event.message?.split('\n')[0] || ''}`; tone = 'failed'; break;
        case 'stage.recovered': title = `Recovering ${stage}`; body = event.message; break;
        case 'agent.requested': title = `${roleName(d.role)} requested model call ${d.call_number}`; body = taskName || stage; break;
        case 'agent.started': title = `${roleName(d.role)} is working on ${taskName || stage}`; body = `Inputs: ${(d.input_keys || []).join(', ')}. Expected output: ${d.output_type || 'validated artifact'}.`; break;
        case 'agent.call':
            title = `${roleName(d.role)} ${d.outcome === 'valid' ? 'returned a validated output' : d.outcome === 'invalid' ? `returned invalid output${d.retrying === true ? '; retrying once' : d.retrying === false ? '; retry limit reached' : ''}` : 'model call failed'}`;
            body = `${taskName || stage}${d.error ? ' · '+d.error : ''}${d.unwrapped_from ? ` · Removed the single “${d.unwrapped_from}” wrapper and validated its contents.` : ''}`; tone = d.outcome === 'valid' ? '' : 'failed'; break;
        case 'task.started': title = `Started ${taskName}`; body = `Sprint ${d.sprint} · planned owner: ${d.assignee || task?.assignee || 'unassigned'} · developer agent writes the code.`; break;
        case 'task.resumed': title = `Resumed ${taskName}`; body = 'Previously passed tasks and their commits are preserved. Checking the current files before requesting a repair.'; break;
        case 'task.resume_requested': title = `Repair requested for ${taskName}`; body = `${d.preserved_tasks} passed task(s) preserved. The run continues from the failed task using the remaining model-call budget.`; break;
        case 'task.completed': title = `${taskName} passed checks and was committed`; body = `Commit ${(d.commit || '').slice(0,8)}${d.passed !== undefined ? ' · '+d.passed+' passing tests' : ''}`; break;
        case 'task.repairing': title = `QA is repairing ${taskName}`; body = `Repair attempt ${d.attempt}${d.limit ? ' / '+d.limit : ''}. ${d.unchanged ? 'The same failures remain after the previous attempt. ' : ''}${d.failing_tests?.length ? d.failing_tests.join(', ')+'. ' : ''}The task will be tested again.`; break;
        case 'tool.write_files': title = `${roleName(d.role)} wrote ${d.files?.length || 0} file(s)`; body = (d.files || []).join(', ') + (d.notes ? ' · '+d.notes : ''); break;
        case 'quality.started': title = `${d.task || 'Release'}: checking dependencies, compiling and running tests`; body = `${d.mode || 'Docker'} sandbox · check attempt ${d.attempt}`; break;
        case 'quality.quick_check': case 'quality.checked': title = `${d.task || 'Release'}: ${d.green ? 'checks passed' : 'checks failed'}`; body = checkText(d) + (d.failures?.length ? ' · '+d.failures.map(f=>f.test+': '+f.message.split('\n')[0]).join(' · ') : ''); tone = d.green ? '' : 'failed'; break;
        case 'sprint.started': title = `Sprint ${d.sprint} execution started`; body = d.goal; break;
        case 'sprint.completed': title = `Sprint ${d.sprint} merged into main`; body = 'All tasks in this sprint passed their checks.'; break;
        case 'planning.completed': title = `Planning engine scheduled ${d.sprints} sprint(s)`; body = 'Dates and owners are capacity estimates. Execution runs one task at a time.'; break;
        case 'planning.warning': title = 'Planning warning'; body = event.message; break;
        case 'planning.repair': title = 'Planning engine repaired the backlog'; body = event.message; break;
        case 'gate.waiting': title = `Your review is needed: ${stage}`; body = 'The run is paused until you approve or revise it.'; break;
        case 'gate.approved': title = `You approved: ${stage}`; break;
        case 'gate.sent_back': title = `You sent back: ${stage}`; body = event.message; break;
        case 'project.rerun': title = `Restart requested from ${stage}`; body = event.message; break;
        case 'project.completed': title = 'Workflow completed'; break;
        case 'sandbox.selected': title = `Using the ${d.mode} sandbox`; break;
    }
    const meta = [d.tokens !== undefined && d.tokens !== null ? `${d.tokens.toLocaleString()} tokens` : '', d.duration_ms !== undefined ? `${(d.duration_ms/1000).toFixed(1)} s` : '', d.response_format==='tool_arguments'?'Structured patch':'', d.model_id || ''].filter(Boolean).join(' · ');
    return {title,body,tone,meta};
}
function activity(items, raw = false) {
    return items.length ? items.map((e,i) => {
        const info = eventInfo(e);
        return `<article class="activity"><time>${esc(time(e.timestamp))}</time><div><h3 class="${info.tone==='failed'?'error':''}">${esc(info.title)}</h3>${info.body?`<p>${esc(info.body)}</p>`:''}${info.meta?`<p class="meta">${esc(info.meta)}</p>`:''}${raw?`<details id="event-${esc(e.timestamp)}-${i}"><summary>Recorded event</summary><pre>${pretty(e)}</pre></details>`:''}</div></article>`;
    }).join('') : '<p class="empty">Activity appears as the run progresses.</p>';
}

function tasksTable(tasks) {
    return `<div class="table-wrap"><table><thead><tr><th>Task</th><th>Work & files</th><th>Planned owner</th><th>Points</th><th>Depends on</th><th>Result</th></tr></thead><tbody>${tasks.map(t => `<tr><td><code>${esc(t.id)}</code></td><td><strong>${esc(t.title)}</strong><p class="muted">${esc((t.files || []).join(', '))}</p></td><td>${esc(t.assignee || 'Unassigned')}<p class="muted">${esc(t.role)}</p></td><td>${t.points}</td><td>${esc(t.dependencies.join(', ') || 'Ready')}</td><td>${badge(taskState(t),taskLabel(t))}</td></tr>`).join('')}</tbody></table></div>`;
}
function planView() {
    const p = current, a = p.artifacts, plan = a.sprint_plan;
    if (!plan) return '<section class="panel"><h2>The plan is being prepared</h2><p class="muted">The product manager writes the scope, the architect designs it, and the tech lead creates tasks. The planning engine then schedules them.</p></section>';
    const tasks = a.backlog?.tasks || [], team = p.blueprint.team;
    return `<section class="panel"><h2>How this schedule was made</h2><p class="capacity-note">The model estimated task size and dependencies. The planning engine packed tasks into ${team.sprint_length_days}-day sprints using each member’s capacity × ${Math.round(team.focus_factor*100)}% focus. Dates are planning estimates; the harness executes tasks immediately in sprint order.</p><p class="capacity-note">${esc(team.members.map(m=>`${m.name}: ${m.role}, ${Math.floor(m.points_per_sprint*team.focus_factor)} effective points/sprint`).join(' · '))}. These names represent planning capacity. The developer agent performs the coding.</p><div class="row">${badge('done',`${plan.sprints.length} sprints`)}${badge('pending',`${tasks.reduce((n,t)=>n+t.points,0)} points`)}${badge('pending',`Projected finish ${date(plan.projected_end)}`)}</div><p class="small muted">Longest dependency chain: ${esc(plan.critical_path.join(' → '))} · ${plan.critical_path_points} points. A delay here can delay the plan.</p></section>
    ${plan.warnings.length || plan.repairs.length ? `<section class="panel"><h2>Review these planning decisions</h2>${[...plan.warnings,...plan.repairs].map(w=>`<div class="warning">${esc(w)}</div>`).join('')}</section>`:''}
    ${plan.sprints.map(s => `<section class="panel"><div class="sprint-head"><div><p class="eyebrow">Sprint ${s.number}</p><h2>${esc(date(s.start))} — ${esc(date(s.end))}</h2><p>${esc(s.goal)}</p></div>${badge('pending',`${s.committed} / ${s.capacity} points`)}</div><div class="load-grid">${team.members.map(m=>{const capacity=Math.floor(m.points_per_sprint*team.focus_factor), used=s.load[m.name]||0; return `<div class="load"><div class="row spread"><span>${esc(m.name)} · ${esc(m.role)}</span><span>${used}/${capacity} points</span></div><div class="meter ${used>capacity?'over':''}"><span style="width:${Math.min(100,used/capacity*100)}%"></span></div></div>`}).join('')}</div>${tasksTable(s.tasks.map(id=>tasks.find(t=>t.id===id)).filter(Boolean))}</section>`).join('')}`;
}
function artifactView(key) {
    const a = current.artifacts, value = a[key];
    if (!value) return '<p class="muted">No accepted output for this stage yet. Agent activity and failed test output remain available.</p>';
    if (key === 'brief') return `<h3>${esc(value.name)}</h3><p>${esc(value.problem)}</p><p class="small muted">Users: ${esc(value.users.join(', '))} · Product entities: ${esc(value.entities.join(', '))}</p>${list(value.constraints)}`;
    if (key === 'requirements') return value.stories.map(s=>`<div class="summary-line"><h3>${esc(s.id)} · ${esc(s.i_want)}</h3><p class="small muted">As ${esc(s.as_a)}, ${esc(s.so_that)} · ${esc(s.priority)}</p>${list(s.acceptance_criteria)}</div>`).join('') + `<h3>Quality requirements</h3>${list(value.nfrs)}`;
    if (key === 'architecture') return `<p><strong>${esc(value.style)}</strong> · ${esc(value.stack.join(', '))}</p><p class="small muted">Components: ${esc(value.components.join(', '))}</p><div class="table-wrap"><table><thead><tr><th>Method</th><th>Path</th><th>Purpose</th></tr></thead><tbody>${value.api.map(x=>`<tr><td>${esc(x.method)}</td><td><code>${esc(x.path)}</code></td><td>${esc(x.description)}</td></tr>`).join('')}</tbody></table></div>${list(value.decisions)}`;
    if (key === 'backlog') return tasksTable(value.tasks);
    if (key === 'sprint_plan') return `<p>${value.sprints.length} sprints · ${date(value.projected_end)} projected finish.</p><button data-tab="plan">Read the full sprint plan →</button>`;
    if (key === 'build') return `<p>${value.commits.length} commits · release ${esc(value.commit.slice(0,8))}</p><div class="file-list">${value.files.map(f=>`<button class="file-button" data-file="${esc(f)}">${esc(f)}</button>`).join('')}</div>`;
    if (key === 'qa') return `<p>${badge(value.green?'passed':'failed',value.green?'Release checks passed':'Release checks failed')}</p><p>${esc(checkText(value))}</p><details id="qa-output"><summary>Full test output</summary><pre>${esc(value.output)}</pre></details>`;
    if (key === 'deployment') return `<p>${badge(value.status)} · ${esc(value.target)}</p><a href="${link(value.url)}" target="_blank" rel="noopener">Open app ↗</a> · <a href="${link(value.url+'/docs')}" target="_blank" rel="noopener">API docs ↗</a>${list(value.checks.map(c=>`${c.path || c.url || 'Health check'}: ${c.status || c.status_code || JSON.stringify(c)}`))}`;
    if (key === 'retro') return `<h3>What went well</h3>${list(value.went_well)}<h3>Improve next time</h3>${list(value.to_improve)}<h3>Actions</h3>${list(value.actions)}`;
    return `<pre>${pretty(value)}</pre>`;
}
function overviewView() {
    const p=current, a=p.artifacts, stage=selectedStage || activeStage(), definition=STAGES[stage];
    return `<div class="grid"><section class="panel"><p class="eyebrow">Original request</p><h2>What you asked for</h2><p class="quote">${esc(p.idea)}</p><p class="small muted">Check that the brief and stories preserve your product and its user actions.</p></section><section class="panel"><p class="eyebrow">Accepted brief</p><h2>What the model understood</h2>${a.brief?artifactView('brief'):'<p class="muted">The product manager has not finished the brief yet.</p>'}</section></div>
    <section class="panel"><div class="row spread"><h2>${esc(definition?.[0] || label(stage))}</h2>${badge(p.stages[stage].status)}</div><p class="muted small">${esc(definition?.[1])} · ${esc(definition?.[2])}</p>${artifactView(definition?.[3])}${p.stages[stage].error?`<details id="stage-error"><summary>Recorded stage error</summary><pre>${esc(p.stages[stage].error)}</pre></details>`:''}</section>
    ${a.requirements?`<section class="panel"><h2>Scope to review before building</h2>${artifactView('requirements')}</section>`:''}
    ${a.sprint_plan?`<section class="panel"><div class="row spread"><h2>The delivery plan</h2><button data-tab="plan">Inspect sprints & assignments →</button></div><p>${a.backlog.tasks.length} tasks across ${a.sprint_plan.sprints.length} sprints · projected finish ${esc(date(a.sprint_plan.projected_end))}.</p><p class="muted small">${a.sprint_plan.warnings.length} planning warning(s). Estimates come from the model; assignments come from the planning engine.</p></section>`:''}
    <section class="panel"><div class="row spread"><h2>Recent agent & engine activity</h2><button data-tab="agents">See the full activity →</button></div>${activity(events.slice(-8).reverse())}</section>`;
}
function agentsView() {
    const inFlight=requestInFlight();
    return `<p class="capacity-note">Each agent is a role prompt sent to the same configured Bedrock model. Roles run sequentially. The planning engine schedules work; Docker tests and health checks decide whether it passes. Activity shows recorded actions and outputs.</p><div class="agent-grid">${Object.entries(ROLES).map(([key,[name,description]])=>{const calls=events.filter(e=>e.type==='agent.call'&&e.data.role===key), active=inFlight?.data.role===key; return `<article class="agent-card ${active?'active':''}">${badge(active?'running':'pending',active?'Working':calls.length?`${calls.length} model responses`:'Not started')}<h3>${name}</h3><p>${description}</p><p class="small muted">${calls.filter(e=>e.data.outcome==='valid').length} valid outputs · ${calls.filter(e=>e.data.outcome!=='valid').length} invalid / failed calls</p></article>`}).join('')}</div><section class="panel"><h2>Recorded activity · latest first</h2><p class="small muted">A validated model response is a schema check. The separate test events show whether the generated app works.</p>${activity(events.slice().reverse(),true)}</section>`;
}
function boardView() {
    const tasks=current.artifacts.backlog?.tasks;
    if (!tasks) return '<p class="empty">Tasks appear after the tech lead creates the backlog.</p>';
    return `<p class="capacity-note">The owner is the planning assignment. The developer agent executes tasks in sprint order. “Done” requires a passing test run and a git commit.</p><div class="board">${[['todo','To do'],['in_progress','In progress / blocked'],['done','Done']].map(([status,title])=>`<section class="column"><h2>${title}</h2>${tasks.filter(t=>t.status===status).map(t=>`<article class="task-card"><div class="row spread"><code>${esc(t.id)}</code>${badge(taskState(t),taskLabel(t))}</div><h3>${esc(t.title)}</h3><p>${esc(t.assignee || t.role)} · ${t.points} points · Sprint ${t.sprint || '—'}</p><p>Dependencies: ${esc(t.dependencies.join(', ') || 'None')}</p><p>${esc(t.files.join(', '))}</p></article>`).join('')||'<p class="muted small">No tasks.</p>'}</section>`).join('')}</div>`;
}
function codeView() {
    return `<section class="panel"><h2>Generated workspace</h2><p class="muted small">Includes files from the current failed attempt, even if they have not been committed.</p><div class="file-list">${sourceFiles.map(f=>`<button class="file-button ${f===sourceFile?'active':''}" data-file="${esc(f)}">${esc(f)}</button>`).join('') || 'Loading files…'}</div></section><h2>${esc(sourceFile || 'Select a file')}</h2><pre id="source">${esc(sourceLoading?'Loading…':sourceText || 'Choose a file to read what the agent wrote.')}</pre>`;
}
async function loadFiles() {
    const id=selected;
    try { const files=await api(`/api/projects/${id}/files`); if(selected===id&&tab==='code'){sourceFiles=files;render();} }
    catch(error){notice(error);}
}
async function showFile(file) {
    tab='code'; sourceFile=file; sourceLoading=true; sourceText=''; render();
    const id=selected;
    try {
        const response=await fetch(`/api/projects/${id}/file?path=${encodeURIComponent(file)}`);
        if(!response.ok)throw Error('Unable to read this workspace file.');
        const text=await response.text();
        if(selected===id&&sourceFile===file){sourceText=text;sourceLoading=false;render();}
        if(!sourceFiles.length)loadFiles();
    }catch(error){sourceLoading=false;notice(error);}
}

function render() {
    if(!current)return;
    const focus=document.activeElement, focusId=focus?.id, selection=typeof focus?.selectionStart==='number'?[focus.selectionStart,focus.selectionEnd]:null;
    const opened=[...document.querySelectorAll('details[open][id]')].map(d=>d.id);
    const p=current,a=p.artifacts,live=p.provider?.name==='bedrock',stage=activeStage(), waiting=live&&p.status==='awaiting_approval';
    const tasks=a.backlog?.tasks||[],done=tasks.filter(t=>t.status==='done').length,recordedCalls=events.filter(e=>e.type==='agent.call');
    const tokens=recordedCalls.filter(e=>e.data.tokens!==null&&e.data.tokens!==undefined).reduce((n,e)=>n+e.data.tokens,0);
    const inFlight=requestInFlight(),lastWork=inFlight?eventInfo(inFlight):null,check=latestCheck(),failedTask=check?.data.task?taskFor(check.data.task):null;
    const workTitle=p.status==='failed'?`Stopped at ${label(stage)}${failedTask?' · '+failedTask.id:''}`:waiting?`Ready for review: ${STAGES[stage]?.[0]}`:p.status==='completed'?'Workflow completed':lastWork?.title || `${STAGES[stage]?.[1]} · ${STAGES[stage]?.[0]}`;
    const draft=drafts[p.id] || '';
    const canRepair=live&&p.status==='failed'&&p.stages.build?.status==='failed'&&tasks.some(t=>t.status==='in_progress');
    $('content').innerHTML=`<div class="row spread project-heading"><div><p class="eyebrow">${esc(p.blueprint.name)} · ${esc(p.id)}</p><h1>${esc(a.brief?.name || p.idea)}</h1><p class="muted small">${esc(p.provider?.model_id || 'Historical fixture')} · ${esc(p.provider?.region || '')}</p></div>${badge(p.status)}</div>
    <div class="stats"><div class="stat"><strong>${done} / ${tasks.length || '—'}</strong><span>Tasks passed & committed</span></div><div class="stat"><strong>${a.sprint_plan?.sprints.length || '—'}</strong><span>Planned sprints</span></div><div class="stat"><strong>${p.model_calls || 0} / ${p.max_model_calls}</strong><span>Model call attempts used</span></div><div class="stat"><strong>${tokens?tokens.toLocaleString():'—'}</strong><span>Tokens recorded in the activity log</span></div></div>
    <section class="panel current-work" aria-live="polite"><p class="eyebrow">Current work</p><h2>${esc(workTitle)}</h2><p class="muted small">${esc(lastWork?.body || STAGES[stage]?.[2])}${inFlight?` <span id="work-elapsed"></span>`:''}</p></section>
    <nav class="pipeline" aria-label="Delivery stages">${p.blueprint.stages.map((s,i)=>`<button class="stage ${p.stages[s.name].status} ${selectedStage===s.name?'active':''}" data-stage="${esc(s.name)}"><span>${String(i+1).padStart(2,'0')} · ${esc(STAGES[s.name]?.[1])}</span><strong>${esc(label(s.name))}</strong><span>${esc(label(p.stages[s.name].status))}${s.gate==='manual'?' · review gate':''}</span></button>`).join('')}</nav>
    ${!live?'<p class="warning">Historical fixture: read-only. Create a new project to use Bedrock.</p>':''}
    ${p.status==='failed'?`<section class="panel failure"><h2>${failedTask?`${esc(failedTask.id)} stopped: ${esc(failedTask.title)}`:'The run stopped on a failed check'}</h2><p>${check&&!check.data.green?esc(checkText(check.data)):'A stage could not complete.'} Later stages did not run.</p>${check&&!check.data.green?`<p class="small">${esc((check.data.output || '').match(/^FAILED .+$/m)?.[0] || check.data.dependency_errors?.[0] || check.data.compile_errors?.[0] || '')}</p>`:''}<p class="muted small">Review the original idea, brief, and stories before retrying. Passing tests alone cannot confirm that the model planned the product you intended.</p><div class="row">${canRepair?'<button class="primary" data-action="repair-task">Repair failed task & continue →</button>':''}<button data-tab="plan">Review sprint plan</button><button data-tab="agents">Inspect agent activity</button></div>${canRepair?'<p class="muted small">Keeps passed tasks and current files. Allows up to three repairs by default, then stops if tests still fail. Uses the remaining AWS call budget.</p>':''}<details id="failure-output"><summary>Full failure output</summary><pre>${esc(p.error || p.stages[stage].error)}</pre></details></section>`:''}
    ${waiting?`<section class="panel gate"><h2>${stage==='sprint_plan'?'Review the scope and sprint plan':'Review the release candidate'}</h2><p>${stage==='sprint_plan'?'Check the original idea, stories, tasks, assignments and warnings below. Approving this plan starts code generation.':'Check the QA results and generated app before allowing deployment.'}</p><div class="row"><button class="primary" data-action="approve">${stage==='sprint_plan'?'Approve plan & start build →':'Approve release & deploy →'}</button><button data-tab="${stage==='sprint_plan'?'plan':'outputs'}">Inspect ${stage==='sprint_plan'?'plan':'QA results'}</button></div></section>`:''}
    ${a.deployment?`<section class="panel row spread"><div><h2>${esc(label(a.deployment.status))} deployment</h2><a href="${link(a.deployment.url)}" target="_blank" rel="noopener">Open app ↗</a> · <a href="${link(a.deployment.url+'/docs')}" target="_blank" rel="noopener">API docs ↗</a></div>${a.deployment.status==='healthy'?'<button data-action="stop">Stop service</button>':''}</section>`:''}
    <nav class="tabs" aria-label="Project views">${[['overview','Overview'],['plan','Sprint plan'],['agents','Agent activity'],['board','Task board'],['outputs','Outputs'],['code','Code']].map(([key,title])=>`<button data-tab="${key}" class="${tab===key?'active':''}" aria-current="${tab===key?'page':'false'}">${title}</button>`).join('')}<a href="/api/projects/${esc(p.id)}/report" target="_blank" rel="noopener">Full report ↗</a></nav>
    <div id="view">${tab==='overview'?overviewView():tab==='plan'?planView():tab==='agents'?agentsView():tab==='board'?boardView():tab==='code'?codeView():Object.entries(a).map(([key,value])=>`<section class="panel"><h2>${esc(label(key))}</h2>${artifactView(key)}<details id="raw-${esc(key)}"><summary>Structured output</summary><pre>${pretty(value)}</pre></details></section>`).join('') || '<p class="empty">Outputs appear as stages finish.</p>'}</div>
    ${live&&p.status!=='running'?`<section class="panel"><h2>Revise or retry</h2><p class="small muted">Replanning restarts intake, replaces the downstream plan and code, and returns to the planning review gate. A stage retry restarts that stage and everything after it. Both use the remaining model call budget (${p.max_model_calls-p.model_calls} attempts).</p><label for="revision-feedback">Feedback for the next attempt</label><textarea id="revision-feedback" maxlength="8000" placeholder="Describe missing user actions, scope changes, or the failure to fix.">${esc(draft)}</textarea><div class="toolbar"><button data-action="replan">Replan from original idea</button><select id="retry-stage" aria-label="Stage to retry">${p.blueprint.stages.map(s=>`<option value="${esc(s.name)}" ${s.name===stage?'selected':''}>${esc(STAGES[s.name]?.[0])}</option>`).join('')}</select><button data-action="retry">Retry selected stage</button>${waiting&&stage==='qa'?'<button data-action="send-back">Send QA back with feedback</button>':''}</div></section>`:''}`;
    for(const id of opened){const element=$(id);if(element)element.open=true;}
    if(focusId&&$(focusId)){const element=$(focusId);element.focus({preventScroll:true});if(selection&&element.setSelectionRange)element.setSelectionRange(...selection);}
    updateElapsed();
}
function updateElapsed(){const event=requestInFlight(),el=$('work-elapsed');if(el&&event)el.textContent=`· ${Math.max(0,Math.floor((Date.now()-Date.parse(event.timestamp))/1000))} s elapsed`;}

document.addEventListener('input',event=>{if(event.target.id==='revision-feedback')drafts[selected]=event.target.value;});
document.addEventListener('click',async event=>{
    const target=event.target.closest('button');if(!target)return;
    if(target.dataset.project){choose(target.dataset.project);return;}
    if(target.dataset.tab){changeTab(target.dataset.tab);return;}
    if(target.dataset.stage){selectedStage=target.dataset.stage;tab='overview';render();return;}
    if(target.dataset.file){showFile(target.dataset.file);return;}
    if(!target.dataset.action)return;
    target.disabled=true;
    const feedback=drafts[selected] || '';
    try{
        switch(target.dataset.action){
            case 'approve':await action('approve');break;
            case 'stop':await action('stop');break;
            case 'replan':tab='overview';await action('rerun',{stage:'intake',feedback});break;
            case 'retry':await action('rerun',{stage:$('retry-stage').value,feedback});break;
            case 'repair-task':await action('repair-task',{feedback});break;
            case 'send-back':await action('send-back',{feedback});break;
        }
    }finally{target.disabled=false;}
});
$('create-form').onsubmit=async event=>{
    event.preventDefault();notice();const button=event.target.querySelector('button');button.disabled=true;
    try{const p=await api('/api/projects','POST',{idea:$('idea').value,blueprint:$('blueprint').value});choose(p.id);}
    catch(error){notice(error);}finally{button.disabled=false;}
};
api('/health').then(h=>{$('runtime').textContent=`Bedrock · ${h.model_id} · ${h.region} · ${h.sandbox}`;}).catch(notice);
refresh();setInterval(refresh,1500);setInterval(updateElapsed,1000);
