// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5501',out=path.resolve('../docs/evidence/workspace-ui-polish');const checks=[];
 const targets=await(await fetch('http://127.0.0.1:9251/json/list')).json(),ws=new WS((await(await fetch('http://127.0.0.1:9251/json/new?about:blank',{method:'PUT'})).json()).webSocketDebuggerUrl);
 await new Promise(r=>ws.on('open',r));let id=0;const pending=new Map();ws.on('message',raw=>{const m=JSON.parse(raw),p=pending.get(m.id);if(p){pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(m.error):p.resolve(m.result)}});
 const cmd=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;const timer=setTimeout(()=>{pending.delete(n);reject(Error('CDP timeout '+method))},15000);pending.set(n,{resolve,reject,timer});ws.send(JSON.stringify({id:n,method,params}))});
 const ev=async expression=>{const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw r.exceptionDetails;return r.result.value};
 const wait=async fn=>{const at=Date.now();while(Date.now()-at<15000){if(await fn())return;await new Promise(r=>setTimeout(r,100))}throw Error('UI wait timed out')};
 const body=()=>ev('document.body.innerText'),has=async s=>(await body()).includes(s);
 const viewport={width:1440,height:1000};
 const click=async(selector,label)=>{
  await cmd('Page.bringToFront');
  const q=`[...document.querySelectorAll(${JSON.stringify(selector)})].filter(e=>e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0).find(e=>${label?`e.textContent.trim()===${JSON.stringify(label)}`:'true'})`;
  await wait(()=>ev(`!!${q} && !${q}.getAttribute('disabled') && !${q}.hasAttribute('disabled')`));
  await new Promise(r=>setTimeout(r,180));
  await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',x:300,y:350});await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',x:Math.min(viewport.width-50,1000),y:350,deltaX:0,deltaY:1});
  await ev(`(()=>{let anchor=${q};for(let p=anchor.parentElement;p;p=p.parentElement){if(p.hasAttribute('data-stable-scroll')){const a=anchor.getBoundingClientRect(),r=p.getBoundingClientRect();p.scrollTop+=a.top-r.top-(p.clientHeight-Math.min(a.height,p.clientHeight))/2;anchor=p}}anchor.scrollIntoView({block:'center',behavior:'instant'})})();true`);await new Promise(r=>setTimeout(r,180));
  const box=await ev(`(()=>{const e=${q},r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,hit:e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}})()`);
  assert(box.hit,'button must receive a real pointer: '+selector+' '+label);
  for(const type of ['mousePressed','mouseReleased'])await cmd('Input.dispatchMouseEvent',{type,x:box.x,y:box.y,button:'left',clickCount:1});
 };
 const fill=async(selector,text)=>{await wait(()=>ev(`!!document.querySelector(${JSON.stringify(selector)}) && !document.querySelector('.mcd-card.mcd-busy')`));await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.focus();e.select()})();true`);await cmd('Input.insertText',{text})};
 const shot=async name=>{const r=await cmd('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,name),Buffer.from(r.data,'base64'))};

 const info=()=>fetch(base+'/__mock_evidence').then(x=>x.json());
 const errors=[];ws.on('message',raw=>{const m=JSON.parse(raw);if(['Runtime.exceptionThrown','Runtime.consoleAPICalled'].includes(m.method)){const value=JSON.stringify(m.params);if(value.includes('TypeError'))errors.push(value.slice(0,1000))}});await cmd('Runtime.enable');
 await cmd('Page.enable');await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});
 await cmd('Page.navigate',{url:base});await wait(()=>ev('document.readyState==="complete"'));
 const token=JSON.parse(fs.readFileSync('/tmp/workspace-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};





 const fixture=JSON.parse(fs.readFileSync('/tmp/workspace-ui-auth.json'));
 const snapshot=()=>fetch(base+'/__ui_evidence').then(x=>x.json());const initial=await snapshot();
 const navigate=async(page,selector)=>{await cmd('Page.bringToFront');await cmd('Page.navigate',{url:base+'/#/pages/'+page+'/index'+(page==='workout'?'?date='+fixture.date+'&session_id='+fixture.session_id:'')});await wait(()=>ev('!!document.querySelector('+JSON.stringify(selector)+')'));await new Promise(r=>setTimeout(r,600));};
 const wheel=async(selector,delta)=>{await cmd('Page.bringToFront');const box=await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),x=r.left+r.width/2,y=(Math.max(60,r.top)+Math.min(innerHeight-60,r.bottom))/2;return {x,y,hit:e.contains(document.elementFromPoint(x,y))}})()`);assert(box.hit,'wheel pointer hits '+selector);await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',x:30,y:40});await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',x:box.x,y:box.y});await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',x:box.x,y:box.y,deltaX:0,deltaY:delta});await new Promise(r=>setTimeout(r,850));};
 const chain=async selector=>{
  await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.scrollIntoView({block:'center',behavior:'instant'});e.scrollTop=0})();true`);await new Promise(r=>setTimeout(r,250));
  const size=await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});return {height:e.clientHeight,total:e.scrollHeight,chain:getComputedStyle(e).overscrollBehaviorY}})()`);assert(size.total>size.height+60,selector+' has real overflow');assert.equal(size.chain,'auto');
  let page=await ev('document.scrollingElement.scrollTop');await wheel(selector,120);assert(await ev('document.querySelector('+JSON.stringify(selector)+').scrollTop>20'));assert.equal(await ev('document.scrollingElement.scrollTop'),page);
  await ev('document.querySelector('+JSON.stringify(selector)+').scrollTop=100000;window.scrollBy(0,-180);true');await new Promise(r=>setTimeout(r,1000));await ev('document.querySelector('+JSON.stringify(selector)+').scrollTop=100000;true');await new Promise(r=>setTimeout(r,200));page=await ev('document.scrollingElement.scrollTop');console.log('bottom-before',selector,await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect();return {page:document.scrollingElement.scrollTop,max:document.scrollingElement.scrollHeight-innerHeight,top:e.scrollTop,innerMax:e.scrollHeight-e.clientHeight,y:r.top+200}})()`));await wheel(selector,280);console.log('bottom-after',selector,await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});return {page:document.scrollingElement.scrollTop,top:e.scrollTop,innerMax:e.scrollHeight-e.clientHeight}})()`));assert(await ev('document.scrollingElement.scrollTop')>page+20,selector+' bottom chains down');
  await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.scrollIntoView({block:'center',behavior:'instant'});e.scrollTop=0})();true`);await new Promise(r=>setTimeout(r,250));page=await ev('document.scrollingElement.scrollTop');assert(page>20);await wheel(selector,-200);assert(await ev('document.scrollingElement.scrollTop')<page-20,selector+' top chains up');
  checks.push(selector+' scrolls internally and chains to page at both edges');
 };
 await navigate('plan','.meals-scroll');await chain('.training-scroll');await chain('.meals-scroll');
 await navigate('profile','.profile-memory-scroll');await wait(()=>ev('document.querySelectorAll(".memory-row").length>15'));await chain('.profile-memory-scroll');await ev('document.scrollingElement.scrollTop=0;true');await shot('profile-desktop.png');
 await navigate('progress','.progress-record-scroll');await wait(()=>ev('document.querySelectorAll(".log-row").length>10'));await chain('.progress-record-scroll');await chain('.progress-weight-scroll');await ev('document.scrollingElement.scrollTop=0;true');await shot('progress-desktop.png');
 assert(await ev(`(()=>{const c=document.querySelector('.progress-dashboard'),cols=c.children;return cols.length===2&&getComputedStyle(c).display==='grid'&&document.querySelector('.trend-chart').getBoundingClientRect().height<=140})()`));checks.push('progress uses independent compact columns with bounded records and one-day trend');
 await navigate('workout','.workout-layout');await wait(()=>ev('document.querySelectorAll(".train-item").length===4'));
 assert(await ev(`(()=>{const a=document.querySelector('.train-card').getBoundingClientRect(),b=document.querySelector('.workout-queue').getBoundingClientRect();return Math.abs(a.top-b.top)<5&&b.left>a.left+a.width})()`));await shot('workout-desktop.png');assert(await has('还差 3 组'));await click('.train-set-btn');await wait(async()=> (await snapshot()).session.progress.done_sets===initial.session.progress.done_sets+1);checks.push('desktop training has side-by-side focus/queue; completing one set still persists exactly one real synthetic set');
 for(const [page,selector] of [['profile','.profile-memory-scroll'],['progress','.progress-record-scroll'],['workout','.workout-layout']]){
  viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await navigate(page,selector);await new Promise(r=>setTimeout(r,250));assert(await ev('document.documentElement.scrollWidth<=390'),page+' no mobile horizontal overflow');await ev('document.scrollingElement.scrollTop=0;true');if(page==='profile')assert(await ev(`getComputedStyle(document.querySelector('.profile-head-badge')).display==='none' && document.querySelector('.profile-head-copy').getBoundingClientRect().width>180`),'mobile profile title has enough room');await shot(page+'-mobile.png');checks.push(page+' fits 390px mobile without horizontal overflow');
 }
 await navigate('today','.composer-orders-entry');await wait(()=>ev('!!document.querySelector(".session-strip")'));await new Promise(r=>setTimeout(r,600));await click('.composer-orders-entry');await wait(()=>ev('!!document.querySelector(".order-history-dialog")'));await new Promise(r=>setTimeout(r,700));const modalPage=await ev('document.scrollingElement.scrollTop');await wheel('.order-history-header',240);assert.equal(await ev('document.scrollingElement.scrollTop'),modalPage);await click('.order-history-close');checks.push('order modal still isolates page scrolling after shared scroll component update');
 const final=await snapshot();assert.equal(final.model_calls,0);assert.equal(final.logs,initial.logs);assert.equal(final.memories,initial.memories);assert.equal(final.session.progress.done_sets,initial.session.progress.done_sets+1);assert.equal(errors.length,0,JSON.stringify(errors));checks.push('no model/MCP calls, unintended log or memory writes, or runtime TypeError');
 fs.writeFileSync(path.join(out,'browser-workspace-ui.json'),JSON.stringify({passed:true,provider:'none',model_calls:0,checks,synthetic_set_writes:1,runtime_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
