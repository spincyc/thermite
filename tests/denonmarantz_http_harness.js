'use strict';
// The Denon/Marantz routing actor (denonmarantz_http.js) on the generic
// executor (lifecycle_io.js), against a scripted receiver. argv: the
// executor's source, then the rendered rule (denonmarantz_http.rule()).
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const executor=fs.readFileSync(process.argv[2],'utf8'), rule=fs.readFileSync(process.argv[3],'utf8');
class NativeMap extends Map { get(key){return super.has(key)?super.get(key):null;} put(key,value){assert.equal(typeof value,'string');this.set(key,value);} }
class Queue {constructor(){this.rows=[];} offer(value){this.rows.push(value);return true;}poll(){return this.rows.length?this.rows.shift():null;}isEmpty(){return this.rows.length===0;}size(){return this.rows.length;} }
const number=()=>({n:0,get(){return this.n;},incrementAndGet(){return ++this.n;},decrementAndGet(){return --this.n;}});

// A minimal DOM for the receiver's XML: elements and text only. It records
// the parser settings, so the secure-processing contract is checked too.
function dom(xml){
 const node=(name,children)=>({name,children,getNodeType:()=>name===null?3:name==='#document'?9:1,getNodeName:()=>name,
  getChildNodes:()=>({getLength:()=>children.length,item:i=>children[i]}),
  getTextContent:()=>children.map(child=>child.text!==undefined?child.text:child.getTextContent()).join('')});
 const text=value=>Object.assign(node(null,[]),{text:value});
 if(/<!DOCTYPE/i.test(xml))throw Error('DOCTYPE is disallowed');
 const body=xml.replace(/^\s*<\?xml[^?]*\?>/,'');
 const root=node('#document',[]),stack=[root];
 const token=/<\/([A-Za-z0-9_]+)\s*>|<([A-Za-z0-9_]+)(?:\s[^>]*)?(\/)?>|([^<]+)/g;let match;
 while((match=token.exec(body))!==null){
  if(match[1]){if(stack.pop().name!==match[1])throw Error('mismatched tag');}
  else if(match[2]){const child=node(match[2],[]);stack[stack.length-1].children.push(child);if(!match[3])stack.push(child);}
  else if(match[4].trim()||stack.length>1)stack[stack.length-1].children.push(text(match[4]));
 }
 if(stack.length!==1)throw Error('unclosed tag');
 return root;
}
const SECURE={features:{'http://apache.org/xml/features/disallow-doctype-decl':true,'http://xml.org/sax/features/external-general-entities':false,
 'http://xml.org/sax/features/external-parameter-entities':false,'http://javax.xml.XMLConstants/feature/secure-processing':true},
 attributes:{'http://javax.xml.XMLConstants/property/accessExternalDTD':'','http://javax.xml.XMLConstants/property/accessExternalSchema':''},
 xinclude:false,expand:false};
function java(parsers){
 const types={
  'javax.xml.parsers.DocumentBuilderFactory':{newInstance(){
   const settings={features:{},attributes:{},xinclude:null,expand:null};parsers.push(settings);
   return {setFeature(name,value){settings.features[name]=value;},setAttribute(name,value){settings.attributes[name]=value;},
    setXIncludeAware(value){settings.xinclude=value;},setExpandEntityReferences(value){settings.expand=value;},
    newDocumentBuilder:()=>({setErrorHandler(){},parse:source=>dom(source.reader.text)})};}},
  'org.xml.sax.helpers.DefaultHandler':function(){},
  'org.xml.sax.InputSource':function(reader){this.reader=reader;},
  'java.io.StringReader':function(text){this.text=text;},
 };
 return {type(name){assert(Object.prototype.hasOwnProperty.call(types,name),name);return types[name];}};
}

