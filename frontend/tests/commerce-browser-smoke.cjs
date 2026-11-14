// Requires isolated Mock API and fresh Chrome profile; never run on real MCP.
const WS=require('ws'),fs=require('fs'),assert=require('assert/strict'),path=require('path');
(async()=>{
const base='http://127.0.0.1:5189',out=path.resolve('../docs/evidence/capability-migration');
const info=()=>fetch(base+'/__mock_evidence').then(x=>x.json());assert.equal((await info()).provider,'mock');assert.equal((await info()).submits,0);
const targets=await(await fetch('http://127.0.0.1:9239/json/list')).json(),ws=new WS(targets.find(x=>x.type==='page').webSocketDebuggerUrl);
await new Promise(r=>ws.on('open',r));let id=0;const pending=new Map();ws.on('message',raw=>{const m=JSON.parse(raw),p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result)}});
const cmd=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}))});
const ev=async expression=>{const r=await cmd('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw r.exceptionDetails;return r.result.value};
const wait=async fn=>{const start=Date.now();while(Date.now()-start<12000){if(await fn())return;await new Promise(r=>setTimeout(r,100))}throw Error('UI wait timeout')};
const has=s=>ev(`document.body.innerText.includes(${JSON.stringify(s)})`);
const click=async label=>{const e=`[...document.querySelectorAll('.mcd-btn')].find(e=>e.textContent.trim()===${JSON.stringify(label)})`;await wait(()=>ev(`!!${e}`));await new Promise(r=>setTimeout(r,450));await cmd('Input.dispatchMouseEvent',{type:'mouseWheel',x:1000,y:450,deltaX:0,deltaY:2000});await new Promise(r=>setTimeout(r,300));await ev(`${e}.scrollIntoView({block:'center',behavior:'instant'});true`);await new Promise(r=>setTimeout(r,100));const box=await ev(`(()=>{const r=${e}.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);for(const type of ['mousePressed','mouseReleased'])await cmd('Input.dispatchMouseEvent',{type,...box,button:'left',clickCount:1})};
const enter=async text=>{await ev(`document.querySelector('textarea').focus();true`);await cmd('Input.insertText',{text});for(const type of ['keyDown','keyUp'])await cmd('Input.dispatchKeyEvent',{type,key:'Enter',code:'Enter',windowsVirtualKeyCode:13});await wait(()=>ev(`document.querySelector('textarea')?.value===''`))};
const shot=async name=>{const r=await cmd('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(out,name),Buffer.from(r.data,'base64'))};
await cmd('Page.enable');await cmd('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});await cmd('Page.navigate',{url:base});await wait(()=>ev('document.readyState==="complete"'));
const token=JSON.parse(fs.readFileSync('/tmp/commerce-ui-auth.json')).Authorization.slice(7);await ev(`localStorage.setItem('planharness_token',${JSON.stringify(token)});true`);
await cmd('Page.navigate',{url:base+'/#/pages/today/index'});await wait(()=>ev('!!document.querySelector("textarea")'));
await enter('查看三里屯门店菜单');await wait(()=>has('门店当前菜单'));assert(await has('麦辣鸡翅'));
await enter('来一份鸡翅先看报价');await wait(()=>has('10.00'));assert(await has('请选择堂食或外带'));assert.equal((await info()).submits,0);
await click('外带');await wait(()=>has('确认创建待支付订单'));assert(await has('取餐方式'));await ev(`document.querySelector('.mcd-checkout-actions').scrollIntoView({block:'center'});true`);await shot('commerce-chat-browser-preview.png');
await click('确认创建待支付订单');await wait(()=>has('订单已创建并经查询核实'));assert(await has('订单号 order-1'));assert.equal((await info()).submits,1);
await click('查询订单状态');await wait(()=>has('已核实订单状态'));assert.equal((await info()).submits,1);await shot('commerce-chat-browser-submitted.png');
fs.writeFileSync(path.join(out,'commerce-browser-smoke.json'),JSON.stringify({provider:'mock',model:'scripted',database:'isolated SQLite',browser:'Chrome headless',passed:true,checks:['Enter clears textarea','catalogue rendered','quote displayed','take way selected','explicit confirmation','one submission','order id displayed','query without re-submit'],mock_submits:1,real_order_writes:0},null,2));console.log('Mock browser checkout smoke passed; submissions=1, real orders=0');ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
