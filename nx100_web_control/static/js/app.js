'use strict';
const $ = id => document.getElementById(id);
const axes = ['x','y','z','rx','ry','rz'];
const jointAxes = ['s','l','u','r','b','t'];
let config, state, tab = 'cartesian', busy = false;
let target = {}, speedValues = {MOVL:5,MOVJ:2}, logs = [], lastLog = 0, toastTimer;
let latestStatusRequest = 0, autoTimer = null, editVersion = 0, dirty = false;
const keyboardDrafts = new Set();

function cancelAutoMove() {
  clearTimeout(autoTimer); autoTimer = null; editVersion++; dirty = false; keyboardDrafts.clear();
}
function scheduleAutoMove(delay = config.auto_send_delay_ms) {
  clearTimeout(autoTimer); autoTimer = null;
  dirty = true;
  const version = ++editVersion;
  const reasons = blockers();
  if (busy || reasons.length || document.hidden) {
    dirty = keyboardDrafts.size > 0; renderControls(); return;
  }
  autoTimer = setTimeout(async () => {
    autoTimer = null;
    if (version !== editVersion || busy || document.hidden || blockers().length) {
      dirty = false; renderControls(); return;
    }
    const body = {motion: motion(), speed: Number(speedValues[motion()]), ...target};
    dirty = false;
    // Only this user-edit timer sends motion. Polling never calls this function.
    await action(async () => {
      state = await api('/api/move/cartesian', body);
      renderState();
      toast('COMMAND ACCEPTED — đang kiểm tra vị trí.');
    });
  }, delay);
  renderControls();
}

