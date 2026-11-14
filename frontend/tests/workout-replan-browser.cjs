// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5197',out=path.resolve('../docs/evidence/workout-replan');const checks=[];
 const targets=await(await fetch('http://127.0.0.1:9244/json/list')).json(),ws=new WS((targets.find(x=>x.type==='page') || await(await fetch('http://127.0.0.1:9244/json/new?about:blank',{method:'PUT'})).json()).webSocketDebuggerUrl);
 await new Promise(r=>ws.on('open',r));let id=0;const pending=new Map();ws.on('message',raw=>{const m=JSON.parse(raw),p=pending.get(m.id);if(p){pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(m.error):p.resolve(m.result)}});
 const cmd=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;const timer=setTimeout(()=>{pending.delete(n);reject(Error('CDP timeout '+method))},15000);pending.set(n,{resolve,reject,timer});ws.send(JSON.stringify({id:n,method,params}))});
 const ev=async expression=>{const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw r.exceptionDetails;return r.result.value};
 const wait=async fn=>{const at=Date.now();while(Date.now()-at<15000){if(await fn())return;await new Promise(r=>setTimeout(r,100))}throw Error('UI wait timed out')};
 const body=()=>ev('document.body.innerText'),has=async s=>(await body()).includes(s);
 const viewport={width:1440,height:1100};
 const click=async(selector,label)=>{
  await cmd('Page.bringToFront');
  const q=`[...document.querySelectorAll(${JSON.stringify(selector)})].filter(e=>e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0).find(e=>${label?`e.textContent.trim()===${JSON.stringify(label)}`:'true'})`;
  await wait(()=>ev(`!!${q} && !${q}.getAttribute('disabled') && !${q}.hasAttribute('disabled')`));
  await new Promise(r=>setTimeout(r,180));
  await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',x:Math.min(viewport.width-50,1000),y:350,deltaX:0,deltaY:1});
  await ev(`${q}.scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,180));
  const box=await ev(`(()=>{const e=${q},r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,hit:e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}})()`);
  assert(box.hit,'button must receive a real pointer: '+selector+' '+label);
  for(const type of ['mousePressed','mouseReleased'])await cmd('Input.dispatchMouseEvent',{type,x:box.x,y:box.y,button:'left',clickCount:1});
 };
 const fill=async(selector,text)=>{await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.focus();e.select()})();true`);await cmd('Input.insertText',{text})};
 const shot=async name=>{const r=await cmd('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,name),Buffer.from(r.data,'base64'))};

 const info=()=>fetch(base+'/__mock_evidence').then(x=>x.json());
 const errors=[];ws.on('message',raw=>{const m=JSON.parse(raw);if(['Runtime.exceptionThrown','Runtime.consoleAPICalled'].includes(m.method)){const value=JSON.stringify(m.params);if(value.includes('TypeError'))errors.push(value.slice(0,1000))}});await cmd('Runtime.enable');
 await cmd('Page.enable');await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});
 await cmd('Page.navigate',{url:base});await wait(()=>ev('document.readyState==="complete"'));
 const token=JSON.parse(fs.readFileSync('/tmp/workout-replan-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};



 const snapshot=()=>fetch(base+'/__synthetic_evidence').then(x=>x.json());
 const headers=JSON.parse(fs.readFileSync('/tmp/workout-replan-ui-auth.json'));
 const api=async(url,body)=>{const r=await fetch(base+'/api/v1'+url,{method:body?'POST':'GET',headers:{...headers,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const j=await r.json();assert(r.ok,JSON.stringify(j));return j.data};
 const initial=await snapshot(),day=Object.keys(initial.days)[0];
 await wait(()=>has('7/10 组'));assert(await has('目标未完成'));checks.push('historical finished status with stale done flags renders 7/10 and incomplete');
 await say('帮我安排一下今天的训练');await wait(()=>has('尚未全部完成'));checks.push('chat inspection reads current counts and asks continue versus replan');
 await say('保留已做记录，今天重新排20分钟');await wait(()=>has('保留已有记录，放弃旧方案未完成部分？'));
 await wait(()=>ev(`!!document.querySelector('.replan-button.primary:not([disabled])')`));await shot('abandonment-consent-desktop.png');
 let state=await snapshot();assert.deepEqual(initial.days,state.days);checks.push('first replan request asks explicit consent without changing the current plan');
 await click('.replan-button','继续原方案');await wait(()=>has('已取消重排'));state=await snapshot();assert.deepEqual(initial.days,state.days);checks.push('native cancel retains existing plan and actual training');
 await say('还是保留已有记录重新安排20分钟吧');await wait(()=>ev(`[...document.querySelectorAll('.replan-button.primary')].some(e=>!e.disabled)`));
 await click('.replan-button.primary','保留记录，重新规划');await wait(()=>has('当天计划 · 差异预览'));await wait(()=>ev(`!!document.querySelector('.revision-button.primary:not([disabled])')`));
 state=await snapshot();assert.deepEqual(initial.days,state.days);assert(await has('进度'));checks.push('consent generates a replacement preview and keeps old plan until final application');
 await shot('replacement-preview-desktop.png');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.revision-card').scrollIntoView({block:'start',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,180));assert(await ev(`document.querySelector('.revision-card').getBoundingClientRect().width<=390`));await shot('replacement-preview-mobile.png');checks.push('replacement preview and controls fit 390px mobile');
 viewport.width=1440;viewport.height=1100;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});
 await click('.revision-button.primary','确认应用');await wait(()=>has('草案已应用并重读验证'));await wait(()=>has('0/7 组'));assert(await has('重排前的已有记录'));checks.push('final application gives same-name new actions zero credit and lists seven old recorded sets separately');
 await ev(`document.querySelector('.previous-training').scrollIntoView({block:'center',behavior:'instant'});true`);await shot('new-cycle-zero-and-history.png');
 const active=await api('/workout-sessions/active?date='+day);assert.equal(active.session,null);checks.push('active session endpoint does not return an old-cycle session');
 const started=await api('/workout-sessions',{date:day});await api('/workout-sessions/'+started.session.session_id+'/sets',{});
 await click('.sidebar-item-text','计划');await wait(()=>has('长期训练模板'));await click('.sidebar-item-text','今天');await wait(()=>has('1/7 组'));assert(await has('重排前的已有记录'));checks.push('new actual warmup set advances only new-cycle progress to 1/7');
 for(let i=0;i<3;i++){await click('.sidebar-item-text','计划');await wait(()=>has('长期训练模板'));await click('.sidebar-item-text','今天');await wait(()=>has('1/7 组'))}checks.push('three native cached-page roundtrips retain correct cycle and no null scrollTop');
 await cmd('Emulation.setDeviceMetricsOverride',{width:390,height:900,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.session-strip').scrollIntoView({block:'start',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,200));await shot('new-cycle-progress-mobile.png');
 assert.equal(errors.length,0,JSON.stringify(errors));checks.push('no runtime TypeError during cancellation, two confirmations, start and cached tabs');
 fs.writeFileSync(path.join(out,'browser-workout-replan.json'),JSON.stringify({passed:true,model:'Scripted; synthetic isolated SQLite',checks,runtime_type_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
