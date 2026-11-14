// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5499',out=path.resolve('../docs/evidence/commerce-order-history');const checks=[];
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
 const token=JSON.parse(fs.readFileSync('/tmp/commerce-history-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};




 const snapshot=()=>fetch(base+'/__synthetic_evidence').then(x=>x.json());
 const initial=await snapshot();assert.equal(initial.submits,0);
 const unsent='这段还没发送，先看看我的订单。';await fill('textarea',unsent);
 await click('.composer-orders-entry');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===2 && !document.querySelector('.order-history-sync').hasAttribute('disabled')`));
 assert.equal(await ev(`document.querySelectorAll('.order-history-item').length`),2);assert(await has('有效订单'));assert(await has('历史订单'));assert(await has('待核实'));checks.push('composer opens an independent order center with two active unpaid orders and separate unknown notice');
 await click('.order-history-item:nth-child(1)');await wait(()=>ev(`!!document.querySelector('.order-history-number') && !document.querySelector('.order-history-refresh-one').hasAttribute('disabled')`));assert(await has('继续支付 →'));assert(await has('复制订单号'));checks.push('active detail exposes order number, food, store and official payment entry');
 await click('.order-history-tabs uni-button:nth-child(2),.order-history-tabs button:nth-child(2)');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===20`));assert(await has('旧版餐厅 · 测试记录'));
 const longIndex=await ev(`[...document.querySelectorAll('.order-history-item')].findIndex(e=>e.textContent.includes('合成餐品 4'))`);assert(longIndex>=0);await click(`.order-history-item:nth-child(${longIndex+1})`);await wait(()=>ev(`document.querySelectorAll('.order-history-detail-food').length===20`));await shot('history-desktop.png');
 const wheel=async(selector,delta=450)=>{const box=await ev(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()`);await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',...box});await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',...box,deltaX:0,deltaY:delta})};
 const pageBefore=await ev('document.scrollingElement.scrollTop');await wheel('.order-history-detail-scroll');await wait(()=>ev(`document.querySelector('.order-history-detail-scroll').scrollTop>100`));assert.equal(await ev('document.scrollingElement.scrollTop'),pageBefore);checks.push('twenty food details scroll independently while the page stays in place');
 await wheel('.order-history-list-scroll');await wait(()=>ev(`document.querySelector('.order-history-list-scroll').scrollTop>100`));await click('.order-history-more');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===32`));assert.equal(new Set(await ev(`[...document.querySelectorAll('.order-history-item')].map(e=>e.textContent.match(/TEST-ORDER-[A-Z0-9]+/)?.[0]||e.textContent)`)).size,32);checks.push('paid history is paged and list scrolls with load-more instead of growing the page');
 await click('.order-history-filters uni-button:nth-child(2),.order-history-filters button:nth-child(2)');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===10`));assert(await has('已过期'));assert(await has('已取消'));assert(await has('提交失败'));assert.equal(await ev(`!!document.querySelector('.order-history-detail-panel .order-history-primary')`),false);checks.push('expired, cancelled and failed records are retained only under explicit inactive filter');
 await click('.order-history-tabs uni-button:nth-child(1),.order-history-tabs button:nth-child(1)');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===2`));await click('.order-history-unknown uni-button,.order-history-unknown button');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===1`));await click('.order-history-item');await wait(()=>ev(`document.querySelector('.order-history-detail-state .order-history-status')?.textContent==='待核实'`));assert.equal(await ev(`!!document.querySelector('.order-history-detail-panel .order-history-primary')`),false);checks.push('old unknown submission is not misclassified expired and never offers direct payment');
 await cmd('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await wait(()=>ev(`!document.querySelector('.order-history-dialog')`));assert.equal(await ev('document.querySelector("textarea").value'),unsent);checks.push('Escape closes without losing unsent composer text');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await click('.composer-orders-entry');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===2 && !document.querySelector('.order-history-sync').hasAttribute('disabled')`));assert(await ev(`document.querySelector('.order-history-dialog').getBoundingClientRect().width<=390`));await click('.order-history-tabs uni-button:nth-child(2),.order-history-tabs button:nth-child(2)');await wait(()=>ev(`document.querySelectorAll('.order-history-item').length===20`));
 const mobileIndex=await ev(`[...document.querySelectorAll('.order-history-item')].findIndex(e=>e.textContent.includes('合成餐品 4'))`);await click(`.order-history-item:nth-child(${mobileIndex+1})`);await wait(()=>ev(`document.querySelectorAll('.order-history-detail-food').length===20`));assert(await ev(`getComputedStyle(document.querySelector('.order-history-list-panel')).display==='none'`));await shot('history-mobile-detail.png');await wheel('.order-history-detail-scroll');await wait(()=>ev(`document.querySelector('.order-history-detail-scroll').scrollTop>50`));await click('.order-history-back','← 返回列表');assert(await ev(`getComputedStyle(document.querySelector('.order-history-list-panel')).display!=='none'`));checks.push('390px mobile detail has its own scroll and a working return-to-list button');
 await click('.order-history-close');await wait(()=>ev(`!document.querySelector('.order-history-dialog')`));assert.equal(await ev('document.querySelector("textarea").value'),unsent);
 const final=await snapshot();assert.equal(final.submits,0);assert.equal(final.model_calls,0);assert.deepEqual(final.history,initial.history);assert.deepEqual(final.days,initial.days);assert.deepEqual(final.draft,initial.draft);assert(final.calls.every(x=>x[0]==='query-order'));assert.equal(errors.length,0,JSON.stringify(errors));checks.push('no model calls, chat changes, draft changes, fitness changes, order writes or runtime TypeError');
 fs.writeFileSync(path.join(out,'browser-order-history.json'),JSON.stringify({passed:true,provider:'fake MCP',model:'unused Scripted',checks,real_order_writes:0,runtime_type_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
