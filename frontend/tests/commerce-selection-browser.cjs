// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5192',out=path.resolve('../docs/evidence/commerce-selection');const checks=[];
 const targets=await(await fetch('http://127.0.0.1:9242/json/list')).json(),ws=new WS((targets.find(x=>x.type==='page') || await(await fetch('http://127.0.0.1:9242/json/new?about:blank',{method:'PUT'})).json()).webSocketDebuggerUrl);
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
 const token=JSON.parse(fs.readFileSync('/tmp/commerce-selection-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 await click('.conversation-item');await wait(()=>ev('document.querySelectorAll(".chat-text").length>1'));
 const pendingLabel=await ev(`[...document.querySelectorAll('.chat-attachment')].find(e=>e.querySelector('.chat-attachment-title')?.textContent==='待确认的点餐信息').textContent.trim()`);
 await click('.chat-attachment',pendingLabel);
 await wait(()=>ev('!!document.querySelector(".mcd-card .mcd-local-actions")'));
 assert(!await ev(`[...document.querySelectorAll('.chat-attachment-kind')].some(e=>e.textContent==='今日调整')`));
 checks.push('read-only pending attaches to McDonald card, without a Today Adjustment chat attachment');await shot('selection-before-confirm.png');
 await click('.mcd-local-actions .mcd-btn','确认');await wait(()=>has('请选择堂食或外带'));
 assert(await has('麦咖啡™热美式中杯'));const reply=await ev(`[...document.querySelectorAll('.chat-row.agent .chat-text')].at(-1).textContent`);assert(!reply.includes('共0项'));assert(!reply.includes('还未确定'));assert.equal((await info()).submits,0);
 checks.push('native confirmation click resumes exact selected SKU quote without zero-options or stale unselected message, no submit');await shot('selection-quote-desktop.png');

 await ev(`[...document.querySelectorAll('.chat-attachment')].filter(e=>e.querySelector('.chat-attachment-kind')?.textContent==='麦当劳菜单').at(-1).setAttribute('data-selection-menu','true');true`);
 await click('[data-selection-menu]');await wait(()=>ev('!!document.querySelector(".mcd-detail-link")'));
 await click('.mcd-detail-link','查看商品详情 →');await wait(()=>has('不加 · 默认'));
 assert(await ev(`document.querySelector('.mcd-detail .mcd-section-title').textContent==='麦咖啡™热美式中杯'`));
 assert(await has('官方特制组 1'));assert(!await ev(`document.querySelector('.mcd-detail').textContent.includes('共0项')`));
 if(await ev('!!document.querySelector(".mcd-token-panel")'))await click('.mcd-token-toggle','Token 设置');
 checks.push('top-level modifiers appear alongside exact SKU even with zero combo rounds');
 await ev(`document.querySelector('.mcd-detail').scrollIntoView({block:'center',behavior:'instant'});true`);await shot('selection-detail-desktop.png');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});
 await ev(`document.querySelector('.mcd-detail-group').scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,200));
 assert(await ev('document.querySelector(".mcd-card").getBoundingClientRect().width<=390'));await shot('selection-detail-mobile.png');
 checks.push('selected product and default modifier card fit a 390px viewport');
 await click('.mcd-detail .mcd-cart-footer .mcd-btn','生成报价预览 →');await wait(()=>has('请选择堂食或外带'));
 assert.equal((await info()).submits,0);assert.equal((await info()).model_calls,2);
 assert(await ev(`document.querySelector('.mcd-order-items').textContent.includes('麦咖啡™热美式中杯')`));
 checks.push('mobile manual preview button generates the same product quote without a model call or order submission');await shot('selection-quote-mobile.png');
 fs.writeFileSync(path.join(out,'browser-selection.json'),JSON.stringify({passed:true,provider:'mock',model:'scripted',real_order_writes:0,mock_submits:0,checks},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
