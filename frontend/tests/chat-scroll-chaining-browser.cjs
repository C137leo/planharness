// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5197',out=path.resolve('../docs/evidence/scroll-replan-fix');const checks=[];
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
 await cmd('Page.enable');await cmd('Page.bringToFront');await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});
 await cmd('Page.navigate',{url:base});await wait(()=>ev('document.readyState==="complete"'));
 const token=JSON.parse(fs.readFileSync('/tmp/workout-replan-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};




 const gesture=async (delta,selector='.workspace-scroll[data-stable-scroll]')=>{await cmd('Page.bringToFront');const b=await ev(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',...b});await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',...b,deltaX:0,deltaY:delta});await new Promise(r=>setTimeout(r,350))};
 await wait(()=>has('今天练'));await new Promise(r=>setTimeout(r,1800));
 await ev(`document.querySelector('.workspace-chat').scrollIntoView({block:'center',behavior:'instant'});true`);
 // Build deterministic overflow with synthetic view content, never a semantic input.
 await ev(`(()=>{const e=document.querySelector('.chat-stream'),p=document.createElement('div');p.style.height='1800px';p.textContent='合成历史消息占位，用于验证原生滚动';e.prepend(p);const s=document.querySelector('.workspace-scroll[data-stable-scroll]');s.scrollTop=0;})();true`);
 const debug=()=>ev(`(()=>{const e=document.querySelector('.workspace-scroll[data-stable-scroll]'),r=e.getBoundingClientRect();return {win:window.scrollY,top:e.scrollTop,height:e.clientHeight,content:e.scrollHeight,box:{x:r.x,y:r.y,width:r.width,height:r.height},hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.tagName,policy:getComputedStyle(e).overscrollBehaviorY}})()`);console.log('before',await debug());const win0=await ev('window.scrollY');await gesture(260);console.log('after',await debug());assert(await ev(`document.querySelector('.workspace-scroll[data-stable-scroll]').scrollTop>100`));assert(Math.abs((await ev('window.scrollY'))-win0)<2);checks.push('native wheel scrolls chat history while there is remaining inner content');
 await ev(`(()=>{const e=document.querySelector('.workspace-scroll[data-stable-scroll]');e.scrollTop=e.scrollHeight})();true`);await new Promise(r=>setTimeout(r,400));
 const before=await ev('window.scrollY');await gesture(440);await wait(async()=>await ev('window.scrollY')>before+40);checks.push('next wheel at chat bottom scrolls outer page instead of trapping the pointer');await shot('chat-bottom-scrolls-page.png');
 // No forced follow-to-bottom or activation crash after the user scrolls.
 await new Promise(r=>setTimeout(r,1600));assert(await ev('window.scrollY')>=before+40);checks.push('idle chat does not pull the page back after scroll chaining');
 await ev(`document.querySelector('textarea').scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,200));const composerBefore=await ev('window.scrollY');await gesture(300,'textarea');await wait(async()=>await ev('window.scrollY')>composerBefore+40);checks.push('wheel above idle composer textarea also scrolls the page');
 assert.equal(errors.length,0,JSON.stringify(errors));checks.push('native scroll chaining produces no TypeError');
 fs.writeFileSync(path.join(out,'browser-scroll-chaining.json'),JSON.stringify({passed:true,model:'Scripted synthetic page, no NLU measurement',checks,runtime_type_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
