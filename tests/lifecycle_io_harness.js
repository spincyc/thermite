'use strict';
const fs=require('fs'), path=require('path'), vm=require('vm'), assert=require('assert');
const {RECORD}=require('./command_record.js');
// argv: lifecycle_io.js, pyatv_http.js, and a JSON file with _device.js as
// that script embeds it under `pyatv_http` (_io.device()).
const source=fs.readFileSync(process.argv[2],'utf8');
// The Apple TV script with its tables, and what it shares embedded as
// _io.py embeds it; its thermiteIO stays each test's fixture.
const appleTvScript=()=>fs.readFileSync(process.argv[3],'utf8').replace('const DEVICES = null;',`const DEVICES = [{id:'source',host:'192.0.2.1',helper:'http://owned'}];`)
 .replace('const PRESSES = null;',"const PRESSES = {menu:'menu'};").replace('const APPS = null;','const APPS = {};')
 .replace('// THERMITE_DEVICE',()=>JSON.parse(fs.readFileSync(process.argv[4],'utf8')).pyatv_http);
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
 const logs=[], sleeps=[], records=[];
 const log={error:m=>logs.push(['error',m]),warn:m=>logs.push(['warn',m]),info:m=>logs.push(['info',m])};
 const env={broker,noRules:true,wake:name=>wakes.push(name),now:options.now||(()=>++serial),id:()=>String(++serial),log,sleep:ms=>sleeps.push(ms)};
 const bridge=module.create(descriptor,env);
 const journal={data:initial||null,load(){return this.data;},save(row){if(fail)throw Error('disk failure');this.data=JSON.parse(JSON.stringify(row));journals.push({...row});if(midway&&row.status==='admitted')midway();}};
 const provider={has:(r,v)=>['D1','D2'].includes(r)&&['1','2','3','4'].includes(String(v)),execute(r,v){writes.push([r,v]);return {outcome:'applied',sample:{value:String(v),fresh:true}};},probe(r){return {value:'3',fresh:true};}};
 // The device script's command record (thermiteDevice.recorder()), as the actor is handed it.
 const actor=module.actor({key:'switch',io:descriptor.io},provider,{...env,journal,id:()=>String(++serial),record:options.record||(row=>records.push(row))});
 const submit=(token,resource,value='3')=>bridge.dispatch({token,key:'switch',resource,value,phase:'acquire',isCurrent:()=>active},r=>events.push({...r}));
 return {bridge,actor,submit,writes,events,journals,wakes,provider,broker,journal,descriptor,env,module,logs,sleeps,records,
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
function httpRig({status=200,body='',thing=true,ack='readback',fail=null,observed=3,rules=null,record=null}={}){
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
  {...f.env,noRules:!rules,journal:{load:()=>null,save(){}},...(record?{record}:{})});
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
 vm.runInContext(appleTvScript(),context);
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
test('a replacement actor ends a flight its dead predecessor left, so recovery and new work go on',()=>{
 // The old actor's context dies between admission and its final broker
 // transaction (emulated: the broker is put back as it was mid-flight).
 let time=1000;const f=setup(null,source,{now:()=>time});let frozen=null;
 f.provider.execute=()=>{frozen={states:new Map(f.broker.states),active:f.broker.active.n,journal:JSON.parse(JSON.stringify(f.journal.data))};return {outcome:'applied'};};
 f.submit('lost','D1','3');f.actor.drain();
 f.broker.states.clear();for(const [key,value] of frozen.states)f.broker.states.put(key,value);
 f.broker.active.n=frozen.active;f.journal.data=frozen.journal;f.broker.events.rows.length=0;f.events.length=0;
 assert.equal(JSON.parse(f.broker.states.get('switch')).flight.token,'lost');assert.equal(f.broker.active.get(),1);
 const writes=[];const provider={...f.provider,execute(r,v){writes.push([r,v]);return {outcome:'applied',sample:{value:String(v),fresh:true}};},probe:()=>({value:'3',fresh:true})};
 const replacement=f.module.actor({key:'switch',io:f.descriptor.io},provider,{...f.env,journal:f.journal});
 let row=JSON.parse(f.broker.states.get('switch'));
 assert.equal(row.flight,null);assert.equal(row.quarantine,'lost');assert.equal(f.broker.active.get(),0);
 // The planner hears that the lost ticket's outcome is unknown.
 f.bridge.events();assert(f.events.some(e=>e.token==='lost'&&e.type==='uncertain'&&e.reason==='provider_outcome_unknown'));
 time+=61000;assert.equal(replacement.recover(),true);
 f.submit('after','D2','4');replacement.drain();f.bridge.events();
 assert.deepEqual(writes,[['D2','4']]);assert.equal(f.broker.active.get(),0);
 assert(f.events.some(e=>e.token==='after'&&e.type==='terminal'&&e.admitted===true));
 // An old actor that was only slow, not dead, finishing after its
 // replacement took over, releases nothing twice.
 const g=setup();let late=null;
 g.provider.execute=()=>{late=g.module.actor({key:'switch',io:g.descriptor.io},g.provider,{...g.env,journal:g.journal});return {outcome:'applied'};};
 g.submit('slow','D1','3');g.actor.drain();
 assert(late);assert.equal(g.broker.active.get(),0);row=JSON.parse(g.broker.states.get('switch'));
 assert.equal(row.flight,null);assert.equal(row.quarantine,'slow');
});
test('an old actor finishing a flight its replacement took changes nothing shared: no quarantine, journal or report',()=>{
 for(const [ending,fields] of [['applied',undefined],['lost',undefined],['lost',{D1:{independent:true}}]]){
  let time=1000;const g=setup(null,source,{now:()=>time});let after=null;
  if(fields)g.actor=g.module.actor({key:'switch',io:g.descriptor.io,fields},g.provider,{...g.env,journal:g.journal});
  g.provider.execute=()=>{
   // A replacement takes the flight, and recovers the key before the old one returns.
   const late=g.module.actor({key:'switch',io:g.descriptor.io,...(fields?{fields}:{})},g.provider,{...g.env,journal:g.journal});
   time+=61000;assert.equal(late.recover(),true);
   after={state:g.broker.states.get('switch'),journal:JSON.stringify(g.journal.data),queued:g.broker.events.rows.length,
    active:g.broker.active.get(),errors:g.logs.filter(l=>l[0]==='error').length};
   if(ending==='lost')throw Error('lost reply');return {outcome:'applied',sample:{value:'3',fresh:true}};
  };
  g.submit(String('slow'),'D1','3');g.actor.drain();
  const label=ending+(fields?' independent':'');
  assert(after,label);
  assert.equal(g.broker.states.get('switch'),after.state,label);
  assert.equal(JSON.stringify(g.journal.data),after.journal,label);
  assert.equal(g.broker.events.rows.length,after.queued,label);
  assert.equal(g.broker.active.get(),after.active,label);
  assert.equal(g.logs.filter(l=>l[0]==='error').length,after.errors,label);
 }
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
test('a manual command says whether its field is independent, so routing holds that field alone',()=>{
 const f=setup();const actor=f.module.actor({key:'switch',io:f.descriptor.io,fields:{D1:{independent:true}}},f.provider,{...f.env,journal:f.journal});
 const seen=[];f.bridge.onLegacy(e=>{if(e.type==='legacy')seen.push([e.resource,e.independent===true]);});
 actor.legacy('D1','3');actor.legacy('D2','3');f.bridge.events();
 assert.deepEqual(seen,[['D1',true],['D2',false]]);
});
test('an event whose handling throws is logged and dropped, and the events after it are still handled',()=>{
 const f=setup();const seen=[];
 f.bridge.onLegacy(e=>{seen.push(e.token);if(e.token==='bad')throw Error('recovery names an unknown property');});
 f.bridge.probe({token:String('other-read'),key:'other',resource:'P'},e=>seen.push('report:'+e.type));
 for(const token of ['bad','next'])f.broker.events.offer(JSON.stringify({type:'terminal',legacy:true,key:'switch',token}));
 f.broker.events.offer(JSON.stringify({type:'terminal',key:'other',token:String('other-read')}));
 f.bridge.events();
 assert.deepEqual(seen,['bad','next','report:terminal']);assert(f.broker.events.isEmpty());
 const errors=f.logs.filter(l=>l[0]==='error').map(l=>l[1]);
 assert.deepEqual(errors,['routing executor could not handle a terminal event for switch (Error); the events after it go on']);
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
test('busy and transient outcomes cross as terminal retries with a bounded wait, never quarantine',()=>{
 const cases=[
  [{outcome:'busy',retryAfterMs:2999.2},{type:'terminal',admitted:true,busy:true,reason:'device_busy',retryAfterMs:3000}],
  [{outcome:'busy',retryAfterMs:1e9},{type:'terminal',admitted:true,busy:true,reason:'device_busy',retryAfterMs:300000}],
  [{outcome:'busy',retryAfterMs:-1},{type:'terminal',admitted:true,busy:true,reason:'device_busy'}],
  [{outcome:'busy',retryAfterMs:'5000'},{type:'terminal',admitted:true,busy:true,reason:'device_busy'}],
  [{outcome:'unsent',transient:true,retryAfterMs:2000},{type:'terminal',admitted:false,reason:'device_not_reached',transient:true,retryAfterMs:2000}],
  [{outcome:'unsent',transient:true},{type:'terminal',admitted:false,reason:'device_not_reached',transient:true}],
  // Only `true` is transient; a wait alone makes no retry.
  [{outcome:'unsent',transient:'yes',retryAfterMs:2000},{type:'terminal',admitted:false,reason:'device_not_reached'}],
  [{outcome:'refused',retryAfterMs:2000},{type:'terminal',admitted:true,refused:true,reason:'device_refused'}],
 ];
 for(const [done,expected] of cases){
  const f=setup();f.provider.execute=()=>done;f.submit('declined','D1');f.actor.drain();f.bridge.events();
  const report=f.events.find(e=>e.token==='declined'&&e.type==='terminal');const {token,key,resource,legacy,managed,...rest}=report;
  assert.deepEqual(rest,expected,JSON.stringify(done));
  assert.equal(JSON.parse(f.broker.states.get('switch')).quarantine,null);
  const row=fieldsOf(tickets(f)[0]);assert.equal(row.readback,'-');assert.equal(row.reason,expected.reason);
  f.provider.execute=(r,v)=>{f.writes.push([r,v]);return {outcome:'applied'};};f.submit('next','D1');f.actor.drain();assert.equal(f.writes.length,1);
 }
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
test('an unknown outcome on an independent field quarantines it alone, across a restart, until a read of it',()=>{
 let time=1000;const f=setup(null,source,{now:()=>time}),proof=[];f.bridge.onLegacy(e=>proof.push(e));
 const fields={D1:{independent:true},D2:{independent:true}};
 const actor=f.module.actor({key:'switch',io:f.descriptor.io,fields},f.provider,{...f.env,journal:f.journal});
 f.provider.execute=(r,v)=>{if(r==='D1')throw Error('lost reply');f.writes.push([r,v]);return {outcome:'applied',sample:{value:String(v),fresh:true}};};
 f.submit('lost','D1','3');actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.token==='lost'&&e.type==='uncertain'&&e.reason==='provider_outcome_unknown'));
 let row=JSON.parse(f.broker.states.get('switch'));
 assert.equal(row.quarantine,null);assert.deepEqual(row.fences,{D1:{token:String('lost'),at:1000}});
 assert(f.logs.some(l=>l[0]==='error'&&l[1].startsWith('switch: routing quarantined D1: D1 had an unknown outcome')));
 // The other field goes on; this one refuses routed and manual commands.
 f.submit('other','D2','4');actor.drain();f.bridge.events();assert.deepEqual(f.writes,[['D2','4']]);
 f.submit('again','D1','2');actor.drain();f.bridge.events();
 assert(f.events.some(e=>e.token==='again'&&e.type==='uncertain'&&e.reason==='executor_quarantined'));
 assert.equal(actor.legacy('D1','2'),false);assert.equal(f.writes.length,1);
 assert.deepEqual(f.bridge.status().quarantinedKeys,['switch']);
 // The journal keeps it though a later ticket retired: a restart fences
 // the field again, or, where it is no longer independent, the device.
 assert.equal(f.journal.data.status,'retired');assert.deepEqual(f.journal.data.fences,{D1:'lost'});
 const g=setup();g.journal.data=f.journal.data;
 g.module.actor({key:'switch',io:g.descriptor.io,fields},g.provider,{...g.env,journal:g.journal});
 row=JSON.parse(g.broker.states.get('switch'));assert.equal(row.quarantine,null);assert.equal(row.fences.D1.token,'lost');
 assert.equal(JSON.parse(setup(f.journal.data).broker.states.get('switch')).quarantine,'lost');
 // Recovery reads that field alone, from the window on, and says so.
 const reads=[];f.provider.probe=r=>{reads.push(r);return {value:'3',fresh:true};};
 time+=59999;assert.equal(actor.recover(),false);assert.deepEqual(reads,[]);
 time+=1;f.events.length=0;proof.length=0;assert.equal(actor.recover(),true);assert.deepEqual(reads,['D1']);
 row=JSON.parse(f.broker.states.get('switch'));assert(!('fences' in row));assert.equal(row.quarantine,null);
 assert.deepEqual(f.journal.data.fences,{});assert.equal(f.journal.data.status,'retired');
 assert(f.logs.some(l=>l[0]==='info'&&l[1]==='switch: routing quarantine of D1 cleared: a fresh read of D1 answered; commands are sent again'));
 f.bridge.events();
 assert(proof.some(e=>e.type==='retired'&&e.recovered&&e.key==='switch'&&e.resource==='D1'&&e.token==='lost'));
 assert(proof.some(e=>e.type==='retired'&&e.recovered&&e.key==='switch'&&!('resource' in e)),'the whole device, once nothing is left');
 f.provider.execute=(r,v)=>{f.writes.push([r,v]);return {outcome:'applied'};};f.submit('after','D1','3');actor.drain();
 assert.deepEqual(f.writes.at(-1),['D1','3']);
 // A field's readiness proves nothing while it is fenced again or flying.
 proof.length=0;row=JSON.parse(f.broker.states.get('switch'));row.fences={D1:{token:String('next'),at:time}};f.broker.states.put('switch',JSON.stringify(row));
 f.broker.events.offer(JSON.stringify({type:'ready',key:'switch',actor:row.actor,resource:'D1',token:String('lost')}));
 f.broker.events.offer(JSON.stringify({type:'ready',key:'switch',actor:row.actor}));f.bridge.events();
 assert.deepEqual(proof,[]);
 // A field not declared independent still quarantines the whole device.
 const h=setup(),only=h.module.actor({key:'switch',io:h.descriptor.io,fields:{D1:{independent:true}}},h.provider,{...h.env,journal:h.journal});
 h.provider.execute=()=>{throw Error('lost reply');};h.submit('whole','D2','3');only.drain();
 row=JSON.parse(h.broker.states.get('switch'));assert.equal(row.quarantine,'whole');assert(!row.fences);
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
  // An integration's own fixed code, and an executor's with a prefix, name themselves too.
  [()=>{throw Error('native_avpro_status_unavailable');},'uncertain','provider_outcome_unknown',' exc=native_avpro_status_unavailable'],
  [()=>{throw Error('unsupported_executor_state_transform');},'uncertain','provider_outcome_unknown',' exc=unsupported_executor_state_transform'],
  [()=>{throw Error('native_power: secret body');},'uncertain','provider_outcome_unknown',' exc=Error'],
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
test('an HTTP actor names why its write was not applied by a problem code',()=>{
 const coded=options=>{const records=[],rig=httpRig({...options,record:row=>records.push(row)});rig.write();
  assert.equal(records.length,1);return [records[0].outcome,records[0].code];};
 assert.deepEqual(coded({}),['applied',null]);
 assert.deepEqual(coded({status:404}),['refused','refused']);
 assert.deepEqual(coded({status:503}),['unknown','protocol']);
 assert.deepEqual(coded({fail:javaError('ExecutionException',javaError('ConnectException'),{unreached:true})}),['unsent','unreachable']);
 assert.deepEqual(coded({fail:javaError('TimeoutException')}),['unknown','no_answer']);
 assert.deepEqual(coded({body:'{"result":0}',ack:'result'}),['unknown','protocol']);
 assert.deepEqual(coded({thing:false}),['unsent','unreachable']);
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
test('each write an actor concludes is handed once to its device script\'s command record, as the device ended it; a probe is not',()=>{
 const cases=[
  [()=>({outcome:'applied',sample:{value:'3',fresh:true}}),'applied',null],
  [()=>({outcome:'refused',code:'not_ready'}),'refused','not_ready'],
  [()=>({outcome:'refused'}),'refused','refused'],
  [()=>({outcome:'busy',code:'busy',retryAfterMs:500}),'busy','busy'],
  [()=>({outcome:'unsent',code:'unreachable'}),'unsent','unreachable'],
  [()=>({outcome:'unknown',code:'no_answer'}),'unknown','no_answer'],
  // No word of why: the record itself names it `other` (_device.js).
  [()=>{throw Error('lost reply');},'unknown',undefined],
  [()=>'done','unknown',undefined],
  // A one-shot its helper completed by declining it is retired, and recorded as it ended.
  [()=>({outcome:'applied',record:{outcome:'unsent',code:'auth'}}),'unsent','auth'],
 ];
 for(const [execute,outcome,code] of cases){
  const f=setup();f.provider.execute=execute;f.submit('ticket','D1');f.actor.drain();f.bridge.events();
  assert.equal(f.records.length,1,outcome);assert.equal(tickets(f).length,1);
  const [row]=f.records;
  assert.deepEqual([row.item,row.command,row.secret,row.outcome,row.code],['D1','3',false,outcome,code]);
  assert(Number.isFinite(row.ms)&&row.ms>=0,outcome);
 }
 const f=setup();f.bridge.probe({token:String('read'),key:'switch',resource:'D1'},()=>{});f.actor.drain();
 assert.equal(tickets(f).length,1);assert.deepEqual(f.records,[]);
 // A record that throws changes no outcome.
 const g=setup(null,source,{record(){throw Error('record failed');}});g.submit('ticket','D1');g.actor.drain();g.bridge.events();
 assert(g.events.some(e=>e.type==='terminal'&&e.token==='ticket'));assert.equal(JSON.parse(g.broker.states.get('switch')).quarantine,null);
});
test('a manual command the actor will not send is recorded dropped: a value it does not have, a device it holds, a read-only copy, a reload',()=>{
 const rows=f=>f.records.map(row=>[row.item,row.command,row.secret,row.outcome,row.code,row.ms]);
 const f=setup();
 assert.equal(f.actor.legacy('D1','99'),false);assert.equal(f.actor.legacy('D1','0000',true),false);
 assert.deepEqual(rows(f),[['D1','99',false,'dropped','invalid',0],['D1','<private>',true,'dropped','invalid',0]]);
 // Quarantined after an unknown outcome, it queues nothing.
 f.provider.execute=()=>{throw Error('lost reply');};f.submit('unknown','D1');f.actor.drain();f.bridge.events();f.records.length=0;
 assert.equal(f.actor.legacy('D2','3'),false);
 assert.deepEqual(rows(f),[['D2','3',false,'dropped','held',0]]);
 const copy=setup(null,source.replace('const SEND = true;','const SEND = false;'));
 copy.bridge.onLegacy(e=>{if(e.type==='legacy')copy.bridge.authorizeLegacy(e.token);});
 assert(copy.actor.legacy('D1','2'));copy.bridge.events();copy.actor.drain();
 assert.deepEqual(rows(copy).map(row=>row.slice(0,5)),[['D1','2',false,'dropped','read_only']]);assert.deepEqual(copy.writes,[]);
 // A reload cancels what the old actor had queued and not sent: its script's new record says so.
 const old=setup(),later=[];
 assert(old.actor.legacy('D1','2'));assert.deepEqual(old.records,[]);
 old.module.actor({key:'switch',io:old.descriptor.io},old.provider,{...old.env,journal:{load:()=>null,save(){}},record:row=>later.push(row)});
 assert.deepEqual(later.map(row=>[row.item,row.command,row.outcome,row.code]),[['D1','2','dropped','held']]);
});
test('a command the Apple TV rule hands its actor is recorded once, by the actor, in the record the rule itself would use',()=>{
 const f=setup(),logs=[],rules={},items={},desc={io:{epoch:'record',wake:'events',actors:{source:{wake:'sourceWake',resources:['source_power']}}}};
 const bridge=f.module.create(desc,f.env);bridge.onLegacy(e=>{if(e.type==='legacy')bridge.authorizeLegacy(e.token);});
 let status=200,actor=null;
 const openhab={rules:{JSRule(rule){rules[rule.id]=rule;}},triggers:{GenericCronTrigger(){},ItemCommandTrigger:name=>name},
  items:{getItem:name=>({get state(){return name in items?items[name]:'NULL';},postUpdate(value){items[name]=String(value);}})}};
 // The module the script requires, its actor on this test's broker and clock.
 const routingIO={actor(config,provider,given){actor=f.module.actor(config,provider,{...f.env,...given,journal:{load:()=>null,save(){}}});return actor;}};
 const context=vm.createContext({Date,JSON,Map,Set,Number,Object,String,Error,
  console:{info:m=>logs.push(['info',m]),warn:m=>logs.push(['warn',m])},
  thermiteIO:{unreached:()=>false,http(url,method,body){const payload=JSON.parse(body);
   return {getStatus:()=>status,getContentAsString:()=>JSON.stringify({id:'source',operation:payload.operation,terminal:true,ok:status===200,paired:true,connected:true,power:'on',power_reported_at:1000})};}},
  require(name){if(name==='openhab')return openhab;assert.equal(name,'thermite-routing-io');return routingIO;}});
 vm.runInContext(appleTvScript().replace('const ROUTING_EXECUTOR = null;',`const ROUTING_EXECUTOR = ${JSON.stringify({key:'source',io:desc.io})};`),context);
 assert(actor,'the script built no actor');
 const press=value=>{rules.pyatv_http_source_command.execute({itemName:'source_key',receivedCommand:value});bridge.events();actor.drain();bridge.events();};
 const recorded=()=>logs.filter(([,line])=>line.startsWith('command device=')).map(([level,line])=>[level,...RECORD.exec(line).slice(2,6)]);
 press('menu');
 assert.deepEqual(recorded(),[['info','source_key','menu','applied','-']]);
 // The helper completes the one-shot by declining it (not paired): the
 // executor retires its ticket, and the record says the key went nowhere.
 status=409;press('menu');
 assert.deepEqual(recorded().slice(1),[['warn','source_key','menu','unsent','auth']]);
 assert.equal(items.source_problem,'auth');
 assert.deepEqual(tickets(f).map(line=>fieldsOf(line).outcome),['terminal','terminal']);
 // What the actor will not queue is the rule's command all the same.
 press('nokey');
 assert.deepEqual(recorded().slice(2),[['warn','source_key','?','dropped','invalid']]);
 status=200;press('menu');
 assert.equal(items.source_problem,'UNDEF');
 const counted=Object.entries(JSON.parse(items.source_pyatv_http_commands).outcomes).filter(([,row])=>row.count).map(([outcome,row])=>[outcome,row.count]);
 assert.deepEqual(counted,[['applied',2],['dropped',1],['unsent',1]]);
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
test('actor startup loads its journal under the broker lock',()=>{
 const f=setup();let depth=0;
 f.module.actor({key:'switch',io:f.descriptor.io},f.provider,{...f.env,journal:{load(){depth=f.broker.lock.depth;return null;},save(){}}});
 assert(depth>0,'journal.load() must run inside transaction()');
});
test('when one independent field is fenced, healthy independent fields recover across startup, reload, overflow and whole-device recovery without clearing the fenced field',()=>{
 let time=1000;const f=setup(null,source,{now:()=>time}),proof=[];f.bridge.onLegacy(e=>proof.push(e));
 const fields={D1:{independent:true},D2:{independent:true}};
 const actor=f.module.actor({key:'switch',io:f.descriptor.io,fields},f.provider,{...f.env,journal:f.journal});
 const {createCoordinator}=require(path.join(path.dirname(path.resolve(process.argv[2])),'lifecycle.js'));
 const planner=createCoordinator({version:1,sources:{},resources:{
  D1:{key:'switch',values:['1','2','3','4'],capable:true,independent:true},
  D2:{key:'switch',values:['1','2','3','4'],capable:true,independent:true}},
  endpoints:{west:{choices:{a:{requirements:{D1:'3'}}}},east:{choices:{b:{requirements:{D2:'4'}}}}}},{
  instanceId:'test',store:{load:()=>null,save(){}},now:()=>time,setTimer:()=>0,clearTimer(){},
  dispatch:(op,report)=>f.bridge.dispatch(op,report),probe:(op,report)=>f.bridge.probe(op,report),changed:()=>f.bridge.invalidateStale()});
 f.bridge.onLegacy(e=>{proof.push(e);if(e.type==='retired'&&e.recovered&&e.key==='switch')planner.recover('switch',e.resource?{terminal:true,resource:e.resource}:{terminal:true});});
 // Fence D1 via an unknown outcome; D2 remains healthy.
 f.provider.probe=()=>({value:'1',fresh:true});
 f.provider.execute=(r,v)=>{if(r==='D1')throw Error('lost reply');return {outcome:'applied',sample:{value:String(v),fresh:true}};};
 planner.request('west','a');actor.drain();f.bridge.events();actor.drain();f.bridge.events();
 assert.equal(planner.snapshot().slots['switch#D1'].quarantine,'provider_outcome_unknown');
 assert.equal(planner.snapshot().slots['switch#D2'].quarantine,null);
 // Event overflow while D2 has an active operation quarantines switch#D2 in the planner;
 // overflow drain must recover D2 via per-field ready without touching D1.
 planner.request('east','b');f.broker.overflow.set(true);f.bridge.events();
 assert.equal(planner.snapshot().slots['switch#D2'].quarantine,'executor_event_overflow');
 proof.length=0;f.bridge.events();
 assert(proof.some(e=>e.type==='retired'&&e.recovered&&e.key==='switch'&&e.resource==='D2'));
 assert(!proof.some(e=>e.resource==='D1'||!('resource' in e)));
 assert.equal(planner.snapshot().slots['switch#D2'].quarantine,null);
 assert.equal(planner.snapshot().slots['switch#D1'].quarantine,'provider_outcome_unknown');
 // Controller reload while D1 is fenced notifies D2 only, never clearing D1.
 const reloaded=f.module.create(f.descriptor,f.env);const reloadProof=[];reloaded.onLegacy(e=>reloadProof.push(e));reloaded.events();
 assert.deepEqual(reloadProof.map(e=>[e.type,e.key,e.resource]),[['retired','switch','D2']]);
 // Device-level ready while D1 is fenced still must not clear D1.
 reloadProof.length=0;const row=JSON.parse(f.broker.states.get('switch'));
 f.broker.events.offer(JSON.stringify({type:'ready',key:'switch',actor:row.actor}));reloaded.events();
 assert.deepEqual(reloadProof,[]);
 // Actor startup (with rules enabled) restoring a journal with D1 fenced on a fresh broker announces D2 only.
 const fresh=setup(null,source,{now:()=>time}),startupProof=[];
 const startupContext=vm.createContext({module:{exports:{}},Date,JSON,Map,Object,String,Error,setTimeout:()=>1,clearTimeout(){},
  require(name){if(name==='openhab')return {rules:{JSRule(){}},triggers:{ItemCommandTrigger:n=>n}};if(name==='@runtime')return {lifecycleTracker:{addDisposeHook(){}}};assert.fail(name);}});
 vm.runInContext(source,startupContext);const sm=startupContext.module.exports;
 const sBridge=sm.create(fresh.descriptor,fresh.env);sBridge.onLegacy(e=>startupProof.push(e));sBridge.events();startupProof.length=0;
 const lost='lost';
 sm.actor({key:'switch',io:fresh.descriptor.io,fields},fresh.provider,{...fresh.env,noRules:false,journal:{load:()=>({version:1,key:'switch',epoch:'config-1',token:lost,status:'retired',fences:{D1:lost}}),save(){}}});
 sBridge.events();
 assert.deepEqual(startupProof.map(e=>[e.type,e.key,e.resource]),[['retired','switch','D2']]);
 // Whole-device quarantine recovery while D1 remains fenced clears quarantine and notifies D2 only.
 const wholeRow=JSON.parse(f.broker.states.get('switch'));wholeRow.quarantine='whole';wholeRow.quarantinedAt=time-61000;wholeRow.fences={D1:{token:lost,at:time}};
 f.broker.states.put('switch',JSON.stringify(wholeRow));
 const recoverActor=f.module.actor({key:'switch',io:f.descriptor.io,fields},f.provider,{...f.env,journal:{load:()=>null,save(){}}});
 const recoverBridge=f.module.create(f.descriptor,f.env);const recoverProof=[];recoverBridge.onLegacy(e=>recoverProof.push(e));recoverBridge.events();recoverProof.length=0;
 assert.equal(recoverActor.recover(),true);recoverBridge.events();
 assert.deepEqual(recoverProof.map(e=>[e.type,e.key,e.resource]),[['retired','switch','D2']]);
 const afterWhole=JSON.parse(f.broker.states.get('switch'));
 assert.equal(afterWhole.quarantine,null);assert.equal(afterWhole.fences.D1.token,'lost');
 // Flight on D1 ending uncertain after overflow (announce=true) still notifies unfenced D2.
 const ov=setup(null,source,{now:()=>time}),ovProof=[];ov.bridge.onLegacy(e=>ovProof.push(e));
 const ovActor=ov.module.actor({key:'switch',io:ov.descriptor.io,fields},ov.provider,{...ov.env,journal:ov.journal});
 ov.provider.execute=()=>{ov.broker.overflow.set(true);ov.bridge.events();throw Error('lost reply');};
 ov.submit('in-flight-d1','D1','3');ov.submit('queued-d2','D2','4');ovActor.drain();
 ovProof.length=0;ov.bridge.events();
 assert.deepEqual(ovProof.map(e=>[e.type,e.key,e.resource]),[['retired','switch','D2']]);
 assert.equal(JSON.parse(ov.broker.states.get('switch')).fences.D1.token,'in-flight-d1');
});
test('pyatv_http unreachable helper returns unsent/unreachable instead of quarantining and updates status',()=>{
 const updates=[];
 const err=Object.assign(Error('connection refused'),{unreached:true});
 const context=vm.createContext({Date,JSON,Map,Set,Number,Object,String,Error,
  console:{info(){},warn(){}},thermiteIO:{http(){throw err;},unreached:e=>!!e&&e.unreached===true},
  require(name){assert.equal(name,'openhab');return {rules:{JSRule(){}},triggers:{GenericCronTrigger(){},ItemCommandTrigger(){}},
   items:{getItem:item=>({state:'UNDEF',postUpdate(v){updates.push([item,String(v)]);}})}};}});
 vm.runInContext(appleTvScript(),context);
 const provider=vm.runInContext('routingProvider()',context), op={token:String('down'),value:'OFF'};
 assert.deepEqual(provider.execute('source_power','OFF',op),{outcome:'unsent',code:'unreachable'});
 assert(updates.some(([item,v])=>item==='source_pairing_status'&&v==='unavailable'));
 assert.throws(()=>provider.probe('source_power',op));
});
process.stdout.write(JSON.stringify({passed:tests.length,tests}));
