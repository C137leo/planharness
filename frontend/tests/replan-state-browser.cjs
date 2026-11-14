// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5199',out=path.resolve('../docs/evidence/replan-state-recovery');const checks=[];
 const targets=await(await fetch('http://127.0.0.1:9244/json/list')).json(),ws=new WS((await(await fetch('http://127.0.0.1:9244/json/new?about:blank',{method:'PUT'})).json()).webSocketDebuggerUrl);
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
  await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',x:300,y:350});await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',x:Math.min(viewport.width-50,1000),y:350,deltaX:0,deltaY:1});
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
 const token=JSON.parse(fs.readFileSync('/tmp/replan-state-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};




 const snapshot=()=>fetch(base+'/__synthetic_evidence').then(x=>x.json());
 const old=JSON.parse(fs.readFileSync('/tmp/replan-state-ui-case.json'));
 const headers=JSON.parse(fs.readFileSync('/tmp/replan-state-ui-auth.json'));
 const initial=await snapshot();
 const outdated=await fetch(base+'/api/v1/agent/workout-replans/'+old.authorization_id,{headers}).then(x=>x.json());assert.equal(outdated.data.status,'expired');assert.equal(outdated.data.stored_status,'approved');assert.equal(outdated.data.usable_for_creation,false);checks.push('expired stored approval is presented as expired and unusable by authenticated API');
 await say('给我重新生成一份训练计划');await wait(()=>has('此前重排确认已过期'));await wait(()=>ev(`!!document.querySelector('.replan-button.primary:not([disabled])')`));
 const consentState=await snapshot();assert.deepEqual(consentState.days,initial.days);assert.equal(consentState.tables.day_plan_revisions.length,0);assert.equal(consentState.tables.workout_replan_authorizations.length,2);checks.push('incorrect planner use of expired ID produces a real nearby consent card rather than repeated error');await shot('expired-consent-recovered-desktop.png');
 await say('我确认放弃原来的部分');await wait(()=>has('当天计划 · 差异预览'));await wait(()=>ev(`!!document.querySelector('.revision-button.primary:not([disabled])')`));
 const preview=await snapshot();assert.deepEqual(preview.days,initial.days);assert.equal(preview.tables.day_plan_revisions.length,1);checks.push('native text confirmation advances the same saved goal to preview without applying business data');await shot('natural-confirmation-preview-desktop.png');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.revision-card').scrollIntoView({block:'start',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,180));assert(await ev(`document.querySelector('.revision-card').getBoundingClientRect().width<=390`));await shot('natural-confirmation-preview-mobile.png');checks.push('recovered preview fits 390px mobile');
 viewport.width=1440;viewport.height=1100;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});
 await click('.revision-button.primary','确认应用');await wait(()=>has('草案已应用并重读验证'));await wait(()=>has('0/7 组'));assert(await has('重排前的已有记录'));checks.push('separate native apply confirmation starts new target at zero and keeps seven old sets');
 const applied=await snapshot();assert.deepEqual(applied.tables.workout_logs,initial.tables.workout_logs);assert.equal(applied.tables.day_plan_revisions[0].status,'applied');checks.push('old actual logs remain byte-for-byte equivalent after application');
 assert.equal(errors.length,0,JSON.stringify(errors));checks.push('no runtime TypeError during recovery, natural confirmation and application');
 fs.writeFileSync(path.join(out,'browser-replan-state.json'),JSON.stringify({passed:true,model:'Scripted synthetic SQLite; not an NLU accuracy measurement',checks,runtime_type_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