// A receiver whose main zone and zone 2 answer reads and take commands.
function receiver(state){
 const requests=[];
 function answer(url,method,body,headers){
  requests.push({url,method,body,headers});
  if(state.fail)throw state.fail;
  if(state.status&&url.includes('formiPhoneAppDirect'))return {getStatus:()=>state.status,getContentAsString:()=>''};
  const command=/formiPhoneAppDirect\.xml\?(.*)$/.exec(url);
  if(command){
   const sent=decodeURIComponent(command[1]);state.commands.push(sent);
   if(state.ignore)return {getStatus:()=>200,getContentAsString:()=>''};
   if(sent==='ZMON'||sent==='ZMOFF')state.main.power=sent==='ZMON'?'ON':'OFF';
   else if(sent.startsWith('SI'))state.main.input=sent.slice(2);
   else if(sent==='Z2ON'||sent==='Z2OFF')state.zone2.power=sent.slice(2);
   else if(sent.startsWith('Z2'))state.zone2.input=sent.slice(2);
   return {getStatus:()=>200,getContentAsString:()=>''};
  }
  if(url.endsWith('/goform/AppCommand.xml')){
   const name=/<cmd id="1">([A-Za-z]+)<\/cmd>/.exec(body)[1];
   const inner=name==='GetAllZonePowerStatus'?`<zone1>${state.main.power}</zone1><zone2>${state.zone2.power}</zone2>`
    :`<source>${state.main.input}</source>`;
   return {getStatus:()=>200,getContentAsString:()=>state.xml||`<?xml version="1.0" encoding="utf-8" ?><rx><cmd>${inner}</cmd></rx>`};
  }
  if(url.endsWith('/goform/formZone2_Zone2XmlStatusLite.xml'))
   return {getStatus:()=>200,getContentAsString:()=>`<?xml version="1.0" encoding="utf-8" ?><item><Power><value>${state.zone2.power}</value></Power><InputFuncSelect><value>${state.zone2.input}</value></InputFuncSelect></item>`};
  return {getStatus:()=>404,getContentAsString:()=>''};
 }
 return {requests,answer};
}

const FIELDS={avr_main_power:{kind:'denon',thing:'avr_thing',zone:1,point:'power',allowed:['ON','OFF']},
 avr_main_input:{kind:'denon',thing:'avr_thing',zone:1,point:'input',allowed:['SAT/CBL','MPLAY','GAME1']},
 avr_zone2_power:{kind:'denon',thing:'avr_thing',zone:2,point:'power',allowed:['ON','OFF']},
 avr_zone2_input:{kind:'denon',thing:'avr_thing',zone:2,point:'input',allowed:['SAT/CBL','MPLAY','GAME1','SOURCE']}};

