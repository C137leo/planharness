// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5299',out=path.resolve('../docs/evidence/commerce-order-lifecycle');const checks=[];
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
 const token=JSON.parse(fs.readFileSync('/tmp/commerce-lifecycle-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};




 const snapshot=()=>fetch(base+'/__synthetic_evidence').then(x=>x.json());
 const initial=await snapshot();assert.equal(initial.submits,1);
 await click('.composer-mcd-entry');await wait(()=>ev(`!!document.querySelector('.mcd-location-fields input') && !document.querySelector('.mcd-card.mcd-busy')`));
 await click('.mcd-location-fields .mcd-btn','查找门店 →');await wait(()=>has('真实门店'));await click('.mcd-row-choice','选这家 →');await wait(()=>ev('!!document.querySelector(".mcd-menu-scroll")'));
 await say('请再点一份鸡翅，先看新预览');await wait(()=>ev(`document.querySelector('.mcd-picked-name')?.textContent.includes('麦辣鸡翅')`));
 await click('.mcd-checkout-actions .mcd-red','确认创建待支付订单');await wait(()=>ev(`!!document.querySelector('.mcd-existing') && !document.querySelector('.mcd-card.mcd-busy')`));
 assert.equal((await snapshot()).submits,1);assert(await has('你还有未支付订单'));assert(await has('继续支付旧订单 →'));assert(await has('保留旧单，确认另下一单'));checks.push('valid unpaid order offers payment, cancellation and explicit new intent without dispatch');
 await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',x:1000,y:350,deltaX:0,deltaY:1});await ev(`(()=>{const e=document.querySelector('.mcd-existing'),s=e.closest('.workspace-scroll');s.scrollTop+=e.getBoundingClientRect().top-s.getBoundingClientRect().top-12})();true`);await new Promise(r=>setTimeout(r,200));await shot('existing-unpaid-desktop.png');
 await click('.mcd-existing-decisions .mcd-outline','刷新旧订单状态');await wait(()=>ev(`!!document.querySelector('.mcd-existing') && !document.querySelector('.mcd-card.mcd-busy')`));assert.equal((await snapshot()).submits,1);checks.push('refresh is read-only and keeps new cart');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.mcd-existing').scrollIntoView({block:'start',behavior:'instant'});true`);assert(await ev(`document.querySelector('.mcd-card').getBoundingClientRect().width<=390`));await shot('existing-unpaid-mobile.png');checks.push('390px choice card fits mobile width');
 await click('.mcd-existing-order .mcd-outline','取消这笔旧单');await wait(()=>ev(`!document.querySelector('.mcd-existing') && !!document.querySelector('.mcd-checkout-actions') && !document.querySelector('.mcd-card.mcd-busy')`));assert.equal((await snapshot()).cancels,0);checks.push('cancel button first produces separate confirmation, no cancellation yet');
 await click('.mcd-checkout-actions .mcd-red','确认');await wait(async()=>((await snapshot()).cancels===1));await wait(()=>ev(`!document.querySelector('.mcd-card.mcd-busy')`));assert.equal((await snapshot()).submits,1);assert.equal(await ev(`!!document.querySelector('.mcd-existing')`),false);checks.push('confirmed cancel returns to new preview without automatically creating new order');
 await click('.mcd-checkout-actions .mcd-red','确认创建待支付订单');await wait(async()=>((await snapshot()).submits===2));await wait(()=>ev(`!document.querySelector('.mcd-card.mcd-busy')`));checks.push('separate new confirmation creates exactly one new order');
 viewport.width=1440;viewport.height=1100;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:false});
 await say('再点一份新的鸡翅，先给我预览');await wait(()=>ev(`!!document.querySelector('.mcd-checkout-actions .mcd-red') && !document.querySelector('.mcd-card.mcd-busy')`));
 await click('.mcd-checkout-actions .mcd-red','确认创建待支付订单');await wait(()=>ev(`!!document.querySelector('.mcd-existing') && !document.querySelector('.mcd-card.mcd-busy')`));
 await click('.mcd-existing-decisions .mcd-red','保留旧单，确认另下一单');await wait(async()=>((await snapshot()).submits===3));await wait(()=>ev(`!document.querySelector('.mcd-card.mcd-busy')`));assert.equal((await snapshot()).cancels,1);checks.push('explicit keep-old and create-new button works without cancelling old order');
 assert.deepEqual((await snapshot()).snapshot.days,initial.snapshot.days);assert.equal(errors.length,0,JSON.stringify(errors));checks.push('no fitness data changes or runtime TypeError');
 fs.writeFileSync(path.join(out,'browser-lifecycle.json'),JSON.stringify({passed:true,provider:'fake MCP',model:'Scripted',checks,real_order_writes:0,runtime_type_errors:errors},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
