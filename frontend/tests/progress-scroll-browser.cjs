// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5195',out=path.resolve('../docs/evidence/progress-scroll');const checks=[];
 const targets=await(await fetch('http://127.0.0.1:9244/json/list')).json(),ws=new WS((targets.find(x=>x.type==='page') || await(await fetch('http://127.0.0.1:9244/json/new?about:blank',{method:'PUT'})).json()).webSocketDebuggerUrl);
 await new Promise(r=>ws.on('open',r));let id=0;const pending=new Map();ws.on('message',raw=>{const m=JSON.parse(raw),p=pending.get(m.id);if(p){pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(m.error):p.resolve(m.result)}});
 const cmd=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;const timer=setTimeout(()=>{pending.delete(n);reject(Error('CDP timeout '+method))},15000);pending.set(n,{resolve,reject,timer});ws.send(JSON.stringify({id:n,method,params}))});
 const ev=async expression=>{const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw r.exceptionDetails;return r.result.value};
 const wait=async fn=>{const at=Date.now();while(Date.now()-at<15000){if(await fn())return;await new Promise(r=>setTimeout(r,100))}throw Error('UI wait timed out')};
 const body=()=>ev('document.body.innerText'),has=async s=>(await body()).includes(s);
 const viewport={width:1440,height:1100};
 const click=async(selector,label)=>{
  await cmd('Page.bringToFront');
  const q=`[...document.querySelectorAll(${JSON.stringify(selector)})].find(e=>${label?`e.textContent.trim()===${JSON.stringify(label)}`:'true'})`;
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
 await cmd('Page.enable');await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});
 await cmd('Page.navigate',{url:base});await wait(()=>ev('document.readyState==="complete"'));
 const token=JSON.parse(fs.readFileSync('/tmp/progress-scroll-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};

 await wait(()=>has('已完成 7/7 组'));assert(await has('今日安排已完成'));assert(await has('100%'));assert(!(await has('已完成 7/10 组')));checks.push('finished legacy session displays dated progress 7/7 and 100 percent');await ev(`document.querySelector('.session-strip').scrollIntoView({block:'center',behavior:'instant'});true`);await shot('finished-seven-of-seven.png');
 const before=await fetch(base+'/api/v1/agent/day?fallback_next=false',{headers:{Authorization:'Bearer '+token}}).then(r=>r.json());assert.equal(before.data.session.progress.total_sets,10);assert.equal(before.data.day_progress.total_sets,7);checks.push('dated projection retains original ten-target historical session unchanged');
 await cmd('Page.navigate',{url:base+'/#/pages/plan/index'});await wait(()=>ev('!!document.querySelector(".meals-scroll")'));
 for(const selector of ['.training-scroll','.meals-scroll']) {
  await ev(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',behavior:'instant'});true`);
  const box=await ev(`(()=>{const root=document.querySelector(${JSON.stringify(selector)}),els=[root,...root.querySelectorAll('*')],e=els.find(e=>e.scrollHeight>e.clientHeight+40 && ['auto','scroll'].includes(getComputedStyle(e).overflowY));if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+Math.min(r.height/2,250),height:r.height,before:e.scrollTop}})()`);
  assert(box,selector+' must have actual scroll overflow');assert(box.height<=760);
  await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',x:box.x,y:box.y,deltaX:0,deltaY:480});
  await wait(()=>ev(`(()=>{const root=document.querySelector(${JSON.stringify(selector)});return [root,...root.querySelectorAll('*')].some(e=>e.scrollTop>20)})()`));
  checks.push(selector+' responds to native wheel within bounded height');
 }
 await shot('independent-plan-scrollers-desktop.png');
 await cmd('Emulation.setDeviceMetricsOverride',{width:390,height:900,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.meals-scroll').scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,200));assert(await ev(`document.querySelector('.meals-scroll').getBoundingClientRect().width<=390`));await shot('independent-plan-scrollers-mobile.png');checks.push('independent meal scroller fits mobile viewport');
 fs.writeFileSync(path.join(out,'browser-progress-scroll.json'),JSON.stringify({passed:true,model:'no LLM, synthetic completed database',checks},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
