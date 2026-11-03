'use strict';
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(process.argv[2],'utf8');
class NativeMap extends Map { get(key){return super.has(key)?super.get(key):null;} put(key,value){assert.equal(typeof value,'string');this.set(key,value);} }
class Queue {constructor(){this.rows=[];} offer(value){assert.equal(typeof value,'string');if(this.rows.length>=512)return false;this.rows.push(value);return true;}poll(){return this.rows.length?this.rows.shift():null;}isEmpty(){return this.rows.length===0;}size(){return this.rows.length;} }
const number=()=>({n:0,get(){return this.n;},incrementAndGet(){return ++this.n;},decrementAndGet(){return --this.n;}});
function setup(initial,moduleSource=source){
 const broker={states:new NativeMap(),events:new Queue(),active:number(),eventWake:{value:false,set(value){this.value=value;},compareAndSet(before,after){if(this.value!==before)return false;this.value=after;return true;}},overflow:{value:false,get(){return this.value;},set(v){this.value=v;}},lock:{depth:0,lock(){this.depth++;},unlock(){this.depth--;}}};
 const descriptor={io:{epoch:'config-1',wake:'events',actors:{switch:{wake:'switchWake',resources:['D1','D2']},other:{wake:'otherWake',resources:['P']}}}};
 const context={module:{exports:{}},require(){throw Error('unexpected native dependency');},Date,JSON,Map,Object,String,Error};
 vm.runInNewContext(moduleSource,context);
 const module=context.module.exports, writes=[], events=[], journals=[], wakes=[];
 let active=true, serial=0, fail=false, midway=null;
 const env={broker,noRules:true,wake:name=>wakes.push(name),now:()=>++serial,id:()=>String(++serial)};
 const bridge=module.create(descriptor,env);
 const journal={data:initial||null,load(){return this.data;},save(row){if(fail)throw Error('disk failure');this.data=JSON.parse(JSON.stringify(row));journals.push({...row});if(midway&&row.status==='admitted')midway();}};
 const provider={has:(r,v)=>['D1','D2'].includes(r)&&['1','2','3','4'].includes(String(v)),execute(r,v){writes.push([r,v]);return {terminal:true};},probe(r){return {value:'3',fresh:true};}};
 const actor=module.actor({key:'switch',io:descriptor.io},provider,{...env,journal,id:()=>String(++serial)});
 const submit=(token,resource,value='3')=>bridge.dispatch({token,key:'switch',resource,value,phase:'acquire',isCurrent:()=>active},r=>events.push({...r}));
 return {bridge,actor,submit,writes,events,journals,wakes,provider,broker,journal,descriptor,env,module,
  inactive(){active=false;},failJournal(){fail=true;},onJournal(fn){midway=fn;}};
}
const tests=[];
function test(name,fn){fn();tests.push(name);}
test('distinct properties survive, latest same property coalesces',()=>{
 const f=setup();f.submit('old','D1','2');f.submit('next','D1','3');f.submit('mouse','D2','4');f.actor.drain();f.actor.drain();f.bridge.events();
 assert.deepEqual(f.writes,[['D1','3'],['D2','4']]);assert(f.events.some(e=>e.token==='old'&&e.admitted===false));
 assert(f.journals.findIndex(e=>e.status==='admitted')<f.journals.findIndex(e=>e.status==='retired'));
});
test('generation change during durable admission cancels before write',()=>{
 const f=setup();f.submit('old','D1');f.onJournal(()=>f.bridge.transaction(()=>{f.inactive();f.bridge.invalidateStale();}));f.actor.drain();f.bridge.events();
 assert.equal(f.writes.length,0);assert(f.events.some(e=>e.type==='terminal'&&e.admitted===false));
});
test('already admitted operations remain correlated across supersession',()=>{
 const f=setup();f.provider.execute=(r,v)=>{f.inactive();f.bridge.invalidateStale();f.writes.push([r,v]);return {terminal:true};};f.submit('admitted','D1');f.actor.drain();f.bridge.events();
 assert.equal(f.writes.length,1);assert(f.events.some(e=>e.type==='terminal'&&e.admitted===true));
});
test('unknown quarantines key and survives restart',()=>{
 const f=setup();f.provider.execute=()=>{throw Error('lost reply');};f.submit('unknown','D1');f.actor.drain();f.bridge.events();
 f.submit('later','D2');f.actor.drain();f.bridge.events();assert.equal(f.writes.length,0);assert(f.events.some(e=>e.reason==='executor_quarantined'));
 const restarted=setup(f.journal.data);restarted.submit('restart','D1');restarted.actor.drain();restarted.bridge.events();assert.equal(restarted.writes.length,0);
});
test('journal failure forbids mutation',()=>{const f=setup();f.failJournal();f.submit('disk','D1');f.actor.drain();f.bridge.events();assert.equal(f.writes.length,0);assert(f.events.some(e=>e.type==='uncertain'));});
test('owned probes are separate from terminal reports',()=>{const f=setup();f.bridge.probe({token:String('read'),key:'switch',resource:'D1'},r=>f.events.push({...r}));f.actor.drain();f.bridge.events();assert(f.events.some(e=>e.type==='sample'&&e.fresh&&e.retired&&e.value==='3'));assert.equal(f.writes.length,0);});
test('wakeup alone cannot authorize mutation',()=>{const f=setup();f.actor.drain();assert.equal(f.writes.length,0);assert(!f.actor.legacy('D1','99'));f.actor.legacy('D1','2');f.actor.drain();assert.equal(f.writes.length,0);f.bridge.onLegacy(row=>f.bridge.transaction(()=>f.bridge.authorizeLegacy(row.token)));f.bridge.events();f.actor.drain();assert.equal(f.writes.length,1);});
test('readonly reports no admission rather than fake completion',()=>{const f=setup();const actor=f.module.actor({key:'other',io:f.descriptor.io},f.provider,{...f.env,readonly:true,journal:{load:()=>null,save(){} }});f.bridge.dispatch({token:String('ro'),key:'other',resource:'P',value:'ON'},e=>f.events.push(e));actor.drain();f.bridge.events();assert(f.events.some(e=>e.readonly&&e.admitted===false));assert.equal(f.writes.length,0);});
test('private command payload stays out of broker and journal',()=>{const f=setup();f.actor.legacy('D1','3',true);const data=String(f.broker.states.get('switch'));assert(!data.includes('"value":"3"'));assert(data.includes('<private>'));});
test('controller replacement revokes pending authority',()=>{const f=setup();f.submit('stale','D1');f.module.create(f.descriptor,f.env);f.actor.drain();assert.equal(f.writes.length,0);assert.throws(()=>f.submit('old-controller','D2'),/stale_executor_controller/);});
test('controller replacement during journal cannot admit old work',()=>{const f=setup();f.submit('stale','D1');f.onJournal(()=>f.module.create(f.descriptor,f.env));f.actor.drain();assert.equal(f.writes.length,0);});
test('guard exceptions revoke every cached permission',()=>{const f=setup();f.bridge.dispatch({token:String('bad-guard'),key:'switch',resource:'D1',value:'3',isCurrent(){throw Error('failed guard');}},()=>{});f.submit('also-pending','D2');assert.throws(()=>f.bridge.invalidateStale(),/failed guard/);f.actor.drain();assert.equal(f.writes.length,0);});
test('manual admission and retirement are reported for busy protection',()=>{const f=setup(),manual=[];f.provider.effects=()=>['D1','outside'];f.bridge.onLegacy(row=>{manual.push(row);if(row.type==='legacy')f.bridge.authorizeLegacy(row.token);});f.actor.legacy('D1','3');f.bridge.events();f.actor.drain();f.bridge.events();assert(manual.some(row=>row.type==='legacy'&&row.managed&&JSON.stringify(row.effectResources)==='["D1"]'));assert(manual.some(row=>row.type==='admitted'));assert(manual.some(row=>row.type==='terminal'));});
test('oversized values and queue overflow fail closed',()=>{const f=setup();assert.equal(f.actor.legacy('D1','0'.repeat(1000)+'3'),false);f.submit('overflow','D1');f.broker.overflow.set(true);f.actor.drain();assert.equal(f.writes.length,0);f.bridge.events();assert(f.events.some(e=>e.type==='uncertain'));});
test('source provider requires correlated native evidence, never cached GET or ACK',()=>{
 const requests=[];
 let answer={id:'source',ok:true,paired:true,connected:true,power:'off',operation:'native',terminal:true,submitted:true,native_sequence:4,power_reported_at:1000}, status=200;
 const context=vm.createContext({Date,JSON,Map,Set,Number,Object,String,Error,
  console:{info(){},warn(){}},thermiteIO:{http(url,method,body){const payload=JSON.parse(body);requests.push([url,method,payload]);return {getStatus:()=>status,getContentAsString:()=>JSON.stringify(typeof answer==='function'?answer(payload):answer)};}},
  require(name){assert.equal(name,'openhab');return {rules:{JSRule(){}},triggers:{GenericCronTrigger(){},ItemCommandTrigger(){}},items:{getItem:()=>({state:'UNDEF',postUpdate(){}})}};}});
 let driver=fs.readFileSync(process.argv[3],'utf8').replace('const DEVICES = null;',`const DEVICES = [{id:'source',host:'192.0.2.1',helper:'http://owned'}];`).replace('const PRESSES = null;','const PRESSES = {};').replace('const APPS = null;','const APPS = {};');
 vm.runInContext(driver,context);
 const provider=vm.runInContext('routingProvider()',context), op={token:String('native'),value:'OFF'};
 assert(provider.execute('source_power','OFF',op).terminal);
 const sample=provider.probe('source_power',op);assert(sample.fresh);assert.equal(sample.value,'OFF');assert.equal(sample.observedAt,1000);assert.equal(sample.nativeSequence,4);
 assert(requests.every(row=>row[0]==='http://owned/devices/source/lifecycle'&&row[1]==='POST'));
 assert.deepEqual(requests[0][2],{host:'192.0.2.1',operation:'native',kind:'power',on:false});
 answer={...answer,terminal:false};assert(!provider.execute('source_power','OFF',op).terminal);assert.throws(()=>provider.probe('source_power',op));
 answer={...answer,terminal:true,operation:'stale'};assert(!provider.execute('source_power','OFF',op).terminal);
 answer={id:'source',ok:true,paired:true,connected:true,power:'off',power_reported_at:1000};assert(!provider.execute('source_power','OFF',op).terminal);
 answer={...answer,operation:'native',terminal:true,submitted:false,native_sequence:4};status=502;
 assert(provider.execute('source_power','OFF',op).terminal);assert.throws(()=>provider.probe('source_power',op));
 status=200;answer={...answer,native_sequence:0};assert.throws(()=>provider.probe('source_power',op));
 // Use the actual broker around the actual source provider: a completed
 // refusal permits another explicit pairing attempt, uncertainty does not.
 const f=setup(),desc={io:{epoch:'pairing',wake:'events',actors:{source:{wake:'sourceWake',resources:['source_power']}}}};
 const bridge=f.module.create(desc,f.env);bridge.onLegacy(e=>{if(e.type==='legacy')bridge.authorizeLegacy(e.token);});
 const actor=f.module.actor({key:'source',io:desc.io},provider,{...f.env,journal:{load:()=>null,save(){}}});
 status=422;answer=payload=>({id:'source',operation:payload.operation,terminal:true,ok:false,power:'unknown',pairing:'none'});
 assert(actor.legacy('source_pairing','1234',true));bridge.events();actor.drain();bridge.events();
 assert.equal(JSON.parse(f.broker.states.get('source')).quarantine,null);
 status=409;assert(actor.legacy('source_pairing','REQUEST',true));bridge.events();actor.drain();bridge.events();
 assert.equal(JSON.parse(f.broker.states.get('source')).quarantine,null);
 answer=payload=>({id:'source',operation:'wrong',terminal:true,ok:false});
 assert(actor.legacy('source_pairing','1234',true));bridge.events();actor.drain();bridge.events();
 assert(JSON.parse(f.broker.states.get('source')).quarantine);
});
test('non-idempotent legacy keys retain FIFO repeats with a finite overflow rejection',()=>{
 const f=setup(),manual=[];f.provider.oneShot=()=>true;
 f.bridge.onLegacy(row=>{manual.push(row);if(row.type==='legacy')f.bridge.authorizeLegacy(row.token);});
 for(let n=0;n<16;n++)assert(f.actor.legacy('D1',String(n%4+1)));
 assert(!f.actor.legacy('D1','1'));
 while(!f.broker.events.isEmpty())f.bridge.events();for(let n=0;n<16;n++)f.actor.drain();while(!f.broker.events.isEmpty())f.bridge.events();
 assert.deepEqual(f.writes,Array.from({length:16},(_,n)=>['D1',String(n%4+1)]));
 assert.equal(manual.filter(e=>e.type==='terminal').length,16);
 assert.equal(f.actor.legacy('D1','2'),true);
});
test('controller replacement reports exact cancelled manual tickets',()=>{
 const f=setup(),old=[],next=[];f.bridge.onLegacy(e=>{old.push(e);if(e.type==='legacy')f.bridge.authorizeLegacy(e.token);});
 f.actor.legacy('D1','3');f.bridge.events();const proposal=old.find(e=>e.type==='legacy');
 const replacement=f.module.create(f.descriptor,f.env);replacement.onLegacy(e=>next.push(e));replacement.events();
 assert(next.some(e=>e.type==='terminal'&&e.token === proposal.token&&e.admitted===false));f.actor.drain();assert.equal(f.writes.length,0);
});
test('actor retirement evidence rechecks live flight and quarantine',()=>{
 const f=setup(),proof=[];f.bridge.onLegacy(e=>proof.push(e));
 function ready(){const row=JSON.parse(f.broker.states.get('switch'));f.broker.events.offer(JSON.stringify({type:'ready',key:'switch',actor:row.actor}));f.bridge.events();}
 ready();assert(proof.some(e=>e.type==='retired'&&e.recovered));proof.length=0;
 let row=JSON.parse(f.broker.states.get('switch'));row.flight={token:String('live')};f.broker.states.put('switch',JSON.stringify(row));ready();assert.equal(proof.length,0);
 row.flight=null;row.quarantine='uncertain';f.broker.states.put('switch',JSON.stringify(row));ready();assert.equal(proof.length,0);
 row.quarantine=null;row.proof.safe=false;f.broker.states.put('switch',JSON.stringify(row));ready();assert.equal(proof.length,0);
});
test('new controller learns when admitted old work truly retires',()=>{
 const f=setup(),proof=[];let replacement;
 f.provider.execute=()=>{replacement=f.module.create(f.descriptor,f.env);replacement.onLegacy(e=>proof.push(e));return {terminal:true};};
 f.submit('old-admitted','D1');f.actor.drain();replacement.events();assert(proof.some(e=>e.type==='retired'&&e.token==='old-admitted'));
});
test('failed pure read retires without a sample and permits later wake',()=>{
 const f=setup();f.provider.probe=()=>{throw Error('unavailable in standby');};
 f.bridge.probe({token:String('failed-read'),key:'switch',resource:'D1'},e=>f.events.push(e));f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.type==='terminal'&&e.reason==='owned_probe_failed'));
 assert(!f.events.some(e=>e.type==='sample'||e.type==='uncertain'));
 assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,null);
 f.submit('manual-wake','D1','3');f.actor.drain();f.bridge.events();assert.equal(f.writes.length,1);
});
test('actor wakeups are coalesced for pending work and never broadcast to idle peers',()=>{
 const f=setup();f.submit('first','D1');f.submit('second','D2');
 assert.equal(f.wakes.filter(name=>name==='switchWake').length,1);
 f.actor.drain();f.actor.drain();f.bridge.events();
 assert.equal(f.wakes.filter(name=>name==='switchWake').length,2);
 assert.equal(f.wakes.filter(name=>name==='otherWake').length,0);
 f.provider.execute=(r,v)=>{f.submit('queued-while-busy','D2');assert.equal(f.wakes.filter(name=>name==='switchWake').length,3);return {terminal:true};};
 f.submit('busy','D1');f.actor.drain();
 assert.equal(f.wakes.filter(name=>name==='switchWake').length,4);
});
test('root event wakeups coalesce and callbacks yield after eight outcomes',()=>{
 const f=setup();for(let n=0;n<16;n++)f.actor.legacy('D'+(n%2+1),'3');
 assert.equal(f.wakes.filter(name=>name==='events').length,1);
 while(!f.broker.events.isEmpty())f.bridge.events();
 const owner=JSON.parse(f.broker.states.get('@controller')).owner;let seen=0;
 f.bridge.onLegacy(()=>seen++);f.wakes.length=0;
 for(let n=0;n<40;n++)f.broker.events.offer(JSON.stringify({type:'terminal',legacy:true,owner,token:String(n)}));
 f.bridge.events();assert.equal(seen,8);assert.equal(f.broker.events.rows.length,32);
 while(!f.broker.events.isEmpty())f.bridge.events();
 assert.equal(seen,40);assert.equal(f.wakes.filter(name=>name==='events').length,4);
});
test('VRROOM requires owned matching readback without inventing an ACK response shape',()=>{
 function run(writeBody,observed,ack){
  const f=setup(),reports=[],requests=[];
  const nativeThing={getConfiguration:()=>new Map([['baseURL','http://owned'],['commandMethod','GET'],['stateMethod','GET']]),
   getChannel:()=>({getConfiguration:()=>new Map([['commandExtension','/cmd?insel=%2$s'],['commandTransformation',['JS(|parseInt(input))']],['stateExtension','/status'],['stateTransformation',['JSONPATH($.portseltx0)']]])})};
  const openhab={things:{getThing:()=>({rawThing:nativeThing})},items:{getItem:()=>({postUpdate(){}})}};
  const context=vm.createContext({module:{exports:{}},require(name){assert.equal(name,'openhab');return openhab;},
   thermiteIO:{http(url){requests.push(url);return {getStatus:()=>200,getContentAsString:()=>url.includes('/cmd')?writeBody:JSON.stringify({portseltx0:observed})};}}});
  vm.runInContext(source,context);const module=context.module.exports,bridge=module.create(f.descriptor,f.env);
  const actor=module.httpActor({key:'switch',io:f.descriptor.io,fields:{D1:{kind:'http',thing:'native',channel:'input',ack,allowed:['1','2','3','4']}}},{...f.env,journal:{load:()=>null,save(){}}});
  bridge.dispatch({token:String('video'),key:'switch',resource:'D1',value:'3',phase:'acquire'},e=>reports.push(e));actor.drain();bridge.events();
  return {reports,requests};
 }
 for(const body of ['', 'OK']){const out=run(body,3,'readback');assert(out.reports.some(e=>e.type==='terminal'&&e.admitted));assert.equal(out.requests.length,2);}
 assert(run('',2,'readback').reports.some(e=>e.type==='uncertain'));
 assert(run('',3,'result').reports.some(e=>e.type==='uncertain'));
});
test('superseded manual proposal cannot take over current intent but its receipt survives',()=>{
 const f=setup(),manual=[];f.bridge.onLegacy(e=>{manual.push(e);if(e.type==='legacy'){assert(f.broker.lock.depth>0);f.bridge.authorizeLegacy(e.token);}});
 f.actor.legacy('D1','3');f.submit('new-intent','D1','2');f.bridge.events();
 assert(!manual.some(e=>e.type==='legacy'));assert(manual.some(e=>e.type==='terminal'&&e.admitted===false));
 f.actor.drain();assert.deepEqual(f.writes,[['D1','2']]);
 f.actor.legacy('D1','4');f.bridge.events();assert(manual.some(e=>e.type==='legacy'));f.actor.drain();
});
test('module read-only guard stops mutation even when its caller omits readonly',()=>{
 const f=setup(null,source.replace('const SEND = true;','const SEND = false;'));
 f.submit('module-readonly','D1');f.actor.drain();f.bridge.events();
 assert.equal(f.writes.length,0);assert(f.events.some(e=>e.readonly&&e.admitted===false));
});
test('sanitized activity counts queued, active and unknown work without exposing tickets',()=>{
 const f=setup(),published=[];const bridge=f.module.create(f.descriptor,{...f.env,onStatus:row=>published.push(JSON.parse(JSON.stringify(row)))});
 assert.deepEqual(bridge.status(),{pending:0,active:0,quarantined:0,eventsQueued:1}); // Actor readiness.
 bridge.events();assert.deepEqual(published.at(-1),{pending:0,active:0,quarantined:0,eventsQueued:0});
 const publications=published.length;bridge.events();assert.equal(published.length,publications+1);
 bridge.dispatch({token:String('private-id'),key:'switch',resource:'D1',value:'3'},()=>{});
 assert.equal(bridge.status().pending,1);let active;
 f.provider.execute=()=>{active=bridge.status();return {terminal:true};};f.actor.drain();
 assert.equal(active.active,1);assert.equal(active.pending,0);assert(bridge.status().eventsQueued>0);
 bridge.events();assert.deepEqual(published.at(-1),{pending:0,active:0,quarantined:0,eventsQueued:0});
 f.provider.execute=()=>{throw Error('lost reply');};bridge.dispatch({token:String('private-unknown'),key:'switch',resource:'D2',value:'3'},()=>{});f.actor.drain();bridge.events();
 assert.equal(bridge.status().quarantined,1);assert.equal(bridge.status().active,0);
 assert(!JSON.stringify(published).includes('private-'));assert(!JSON.stringify(published).includes('D1'));
});
test('qualified terminal write readback is sanitized and published after durable retirement',()=>{
 const f=setup();f.provider.execute=()=>({terminal:true,sample:{value:3,fresh:true,retired:false,basis:'provider text',observedAt:123,privatePayload:'discard'}});
 f.submit('owned-write-read','D1');f.actor.drain();f.bridge.events();
 const receipt=f.events.find(e=>e.type==='terminal');assert.equal(f.journal.data.status,'retired');
 assert.deepEqual(receipt.sample,{value:'3',fresh:true,retired:true,basis:'owned_post_write_read',observedAt:123});
});
test('stale mismatched refused ambiguous and untracked write samples never qualify',()=>{
 for(const done of [
  {terminal:true,sample:{value:'3',fresh:false}}, {terminal:true,sample:{value:'2',fresh:true}},
  {terminal:false,sample:{value:'3',fresh:true}}, {terminal:true,refused:true,sample:{value:'3',fresh:true}},
  {terminal:true,accepted:false,sample:{value:'3',fresh:true}}]) {
  const f=setup();f.provider.execute=()=>done;f.submit('unqualified','D1');f.actor.drain();f.bridge.events();assert(f.events.every(e=>!e.sample));
 }
 const f=setup(),manual=[];f.provider.has=()=>true;f.provider.execute=()=>({terminal:true,sample:{value:'3',fresh:true}});
 f.bridge.onLegacy(e=>{manual.push(e);if(e.type==='legacy')f.bridge.authorizeLegacy(e.token);});f.actor.legacy('unmanaged','3');f.bridge.events();f.actor.drain();f.bridge.events();assert(manual.every(e=>!e.sample));
});
test('failed retirement journal cannot leak a terminal write sample',()=>{
 const f=setup();f.provider.execute=()=>{f.failJournal();return {terminal:true,sample:{value:'3',fresh:true}};};
 f.submit('retirement-disk-fault','D1');f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.type==='uncertain'));assert(f.events.every(e=>!e.sample));assert.equal(f.journal.data.status,'admitted');
});
process.stdout.write(JSON.stringify({passed:tests.length,tests}));
