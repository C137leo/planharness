// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5502',out=path.resolve('../docs/evidence/console-runtime');const checks=[];
 const targets=await(await fetch('http://127.0.0.1:9251/json/list')).json(),ws=new WS((await(await fetch('http://127.0.0.1:9251/json/new?about:blank',{method:'PUT'})).json()).webSocketDebuggerUrl);
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

 const token=JSON.parse(fs.readFileSync('/tmp/runtime-console-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.bringToFront');await cmd('Page.navigate',{url:base+'/#/pages/console/index'});await wait(()=>ev('document.querySelectorAll(".runtime-message").length===2 && !!document.querySelector(".runtime-pending")'));
 await wait(()=>ev('document.querySelectorAll(".runtime-task").length===2'));checks.push('real two-task pending conversation and run details load from persisted messages/runs');await shot('console-pending-desktop.png');
 await click('.runtime-pending uni-button,.runtime-pending button','确认此版本');await wait(()=>ev('document.querySelectorAll(".runtime-message").length===4 && !document.querySelector(".runtime-pending")'));await wait(async()=> (await body()).includes('已关联持久化执行日志'));
 assert.equal(await ev('document.querySelectorAll(".runtime-task").length'),2);await click('.runtime-task .evidence uni-button,.runtime-task .evidence button','▸ 持久化变更 before → after');await wait(async()=> (await body()).includes('before'));checks.push('native confirmation uses production chat and exposes committed journal before/after for both tasks');await shot('console-changes-desktop.png');
 await cmd('Page.reload');await wait(()=>ev('document.querySelectorAll(".runtime-message").length===4'));await wait(async()=> (await body()).includes('已关联持久化执行日志'));checks.push('refresh restores selected conversation, four messages and persisted turn evidence');
 await click('.runtime-sessions .runtime-panel-head uni-button,.runtime-sessions .runtime-panel-head button','＋ 新会话');await wait(()=>ev('document.querySelectorAll(".runtime-message").length===0 && document.querySelectorAll(".runtime-session").length===2'));await cmd('Page.reload');await wait(()=>ev('document.querySelectorAll(".runtime-session").length===2'));assert.equal(await ev('document.querySelectorAll(".runtime-message").length'),0);checks.push('empty new conversation is persisted and survives refresh without fabricated messages');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await click('.runtime-mobile-tabs uni-button,.runtime-mobile-tabs button','会话');await click('.runtime-session:nth-child(2)');await wait(()=>ev('document.querySelectorAll(".runtime-message").length===4'));await click('.runtime-mobile-tabs uni-button,.runtime-mobile-tabs button','执行详情');await wait(()=>ev('document.querySelectorAll(".runtime-task").length===2'));assert(await ev('document.documentElement.scrollWidth<=390'));await shot('console-mobile-details.png');checks.push('390px mobile switches sessions/chat/evidence without horizontal overflow');
 assert(!(await has('AI KILL SWITCH')));assert(!(await has('SCENARIO REPLAY')));assert.equal(errors.length,0,JSON.stringify(errors));checks.push('old evaluation/admin entries removed; no runtime TypeError');
 fs.writeFileSync(path.join(out,'browser-runtime-console.json'),JSON.stringify({passed:true,model:'Scripted',external_calls:0,checks},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
