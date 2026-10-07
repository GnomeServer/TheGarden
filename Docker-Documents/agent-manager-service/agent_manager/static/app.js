"use strict";

const state={session:null,summary:null,tasks:[],workers:[],users:[],runs:[],activity:[],workSessions:[],peopleSummary:[],accessEvents:[],nodeHealth:{available:false,nodes:[]},notifications:[],refreshTimer:null};
const $=id=>document.getElementById(id);
const esc=value=>String(value??"").replace(/[&<>'"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const fmt=value=>value?new Intl.DateTimeFormat(undefined,{dateStyle:"medium",timeStyle:"short"}).format(new Date(value)):"—";
const relative=value=>{if(!value)return"never";const seconds=Math.round((new Date(value)-Date.now())/1000),abs=Math.abs(seconds);if(abs<60)return `${abs}s ${seconds<=0?"ago":"from now"}`;const minutes=Math.round(abs/60);if(minutes<60)return `${minutes}m ${seconds<=0?"ago":"from now"}`;const hours=Math.round(minutes/60);return `${hours}h ${seconds<=0?"ago":"from now"}`};
const duration=seconds=>{seconds=Math.max(0,Number(seconds)||0);const hours=Math.floor(seconds/3600),minutes=Math.floor((seconds%3600)/60);return hours?`${hours}h ${minutes}m`:`${minutes}m`};
const userName=id=>{const user=state.users.find(item=>item.id===id);return user?(user.display_name||user.login):"Unassigned"};
const taskName=id=>state.tasks.find(item=>item.id===id)?.title||"No linked task";

async function api(path,options={}){
  const headers={Accept:"application/json",...(options.headers||{})};
  if(options.body&&!headers["Content-Type"])headers["Content-Type"]="application/json";
  if(state.session?.csrf_token&&!['GET','HEAD'].includes((options.method||'GET').toUpperCase()))headers['X-CSRF-Token']=state.session.csrf_token;
  const response=await fetch(path,{...options,headers});
  if(response.status===204)return null;
  const data=await response.json().catch(()=>({detail:`HTTP ${response.status}`}));
  if(!response.ok){const detail=typeof data.detail==='string'?data.detail:(data.detail?.message||JSON.stringify(data.detail));const error=new Error(detail);error.status=response.status;error.data=data;throw error}
  return data;
}
async function safeApi(path,fallback){try{return await api(path)}catch(error){if(error.status===403||error.status===503)return fallback;throw error}}
function showLogin(){$('app').classList.add('hidden');$('login').classList.remove('hidden')}
function showApp(){$('login').classList.add('hidden');$('app').classList.remove('hidden');$('identity').textContent=`${state.session.user.display_name} · ${state.session.user.role}`}
function toast(message){const node=$('toast');node.textContent=message;node.classList.remove('hidden');setTimeout(()=>node.classList.add('hidden'),2600)}

async function configureAuth(){try{const config=await api('/v1/auth/config');$('oauth-login').classList.toggle('hidden',!config.forgejo_oauth);$('oauth-unavailable').classList.toggle('hidden',config.forgejo_oauth);$('login-divider').classList.toggle('hidden',!config.forgejo_oauth)}catch(error){$('oauth-login').classList.add('hidden');$('oauth-unavailable').classList.remove('hidden');$('login-divider').classList.add('hidden')}}
function handleAuthError(error){if(error.status===401){state.session=null;showLogin();return true}return false}
async function boot(){await configureAuth();try{state.session=await api('/v1/session');showApp();await refreshAll();connectEvents()}catch(error){state.session=null;showLogin()}}
function scheduleRefresh(){clearTimeout(state.refreshTimer);state.refreshTimer=setTimeout(()=>refreshAll().catch(error=>{if(!handleAuthError(error))toast(error.message)}),250)}
async function refreshAll(){
  const [summary,tasks,workers,users,runs,activity,workSessions,peopleSummary,accessEvents,nodeHealth,notifications]=await Promise.all([
    api('/v1/dashboard/summary'),api(taskQuery()),api('/v1/workers'),api('/v1/users'),api('/v1/runs?limit=100'),api('/v1/activity?limit=150'),
    api('/v1/work-sessions?limit=200'),api('/v1/people/summary'),safeApi('/v1/access-events?limit=200',[]),api('/v1/nodes/health'),api('/v1/notifications?limit=100')
  ]);
  Object.assign(state,{summary,tasks,workers,users,runs,activity,workSessions,peopleSummary,accessEvents,nodeHealth,notifications});render();
}
function connectEvents(){const events=new EventSource('/v1/events/stream');events.onopen=()=>{$('connection').textContent='Live';$('connection').classList.add('live')};events.onerror=()=>{$('connection').textContent='Reconnecting';$('connection').classList.remove('live')};['task','worker','activity','run','user','time','notification'].forEach(name=>events.addEventListener(name,scheduleRefresh))}
function taskQuery(){const query=new URLSearchParams();if($('task-status-filter')?.value)query.set('status',$('task-status-filter').value);if($('overdue-filter')?.checked)query.set('overdue','true');return `/v1/tasks?${query}`}

function render(){renderSummary();renderTasks();renderWorkers();renderUsers();renderRuns();renderActivity();renderTime();renderNodes();renderAccess();populateAssignments()}
function metric(label,value,alert=false){return `<div class="metric ${alert?'alert':''}"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`}
function renderSummary(){
  const s=state.summary||{tasks:{},runs:{},workers:{}};const failedNotifications=state.notifications.filter(item=>item.status==='failed').length;
  $('summary').innerHTML=metric('Workers online',s.workers.online||0)+metric('Workers busy',s.workers.busy||0)+metric('Tasks in progress',s.tasks.in_progress||0)+metric('Blocked tasks',s.tasks.blocked||0,Boolean(s.tasks.blocked))+metric('Overdue',s.overdue||0,Boolean(s.overdue))+metric('Notification failures',failedNotifications,Boolean(failedNotifications));
  const overdue=state.tasks.filter(task=>task.due_at&&new Date(task.due_at)<new Date()&&!['done','cancelled'].includes(task.status));const blocked=state.tasks.filter(task=>task.status==='blocked');
  $('attention').innerHTML=[...overdue,...blocked.filter(task=>!overdue.includes(task))].slice(0,8).map(task=>`<div class="event"><span class="badge ${esc(task.status)}">${esc(task.status.replace('_',' '))}</span><div><strong>${esc(task.title)}</strong><small> · ${esc(userName(task.assignee_user_id))} · ${esc(fmt(task.due_at))}</small></div></div>`).join('')||'<p class="empty">Nothing requires attention.</p>';
  $('overview-activity').innerHTML=activityMarkup(state.activity.slice(0,8));
}
function renderTasks(){const columns=[['todo','Todo'],['in_progress','In progress'],['blocked','Blocked'],['done','Done']];$('task-board').innerHTML=columns.map(([status,label])=>{const tasks=state.tasks.filter(task=>task.status===status);return `<section class="task-column"><h3>${label}<span class="count">${tasks.length}</span></h3>${tasks.map(taskCard).join('')||'<p class="empty">No tasks</p>'}</section>`}).join('');document.querySelectorAll('[data-task-id]').forEach(button=>button.addEventListener('click',()=>openTask(state.tasks.find(task=>task.id===button.dataset.taskId))))}
function taskCard(task){const overdue=task.due_at&&new Date(task.due_at)<new Date()&&!['done','cancelled'].includes(task.status);return `<article class="task-card priority-${esc(task.priority)} ${overdue?'overdue':''}"><button data-task-id="${esc(task.id)}"><h3>${esc(task.title)}</h3><div class="task-meta"><span class="badge">${esc(task.priority)}</span><span>${esc(userName(task.assignee_user_id))}</span>${task.assignee_worker_id?`<span>${esc(task.assignee_worker_id)}</span>`:''}<span>${overdue?'Overdue · ':''}${esc(fmt(task.due_at))}</span></div></button></article>`}
function renderWorkers(){$('workers').innerHTML=table(['Worker ID','Host','Role','Status','Current run','Capabilities','Last seen','Version'],state.workers.map(worker=>[esc(worker.id),esc(worker.hostname||'—'),esc(worker.role),`<span class="badge ${esc(worker.status)}">${esc(worker.status)}</span>`,esc(worker.current_run_id||'—'),esc((worker.capabilities||[]).join(', ')||'—'),esc(relative(worker.last_seen_at)),esc(worker.version)]))}
function renderUsers(){
  const admin=state.session?.user.role==='admin';const summaries=new Map(state.peopleSummary.map(item=>[item.user.id,item]));
  const rows=state.users.map(user=>{const summary=summaries.get(user.id)||{seconds:{},active_session:null};return [esc(user.display_name||user.login),esc(user.login),admin?`<select data-role="${esc(user.id)}">${['viewer','member','manager','admin'].map(role=>`<option ${role===user.role?'selected':''}>${role}</option>`).join('')}</select>`:esc(user.role),summary.active_session?`<span class="badge busy">${esc(taskName(summary.active_session.task_id))} · ${duration(summary.active_session.duration_seconds)}</span>`:'—',duration(summary.seconds.submitted||0),duration(summary.seconds.approved||0),esc(fmt(user.last_activity_at)),admin?`<button class="button ghost" data-save-user="${esc(user.id)}">Save</button>`:'']});
  $('people').innerHTML=table(['Name','Forgejo login','Role','Active timer','Submitted','Approved','Last activity',''],rows);
  document.querySelectorAll('[data-save-user]').forEach(button=>button.addEventListener('click',async()=>{const target=state.users.find(user=>user.id===button.dataset.saveUser);const role=document.querySelector(`[data-role="${CSS.escape(target.id)}"]`).value;try{await api(`/v1/users/${target.id}`,{method:'PATCH',body:JSON.stringify({role,active:target.active})});toast('User updated');scheduleRefresh()}catch(error){toast(error.message)}}));
}
function renderRuns(){$('runs').innerHTML=table(['Run','Goal','Repository','Status','Created','Updated'],state.runs.map(run=>[esc(run.id.slice(0,8)),esc(run.goal),esc(run.forgejo_repository),`<span class="badge ${esc(run.status)}">${esc(run.status)}</span>`,esc(fmt(run.created_at)),esc(relative(run.updated_at))]))}
function renderActivity(){$('activity').innerHTML=activityMarkup(state.activity)}
function activityMarkup(events){return events.map(event=>`<div class="event"><time datetime="${esc(event.occurred_at)}">${esc(fmt(event.occurred_at))}</time><div><strong>${esc(event.summary)}</strong><br><small>${esc(event.source)} · ${esc(event.event_type)}${event.repository?' · '+esc(event.repository):''}</small></div></div>`).join('')||'<p class="empty">No activity recorded.</p>'}
function table(headers,rows){return `<table><thead><tr>${headers.map(item=>`<th>${esc(item)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(item=>`<td>${item}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${headers.length}" class="empty">No records</td></tr>`}</tbody></table>`}

function renderTime(){
  const own=state.workSessions.find(item=>item.user_id===state.session.user.id&&item.status==='running');
  $('timer-current').innerHTML=own?`<div class="active-timer"><div><strong>${esc(taskName(own.task_id))}</strong><p class="muted">Started ${esc(fmt(own.started_at))} · ${duration(own.duration_seconds)}</p></div><button class="button danger" data-stop-timer="${esc(own.id)}">Stop timer</button></div>`:'<p class="muted">No timer is running.</p>';
  $('timer-form').classList.toggle('hidden',Boolean(own));
  $('timer-task').innerHTML='<option value="">No linked task</option>'+state.tasks.filter(task=>!['done','cancelled'].includes(task.status)).map(task=>`<option value="${esc(task.id)}">${esc(task.title)}</option>`).join('');
  const manager=['admin','manager'].includes(state.session.user.role);
  $('work-sessions').innerHTML=table(['Person','Task','Started','Duration','Status','Notes','Actions'],state.workSessions.map(item=>{let actions='';if(item.user_id===state.session.user.id&&['stopped','rejected'].includes(item.status))actions=`<button class="button ghost" data-submit-time="${esc(item.id)}">Submit</button>`;if(manager&&item.status==='submitted')actions=`<button class="button ghost" data-time-decision="approve" data-session="${esc(item.id)}">Approve</button> <button class="button danger" data-time-decision="reject" data-session="${esc(item.id)}">Reject</button>`;return [esc(userName(item.user_id)),esc(taskName(item.task_id)),esc(fmt(item.started_at)),duration(item.duration_seconds),`<span class="badge ${esc(item.status)}">${esc(item.status)}</span>`,esc(item.notes||'—'),actions]}));
  document.querySelectorAll('[data-stop-timer]').forEach(button=>button.addEventListener('click',()=>timerAction(`/v1/work-sessions/${button.dataset.stopTimer}/stop`,{notes:''},'Timer stopped')));
  document.querySelectorAll('[data-submit-time]').forEach(button=>button.addEventListener('click',()=>timerAction(`/v1/work-sessions/${button.dataset.submitTime}/submit`,{notes:''},'Time submitted')));
  document.querySelectorAll('[data-time-decision]').forEach(button=>button.addEventListener('click',()=>timerAction(`/v1/work-sessions/${button.dataset.session}/decision`,{approved:button.dataset.timeDecision==='approve',reason:''},button.dataset.timeDecision==='approve'?'Time approved':'Time rejected')));
}
async function timerAction(path,body,message){try{await api(path,{method:'POST',body:JSON.stringify(body)});toast(message);await refreshAll()}catch(error){toast(error.message)}}
function renderNodes(){
  const overview=$('grafana-overview');
  const grafanaUrl=String(state.nodeHealth.grafana_url||'');
  const validGrafana=/^https?:\/\//.test(grafanaUrl);
  overview.classList.toggle('hidden',!validGrafana);
  if(validGrafana)overview.href=grafanaUrl;
  if(!state.nodeHealth.available){$('node-health').innerHTML=`<p class="empty">Prometheus unavailable: ${esc(state.nodeHealth.error||'no response')}. Grafana remains available for historical data and diagnostics.</p>`;return}
  $('node-health').innerHTML=table(['Node','Reachable','CPU','Memory','Disk','Details'],state.nodeHealth.nodes.map(node=>{const link=/^https?:\/\//.test(String(node.grafana_url||''))?`<a class="button ghost" href="${esc(node.grafana_url)}" target="_blank" rel="noopener noreferrer">Open in Grafana</a>`:'—';return [esc(node.node_id),`<span class="badge ${node.up?'idle':'offline'}">${node.up?'up':'down'}</span>`,node.cpu_percent==null?'—':`${esc(node.cpu_percent)}%`,node.memory_percent==null?'—':`${esc(node.memory_percent)}%`,node.disk_percent==null?'—':`${esc(node.disk_percent)}%`,link]}))
}
function renderAccess(){$('access-events').innerHTML=table(['Time','Node','Person','Source','Event','Service','Remote identity'],state.accessEvents.map(item=>[esc(fmt(item.occurred_at)),esc(item.node_id),esc(item.user_id?userName(item.user_id):(item.identity||'Unmapped')),esc(item.source),`<span class="badge ${item.event_type==='auth_failed'?'failed':''}">${esc(item.event_type)}</span>`,esc(item.service),esc(item.remote_identity||'—')]))}
function populateAssignments(){const userSelect=$('task-user'),workerSelect=$('task-worker');const oldUser=userSelect.value,oldWorker=workerSelect.value;userSelect.innerHTML='<option value="">Unassigned</option>'+state.users.filter(user=>user.active).map(user=>`<option value="${esc(user.id)}">${esc(user.display_name||user.login)}</option>`).join('');workerSelect.innerHTML='<option value="">Unassigned</option>'+state.workers.map(worker=>`<option value="${esc(worker.id)}">${esc(worker.id)} · ${esc(worker.status)}</option>`).join('');userSelect.value=oldUser;workerSelect.value=oldWorker}
function openTask(task=null){$('task-form').reset();$('task-error').textContent='';$('task-id').value=task?.id||'';$('task-version').value=task?.version||'';$('task-form-title').textContent=task?'Edit task':'New task';$('task-title').value=task?.title||'';$('task-description').value=task?.description||'';$('task-status').value=task?.status||'todo';$('task-priority').value=task?.priority||'normal';$('task-due').value=task?.due_at?new Date(new Date(task.due_at).getTime()-new Date(task.due_at).getTimezoneOffset()*60000).toISOString().slice(0,16):'';$('task-user').value=task?.assignee_user_id||'';$('task-worker').value=task?.assignee_worker_id||'';$('task-repository').value=task?.repository||'';$('archive-task').classList.toggle('hidden',!task||!['admin','manager'].includes(state.session.user.role));$('task-dialog').showModal()}
function taskPayload(){const due=$('task-due').value;return {title:$('task-title').value.trim(),description:$('task-description').value.trim(),status:$('task-status').value,priority:$('task-priority').value,due_at:due?new Date(due).toISOString():null,assignee_user_id:$('task-user').value||null,assignee_worker_id:$('task-worker').value||null,repository:$('task-repository').value.trim()||null}}

$('login-form').addEventListener('submit',async event=>{event.preventDefault();$('login-error').textContent='';try{const result=await api('/auth/token',{method:'POST',body:JSON.stringify({token:$('token').value})});state.session={user:result.user,csrf_token:result.csrf_token};showApp();await refreshAll();connectEvents()}catch(error){$('login-error').textContent=error.message}});
$('logout').addEventListener('click',async()=>{await api('/auth/logout',{method:'POST'}).catch(()=>null);state.session=null;showLogin()});
document.querySelectorAll('.tabs button').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.tabs button').forEach(item=>item.classList.toggle('active',item===button));document.querySelectorAll('.view').forEach(view=>view.classList.toggle('active',view.id===`view-${button.dataset.view}`))}));
$('new-task').addEventListener('click',()=>openTask());$('close-task').addEventListener('click',()=>$('task-dialog').close());$('cancel-task').addEventListener('click',()=>$('task-dialog').close());
$('task-form').addEventListener('submit',async event=>{event.preventDefault();const id=$('task-id').value;const payload=taskPayload();if(id)payload.version=Number($('task-version').value);try{await api(id?`/v1/tasks/${id}`:'/v1/tasks',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});$('task-dialog').close();toast(id?'Task updated':'Task created');await refreshAll()}catch(error){$('task-error').textContent=error.status===409?'This task changed elsewhere. Close and reopen it.':error.message}});
$('archive-task').addEventListener('click',async()=>{const id=$('task-id').value;if(!id||!confirm('Archive this task? Its audit history will be retained.'))return;try{await api(`/v1/tasks/${id}`,{method:'DELETE'});$('task-dialog').close();toast('Task archived');await refreshAll()}catch(error){$('task-error').textContent=error.message}});
$('task-status-filter').addEventListener('change',async()=>{state.tasks=await api(taskQuery());renderTasks()});$('overdue-filter').addEventListener('change',async()=>{state.tasks=await api(taskQuery());renderTasks()});
$('timer-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/v1/work-sessions/start',{method:'POST',body:JSON.stringify({task_id:$('timer-task').value||null,notes:$('timer-notes').value.trim()})});$('timer-form').reset();toast('Timer started');await refreshAll()}catch(error){toast(error.message)}});
boot();
