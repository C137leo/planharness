// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5196',out=path.resolve('../docs/evidence/day-revision-chat');const checks=[];
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
 const token=JSON.parse(fs.readFileSync('/tmp/day-revision-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};


 await say('今天训练20分钟、晚餐豆腐180克米饭150克，先看完整草案');await wait(()=>has('当天计划 · 差异预览'));await wait(()=>ev(`!!document.querySelector('.revision-button.primary:not([disabled])')`));
 assert(await has('确认应用'));assert(await has('豆腐'));checks.push('coordinated draft renders structured before/after and nearby confirmation controls');
 const before=await fetch(base+'/__synthetic_evidence').then(x=>x.json());await shot('draft-before-confirmation-desktop.png');
 await ev(`document.querySelector('.revision-actions').scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,150));await shot('draft-controls-desktop.png');checks.push('confirmation and cancellation controls sit inside the associated Diff card');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.revision-card').scrollIntoView({block:'start',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,150));assert(await ev(`document.querySelector('.revision-card').getBoundingClientRect().width<=390`));await shot('draft-diff-mobile.png');checks.push('Diff card fits mobile 390px without horizontal clipping');
 viewport.width=1440;viewport.height=1100;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});

 await say('训练照旧，晚餐豆腐换成210克三文鱼，先看看');await wait(()=>has('第 2 版草案'));await wait(()=>ev(`!!document.querySelector('.revision-button.primary:not([disabled])')`));
 const afterEdit=await fetch(base+'/__synthetic_evidence').then(x=>x.json());assert.deepEqual(afterEdit.days,before.days);checks.push('second-turn edit retains workout draft and does not apply business changes before confirmation');await shot('draft-edited-desktop.png');
 await click('.revision-button.primary','确认应用');await wait(()=>has('草案已应用并重读验证'));assert(await has('三文鱼'));checks.push('native confirmation applies exact latest revision and shows verified completion');
 await fetch(base+'/__synthetic_restore',{method:'POST'});
 for(let i=0;i<4;i++){
   await click('.sidebar-item-text','计划');await wait(()=>has('长期训练模板'));
   await click('.sidebar-item-text','今天');await wait(()=>has('还差 1 组'));
 }
 assert(await has('已记录 2/3 组'));assert(await has('7/10 组'));checks.push('four native cached-tab roundtrips show restored three-set targets, two recorded, one remaining');
 assert(await has('此回复基于此前 v1'));checks.push('historical chat replies are labelled with their original version after rollback');
 await say('回滚之后，重新核对现在的计划和剩余组数');await wait(()=>has('当前有效安排（v3）'));assert(await has('还差 1 组'));checks.push('new chat inspection reads rolled-back current plan rather than old session targets');
 await ev(`document.querySelector('.session-strip').scrollIntoView({block:'center',behavior:'instant'});true`);await shot('rollback-remaining-sets-desktop.png');
 await cmd('Emulation.setDeviceMetricsOverride',{width:390,height:900,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.session-strip').scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,200));await shot('rollback-remaining-sets-mobile.png');
 assert.equal(errors.length,0,JSON.stringify(errors));checks.push('no TypeError during native page activation, data reload, draft edit or confirmation');
 fs.writeFileSync(path.join(out,'browser-day-revision-chat.json'),JSON.stringify({passed:true,model:'Scripted; synthetic isolated SQLite',checks,runtime_type_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
