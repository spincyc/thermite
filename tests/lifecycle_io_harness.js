'use strict';
const fs=require('fs'), path=require('path'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(process.argv[2],'utf8');
class NativeMap extends Map { get(key){return super.has(key)?super.get(key):null;} put(key,value){assert.equal(typeof value,'string');this.set(key,value);} }
class Queue {constructor(){this.rows=[];} offer(value){assert.equal(typeof value,'string');if(this.rows.length>=512)return false;this.rows.push(value);return true;}poll(){return this.rows.length?this.rows.shift():null;}isEmpty(){return this.rows.length===0;}size(){return this.rows.length;} }
const number=()=>({n:0,get(){return this.n;},incrementAndGet(){return ++this.n;},decrementAndGet(){return --this.n;}});
function setup(initial,moduleSource=source,options={}){
 const broker={states:new NativeMap(),events:new Queue(),active:number(),eventWake:{value:false,set(value){this.value=value;},compareAndSet(before,after){if(this.value!==before)return false;this.value=after;return true;}},overflow:{value:false,get(){return this.value;},set(v){this.value=v;}},lock:{depth:0,lock(){this.depth++;},unlock(){this.depth--;}}};
 const descriptor={io:{epoch:'config-1',wake:'events',actors:{switch:{wake:'switchWake',resources:['D1','D2']},other:{wake:'otherWake',resources:['P']}}}};
 const context={module:{exports:{}},require(){throw Error('unexpected native dependency');},Date,JSON,Map,Object,String,Error};
 vm.runInNewContext(moduleSource,context);
 const module=context.module.exports, writes=[], events=[], journals=[], wakes=[];
 let active=true, serial=0, fail=false, midway=null;
 const logs=[], sleeps=[];
 const log={error:m=>logs.push(['error',m]),warn:m=>logs.push(['warn',m]),info:m=>logs.push(['info',m])};
 const env={broker,noRules:true,wake:name=>wakes.push(name),now:options.now||(()=>++serial),id:()=>String(++serial),log,sleep:ms=>sleeps.push(ms)};
 const bridge=module.create(descriptor,env);
 const journal={data:initial||null,load(){return this.data;},save(row){if(fail)throw Error('disk failure');this.data=JSON.parse(JSON.stringify(row));journals.push({...row});if(midway&&row.status==='admitted')midway();}};
 const provider={has:(r,v)=>['D1','D2'].includes(r)&&['1','2','3','4'].includes(String(v)),execute(r,v){writes.push([r,v]);return {outcome:'applied',sample:{value:String(v),fresh:true}};},probe(r){return {value:'3',fresh:true};}};
 const actor=module.actor({key:'switch',io:descriptor.io},provider,{...env,journal,id:()=>String(++serial)});
 const submit=(token,resource,value='3')=>bridge.dispatch({token,key:'switch',resource,value,phase:'acquire',isCurrent:()=>active},r=>events.push({...r}));
 return {bridge,actor,submit,writes,events,journals,wakes,provider,broker,journal,descriptor,env,module,logs,sleeps,
  inactive(){active=false;},failJournal(){fail=true;},onJournal(fn){midway=fn;}};
}
const tests=[];
function test(name,fn){fn();tests.push(name);}
// Ticket log lines: their shape, and the fields of one.
const TICKET=/^routing ticket route=(?:[0-9a-f]{8}|-) room=\S+ choice=\S+ resource=\S+ value=\S+ phase=\S+ outcome=(?:terminal|sample|uncertain) reason=\S+ readback=(?:confirmed|unconfirmed|-) ms=\d+ wait=(?:\d+|-) polls=\d+(?: http=\d{3}| exc=[A-Za-z_][A-Za-z0-9_]*)?$/;
const tickets=f=>f.logs.filter(l=>l[1].startsWith('routing ticket ')).map(([level,line])=>{assert.equal(level,'info');assert.match(line,TICKET);return line;});
const fieldsOf=line=>Object.fromEntries(line.slice('routing ticket '.length).split(' ').map(part=>[part.slice(0,part.indexOf('=')),part.slice(part.indexOf('=')+1)]));
const timeless=line=>{const row=fieldsOf(line);delete row.ms;delete row.wait;return row;};
// An httpActor around the VRROOM wiring above, with a scripted exchange.
function httpRig({status=200,body='',thing=true,ack='readback',fail=null,observed=3,rules=null}={}){
 const f=setup();
 const nativeThing={getConfiguration:()=>new Map([['baseURL','http://owned'],['commandMethod','GET'],['stateMethod','GET']]),
  getChannel:()=>({getConfiguration:()=>new Map([['commandExtension','/cmd?insel=%2$s'],['commandTransformation',['JS(|parseInt(input))']],['stateExtension','/status'],['stateTransformation',['JSONPATH($.portseltx0)']]])})};
 const openhab={things:{getThing:()=>thing?{rawThing:nativeThing}:null},items:{getItem:()=>({postUpdate(){}})},
  rules:{JSRule(rule){rules[rule.id]=rule;}},triggers:{ItemCommandTrigger:name=>({name})}};
 const context=vm.createContext({module:{exports:{}},
  require(name){if(name==='openhab')return openhab;if(name==='@runtime'&&rules)return {lifecycleTracker:{addDisposeHook(){}}};assert.fail(name);},
  thermiteIO:{http(url){if(fail)throw fail;return {getStatus:()=>status,getContentAsString:()=>url.includes('/cmd')?body:JSON.stringify({portseltx0:observed})};},
   unreached:error=>!!error&&error.unreached===true}});
 vm.runInContext(source,context);const module=context.module.exports,bridge=module.create(f.descriptor,f.env);
 const actor=module.httpActor({key:'switch',io:f.descriptor.io,fields:{D1:{kind:'http',thing:'native',channel:'input',ack,allowed:['1','2','3','4']}}},
  {...f.env,noRules:!rules,journal:{load:()=>null,save(){}}});
 const write=(value='3',labels={})=>{bridge.dispatch({token:String('w')+value,key:'switch',resource:'D1',value,phase:'acquire',...labels},()=>{});actor.drain();bridge.events();};
 const read=()=>{bridge.probe({token:String('read'),key:'switch',resource:'D1'},()=>{});actor.drain();bridge.events();};
 return {f,bridge,actor,write,read};
}
// A Java exception as GraalJS hands it to a catch: a host object with a cause chain.
const javaError=(name,cause=null,extra={})=>({getClass:()=>({getSimpleName:()=>name}),getCause:()=>cause,getMessage:()=>'secret message',...extra});
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
 const f=setup();f.provider.execute=(r,v)=>{f.inactive();f.bridge.invalidateStale();f.writes.push([r,v]);return {outcome:'applied',sample:{value:String(v),fresh:true}};};f.submit('admitted','D1');f.actor.drain();f.bridge.events();
 assert.equal(f.writes.length,1);assert(f.events.some(e=>e.type==='terminal'&&e.admitted===true));
});
test('unknown quarantines key and survives restart',()=>{
 const f=setup();f.provider.execute=()=>{throw Error('lost reply');};f.submit('unknown','D1');f.actor.drain();f.bridge.events();
 f.submit('later','D2');f.actor.drain();f.bridge.events();assert.equal(f.writes.length,0);assert(f.events.some(e=>e.reason==='executor_quarantined'));
 const restarted=setup(f.journal.data);restarted.submit('restart','D1');restarted.actor.drain();restarted.bridge.events();assert.equal(restarted.writes.length,0);
});
test('journal failure forbids mutation without quarantining what was never sent',()=>{const f=setup();f.failJournal();f.submit('disk','D1');f.actor.drain();f.bridge.events();assert.equal(f.writes.length,0);
 assert(f.events.some(e=>e.type==='terminal'&&e.admitted===false&&e.reason==='journal_failed'));assert(!f.events.some(e=>e.type==='uncertain'));
 assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,null);assert(f.logs.some(l=>l[0]==='error'&&/journal/.test(l[1])));});
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
 assert.equal(provider.execute('source_power','OFF',op).outcome,'applied');
 const sample=provider.probe('source_power',op);assert(sample.fresh);assert.equal(sample.value,'OFF');assert.equal(sample.observedAt,1000);assert.equal(sample.nativeSequence,4);
 assert(requests.every(row=>row[0]==='http://owned/devices/source/lifecycle'&&row[1]==='POST'));
 assert.deepEqual(requests[0][2],{host:'192.0.2.1',operation:'native',kind:'power',on:false});
 answer={...answer,terminal:false};assert.equal(provider.execute('source_power','OFF',op).outcome,'unknown');assert.throws(()=>provider.probe('source_power',op));
 answer={...answer,terminal:true,operation:'stale'};assert.equal(provider.execute('source_power','OFF',op).outcome,'unknown');
 answer={id:'source',ok:true,paired:true,connected:true,power:'off',power_reported_at:1000};assert.equal(provider.execute('source_power','OFF',op).outcome,'unknown');
 answer={...answer,operation:'native',terminal:true,submitted:false,native_sequence:4};status=502;
 assert.equal(provider.execute('source_power','OFF',op).outcome,'unsent');assert.throws(()=>provider.probe('source_power',op));
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
 f.provider.execute=()=>{replacement=f.module.create(f.descriptor,f.env);replacement.onLegacy(e=>proof.push(e));return {outcome:'applied'};};
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
 f.provider.execute=(r,v)=>{f.submit('queued-while-busy','D2');assert.equal(f.wakes.filter(name=>name==='switchWake').length,3);return {outcome:'applied'};};
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
 // A switch that answered but still reads the old input is re-read four
 // times, then retires without an owned sample; the planner marks it.
 const lagging=run('',2,'readback');assert(lagging.reports.some(e=>e.type==='terminal'&&e.admitted&&!e.sample));assert.equal(lagging.requests.length,6);
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
 assert.deepEqual(bridge.status(),{pending:0,active:0,quarantined:0,eventsQueued:1,quarantinedKeys:[]}); // Actor readiness.
 bridge.events();assert.deepEqual(published.at(-1),{pending:0,active:0,quarantined:0,eventsQueued:0,quarantinedKeys:[]});
 const publications=published.length;bridge.events();assert.equal(published.length,publications+1);
 bridge.dispatch({token:String('private-id'),key:'switch',resource:'D1',value:'3'},()=>{});
 assert.equal(bridge.status().pending,1);let active;
 f.provider.execute=()=>{active=bridge.status();return {outcome:'applied'};};f.actor.drain();
 assert.equal(active.active,1);assert.equal(active.pending,0);assert(bridge.status().eventsQueued>0);
 bridge.events();assert.deepEqual(published.at(-1),{pending:0,active:0,quarantined:0,eventsQueued:0,quarantinedKeys:[]});
 f.provider.execute=()=>{throw Error('lost reply');};bridge.dispatch({token:String('private-unknown'),key:'switch',resource:'D2',value:'3'},()=>{});f.actor.drain();bridge.events();
 assert.equal(bridge.status().quarantined,1);assert.equal(bridge.status().active,0);assert.deepEqual(bridge.status().quarantinedKeys,['switch']);
 f.broker.overflow.set(true);assert.equal(bridge.status().quarantined,2);assert.deepEqual(bridge.status().quarantinedKeys,['switch'],'overflow names no device');f.broker.overflow.set(false);
 const row=JSON.parse(f.broker.states.get('other')||'{}');row.quarantine='unknown';row.pending=row.pending||{};row.queue=[];f.broker.states.put('other',JSON.stringify(row));
 assert.deepEqual(bridge.status().quarantinedKeys,['other','switch'],'sorted, stable for change detection');
 assert(!JSON.stringify(published).includes('private-'));assert(!JSON.stringify(published).includes('D1'));
});
test('qualified terminal write readback is sanitized and published after durable retirement',()=>{
 const f=setup();f.provider.execute=()=>({outcome:'applied',sample:{value:3,fresh:true,retired:false,basis:'provider text',observedAt:123,privatePayload:'discard'}});
 f.submit('owned-write-read','D1');f.actor.drain();f.bridge.events();
 const receipt=f.events.find(e=>e.type==='terminal');assert.equal(f.journal.data.status,'retired');
 assert.deepEqual(receipt.sample,{value:'3',fresh:true,retired:true,basis:'owned_post_write_read',observedAt:123});
});
test('stale mismatched refused ambiguous and untracked write samples never qualify',()=>{
 for(const done of [
  {outcome:'applied',sample:{value:'3',fresh:false}}, {outcome:'applied',sample:{value:'2',fresh:true}},
  {outcome:'unknown',sample:{value:'3',fresh:true}}, {outcome:'refused',sample:{value:'3',fresh:true}},
  {terminal:true,sample:{value:'3',fresh:true}}]) {
  // Re-reads still show another value, so no owned read can qualify either.
  const f=setup();f.provider.execute=()=>done;f.provider.probe=()=>({value:'2',fresh:true});f.submit('unqualified','D1');f.actor.drain();f.bridge.events();assert(f.events.every(e=>!e.sample));
 }
 const f=setup(),manual=[];f.provider.has=()=>true;f.provider.execute=()=>({outcome:'applied',sample:{value:'3',fresh:true}});
 f.bridge.onLegacy(e=>{manual.push(e);if(e.type==='legacy')f.bridge.authorizeLegacy(e.token);});f.actor.legacy('unmanaged','3');f.bridge.events();f.actor.drain();f.bridge.events();assert(manual.every(e=>!e.sample));
});
test('failed retirement journal cannot leak a terminal write sample',()=>{
 const f=setup();f.provider.execute=()=>{f.failJournal();return {outcome:'applied',sample:{value:'3',fresh:true}};};
 f.submit('retirement-disk-fault','D1');f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.type==='uncertain'));assert(f.events.every(e=>!e.sample));assert.equal(f.journal.data.status,'admitted');
});
test('connect failure is terminal and unadmitted, refusal fails cleanly, only silence quarantines',()=>{
 const f=setup();f.provider.execute=()=>({outcome:'unsent'});f.submit('offline','D1');f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.token==='offline'&&e.type==='terminal'&&e.admitted===false&&e.reason==='device_not_reached'));
 assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,null);assert.equal(f.journal.data.status,'retired');
 f.provider.execute=(r,v)=>{f.writes.push([r,v]);return {outcome:'refused',sample:{value:'4',fresh:true}};};
 f.submit('declined','D2','4');f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.token==='declined'&&e.type==='terminal'&&e.admitted&&e.refused===true&&e.reason==='device_refused'&&!e.sample));
 assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,null);
 assert.equal(f.logs.filter(l=>l[0]==='error').length,0);
 f.provider.execute=(r,v)=>{f.writes.push([r,v]);return {outcome:'unknown'};};f.submit('silent','D1');f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.token==='silent'&&e.type==='uncertain'));assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,'silent');
 assert.equal(f.logs.filter(l=>l[0]==='error'&&/quarantined/.test(l[1])).length,1);
});
test('an applied write whose readback lags is re-read within a bounded window',()=>{
 const f=setup();let reads=0;f.provider.execute=()=>({outcome:'applied',sample:{value:'2',fresh:true}});
 f.provider.probe=()=>(++reads<2?{value:'2',fresh:true}:{value:'3',fresh:true,observedAt:7,privatePayload:'discard'});
 f.submit('slow','D1','3');f.actor.drain();f.bridge.events();
 const settled=f.events.find(e=>e.token==='slow'&&e.type!=='admitted');assert.equal(settled.type,'terminal');assert.equal(reads,2);
 assert.deepEqual(settled.sample,{value:'3',fresh:true,retired:true,basis:'owned_post_write_read',observedAt:7});
 reads=0;f.provider.probe=()=>{reads++;return {value:'2',fresh:true};};f.submit('stuck','D1','3');f.actor.drain();f.bridge.events();
 const stuck=f.events.find(e=>e.token==='stuck'&&e.type!=='admitted');assert.equal(stuck.type,'terminal');assert(!stuck.sample);assert.equal(reads,4);
 assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,null);assert.equal(f.sleeps.length,6);
 // Unmanaged one-shot keys have no readback to wait for.
 const g=setup(),manual=[];g.provider.has=()=>true;g.provider.oneShot=()=>true;g.provider.execute=()=>({outcome:'applied'});
 g.provider.probe=()=>{throw Error('keys have no readback');};
 g.bridge.onLegacy(e=>{manual.push(e);if(e.type==='legacy')g.bridge.authorizeLegacy(e.token);});
 g.actor.legacy('menu_key','UP');g.bridge.events();g.actor.drain();g.bridge.events();
 assert(manual.some(e=>e.type==='terminal'&&e.admitted));assert.equal(g.sleeps.length,0);
});
test('a transitional read crosses the broker as data, on a probe and on the post-write readback',()=>{
 const f=setup();f.provider.probe=()=>({value:'3',fresh:true,transition:true});
 f.bridge.probe({token:String('warming'),key:'switch',resource:'D1'},r=>f.events.push({...r}));f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.token==='warming'&&e.type==='sample'&&e.value==='3'&&e.transition===true));
 // A transitional readback of the written value confirms the write, flagged.
 f.provider.execute=()=>({outcome:'applied',sample:{value:'3',fresh:true,transition:true,observedAt:5}});
 f.submit('warming-write','D1','3');f.actor.drain();f.bridge.events();
 assert.deepEqual(f.events.find(e=>e.token==='warming-write'&&e.type==='terminal').sample,
  {value:'3',fresh:true,retired:true,basis:'owned_post_write_read',observedAt:5,transition:true});
 // Only `true` is carried; a stable readback has no flag at all.
 f.provider.execute=()=>({outcome:'applied',sample:{value:'3',fresh:true,transition:'yes'}});
 f.submit('odd','D1','3');f.actor.drain();f.bridge.events();
 assert.deepEqual(f.events.find(e=>e.token==='odd'&&e.type==='terminal').sample,{value:'3',fresh:true,retired:true,basis:'owned_post_write_read'});
});
test('quarantine recovers from one fresh in-domain read after the window, then survives no restart',()=>{
 let time=1000;const f=setup(null,source,{now:()=>time}),proof=[];f.bridge.onLegacy(e=>proof.push(e));
 f.provider.execute=()=>{throw Error('lost reply');};f.submit('unknown','D2','4');f.actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.token==='unknown'&&e.type==='uncertain'));
 const restarted=setup(f.journal.data);assert.equal(JSON.parse(restarted.broker.states.get('switch')).quarantine,'unknown');
 assert(restarted.logs.some(l=>l[0]==='error'&&/restart/.test(l[1])));
 const reads=[];f.provider.probe=r=>{reads.push(r);return {value:'3',fresh:true};};
 time+=59999;assert.equal(f.actor.recover(),false);assert.equal(reads.length,0);
 time+=1;f.provider.probe=r=>{reads.push(r);throw Error('still offline');};assert.equal(f.actor.recover(),false);
 assert.deepEqual(reads,['D2','D1']);
 f.provider.probe=()=>({value:'99',fresh:true});assert.equal(f.actor.recover(),false);
 f.provider.probe=()=>({value:'4',fresh:false});assert.equal(f.actor.recover(),false);
 assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,'unknown');assert.equal(f.journal.data.status,'admitted');
 f.provider.probe=()=>({value:'4',fresh:true});assert.equal(f.actor.recover(),true);
 assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,null);assert.equal(f.journal.data.status,'retired');
 assert(f.logs.some(l=>l[0]==='info'&&/quarantine cleared/.test(l[1])));
 f.bridge.events();assert(proof.some(e=>e.type==='retired'&&e.recovered&&e.key==='switch'));
 f.provider.execute=(r,v)=>{f.writes.push([r,v]);return {outcome:'applied'};};f.submit('after','D1','3');f.actor.drain();f.bridge.events();
 assert.deepEqual(f.writes,[['D1','3']]);
 assert.equal(JSON.parse(setup(f.journal.data).broker.states.get('switch')).quarantine,null);
});
test('event overflow reports live tickets uncertain, revokes unadmitted work when drained, then resumes',()=>{
 const f=setup(),proof=[];f.bridge.onLegacy(e=>{proof.push(e);if(e.type==='legacy')f.bridge.authorizeLegacy(e.token);});
 f.actor.legacy('D2','2');f.submit('lost','D1','3');f.broker.overflow.set(true);f.actor.drain();assert.equal(f.writes.length,0);
 f.bridge.events();
 assert(f.events.some(e=>e.token==='lost'&&e.type==='uncertain'&&e.reason==='executor_event_overflow'));
 assert.equal(f.broker.overflow.get(),false);assert.deepEqual(JSON.parse(f.broker.states.get('switch')).pending,{});
 assert.equal(f.logs.filter(l=>l[0]==='error'&&/overflowed/.test(l[1])).length,1);
 f.actor.drain();assert.equal(f.writes.length,0);
 f.bridge.events();assert(proof.some(e=>e.type==='terminal'&&e.reason==='executor_event_overflow'));
 assert(proof.some(e=>e.type==='retired'&&e.key==='switch'));
 f.submit('fresh','D2','4');f.actor.drain();f.bridge.events();assert.deepEqual(f.writes,[['D2','4']]);
 // A flight admitted before the overflow announces its retirement.
 const g=setup(),retired=[];g.bridge.onLegacy(e=>retired.push(e));
 g.provider.execute=(r,v)=>{g.broker.overflow.set(true);g.bridge.events();g.writes.push([r,v]);return {outcome:'applied'};};
 g.submit('inflight','D1','3');g.actor.drain();assert.equal(g.broker.overflow.get(),false);
 g.bridge.events();assert(retired.some(e=>e.type==='retired'&&e.key==='switch'));
});
test('dropped manual commands warn once per device and reason, never with their value',()=>{
 const f=setup();f.provider.execute=()=>{throw Error('lost reply');};f.submit('unknown','D1');f.actor.drain();f.bridge.events();
 f.logs.length=0;
 assert.equal(f.actor.legacy('D1','2',true),false);assert.equal(f.actor.legacy('D2','3'),false);
 assert.equal(f.actor.legacy('D1','4321',true),false);assert.equal(f.actor.legacy('D2','9876'),false);
 const warnings=f.logs.filter(l=>l[0]==='warn');assert.equal(warnings.length,2);
 assert(/quarantined/.test(warnings[0][1]));assert(/not a value/.test(warnings[1][1]));
 assert(!warnings.some(l=>l[1].includes('4321')||l[1].includes('9876')));
});
test('each ticket ends with one key=value line carrying its route labels',()=>{
 const f=setup();
 f.bridge.dispatch({token:String('routed'),key:'switch',resource:'D1',value:'3',phase:'acquire',route:'1a2b3c4d',room:'den',choice:'tv@lounge',isCurrent:()=>true},()=>{});
 f.actor.drain();f.bridge.events();
 let lines=tickets(f);assert.equal(lines.length,1);
 assert.deepEqual(timeless(lines[0]),{route:'1a2b3c4d',room:'den',choice:'tv@lounge',resource:'D1',value:'3',phase:'acquire',outcome:'terminal',reason:'-',readback:'confirmed',polls:'0'});
 f.bridge.probe({token:String('owned-read'),key:'switch',resource:'D2',route:'1a2b3c4d',room:'den',choice:'tv'},()=>{});f.actor.drain();
 assert.deepEqual(timeless(tickets(f)[1]),{route:'1a2b3c4d',room:'den',choice:'tv',resource:'D2',value:'-',phase:'probe',outcome:'sample',reason:'-',readback:'-',polls:'0'});
 // Malformed labels never reach the broker, so the line names none.
 f.bridge.dispatch({token:String('forged'),key:'switch',resource:'D1',value:'4',phase:'release',route:'1A2B3C4D',room:'den room=x',choice:'<b>',isCurrent:()=>true},()=>{});
 const queued=JSON.parse(f.broker.states.get('switch')).pending.D1;assert(!('route' in queued)&&!('room' in queued)&&!('choice' in queued));
 f.actor.drain();lines=tickets(f);assert.equal(lines.length,3);
 assert.deepEqual(timeless(lines[2]),{route:'-',room:'-',choice:'-',resource:'D1',value:'4',phase:'release',outcome:'terminal',reason:'-',readback:'confirmed',polls:'0'});
 // A read-only actor still says what became of its tickets.
 const g=setup(null,source.replace('const SEND = true;','const SEND = false;'));g.submit('readonly','D1');g.actor.drain();
 assert.equal(fieldsOf(tickets(g)[0]).reason,'read_only');
});
test('failed exchanges keep only their HTTP status or exception class',()=>{
 const cases=[
  [()=>({outcome:'unsent',exc:'ConnectException'}),'terminal','device_not_reached',' exc=ConnectException'],
  [()=>({outcome:'unknown',http:503}),'uncertain','provider_outcome_unknown',' http=503'],
  [()=>({outcome:'refused',http:409,body:'secret body'}),'terminal','device_refused',' http=409'],
  [()=>{throw javaError('ExecutionException',javaError('ConnectException'));},'uncertain','provider_outcome_unknown',' exc=ConnectException'],
  [()=>{throw Error('lost reply: secret body');},'uncertain','provider_outcome_unknown',' exc=Error'],
  [()=>{throw Error('executor_thing_unavailable');},'uncertain','provider_outcome_unknown',' exc=executor_thing_unavailable'],
  [()=>({outcome:'unsent',exc:'secret class name'}),'terminal','device_not_reached',''],
  [()=>({outcome:'unsent',http:999,exc:7}),'terminal','device_not_reached',''],
 ];
 for(const [execute,outcome,reason,cause] of cases){
  const f=setup();f.provider.execute=execute;f.submit('failing','D1');f.actor.drain();
  const [line]=tickets(f);const row=fieldsOf(line);
  assert.equal(row.outcome,outcome,line);assert.equal(row.reason,reason,line);assert.equal(row.readback,'-',line);
  assert(line.endsWith(` polls=0${cause}`),line);assert(!line.includes('secret'),line);
 }
 const f=setup();f.provider.probe=()=>{throw Object.assign(Error('executor_http_refused'),{http:502});};
 f.bridge.probe({token:String('failed-read'),key:'switch',resource:'D1'},()=>{});f.actor.drain();
 assert.deepEqual(timeless(tickets(f)[0]),{route:'-',room:'-',choice:'-',resource:'D1',value:'-',phase:'probe',outcome:'terminal',reason:'owned_probe_failed',readback:'-',polls:'0',http:'502'});
});
test('HTTP actors surface the status or exception class but never a body',()=>{
 const expect=(rig,act,outcome,reason,cause)=>{act(rig);const line=tickets(rig.f).at(-1);const row=fieldsOf(line);
  assert.equal(row.outcome,outcome,line);assert.equal(row.reason,reason,line);assert(line.endsWith(cause),line);assert(!line.includes('secret'),line);};
 expect(httpRig({status:503,body:'secret body'}),r=>r.write(),'uncertain','provider_outcome_unknown',' http=503');
 expect(httpRig({status:404,body:'secret body'}),r=>r.write(),'terminal','device_refused',' http=404');
 expect(httpRig({fail:javaError('ExecutionException',javaError('ConnectException'),{unreached:true})}),r=>r.write(),'terminal','device_not_reached',' exc=ConnectException');
 expect(httpRig({fail:javaError('TimeoutException')}),r=>r.write(),'uncertain','provider_outcome_unknown',' exc=TimeoutException');
 expect(httpRig({body:'{"result":0,"detail":"secret payload"}',ack:'result'}),r=>r.write(),'uncertain','provider_outcome_unknown',' http=200');
 expect(httpRig({body:'secret, not JSON',ack:'result'}),r=>r.write(),'uncertain','provider_outcome_unknown',' http=200');
 expect(httpRig({thing:false}),r=>r.write(),'terminal','device_not_reached',' exc=executor_thing_unavailable');
 expect(httpRig({status:500,body:'secret body'}),r=>r.read(),'terminal','owned_probe_failed',' http=500');
 const lagging=httpRig({observed:2});lagging.write('3');
 assert.deepEqual(timeless(tickets(lagging.f)[0]),{route:'-',room:'-',choice:'-',resource:'D1',value:'3',phase:'acquire',outcome:'terminal',reason:'-',readback:'unconfirmed',polls:'4'});
});
test('a manual command keeps a private value private and takes a gateway id from its source',()=>{
 const f=setup(),values=[];f.provider.has=()=>true;f.provider.execute=(r,v)=>{values.push(v);return {outcome:'applied'};};
 f.bridge.onLegacy(e=>{if(e.type==='legacy')f.bridge.authorizeLegacy(e.token);});
 assert(f.actor.legacy('source_pairing','4321',true,'thermite.tui:0123abcd=>org.openhab.core.io.rest$thermite'));
 f.bridge.events();f.actor.drain();f.bridge.events();
 assert.deepEqual(values,['4321']);
 assert.deepEqual(timeless(tickets(f)[0]),{route:'0123abcd',room:'-',choice:'-',resource:'source_pairing',value:'<private>',phase:'legacy',outcome:'terminal',reason:'-',readback:'-',polls:'0'});
 assert(!f.logs.some(l=>l[1].includes('4321')));assert(!String(f.broker.states.get('switch')).includes('4321'));
 for(const source of ['org.openhab.core.io.rest$admin','thermite.tui:0123ABCD','free text thermite.tui:0123abcd',null,undefined,42]){
  f.logs.length=0;assert(f.actor.legacy('D1','2',false,source));f.bridge.events();f.actor.drain();f.bridge.events();
  const line=tickets(f)[0];assert.equal(fieldsOf(line).route,'-',String(source));assert(!/admin|free|0123/i.test(line),line);
 }
 // The native control rule hands its event source over.
 const rules={},rig=httpRig({rules});rig.bridge.onLegacy(e=>{if(e.type==='legacy')rig.bridge.authorizeLegacy(e.token);});
 rules.thermite_routing_legacy_switch.execute({itemName:'D1',receivedCommand:'3',eventSource:'thermite.web:1a2b3c4d=>org.openhab.core.io.rest$thermite'});
 rig.bridge.events();rig.actor.drain();rig.bridge.events();
 assert.deepEqual(timeless(tickets(rig.f)[0]),{route:'1a2b3c4d',room:'-',choice:'-',resource:'D1',value:'3',phase:'legacy',outcome:'terminal',reason:'-',readback:'confirmed',polls:'0'});
});
test('a planner route carries its command id through the broker onto every ticket line',()=>{
 const f=setup();let actual='1';
 f.provider.probe=()=>({value:actual,fresh:true});f.provider.execute=(r,v)=>{actual=v;return {outcome:'applied',sample:{value:v,fresh:true}};};
 const {createCoordinator}=require(path.join(path.dirname(path.resolve(process.argv[2])),'lifecycle.js'));
 const planner=createCoordinator({version:1,sources:{},resources:{D1:{key:'switch',values:['1','2','3','4'],capable:true}},
  endpoints:{den:{choices:{tv:{requirements:{D1:'3'}}}}}},{instanceId:'test',store:{load:()=>null,save(){}},now:()=>0,setTimer:()=>0,clearTimer(){},
  dispatch:(op,report)=>f.bridge.dispatch(op,report),probe:(op,report)=>f.bridge.probe(op,report),changed:()=>f.bridge.invalidateStale()});
 planner.request('den','tv','1a2b3c4d');
 for(let n=0;n<4;n++){f.actor.drain();f.bridge.events();}
 assert(planner.snapshot().endpoints.den.pathReady);
 assert.deepEqual(tickets(f).map(timeless),[
  {route:'1a2b3c4d',room:'den',choice:'tv',resource:'D1',value:'-',phase:'probe',outcome:'sample',reason:'-',readback:'-',polls:'0'},
  {route:'1a2b3c4d',room:'den',choice:'tv',resource:'D1',value:'3',phase:'acquire',outcome:'terminal',reason:'-',readback:'confirmed',polls:'0'}]);
});
test('Java exceptions are named by class however GraalJS presents it, never by message',()=>{
 const shapes=[
  name=>({getClass:()=>({getSimpleName:()=>name})}),
  name=>({getClass:()=>({class:{getSimpleName:()=>name}})}),
  name=>({getClass:()=>{throw Error('no reflection');},toString:()=>`java.net.${name}: secret message`}),
  name=>({getClass:()=>({}),toString:()=>`org.eclipse.jetty.Outer$${name}`}),
 ];
 for(const shape of shapes){
  const f=setup();
  f.provider.execute=()=>{throw {...shape('ExecutionException'),getCause:()=>({...shape('ConnectException'),getCause:()=>null})};};
  f.submit('java','D1');f.actor.drain();
  const [line]=tickets(f);assert(line.endsWith(' exc=ConnectException'),line);assert(!line.includes('secret'),line);
 }
 // A toString() that is not a qualified class name names nothing.
 const f=setup();f.provider.execute=()=>{throw {getClass:()=>({}),getCause:()=>null,toString:()=>'secret: message'};};
 f.submit('opaque','D1');f.actor.drain();assert(tickets(f)[0].endsWith(' polls=0'));
});
process.stdout.write(JSON.stringify({passed:tests.length,tests}));