function rig(options={}){
 const state={main:{power:'ON',input:'SAT/CBL'},zone2:{power:'ON',input:'SOURCE'},commands:[],...options};
 const device=receiver(state),parsers=[],updates=[],rules=[],logs=[];
 const broker={states:new NativeMap(),events:new Queue(),active:number(),eventWake:{value:false,set(v){this.value=v;},compareAndSet(b,a){if(this.value!==b)return false;this.value=a;return true;}},
  overflow:{value:false,get(){return this.value;},set(v){this.value=v;}},lock:{lock(){},unlock(){}}};
 let serial=0;
 const env={broker,noRules:true,wake:()=>true,now:()=>++serial,id:()=>String(++serial),sleep(){},
  log:{error:m=>logs.push(['error',m]),warn:m=>logs.push(['warn',m]),info:m=>logs.push(['info',m])},journal:{load:()=>null,save(){}}};
 const thing={getConfiguration:()=>new NativeMap([['host','192.0.2.1'],['httpPort',8080]])};
 const openhab={things:{getThing:uid=>uid==='avr_thing'&&!state.noThing?{rawThing:thing}:null},items:{getItem:name=>({postUpdate:value=>updates.push([name,value])})},
  rules:{JSRule:row=>rules.push(row)},triggers:{ItemCommandTrigger:name=>({name})}};
 const shared={Java:java(parsers),thermiteIO:{http:device.answer,unreached:error=>!!error&&error.unreached===true},Date,JSON,Map,Object,String,Error,Number,encodeURIComponent,decodeURIComponent};
 const io=vm.createContext({...shared,module:{exports:{}},require(name){assert.equal(name,'openhab');return openhab;}});
 vm.runInContext(executor,io);
 const module=io.module.exports;
 const descriptor={io:{epoch:'denon-1',wake:'events',actors:{avr:{wake:'avr_wake',resources:Object.keys(FIELDS)}}}};
 const bridge=module.create(descriptor,env);
 let api=null;
 const routingIO={...module,actor(config,provider,given){assert.deepEqual(given,{readonly:false});api=module.actor(config,provider,{...env,...given});return api;},
  explicitControls:(config,actor)=>module.explicitControls(config,actor,{})};
 const config={key:'avr',io:descriptor.io,fields:FIELDS};
 const script=rule.replace('const ROUTING_EXECUTOR = null;','const ROUTING_EXECUTOR = '+JSON.stringify(config)+';');
 vm.runInContext(script,vm.createContext({...shared,require(name){if(name==='openhab')return openhab;assert.equal(name,'thermite-routing-io');return routingIO;}}));
 assert(api,'the rule built no actor');
 const reports=[];
 const probe=resource=>{bridge.probe({token:String('p')+(++serial),key:'avr',resource},row=>reports.push(row));api.drain();bridge.events();return reports.at(-1);};
 const write=(resource,value)=>{bridge.dispatch({token:String('w')+(++serial),key:'avr',resource,value,phase:'acquire'},row=>reports.push(row));api.drain();bridge.events();return reports.at(-1);};
 return {state,requests:device.requests,parsers,updates,rules,logs,reports,probe,write,api};
}
const tests=[];
function test(name,fn){fn();tests.push(name);}

