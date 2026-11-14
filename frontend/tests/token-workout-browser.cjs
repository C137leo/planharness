// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5193',out=path.resolve('../docs/evidence/token-workout');const checks=[];
 const targets=await(await fetch('http://127.0.0.1:9243/json/list')).json(),ws=new WS((targets.find(x=>x.type==='page') || await(await fetch('http://127.0.0.1:9243/json/new?about:blank',{method:'PUT'})).json()).webSocketDebuggerUrl);
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
 const token=JSON.parse(fs.readFileSync('/tmp/training-token-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 await click('.composer-mcd-entry');await wait(()=>has('麦当劳已连接'));await wait(()=>ev('!!document.querySelector(".mcd-card")'));await new Promise(r=>setTimeout(r,250));
 assert(!await ev('!!document.querySelector(".mcd-token-panel")'));checks.push('configured server token stays collapsed with no local MCP token');await shot('token-configured-desktop.png');
 await click('.mcd-token-toggle','Token 设置');await wait(()=>ev('!!document.querySelector(".mcd-token-panel")'));await click('.mcd-token-toggle','Token 设置');await wait(()=>ev('!document.querySelector(".mcd-token-panel")'));checks.push('native Token Settings click opens and closes the input');
 await fetch(base+'/__mock_connection/0',{method:'POST'});await click('.composer-mcd-entry');await wait(()=>ev('!!document.querySelector(".mcd-token-panel")'));assert(await has('去麦当劳官方平台获取'));checks.push('missing credential automatically exposes masked input and official guide');await shot('token-missing-desktop.png');
 await fetch(base+'/__mock_connection/1',{method:'POST'});await click('.composer-mcd-entry');await wait(()=>ev('!document.querySelector(".mcd-token-panel")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send');};
 await say('今天到这里，未记录的组不要补，帮我结束训练。');await wait(()=>has('当前实际记录：'));assert.equal((await info()).logs,0);
 await say('确认');await wait(()=>ev('!!document.querySelector(".workout-facts")'));await wait(()=>info().then(x=>x.logs===1));
 assert(await ev(`document.querySelector('.workout-facts').textContent.includes('实际 3 / 计划 4 组')`));assert(await ev(`document.querySelector('.workout-facts').textContent.includes('实际时长未记录')`));
 checks.push('natural confirmation closes and persists three actual sets without filling the other planned sets');await ev(`document.querySelector('.workout-facts').scrollIntoView({block:'center',behavior:'instant'});true`);await shot('training-records-desktop.png');
 await say('深蹲最后一组重量改成65公斤，其他数据别改');await wait(()=>has('实际重量 65 kg'));await say('确认这次更正');await wait(()=>info().then(x=>x.audits===1));
 assert.equal((await info()).last_weight,65);assert.deepEqual((await info()).sets,[3]);assert.equal((await info()).submits,0);assert.equal((await info()).model_calls,4);
 checks.push('confirmed correction retains one log and three sets while auditing only the last weight change');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.workout-facts .result-line:last-child').scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,200));
 assert(await ev('document.querySelector(".workout-facts").getBoundingClientRect().width<=390'));await shot('training-records-mobile.png');checks.push('actual per-set repetitions and weight render within the 390px card');
 fs.writeFileSync(path.join(out,'browser-token-workout.json'),JSON.stringify({passed:true,model:'scripted',provider:'mock',real_order_writes:0,checks},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
