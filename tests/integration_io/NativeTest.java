// Executes the production embedded helper on the pinned JVM and GraalJS.
// Every network endpoint is an owned loopback fault fixture.
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import com.sun.net.httpserver.HttpServer;
import org.eclipse.jetty.client.HttpClient;
import org.eclipse.jetty.client.http.HttpClientTransportOverHTTP;
import org.eclipse.jetty.util.component.LifeCycle;
import org.graalvm.polyglot.Context;
import org.graalvm.polyglot.PolyglotException;

public class NativeTest {
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    private static Context context(HttpClient common, String source) {
        Context context = Context.newBuilder("js").allowAllAccess(true).option("engine.WarnInterpreterOnly", "false").build();
        context.getBindings("js").putMember("common", common);
        context.eval("js", "const hooks=[]; function require(name) { if(name==='openhab') return {osgi:{getService:()=>({getCommonHttpClient:()=>common})}}; if(name==='@runtime') return {lifecycleTracker:{addDisposeHook:h=>hooks.push(h)}}; throw Error(name); }");
        context.eval("js", source);
        return context;
    }
    private static void failure(Context context, String operation, long maxMs) {
        long start = System.nanoTime();
        try { context.eval("js", operation); throw new AssertionError("fault accepted"); }
        catch (PolyglotException expected) { /* classification/payload stays private */ }
        long elapsed = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
        check(elapsed < maxMs, "fault exceeded absolute bound: " + elapsed);
    }
    private static void http(Context context, HttpClient common, String source) throws Exception {
        var executor = Executors.newCachedThreadPool();
        var cookies = Collections.synchronizedList(new ArrayList<String>());
        var counts = Collections.synchronizedMap(new HashMap<String,Integer>());
        CountDownLatch stalled = new CountDownLatch(1);
        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        server.setExecutor(executor);
        server.createContext("/", exchange -> {
            try {
                cookies.add(Objects.toString(exchange.getRequestHeaders().getFirst("Cookie"), ""));
                exchange.getRequestBody().readAllBytes();
                String route = exchange.getRequestURI().getPath();
                counts.merge(route, 1, Integer::sum);
                exchange.getResponseHeaders().add("Set-Cookie", "auth=fixture-cookie; Path=/");
                if (route.equals("/stall") || route.equals("/parallel")) {
                    exchange.sendResponseHeaders(200, 1000);
                    exchange.getResponseBody().write('x'); exchange.getResponseBody().flush();
                    if (route.equals("/parallel")) stalled.countDown();
                    Thread.sleep(2000);
                } else {
                    byte[] body = (route.equals("/large") ? "x".repeat(70000) : "ok").getBytes(StandardCharsets.UTF_8);
                    exchange.sendResponseHeaders(route.equals("/pin") ? 401 : 200, route.equals("/large") ? 0 : body.length);
                    exchange.getResponseBody().write(body);
                }
            } catch (Exception ignored) { /* cancellation closes expected fault fixtures */ }
            finally { exchange.close(); }
        });
        server.start();
        String base = "http://127.0.0.1:" + server.getAddress().getPort();
        try {
            context.getBindings("js").putMember("base", base);
            context.eval("js", "if(thermiteIO.http(base+'/ok','GET',null,{},2000,500).getStatus()!==200) throw Error('status');");
            context.eval("js", "thermiteIO.http(base+'/ok','GET',null,{},2000,500);");
            context.eval("js", "thermiteIO.http(base+'/ok','GET',null,{Cookie:'auth=explicit-owner'},2000,500);");
            check(cookies.subList(0,3).equals(List.of("", "", "auth=explicit-owner")), "cookie authority changed");
            // Sony uses a raw 401 without WWW-Authenticate to start its PIN
            // protocol. It must reach the integration without Jetty retries.
            context.eval("js", "if(thermiteIO.http(base+'/pin','POST','{}',{},2000,500).getStatus()!==401) throw Error('PIN challenge lost');");
            check(counts.get("/pin")==1, "PIN challenge was automatically retried");
            failure(context, "thermiteIO.http(base+'/stall','POST','{}',{},150,500)", 1500);
            check(counts.get("/stall")==1, "timed-out command was replayed");
            failure(context, "thermiteIO.http(base+'/large','GET',null,{},1000,500)", 1500);
            context.eval("js", "if(thermiteIO.http(base+'/ok','GET',null,{},2000,500).getContentAsString()!=='ok') throw Error('recovery');");
            try (Context slow=context(common,source); Context fast=context(common,source)) {
                for (Context device:List.of(slow,fast)) {
                    device.getBindings("js").putMember("base",base);
                    device.eval("js", "thermiteIO.http(base+'/ok','GET',null,{},2000,500)");
                }
                var dispatch=Executors.newSingleThreadExecutor();
                try {
                    Future<?> pending=dispatch.submit(() -> failure(slow,"thermiteIO.http(base+'/parallel','GET',null,{},1200,500)",2000));
                    check(stalled.await(2,TimeUnit.SECONDS), "slow device never entered its request");
                    fast.eval("js", "thermiteIO.http(base+'/ok','GET',null,{},2000,500)");
                    check(!pending.isDone(), "fast device waited for slow context");
                    pending.get(2500,TimeUnit.MILLISECONDS);
                } finally { dispatch.shutdownNow(); }
                slow.eval("js", "hooks.forEach(h=>h());");
                fast.eval("js", "hooks.forEach(h=>h());");
            }
            context.eval("js", "hooks.forEach(h=>h());");
            check(common.isStarted(), "owned client stopped common client");
            check(((LifeCycle)common.getExecutor()).isStarted(), "owned client stopped shared executor");
            check(common.getScheduler().isStarted(), "owned client stopped shared scheduler");
            check(common.GET(base+"/ok").getStatus()==200, "common client unusable after disposal");
        } finally { server.stop(0); executor.shutdownNow(); executor.awaitTermination(3,TimeUnit.SECONDS); }
    }
    private static void socket(Context context, String mode) throws Exception {
        try (ServerSocket server = new ServerSocket(0, 1, InetAddress.getLoopbackAddress())) {
            var peers = Collections.synchronizedList(new ArrayList<Socket>());
            Thread fixture = new Thread(() -> {
                try (Socket peer=server.accept()) {
                    peers.add(peer);
                    OutputStream output=peer.getOutputStream();
                    if (mode.equals("trickle")) {
                        for (int i=0;i<100;i++) { output.write('x'); output.flush(); Thread.sleep(15); }
                    } else if (mode.equals("large")) {
                        output.write(("x".repeat(4097)+"\r\n").getBytes(StandardCharsets.US_ASCII)); output.flush();
                    } else if (mode.equals("session")) {
                        output.write(("x".repeat(100)+"\r\n").repeat(800).getBytes(StandardCharsets.US_ASCII)); output.flush();
                    } else {
                        output.write("NOKEY\r\n".getBytes(StandardCharsets.US_ASCII)); output.flush();
                        BufferedReader reader=new BufferedReader(new InputStreamReader(peer.getInputStream(), StandardCharsets.US_ASCII));
                        check(reader.readLine().equals("power_status ?"), "write changed framing");
                        output.write("\"on\"\r\n".getBytes(StandardCharsets.US_ASCII)); output.flush();
                    }
                } catch (Exception ignored) { /* expected absolute timeout and resource shutdown */ }
            });
            fixture.setDaemon(true); fixture.start();
            String start = "const link=thermiteIO.open('127.0.0.1'," +server.getLocalPort()+",500,thermiteIO.deadline("+(mode.equals("session")?2000:250)+"));";
            try {
                if (mode.equals("ok")) {
                    context.eval("js", "{ "+start+" try { if(thermiteIO.line(link,150)!=='NOKEY') throw Error('greeting'); let quiet=false; try { thermiteIO.line(link,30); } catch(e) { quiet=true; } if(!quiet||link.closed) throw Error('quiet drain closed connection'); thermiteIO.write(link,'power_status ?\\r\\n'); if(thermiteIO.line(link,150)!=='\"on\"') throw Error('answer'); } finally { thermiteIO.close(link); } }");
                } else if (mode.equals("session")) {
                    var result=context.eval("js", "(()=>{ "+start+" try { for(let n=0;n<800;n++) thermiteIO.line(link,500); return ['',link.closed]; } catch(error) { return [String(error.message),link.closed]; } finally { thermiteIO.close(link); } })()");
                    check(result.getArrayElement(0).asString().equals("device response exceeds limit"), "short-line stream did not reach bounded session limit");
                    check(result.getArrayElement(1).asBoolean(), "fatal session limit left connection usable");
                } else {
                    failure(context, "{ "+start+" try { thermiteIO.line(link,250); } finally { thermiteIO.close(link); } }", 1500);
                }
            } finally {
                for(Socket peer:peers) peer.close();
                server.close(); fixture.join(1500);
                check(!fixture.isAlive(), "fixture did not shut down");
            }
        }
    }
    public static void main(String[] args) throws Exception {
        HttpClient common=new HttpClient(new HttpClientTransportOverHTTP(1));
        common.start();
        String source=Files.readString(Path.of(args[0]));
        try (Context context=context(common,source)) {
            try {
                // Warm the host interop before imposing tight operation budgets.
                context.eval("js", "thermiteIO.remaining(thermiteIO.deadline(1000));");
                http(context,common,source);
                socket(context,"ok"); socket(context,"trickle"); socket(context,"large"); socket(context,"session");
                System.out.println("bounded HTTP/body/frame deadlines, cookie ownership and disposal passed");
            } finally { context.eval("js", "hooks.forEach(h=>h());"); }
        } finally { common.stop(); }
    }
}
