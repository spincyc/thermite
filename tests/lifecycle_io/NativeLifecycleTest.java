// Real Java containers shared by independent Graal contexts; all HTTP is loopback.
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import com.sun.net.httpserver.HttpServer;
import org.eclipse.jetty.client.HttpClient;
import org.eclipse.jetty.client.http.HttpClientTransportOverHTTP;
import org.eclipse.jetty.client.util.StringContentProvider;
import org.graalvm.polyglot.Context;
import org.openhab.core.automation.module.script.rulesupport.shared.ValueCache;

public class NativeLifecycleTest {
    public static final class RuntimeCache implements ValueCache {
        private final Map<String,Object> values;
        RuntimeCache(Map<String,Object> values) {this.values=values;}
        public Object put(String key,Object value) {return values.put(key,value);}
        public Object remove(String key) {return values.remove(key);}
        public Object get(String key) {return values.get(key);}
        public Object get(String key,java.util.function.Supplier<Object> supplier) {return values.computeIfAbsent(key,ignored->supplier.get());}
        public Object compute(String key,java.util.function.BiFunction<String,Object,Object> function) {return values.compute(key,function);}
    }
    public static final class Channel {
        public Map<String,Object> getConfiguration() {
            return Map.of("stateContent", "{\"comhead\":\"get_matrix_status\"}",
                "stateTransformation", List.of("JSONPATH($.all_host[0])"),
                "commandTransformation", List.of("JS(|JSON.stringify({comhead:'host_switch',device:[1,parseInt(input)]}))"));
        }
    }
    public static final class Thing {
        private final String url;
        Thing(String url) { this.url=url; }
        public Map<String,Object> getConfiguration() {
            URI uri=URI.create(url);
            return Map.of("baseURL",url,"stateMethod","POST","commandMethod","POST","contentType","application/json",
                "host",uri.getHost(),"httpPort",uri.getPort());
        }
        public Channel getChannel(String name) { return new Channel(); }
    }
    private static void check(boolean condition, String message) { if(!condition)throw new AssertionError(message); }
    private static Context context(Map<String,Object> shared, HttpClient common, Thing thing, String source, String descriptor) {
        Context ctx=Context.newBuilder("js").allowAllAccess(true).option("engine.WarnInterpreterOnly","false").build();
        ctx.getBindings("js").putMember("shared",shared);
        ctx.getBindings("js").putMember("runtimeCache",new RuntimeCache(shared));
        ctx.getBindings("js").putMember("common",common);
        ctx.getBindings("js").putMember("nativeThing",thing);
        ctx.eval("js", "const hooks=[], reports=[], updates=[], wakes=[]; const module={exports:{}};"+
            "function require(name){if(name==='@runtime/cache')return {sharedCache:runtimeCache};if(name==='@runtime')return {lifecycleTracker:{addDisposeHook:h=>hooks.push(h)}};"+
            "if(name==='thermite-routing-io')return module.exports;if(name!=='openhab')throw Error(name);return {cache:{shared:{get:()=>{throw Error('unsafe jsifying cache wrapper');}}},"+
            "osgi:{getService:()=>({getCommonHttpClient:()=>common})},things:{getThing:()=>({rawThing:nativeThing})},"+
            "rules:{JSRule(){}},triggers:{GenericCronTrigger(){},ItemCommandTrigger(){}},"+
            "items:{getItem:name=>({state:'UNDEF',postUpdate:value=>updates.push([name,value]),sendCommand:value=>wakes.push([name,value])})}};}"+
            "const descriptor="+descriptor+";");
        ctx.eval("js",source);
        return ctx;
    }
    private static String denonSource;
    // The receiver's own rule (denonmarantz_http.js, rendered) as its actor's provider.
    private static String denon(String fields) {
        return "const executor=(()=>{"+denonSource+";const fields="+fields+";"+
            "return module.exports.actor({key:'receiver',io:descriptor.io,fields},routingProvider(module.exports,fields),{noRules:true});})();";
    }
    private static String operation(String token,String key,String resource,String value) {
        return "{token:String('"+token+"'),key:'"+key+"',resource:'"+resource+"',value:'"+value+"',phase:'acquire',isCurrent:()=>true}";
    }
    private static String appCommand(String name) {
        return "<?xml version=\"1.0\" encoding=\"utf-8\"?>\r\n<tx>\r\n<cmd id=\"1\">"+name+"</cmd>\r\n</tx>\r\n";
    }
    public static void main(String[] args) throws Exception {
        System.setProperty("openhab.userdata",args[2]);
        String source=Files.readString(Path.of(args[0])).replace("// THERMITE_BOUNDED_IO",Files.readString(Path.of(args[1])));
        denonSource=Files.readString(Path.of(args[4]));
        String descriptor="{io:{epoch:'native-test',wake:'events',actors:{slow:{wake:'slow_wake',resources:['slow_D1']},fast:{wake:'fast_wake',resources:['fast_D1']},receiver:{wake:'receiver_wake',resources:['receiver_power','receiver_input']}}}}";
        var shared=new ConcurrentHashMap<String,Object>();
        var pool=Executors.newCachedThreadPool();
        var workers=Executors.newFixedThreadPool(2);
        var entered=new CountDownLatch(1);
        var release=new CountDownLatch(1);
        var state=new ConcurrentHashMap<String,AtomicInteger>();
        var writes=new ConcurrentHashMap<String,AtomicInteger>();
        var lost=new AtomicInteger();
        var mainPower=new java.util.concurrent.atomic.AtomicBoolean(true);
        var zone2Power=new java.util.concurrent.atomic.AtomicBoolean(true);
        var xmlMode=new AtomicInteger();
        var denonCommands=new CopyOnWriteArrayList<String>();
        HttpServer server=HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(),0),0);
        server.setExecutor(pool);
        server.createContext("/",exchange->{
            try {
                String path=exchange.getRequestURI().getPath();
                String body=new String(exchange.getRequestBody().readAllBytes(),StandardCharsets.UTF_8);
                var value=state.computeIfAbsent(path,key->new AtomicInteger(1));
                String answer;
                if(path.equals("/goform/formiPhoneAppDirect.xml")) {
                    String command=URLDecoder.decode(exchange.getRequestURI().getRawQuery(),StandardCharsets.UTF_8);
                    denonCommands.add(command);
                    if(command.equals("ZMOFF"))mainPower.set(false);
                    if(command.equals("PWSTANDBY")){mainPower.set(false);zone2Power.set(false);}
                    answer="";
                } else if(path.equals("/goform/AppCommand.xml")) {
                    String power=mainPower.get()?"ON":"OFF";
                    // Match the physical protocol: HTTP success alone does not
                    // imply a usable response to compact or encoding-less XML.
                    if(!body.equals(appCommand("GetAllZonePowerStatus"))&&!body.equals(appCommand("GetSourceStatus")))answer="<rx>\n</rx>";
                    else if(body.equals(appCommand("GetSourceStatus")))answer="<rx><cmd><source>SAT/CBL</source></cmd></rx>";
                    else answer=xmlMode.get()==0 ? "<rx><cmd><zone1>"+power+"</zone1><zone2>ON</zone2></cmd></rx>" :
                        xmlMode.get()==1 ? "<rx><cmd><zone1>OFF</zone1><zone1>ON</zone1></cmd></rx>" :
                        "<!DOCTYPE rx [<!ENTITY power SYSTEM 'http://127.0.0.1:"+server.getAddress().getPort()+"/entity'>]><rx><cmd><zone1>&power;</zone1></cmd></rx>";
                } else if(path.equals("/entity")) {throw new AssertionError("external XML entity fetched");}
                else if(body.contains("host_switch")) {
                    writes.computeIfAbsent(path,key->new AtomicInteger()).incrementAndGet();
                    value.set(3);
                    if(path.equals("/slow")){entered.countDown();release.await(5,TimeUnit.SECONDS);}
                    if(path.equals("/lost")){lost.incrementAndGet();Thread.sleep(4000);}
                    answer="{\"result\":1}";
                } else answer="{\"all_host\":["+value.get()+"]}";
                byte[] data=answer.getBytes(StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(200,data.length);exchange.getResponseBody().write(data);
            } catch(Exception expected) { /* disconnect is part of the lost-reply fixture */ }
            finally{exchange.close();}
        });
        server.start();
        String url="http://127.0.0.1:"+server.getAddress().getPort();
        HttpClient common=new HttpClient(new HttpClientTransportOverHTTP(1),null);
        common.start();
        try {
        for(String body:List.of(
                "<?xml version=\"1.0\"?><tx><cmd id=\"1\">GetAllZonePowerStatus</cmd></tx>",
                "<?xml version=\"1.0\" encoding=\"utf-8\"?><tx><cmd id=\"1\">GetAllZonePowerStatus</cmd></tx>",
                "<?xml version=\"1.0\"?>\r\n<tx>\r\n<cmd id=\"1\">GetAllZonePowerStatus</cmd>\r\n</tx>\r\n")) {
            var response=common.POST(url+"/goform/AppCommand.xml")
                .content(new StringContentProvider(body,StandardCharsets.UTF_8),"application/xml")
                .timeout(3,TimeUnit.SECONDS).send();
            check(response.getStatus()==200&&response.getContentAsString().equals("<rx>\n</rx>"),"fixture accepted unsupported AppCommand serialization");
        }
        try(Context root=context(shared,common,new Thing(url+"/unused"),source,descriptor);
            Context slow=context(shared,common,new Thing(url+"/slow"),source,descriptor);
            Context fast=context(shared,common,new Thing(url+"/fast"),source,descriptor)) {
            root.eval("js","const bridge=module.exports.create(descriptor); function report(e){reports.push(e);}");
            for(var entry:Map.of("slow",slow,"fast",fast).entrySet()) {
                String key=entry.getKey();
                entry.getValue().eval("js","const executor=module.exports.httpActor({key:'"+key+"',io:descriptor.io,fields:{"+key+"_D1:{kind:'http',thing:'owned',channel:'device1_host',allowed:['1','2','3','4']}}},{noRules:true});");
            }
            root.eval("js","bridge.dispatch("+operation("slow-write","slow","slow_D1","3")+",report);");
            Future<?> pending=workers.submit(()->slow.eval("js","executor.drain();"));
            if(!entered.await(3,TimeUnit.SECONDS)) {
                if(pending.isDone())pending.get();
                root.eval("js","bridge.events();");
                throw new AssertionError("slow write not admitted: "+root.eval("js","JSON.stringify(reports)"));
            }
            long start=System.nanoTime();
            root.eval("js","bridge.dispatch("+operation("fast-write","fast","fast_D1","3")+",report);");
            check(TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-start)<500,"router waited for network I/O");
            fast.eval("js","executor.drain();");
            root.eval("js","bridge.events(); if(!reports.some(e=>e.token==='fast-write'&&e.type==='terminal'&&e.admitted&&e.sample&&e.sample.fresh&&e.sample.retired&&e.sample.value==='3'&&e.sample.basis==='owned_post_write_read'))throw Error('owned fast terminal sample missing');");
            check(!pending.isDone(),"independent device waited for slow context");
            release.countDown();pending.get(3,TimeUnit.SECONDS);
            root.eval("js","bridge.events(); bridge.probe({token:String('owned-probe'),key:'fast',resource:'fast_D1'},report);");
            fast.eval("js","executor.drain();");
            root.eval("js","bridge.events();if(!reports.some(e=>e.token==='owned-probe'&&e.type==='sample'&&e.fresh&&e.retired&&e.value==='3'))throw Error('owned probe missing');");
            check(writes.get("/fast").get()==1,"probe replayed mutation");
            // Root context replacement revokes unadmitted old permissions.
            root.eval("js","bridge.dispatch("+operation("stale","fast","fast_D1","3")+",report); const replacement=module.exports.create(descriptor);");
            fast.eval("js","executor.drain();");
            check(writes.get("/fast").get()==1,"controller reload replayed a queued write");
            Object broker=shared.get("thermite.routing.executor.v1");
            var nativeStates=(Map<?,?>)((Map<?,?>)broker).get("states");
            check(nativeStates.values().stream().allMatch(value->value instanceof String),"shared broker retained Graal objects");
            Path journal=Path.of(args[2],"thermite-routing","executor_fast.json");
            check(Files.exists(journal),"native journal absent");
            check(Files.getPosixFilePermissions(journal).equals(PosixFilePermissions.fromString("rw-------")),"journal is not private");
            check(Files.getPosixFilePermissions(journal.getParent()).equals(PosixFilePermissions.fromString("rwx------")),"journal directory is not private");
            root.eval("js","const large=module.exports.nativeJournal('coordinator',1048576);large.save({value:'x'.repeat(70000)});if(large.load().value.length!==70000)throw Error('coordinator bound');let rejected=false;try{module.exports.nativeJournal('bounded').save({value:'x'.repeat(70000)});}catch(e){rejected=true;}if(!rejected)throw Error('actor journal unbounded');");
            for(Context ctx:List.of(slow,fast))ctx.eval("js","hooks.forEach(h=>h());");
        }
        // A transport timeout may have written. Persist unknown and never replay.
        shared.clear();
        try(Context root=context(shared,common,new Thing(url+"/unused"),source,descriptor);
            Context actor=context(shared,common,new Thing(url+"/lost"),source,descriptor)) {
            root.eval("js","const bridge=module.exports.create(descriptor);function report(e){reports.push(e);}bridge.dispatch("+operation("lost-write","slow","slow_D1","3")+",report);");
            actor.eval("js","const executor=module.exports.httpActor({key:'slow',io:descriptor.io,fields:{slow_D1:{kind:'http',thing:'owned',channel:'device1_host',allowed:['1','3']}}},{noRules:true});executor.drain();");
            root.eval("js","bridge.events();if(!reports.some(e=>e.token==='lost-write'&&e.type==='uncertain'))throw Error('timeout not quarantined'); bridge.dispatch("+operation("must-not-replay","slow","slow_D1","3")+",report);");
            actor.eval("js","executor.drain();hooks.forEach(h=>h());");
            check(lost.get()==1,"unknown operation replayed");
        }
        shared.clear();
        try(Context root=context(shared,common,new Thing(url+"/unused"),source,descriptor);
            Context actor=context(shared,common,new Thing(url+"/lost"),source,descriptor)) {
            root.eval("js","const bridge=module.exports.create(descriptor);function report(e){reports.push(e);}");
            actor.eval("js","const executor=module.exports.actor({key:'slow',io:descriptor.io},{has:()=>true,execute:()=>{throw Error('restart must not execute');}},{noRules:true});");
            root.eval("js","bridge.dispatch("+operation("restart","slow","slow_D1","3")+",report);bridge.events();if(!reports.some(e=>e.type==='uncertain'))throw Error('restart lost quarantine');");
            actor.eval("js","executor.drain();");
            check(lost.get()==1,"restart replayed operation");
        }
        // Zone power is independent. Native XML is parsed securely and rejects
        // duplicate fields instead of silently selecting whichever looks safe.
        shared.clear();
        try(Context root=context(shared,common,new Thing(url),source,descriptor);
            Context receiver=context(shared,common,new Thing(url),source,descriptor)) {
            root.eval("js","const bridge=module.exports.create(descriptor);function report(e){reports.push(e);}");
            receiver.eval("js",denon("{receiver_power:{kind:'denon',thing:'owned',zone:1,point:'power',allowed:['ON','OFF']},receiver_input:{kind:'denon',thing:'owned',zone:1,point:'input',allowed:['SAT/CBL','MPLAY']}}"));
            root.eval("js","bridge.probe({token:String('receiver-input'),key:'receiver',resource:'receiver_input'},report);");
            receiver.eval("js","executor.drain();");
            root.eval("js","bridge.events();if(!reports.some(e=>e.token==='receiver-input'&&e.type==='sample'&&e.value==='SAT/CBL'&&e.fresh&&e.retired))throw Error('main input not read with accepted wire format');");
            check(denonCommands.isEmpty(),"input probe mutated receiver");
            root.eval("js","bridge.dispatch("+operation("zone-off","receiver","receiver_power","OFF")+",report);");
            receiver.eval("js","executor.drain();");
            root.eval("js","bridge.events();if(!reports.some(e=>e.token==='zone-off'&&e.type==='terminal'&&e.admitted&&e.sample&&e.sample.fresh&&e.sample.retired&&e.sample.value==='OFF'))throw Error('zone off not confirmed by owned readback');");
            check(denonCommands.equals(List.of("ZMOFF"))&&zone2Power.get(),"main OFF affected independent zone2");
            xmlMode.set(1);
            root.eval("js","bridge.probe({token:String('ambiguous-xml'),key:'receiver',resource:'receiver_power'},report);");
            receiver.eval("js","executor.drain();");
            root.eval("js","bridge.events();if(!reports.some(e=>e.token==='ambiguous-xml'&&e.type==='terminal'&&e.reason==='owned_probe_failed'))throw Error('ambiguous XML accepted');");
            receiver.eval("js","hooks.forEach(h=>h());");
        }
        shared.clear();
        Files.delete(Path.of(args[2],"thermite-routing","executor_receiver.json"));
        xmlMode.set(2);
        try(Context root=context(shared,common,new Thing(url),source,descriptor);
            Context receiver=context(shared,common,new Thing(url),source,descriptor)) {
            root.eval("js","const bridge=module.exports.create(descriptor);function report(e){reports.push(e);}");
            receiver.eval("js",denon("{receiver_power:{kind:'denon',thing:'owned',zone:1,point:'power',allowed:['ON','OFF']}}"));
            root.eval("js","bridge.probe({token:String('entity-xml'),key:'receiver',resource:'receiver_power'},report);");
            receiver.eval("js","executor.drain();");
            root.eval("js","bridge.events();if(!reports.some(e=>e.token==='entity-xml'&&e.type==='terminal'&&e.reason==='owned_probe_failed'))throw Error('entity XML accepted');");
            receiver.eval("js","hooks.forEach(h=>h());");
        }
        // The real Sony provider's local auth guard cannot submit a command
        // without pairing. A refused power attempt must leave pairing usable.
        shared.clear();
        String sonySource=Files.readString(Path.of(args[3])).replace("// THERMITE_BOUNDED_IO",Files.readString(Path.of(args[1])))
            .replace("const DEVICES = null;","const DEVICES = [{id:'receiver',host:'127.0.0.1',speakerAudio:true}];")
            .replace("const PRESSES = null;","const PRESSES = {};");
        try(Context root=context(shared,common,new Thing(url),source,descriptor);
            Context receiver=context(shared,common,new Thing(url),source,descriptor)) {
            root.eval("js","const bridge=module.exports.create(descriptor);function report(e){reports.push(e);}");
            receiver.eval("js","const sonyProvider=(()=>{"+sonySource+";return routingProvider();})();const executor=module.exports.actor({key:'receiver',io:descriptor.io},sonyProvider,{noRules:true});");
            root.eval("js","bridge.dispatch("+operation("unpaired-power","receiver","receiver_power","ON")+",report);");
            receiver.eval("js","executor.drain();");
            root.eval("js","bridge.events();if(!reports.some(e=>e.token==='unpaired-power'&&e.type==='terminal'))throw Error('unpaired Sony refusal was not terminal');");
            receiver.eval("js","if(!executor.legacy('receiver_pairing','REQUEST',true))throw Error('unpaired Sony cannot start pairing');hooks.forEach(h=>h());");
        }
        } finally {
            release.countDown();workers.shutdownNow();server.stop(0);pool.shutdownNow();
            workers.awaitTermination(3,TimeUnit.SECONDS);pool.awaitTermination(3,TimeUnit.SECONDS);common.stop();
        }
        System.out.println("native broker isolation, actor concurrency, durable uncertainty and reload fencing passed");
    }
}