function toast(message, error=false) {
  $('toast').textContent = message; $('toast').className = 'toast' + (error?' error':'');
  $('toast').hidden=false; clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>{$('toast').hidden=true;},7000);
}
async function api(path, body, options={}) {
  const abort = new AbortController();
  const timeout = setTimeout(()=>abort.abort(), 45000);
  try {
    const response=await fetch(path,{method:body===undefined?'GET':'POST',
      headers:body===undefined?{}:{'Content-Type':'application/json','X-NX100-Control':'local'},
      body:body===undefined?undefined:JSON.stringify(body),signal:abort.signal});
    const data=await response.json();
    if(!response.ok) {
      const message=data.message || (Array.isArray(data.detail)?data.detail.map(x=>`${x.loc.join('.')}: ${x.msg}`).join('; '):'Request failed');
      const error=new Error(`${data.code||response.status}: ${message}`); error.code=data.code; throw error;
    }
    return data;
  } catch(error) {
    if(error.name==='AbortError') throw new Error('COMMUNICATION TIMEOUT — kết quả chưa xác định. Đọc lại trạng thái; không tự gửi lại MOVE.');
    throw error;
  } finally {clearTimeout(timeout);}
}
async function action(task) {
  if(busy) return;
  busy=true; renderControls();
  try {await task();}
  catch(error) {toast(error.message,true); addLocalLog('ERROR',error.message);}
  finally {busy=false; await pollStatus(); renderControls();}
}
function addLocalLog(level,message) {
  logs.push({time:new Date().toISOString(),level,message}); logs=logs.slice(-600);renderLogs();
}
function renderLogs() {
  const container=$('log'), stick=container.scrollHeight-container.scrollTop-container.clientHeight<45;
  const visible=logs.filter(e=>$('rawToggle').checked||!['TX','RX'].includes(e.level));
  container.replaceChildren(...visible.map(e=>{
    const row=document.createElement('div');row.className='log-entry'+(e.level==='ERROR'?' error':'')+(['TX','RX'].includes(e.level)?' raw':'');
    const time=document.createElement('time');time.textContent=new Date(e.time).toLocaleTimeString('en-GB');
    const level=document.createElement('span');level.className='level';level.textContent=e.level;
    const message=document.createElement('span');message.className='message';message.textContent=e.message;
    row.append(time,level,message);return row;
  }));
  if(stick) container.scrollTop=container.scrollHeight;
}
function motion() {return tab==='joint'?'MOVJ':$('motion').value;}
function makeAxes(values={}) {
  const keys=tab==='cartesian'?axes:jointAxes;
  const specs=config[tab]; target={};
  $('axes').replaceChildren(...keys.map((key,index)=>{
    const spec=specs[key], row=document.createElement('div');row.className='axis';
    const label=tab==='cartesian'?key.toUpperCase():`J${index+1} / ${key.toUpperCase()}`;
    row.innerHTML=`<div class="axis-head"><label for="axis-${key}">${label}<small>${spec.unit}</small></label><span class="axis-current" id="current-${key}">NOW —</span></div><div class="axis-inputs"><button aria-label="Decrease ${label}">−</button><input id="axis-${key}" type="number" step="${spec.step}" aria-label="Target ${label}"><button aria-label="Increase ${label}">+</button></div><input type="range" aria-label="${label} target slider" step="${spec.step}" min="${spec.min}" max="${spec.max}"><div class="range-labels"><span>${spec.min.toLocaleString()}</span><span>${spec.max.toLocaleString()}</span></div>`;
    const number=row.querySelector('input[type=number]'), slider=row.querySelector('input[type=range]');
    const precision=tab==='joint'?0:key.length>1?2:3;
    const update=(value,clamp=false,userEdit=false)=>{
      let n=Number(value);
      if(value===''||!Number.isFinite(n)){target[key]=null;renderControls();return;}
      if(clamp)n=Math.max(Number(slider.min),Math.min(Number(slider.max),n));
      n=Number(n.toFixed(precision));target[key]=n;number.value=n.toFixed(precision);
      // Expand presentation range for actual readings; never silently clamp robot pose.
      slider.min=Math.min(spec.min,n);slider.max=Math.max(spec.max,n);slider.value=n;
      const limits=row.querySelectorAll('.range-labels span');limits[0].textContent=Number(slider.min).toLocaleString();limits[1].textContent=Number(slider.max).toLocaleString();
      renderControls();
      if(userEdit)scheduleAutoMove();
    };
    number.title='Nhập tọa độ rồi nhấn Enter để gửi. Rời ô không gửi lệnh.';
    number.addEventListener('input',()=>{
      // Keep keyboard drafts separate from the target sent by sliders/nudges.
      clearTimeout(autoTimer); autoTimer=null; editVersion++; dirty=true; keyboardDrafts.add(key);
      const value=number.valueAsNumber;
      if(Number.isFinite(value)){slider.min=Math.min(spec.min,value);slider.max=Math.max(spec.max,value);slider.value=value;}
      renderControls();
    });
    number.addEventListener('keydown',event=>{
      if(event.key!=='Enter'||event.isComposing)return;
      event.preventDefault();
      if(event.repeat)return;
      if(number.value===''||!Number.isFinite(number.valueAsNumber)){
        toast('Nhập tọa độ hợp lệ trước khi nhấn Enter.',true);return;
      }
      keyboardDrafts.delete(key);update(number.value);scheduleAutoMove(0);
    });
    // No change/blur handler: only Enter commits a typed number.
    slider.addEventListener('input',()=>{keyboardDrafts.delete(key);update(slider.value,false,true);});
    const buttons=row.querySelectorAll('button');
    buttons[0].onclick=()=>{keyboardDrafts.delete(key);update((Number(target[key])||0)-spec.nudge,true,true);};
    buttons[1].onclick=()=>{keyboardDrafts.delete(key);update((Number(target[key])||0)+spec.nudge,true,true);};
    const value=values[key];
    if(value!==undefined)update(value);else{target[key]=null;number.value='';slider.disabled=true;}
    return row;
  }));
  renderControls();
}
function setTab(value) {
  cancelAutoMove();tab=value;
  $('cartTab').classList.toggle('active',tab==='cartesian');$('jointTab').classList.toggle('active',tab==='joint');
  $('cartTab').setAttribute('aria-selected',String(tab==='cartesian'));$('jointTab').setAttribute('aria-selected',String(tab==='joint'));
  $('motion').disabled=tab==='joint';$('motion').hidden=tab==='joint';
  $('jointNotice').hidden=tab!=='joint';$('frameLabel').textContent=tab==='joint'?'JOINT COORDINATES · PULSE':'BASE FRAME · ABSOLUTE';
  $('autoNotice').hidden=tab==='joint';
  $('referenceInfo').textContent='Đang đọc vị trí realtime…';
  makeAxes(state?.connected ? state[tab] || {} : {});setSpeed();renderState();
}
function setSpeed() {
  const kind=motion(),spec=config.speed[kind.toLowerCase()];
  for(const id of ['speedNumber','speedSlider']){const el=$(id);el.min=spec.min;el.max=spec.max;el.step=spec.step;el.value=speedValues[kind];}
  $('speedUnit').textContent=spec.unit;$('speedMin').textContent=`${spec.min} ${spec.unit}`;$('speedMax').textContent=`${spec.max} ${spec.unit}`;
  $('motionHint').textContent=tab==='joint'?'Pulse readout · move unavailable':kind==='MOVL'?'Linear · straight TCP path':'Joint motion · Cartesian target';
}
function updateSpeed(value) {
  cancelAutoMove();
  const n=Number(value),kind=motion(),spec=config.speed[kind.toLowerCase()];
  speedValues[kind]=value===''?null:n;
  if(Number.isFinite(n)&&n>=spec.min&&n<=spec.max)$('speedSlider').value=n;
  renderControls();
}
function blockers() {
  if(!state?.connected)return ['NX100 DISCONNECTED'];
  if(state.error)return [state.error.message];
  const f=state.flags;
  if(!f||state.status_age_ms>Math.max(4000,config.poll_interval_ms*3))return ['STATUS STALE — READ STATUS'];
  const result=[];
  if(!f.remote)result.push('NX100 NOT IN COMMAND REMOTE');
  for(const key of ['alarm','error','hold','running'])if(f[key])result.push(key==='running'?'ROBOT RUNNING':`${key.toUpperCase()} ON`);
  if(!f.servo)result.push('SERVO OFF');
  if(['COMMAND ACCEPTED','MOVING / VERIFYING'].includes(state.motion?.phase))result.push('MOVE BEING MONITORED');
  if(tab==='joint')result.push('PULSE MOVE NOT SUPPORTED');
  else if(!state.cartesian)result.push('WAITING FOR LIVE POSITION');
  if(keyboardDrafts.size)result.push('PRESS ENTER TO CONFIRM: '+[...keyboardDrafts].map(key=>key.toUpperCase()).join(', '));
  if(Object.values(target).some(v=>v===null||!Number.isFinite(v)))result.push('ENTER ALL TARGET VALUES');
  const speed=speedValues[motion()],spec=config.speed[motion().toLowerCase()];
  if(!Number.isFinite(speed)||speed<spec.min||speed>spec.max)result.push('INVALID SPEED');
  if(state.hold_pending)result.push('HOLD PENDING');
  return result;
}
function renderControls() {
  if(!config)return;
  const connected=!!state?.connected, active=['COMMAND ACCEPTED','MOVING / VERIFYING'].includes(state?.motion?.phase);
  $('connect').disabled=connected||busy;$('disconnect').disabled=!connected||busy;
  $('ip').disabled=connected||busy;$('port').disabled=connected||busy;
  $('readStatus').disabled=!connected||busy;
  $('reset').disabled=busy||active;
  $('hold').disabled=!connected; // HOLD stays available during MOVE/API requests.
  $('cartTab').disabled=busy;$('jointTab').disabled=busy;
  $('motion').disabled=busy||tab==='joint';
  const reasons=blockers();
  $('readiness').textContent=reasons.length?'AUTO MOVE BLOCKED':autoTimer?'AUTO SEND PENDING':busy?'SENDING COMMAND':'AUTO SEND READY';
  $('blockedReason').textContent=reasons.length?reasons.join(' · '):`Nhập số → Enter để chạy. Slider / + − → tự gửi sau ${config.auto_send_delay_ms} ms.`;
  for(const el of $('axes').querySelectorAll('input,button'))el.disabled=busy||active||!connected||tab==='joint'||!state?.cartesian;
}
function renderState() {
  if(!state)return;
  $('connection').className='connection '+(state.connected?'connected':'disconnected');
  $('connection').replaceChildren();const dot=document.createElement('i');$('connection').append(dot,document.createTextNode(state.connected?'CONNECTED':'DISCONNECTED'));
  const stale=!state.connected||state.status_age_ms>Math.max(4000,config.poll_interval_ms*3);
  $('heartbeat').className=stale?'':'fresh';
  $('freshness').textContent=state.status_time?`${stale?'Last status':'Updated'} ${Math.round(state.status_age_ms/1000)}s ago`:'No status received';
  const statusKeys=['remote','play','teach','servo','running','hold','alarm','error'];
  $('statusGrid').replaceChildren(...statusKeys.map(key=>{
    const card=document.createElement('div'),on=state.connected&&state.flags?.[key];
    card.className='status-card'+(on?' on':'')+(['hold','alarm','error'].includes(key)?' warning':'');
    const label=document.createElement('span');label.textContent=key==='remote'?'CMD REMOTE':key.toUpperCase();
    const value=document.createElement('b');value.textContent=!state.connected?'—':on?'ON':'OFF';card.append(label,value);return card;
  }));
  const current=state[tab];
  const active=['COMMAND ACCEPTED','MOVING / VERIFYING'].includes(state.motion?.phase);
  // Realtime updates never dispatch input/change and never schedule a MOVE.
  if(state.connected && current && !dirty && !busy && !active && !$('axes').contains(document.activeElement))makeAxes(current);
  $('referenceInfo').textContent=!state.connected?'Chờ kết nối để đọc vị trí realtime.':tab==='cartesian'&&current?`BASE · Tool ${current.tool} · Posture ${current.posture} · Realtime ${config.poll_interval_ms} ms`:'S/L/U/R/B/T · pulse · Realtime · Chỉ đọc';
  for(const key of tab==='cartesian'?axes:jointAxes)if($(`current-${key}`))$(`current-${key}`).textContent=current?`NOW ${Number(current[key]).toFixed(tab==='joint'?0:2)}`:'NOW —';
  const m=state.motion;
  $('motionPhase').textContent=m?.phase||'IDLE';$('motionMessage').textContent=state.error?`${state.error.code}: ${state.error.message}`:m?.message||'Vị trí cập nhật realtime. Nhập số rồi Enter; slider / + − tự gửi.';
  $('positionRows').replaceChildren(...['TARGET','CURRENT','Δ ERROR'].map(label=>{
    const row=document.createElement('tr'),head=document.createElement('td');head.textContent=label;row.append(head);
    const values=label==='CURRENT'?state.cartesian:label==='TARGET'?m?.target:m?.delta;
    for(const key of axes){const cell=document.createElement('td');cell.textContent=values?Number(values[key]).toFixed(3):'—';row.append(cell);}
    return row;
  }));
  $('positionTime').textContent=state.cartesian?`BASE · ${new Date(state.cartesian.timestamp*1000).toLocaleTimeString('en-GB')}`:'No position received';
  if(!state.connected || state.hold_pending || state.flags?.hold || state.flags?.alarm || state.flags?.error || state.flags?.running || !state.flags?.remote || !state.flags?.servo)cancelAutoMove();
  renderControls();
}
async function pollStatus() {
  const seq=++latestStatusRequest;
  try {
    const next=await api('/api/status');
    if(seq!==latestStatusRequest)return;
    state=next;renderState();
  } catch(error) {
    cancelAutoMove();state={...state,connected:false,flags:null,error:{code:'BACKEND UNAVAILABLE',message:error.message}};renderState();
  }
}
async function tick() {
  await pollStatus();
  try{const data=await api(`/api/logs?after=${lastLog}`);for(const event of data.events){logs.push(event);lastLog=Math.max(lastLog,event.id);}logs=logs.slice(-600);if(data.events.length)renderLogs();}catch{/* status panel already indicates failure */}
  setTimeout(tick,config.poll_interval_ms);
}
async function init() {
  try{config=await api('/api/config');}catch(error){toast('Không tải được cấu hình: '+error.message,true);return;}
  $('modeBadge').textContent=config.mode==='MOCK'?'MOCK / SIMULATOR':'REAL / ROBOT';$('mockBanner').hidden=config.mode!=='MOCK';
  $('ip').value=config.robot_ip;$('port').value=config.robot_port;
  speedValues={MOVL:config.speed.movl.default,MOVJ:config.speed.movj.default};
  $('xyzTolerance').textContent=`±${config.tolerance.xyz_mm} mm`;$('angleTolerance').textContent=`±${config.tolerance.angle_deg}°`;
  $('autoNotice').textContent=`Nhập số: nhấn ENTER mới chạy · Slider / + −: tự gửi sau ${config.auto_send_delay_ms} ms ngừng thay đổi.`;
  $('cartTab').onclick=()=>setTab('cartesian');$('jointTab').onclick=()=>setTab('joint');
  $('motion').onchange=()=>{cancelAutoMove();setSpeed();renderControls();};
  $('speedNumber').oninput=()=>updateSpeed($('speedNumber').value);
  $('speedSlider').oninput=()=>{$('speedNumber').value=$('speedSlider').value;updateSpeed($('speedSlider').value);};
  for(const [id,sign] of [['speedMinus',-1],['speedPlus',1]])$(id).onclick=()=>{const spec=config.speed[motion().toLowerCase()];const value=Math.max(spec.min,Math.min(spec.max,(Number(speedValues[motion()])||spec.min)+sign*spec.step));$('speedNumber').value=value.toFixed(2);updateSpeed($('speedNumber').value);};
  $('connect').onclick=()=>{cancelAutoMove();action(async()=>{state=await api('/api/connect',{ip:$('ip').value.trim(),port:Number($('port').value)});makeAxes(state[tab]);renderState();toast('CONNECTED — nhập số rồi Enter; slider / + − tự gửi.');});};
  $('disconnect').onclick=()=>{cancelAutoMove();action(async()=>{state=await api('/api/disconnect',{});renderState();});};
  $('readStatus').onclick=()=>{cancelAutoMove();action(async()=>{state=await api('/api/status?refresh=true');renderState();toast('Status updated');});};
  $('hold').onclick=async()=>{
    cancelAutoMove();renderControls();
    $('hold').textContent='HOLD REQUESTED…';
    try{await api('/api/hold',{});toast('HOLD ACCEPTED — kiểm tra trạng thái robot.');}
    catch(error){toast(error.message,true);addLocalLog('ERROR',error.message);}
    finally{$('hold').textContent='Ⅱ  HOLD';await pollStatus();}
  };
  $('reset').onclick=()=>{cancelAutoMove();makeAxes(state?.connected?state[tab]||{}:{});speedValues={MOVL:config.speed.movl.default,MOVJ:config.speed.movj.default};setSpeed();addLocalLog('INFO','RESET UI — no robot command sent');};
  $('rawToggle').onchange=renderLogs;
  window.addEventListener('focus',pollStatus);
  window.addEventListener('blur',()=>{cancelAutoMove();renderControls();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){cancelAutoMove();renderControls();}});
  setTab('cartesian');addLocalLog('INFO',`Local panel ready · ${config.mode} · No auto-connect`);tick();
}
init();