test('main-zone reads keep the firmware UTF-8 CRLF serialization on AppCommand.xml',()=>{
 const f=rig();
 assert.equal(f.probe('avr_main_power').value,'ON');assert.equal(f.probe('avr_main_input').value,'SAT/CBL');
 for(const [index,name] of ['GetAllZonePowerStatus','GetSourceStatus'].entries()){
  const request=f.requests[index];
  assert.equal(request.url,'http://192.0.2.1:8080/goform/AppCommand.xml');assert.equal(request.method,'POST');
  assert.deepEqual(request.headers,{'Content-Type':'application/xml'});
  assert.equal(request.body,'<?xml version="1.0" encoding="utf-8"?>\r\n<tx>\r\n<cmd id="1">'+name+'</cmd>\r\n</tx>\r\n');
 }
 assert.deepEqual(f.updates,[['avr_main_power','ON'],['avr_main_input','SAT/CBL']]);
 assert.deepEqual(f.state.commands,[],'a read sends no command');
});
test('zone 2 reads its XmlStatusLite page',()=>{
 const f=rig();
 assert.equal(f.probe('avr_zone2_power').value,'ON');assert.equal(f.probe('avr_zone2_input').value,'SOURCE');
 assert.deepEqual(f.requests.map(r=>[r.method,r.url,r.body]),Array(2).fill(['GET','http://192.0.2.1:8080/goform/formZone2_Zone2XmlStatusLite.xml',null]));
});
test('every parse is secure: no DTD, no external entity or schema, no XInclude',()=>{
 const f=rig();f.probe('avr_main_power');f.probe('avr_zone2_input');
 assert.equal(f.parsers.length,2);for(const settings of f.parsers)assert.deepEqual(settings,SECURE);
});
test('a DOCTYPE, a duplicate field or a nested one is no reading',()=>{
 for(const xml of ['<?xml version="1.0"?><!DOCTYPE rx [<!ENTITY x "ON">]><rx><cmd><zone1>&x;</zone1></cmd></rx>',
  '<rx><cmd><zone1>ON</zone1><zone1>OFF</zone1></cmd></rx>','<rx><cmd><zone1><b>ON</b></zone1></cmd></rx>','<rx></rx>']){
  const f=rig({xml});const report=f.probe('avr_main_power');
  assert.equal(report.type,'terminal');assert.equal(report.reason,'owned_probe_failed');assert.deepEqual(f.updates,[]);
 }
});
test('a reported GAME reads as GAME1 (denonmarantz_http.INPUT_ALIASES)',()=>{
 const f=rig({main:{power:'ON',input:'GAME'}});
 const report=f.probe('avr_main_input');assert.equal(report.type,'sample');assert.equal(report.value,'GAME1');
 assert.deepEqual(f.updates,[['avr_main_input','GAME1']]);
});
test('a value outside the field is no reading',()=>{
 const f=rig({main:{power:'ON',input:'PHONO'}});
 assert.equal(f.probe('avr_main_input').reason,'owned_probe_failed');assert.deepEqual(f.updates,[]);
});
test('main-zone power is ZMON/ZMOFF, never the chassis PWSTANDBY; its input is SI',()=>{
 const f=rig();
 for(const [resource,value] of [['avr_main_power','OFF'],['avr_main_power','ON'],['avr_main_input','MPLAY']]){
  const report=f.write(resource,value);
  assert.equal(report.type,'terminal');assert(report.admitted);assert.equal(report.sample.value,value);
 }
 assert.deepEqual(f.state.commands,['ZMOFF','ZMON','SIMPLAY']);
 assert.equal(f.state.zone2.power,'ON','main Off left zone 2 on');
 const sent=f.requests.filter(r=>r.url.includes('formiPhoneAppDirect'));
 assert.deepEqual(sent.map(r=>[r.method,r.url,r.body]),[['GET','http://192.0.2.1:8080/goform/formiPhoneAppDirect.xml?ZMOFF',null],
  ['GET','http://192.0.2.1:8080/goform/formiPhoneAppDirect.xml?ZMON',null],['GET','http://192.0.2.1:8080/goform/formiPhoneAppDirect.xml?SIMPLAY',null]]);
});
test('zone 2 takes Z2<value>, its follow-main input included, URL-encoded',()=>{
 const f=rig();
 for(const [resource,value] of [['avr_zone2_power','OFF'],['avr_zone2_input','SAT/CBL'],['avr_zone2_input','SOURCE']])assert(f.write(resource,value).admitted);
 assert.deepEqual(f.state.commands,['Z2OFF','Z2SAT/CBL','Z2SOURCE']);
 assert(f.requests.some(r=>r.url.endsWith('formiPhoneAppDirect.xml?Z2SAT%2FCBL')));
});
test('a write outside the field, or without the Thing, sends nothing',()=>{
 const f=rig();
 const outside=f.api.legacy('avr_main_input','PHONO');assert(!outside);assert.equal(f.requests.length,0);
 const g=rig({noThing:true});const report=g.write('avr_main_power','OFF');
 assert.equal(report.type,'terminal');assert.equal(report.reason,'device_not_reached');assert.equal(g.requests.length,0);
});
test('a declined, unreached or lost command ends refused, unsent or unknown',()=>{
 for(const [options,outcome] of [[{status:403},'device_refused'],[{fail:{unreached:true}},'device_not_reached']]){
  const f=rig(options);const report=f.write('avr_main_power','OFF');
  assert.equal(report.type,'terminal');assert.equal(report.reason,outcome);assert(report.admitted!==true||outcome==='device_refused');
 }
 const lost=rig({status:500}).write('avr_main_power','OFF');assert.equal(lost.type,'uncertain');
});
test('an answered command whose readback still differs is applied without a sample',()=>{
 const f=rig({ignore:true});const report=f.write('avr_main_input','MPLAY');
 assert.equal(report.type,'terminal');assert(report.admitted);assert(!report.sample);
});
test('the rule routes explicit commands to its fields through the actor',()=>{
 const f=rig();
 assert.equal(f.rules.length,1);assert.equal(f.rules[0].id,'thermite_routing_legacy_avr');
 assert.deepEqual(f.rules[0].triggers.map(t=>t.name),Object.keys(FIELDS));
});
process.stdout.write(JSON.stringify({passed:tests.length,tests}));
