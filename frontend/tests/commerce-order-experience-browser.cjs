// Native browser interaction with isolated Mock API; no real order/payment.
const WS=require('ws'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const base='http://127.0.0.1:5198',out=path.resolve('../docs/evidence/commerce-order-experience');const checks=[];
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
 const token=JSON.parse(fs.readFileSync('/tmp/commerce-experience-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
 await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
 const say=async text=>{await fill('textarea',text);await click('.composer-send')};




 const snapshot=()=>fetch(base+'/__synthetic_evidence').then(x=>x.json());
 const initial=await snapshot();assert.equal(initial.submits,0);
 await click('.composer-mcd-entry');await wait(()=>ev(`!!document.querySelector('.mcd-location-fields input') && !document.querySelector('.mcd-card.mcd-busy')`));
 await fill('.mcd-location-fields input','北京');await ev(`document.querySelectorAll('.mcd-location-fields input')[1].focus();document.querySelectorAll('.mcd-location-fields input')[1].select();true`);await cmd('Input.insertText',{text:'三里屯'});
 await click('.mcd-location-tools .mcd-detail-link','保存为默认位置');await wait(()=>has('默认位置已保存'));assert.deepEqual((await snapshot()).location,{city:'北京',keyword:'三里屯'});checks.push('native settings save user-specific city and landmark without a fabricated default');
 await click('.mcd-location-fields .mcd-btn','查找门店 →');await wait(()=>has('真实门店'));await click('.mcd-row-choice','选这家 →');await wait(()=>ev('!!document.querySelector(".mcd-menu-scroll")'));
 await wait(()=>ev(`!document.querySelector('.mcd-card.mcd-busy')`));
 const parentPoint=await ev(`(()=>{const r=document.querySelector('.mcd-menu-scroll').closest('.workspace-scroll').getBoundingClientRect();return {x:r.right-16,y:Math.max(10,r.top+20)}})()`);
 await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',...parentPoint});await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',...parentPoint,deltaX:0,deltaY:1});await new Promise(r=>setTimeout(r,150));
 const dimensions=await ev(`(()=>{const e=document.querySelector('.mcd-menu-scroll');return {height:e.clientHeight,scroll:e.scrollHeight,count:document.querySelectorAll('.mcd-product-row').length}})()`);assert.equal(dimensions.count,120);assert(dimensions.height<=480 && dimensions.scroll>dimensions.height*3);checks.push('120 real fixture menu rows stay in a bounded scroll area with footer outside');
 await ev(`(()=>{const e=document.querySelector('.mcd-menu-scroll'),s=e.closest('.workspace-scroll');s.scrollIntoView({block:'center',behavior:'instant'});s.scrollTop+=e.getBoundingClientRect().top-s.getBoundingClientRect().top-24})();true`);await new Promise(r=>setTimeout(r,200));
 const box=await ev(`(()=>{const r=document.querySelector('.mcd-menu-scroll').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+Math.min(150,r.height/2)}})()`);
 console.log(JSON.stringify({wheelBefore:await ev(`(()=>{const e=document.querySelector('.mcd-menu-scroll'),s=e.closest('.workspace-scroll'),r=e.getBoundingClientRect(),p=s.getBoundingClientRect();return {menu:r.toJSON(),parent:p.toJSON(),view:[innerWidth,innerHeight],hit:document.elementFromPoint(${box.x},${box.y})?.className,top:e.scrollTop}})()`)}));await cmd('Input.dispatchMouseEvent',{type:'mouseMoved',...box});await new Promise(r=>setTimeout(r,200));await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',...box,deltaX:0,deltaY:450});await wait(()=>ev('document.querySelector(".mcd-menu-scroll").scrollTop>100'));checks.push('native mouse wheel scrolls the menu');await shot('long-menu-desktop.png');
 await say('我在三里屯，帮我点份鸡翅，先给我预览');await wait(()=>has('已为你选好，核对后再下单'));assert(await ev(`document.querySelector('.mcd-picked-name').textContent.includes('麦辣鸡翅')`));assert(await has('已默认外带'));assert.equal(await ev(`!!document.querySelector('.mcd-menu-scroll')`),false);checks.push('requested wings are prominent and default takeaway is shown without dumping menu');await shot('selected-wings-desktop.png');
 const first=(await snapshot()).snapshot.tables.checkout_drafts[0];
 await click('.mcd-pickup .mcd-btn','堂食');await wait(async()=>((await snapshot()).snapshot.tables.checkout_drafts[0]?.take_way_code==='eat-in'));const dine=(await snapshot()).snapshot.tables.checkout_drafts[0];assert.equal(dine.id,first.id);assert.equal(dine.take_way_code,'eat-in');assert.equal(dine.version,first.version+1);checks.push('native dine-in change requotes same draft and invalidates old confirmation');
 await click('.mcd-picked .mcd-detail-link','不满意？换餐品 / 查看完整菜单 →');await wait(()=>ev('!!document.querySelector(".mcd-menu-scroll")'));assert(await ev(`document.querySelector('.mcd-product-row').classList.contains('mcd-product-selected')`));
 await click('.mcd-quantity button,.mcd-quantity uni-button','＋');await click('.mcd-cart-footer .mcd-btn','更新订单预览 →');await wait(()=>ev(`document.querySelector('.mcd-picked-name')?.textContent.includes('× 2')`));const revised=(await snapshot()).snapshot.tables.checkout_drafts[0];assert.equal(revised.id,first.id);assert.equal(revised.version,dine.version+1);assert.equal(revised.cart[0].quantity,2);checks.push('changing quantity through full menu revises one cart and highlights new choice');
 viewport.width=390;viewport.height=900;await cmd('Emulation.setDeviceMetricsOverride',{...viewport,deviceScaleFactor:1,mobile:true});await ev(`document.querySelector('.mcd-checkout').scrollIntoView({block:'start',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,200));assert(await ev(`document.querySelector('.mcd-card').getBoundingClientRect().width<=390`));await shot('selected-wings-mobile.png');checks.push('390px mobile preview fits screen');
 assert.equal((await snapshot()).submits,0);assert.deepEqual((await snapshot()).snapshot.days,initial.snapshot.days);assert.equal(errors.length,0,JSON.stringify(errors));checks.push('no orders or fitness writes and no runtime TypeError');
 fs.writeFileSync(path.join(out,'browser-commerce-experience.json'),JSON.stringify({passed:true,model:'Scripted synthetic SQLite and fake MCP, not NLU measurement',checks,runtime_type_errors:errors,real_order_writes:0},null,2));console.log(JSON.stringify({passed:true,checks}));ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
