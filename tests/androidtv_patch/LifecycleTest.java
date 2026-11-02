package org.openhab.binding.androidtv.internal.protocol.googletv;

import java.io.*;
import java.lang.reflect.*;
import java.net.*;
import java.security.cert.Certificate;
import java.util.*;
import java.util.concurrent.*;
import javax.net.ssl.*;
import org.openhab.binding.androidtv.internal.AndroidTVHandler;
import org.slf4j.LoggerFactory;
import sun.misc.Unsafe;

/** Runs the rebuilt production manager with fake, non-network socket/handler boundaries. */
public class LifecycleTest {
    static final Unsafe UNSAFE;
    static int checks;
    static {
        try {
            Field field = Unsafe.class.getDeclaredField("theUnsafe");
            field.setAccessible(true);
            UNSAFE = (Unsafe) field.get(null);
        } catch (Exception e) { throw new ExceptionInInitializerError(e); }
    }
    static void check(boolean value, String name) {
        if (!value) throw new AssertionError(name);
        checks++;
    }
    static void set(Object target, String name, Object value) throws Exception {
        Field field = GoogleTVConnectionManager.class.getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }
    static Object get(Object target, String name) throws Exception {
        Field field = GoogleTVConnectionManager.class.getDeclaredField(name);
        field.setAccessible(true);
        return field.get(target);
    }
    static void call(Object target, String name, Class<?>[] types, Object... args) throws Exception {
        Method method = GoogleTVConnectionManager.class.getDeclaredMethod(name, types);
        method.setAccessible(true);
        method.invoke(target, args);
    }
    static class SocketStub extends SSLSocket {
        final String mode;
        final CountDownLatch entered = new CountDownLatch(1), closed = new CountDownLatch(1);
        final List<Integer> timeouts = new ArrayList<>();
        int connectTimeout;
        SocketStub(String mode) { this.mode = mode; }
        void block() throws IOException {
            entered.countDown();
            while (closed.getCount() != 0) {
                try { closed.await(); } catch (InterruptedException ignored) { }
            }
            throw new SocketException("closed synthetic socket");
        }
        public void connect(SocketAddress address, int timeout) throws IOException {
            connectTimeout = timeout;
            if (mode.equals("connect")) block();
            if (mode.equals("connect-timeout")) throw new SocketTimeoutException("synthetic timeout");
        }
        public void startHandshake() throws IOException {
            if (mode.equals("tls")) block();
            if (mode.equals("tls-timeout")) throw new SocketTimeoutException("synthetic timeout");
        }
        public void setSoTimeout(int timeout) { timeouts.add(timeout); }
        public synchronized void close() { closed.countDown(); }
        public boolean isClosed() { return closed.getCount() == 0; }
        public InputStream getInputStream() {
            return new InputStream() { public int read() throws IOException { block(); return -1; } };
        }
        public OutputStream getOutputStream() { return new ByteArrayOutputStream(); }
        public SSLSession getSession() {
            return (SSLSession) java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{SSLSession.class},
                (proxy, method, args) -> method.getName().equals("getPeerCertificates") ? new Certificate[0] : null);
        }
        public String[] getSupportedCipherSuites() { return new String[0]; }
        public String[] getEnabledCipherSuites() { return new String[0]; }
        public void setEnabledCipherSuites(String[] value) { }
        public String[] getSupportedProtocols() { return new String[0]; }
        public String[] getEnabledProtocols() { return new String[0]; }
        public void setEnabledProtocols(String[] value) { }
        public void addHandshakeCompletedListener(HandshakeCompletedListener value) { }
        public void removeHandshakeCompletedListener(HandshakeCompletedListener value) { }
        public void setUseClientMode(boolean value) { }
        public boolean getUseClientMode() { return true; }
        public void setNeedClientAuth(boolean value) { }
        public boolean getNeedClientAuth() { return false; }
        public void setWantClientAuth(boolean value) { }
        public boolean getWantClientAuth() { return false; }
        public void setEnableSessionCreation(boolean value) { }
        public boolean getEnableSessionCreation() { return true; }
    }
    static class Factory extends SSLSocketFactory {
        final List<SocketStub> sockets = new ArrayList<>();
        final String mode;
        Factory(String mode) { this.mode = mode; }
        public synchronized Socket createSocket() {
            SocketStub socket = new SocketStub(mode); sockets.add(socket); return socket;
        }
        public String[] getDefaultCipherSuites() { return new String[0]; }
        public String[] getSupportedCipherSuites() { return new String[0]; }
        public Socket createSocket(String host, int port) { throw new AssertionError("unbounded connect overload"); }
        public Socket createSocket(InetAddress host, int port) { throw new AssertionError("network prohibited"); }
        public Socket createSocket(String host, int port, InetAddress local, int localPort) { throw new AssertionError(); }
        public Socket createSocket(InetAddress host, int port, InetAddress local, int localPort) { throw new AssertionError(); }
        public Socket createSocket(Socket socket, String host, int port, boolean close) { throw new AssertionError(); }
    }
    static class ChildManager extends GoogleTVConnectionManager {
        GoogleTVConnectionManager next;
        boolean disposeDuringCreation;
        ChildManager() { super(null, null); } // Never invoked: allocateInstance avoids PKI/framework initialization.
        @Override GoogleTVConnectionManager newChild(GoogleTVConfiguration config) {
            if (disposeDuringCreation) dispose();
            return next;
        }
    }
    static class Scheduler extends ScheduledThreadPoolExecutor {
        Runnable last;
        long delayMillis;
        Scheduler() { super(1); setRemoveOnCancelPolicy(true); }
        @Override public ScheduledFuture<?> schedule(Runnable task, long delay, TimeUnit unit) {
            last = task;
            delayMillis = unit.toMillis(delay);
            return super.schedule(task, delay, unit);
        }
    }
    static <T extends GoogleTVConnectionManager> T manager(Class<T> type, Factory factory,
            ScheduledThreadPoolExecutor scheduler) throws Exception {
        T manager = (T) UNSAFE.allocateInstance(type);
        AndroidTVHandler handler = new AndroidTVHandler(); handler.scheduler = scheduler;
        GoogleTVConfiguration config = new GoogleTVConfiguration();
        config.ipAddress = "192.0.2.1"; config.mode = "NORMAL"; config.reconnect = 3600;
        set(manager, "logger", LoggerFactory.getLogger(LifecycleTest.class));
        set(manager, "handler", handler); set(manager, "config", config);
        set(manager, "translationProvider", handler.getTranslationProvider());
        set(manager, "scheduler", scheduler); set(manager, "sslSocketFactory", factory);
        set(manager, "sockets", new GoogleTVSocketLifecycle());
        set(manager, "connectionLock", new Object()); set(manager, "childLock", new Object());
        set(manager, "keepAliveReconnectLock", new Object());
        set(manager, "sendQueue", new LinkedBlockingQueue<>());
        set(manager, "shimQueue", new LinkedBlockingQueue<>());
        set(manager, "statusMessage", ""); set(manager, "isOnline", true);
        return manager;
    }
    static void blocked(String mode) throws Exception {
        Scheduler scheduler = new Scheduler();
        ExecutorService executor = Executors.newSingleThreadExecutor();
        Factory factory = new Factory(mode);
        GoogleTVConnectionManager manager = manager(GoogleTVConnectionManager.class, factory, scheduler);
        try {
            Future<?> task = executor.submit(manager::connect);
            long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
            while (factory.sockets.isEmpty() && System.nanoTime() < until) Thread.yield();
            SocketStub socket = factory.sockets.get(0);
            check(socket.entered.await(2, TimeUnit.SECONDS), mode + " reached blocking boundary");
            manager.connect();
            check(factory.sockets.size() == 1, mode + " exclusive attempt");
            long start = System.nanoTime(); manager.dispose();
            check(System.nanoTime() - start < TimeUnit.MILLISECONDS.toNanos(500), mode + " prompt dispose");
            task.get(2, TimeUnit.SECONDS);
            check(socket.isClosed(), mode + " closes in-progress socket");
            check(scheduler.getQueue().isEmpty(), mode + " no retry after dispose");
            manager.connect();
            check(factory.sockets.size() == 1, mode + " cannot resurrect disposed manager");
            check(socket.connectTimeout == 5000, mode + " bounded TCP");
            if (mode.equals("tls")) check(socket.timeouts.equals(List.of(10000)), "bounded TLS");
        } finally {
            manager.dispose(); executor.shutdownNow(); scheduler.shutdownNow();
            check(executor.awaitTermination(2, TimeUnit.SECONDS), "connect executor closed");
            check(scheduler.awaitTermination(2, TimeUnit.SECONDS), "scheduler closed");
        }
    }
    static void failuresAndSuccess() throws Exception {
        Scheduler scheduler = new Scheduler();
        try {
            for (String mode : List.of("connect-timeout", "tls-timeout")) {
                Factory factory = new Factory(mode);
                GoogleTVConnectionManager manager = manager(GoogleTVConnectionManager.class, factory, scheduler);
                for (int i = 0; i < 20; i++) manager.connect();
                check(factory.sockets.size() == 20, mode + " attempts finish");
                check(factory.sockets.stream().allMatch(SocketStub::isClosed), mode + " closes failed sockets");
                check(scheduler.getQueue().size() == 1, mode + " only one retry");
                Runnable stale = scheduler.last;
                manager.dispose();
                check(scheduler.getQueue().isEmpty(), mode + " retry canceled");
                stale.run(); // Simulate a callback already dequeued before cancellation.
                check(factory.sockets.size() == 20, mode + " dequeued retry fenced after dispose");
                call(manager, "scheduleConnectRetry", new Class<?>[]{long.class}, 1L);
                check(scheduler.getQueue().isEmpty(), mode + " late failure cannot retry");
            }
            Factory factory = new Factory("success");
            GoogleTVConnectionManager manager = manager(GoogleTVConnectionManager.class, factory, scheduler);
            manager.connect();
            check(factory.sockets.get(0).timeouts.equals(List.of(10000, 0)), "quiet established reader has no timeout");
            check(get(manager, "sslSocket") == factory.sockets.get(0), "publishes exact handshaken socket");
            manager.connect();
            check(factory.sockets.size() == 1, "established socket not duplicated");
            manager.dispose();
            Thread reader = (Thread) get(manager, "readerThread"), sender = (Thread) get(manager, "senderThread");
            reader.join(2000); sender.join(2000);
            check(!reader.isAlive() && !sender.isAlive(), "established reader/sender stopped");
        } finally { scheduler.shutdownNow(); check(scheduler.awaitTermination(2, TimeUnit.SECONDS), "retry scheduler closed"); }
    }
    static void childAndStale() throws Exception {
        ScheduledThreadPoolExecutor scheduler = new ScheduledThreadPoolExecutor(1);
        scheduler.setRemoveOnCancelPolicy(true);
        try {
            ChildManager parent = manager(ChildManager.class, new Factory("success"), scheduler);
            GoogleTVConnectionManager first = manager(GoogleTVConnectionManager.class, new Factory("success"), scheduler);
            GoogleTVConnectionManager second = manager(GoogleTVConnectionManager.class, new Factory("success"), scheduler);
            set(parent, "childConnectionManager", first); parent.next = second;
            call(parent, "startChildConnectionManager", new Class<?>[]{int.class, String.class}, 6467, "PIN");
            check((boolean) get(first, "disposing"), "replacing PIN child disposes predecessor");
            check(get(parent, "childConnectionManager") == second, "replacement is owned");
            parent.dispose();
            check((boolean) get(second, "disposing"), "parent dispose retires replacement");
            check(get(parent, "childConnectionManager") == null, "parent clears child reference");
            call(parent, "startChildConnectionManager", new Class<?>[]{int.class, String.class}, 6467, "PIN");
            check(get(parent, "childConnectionManager") == null, "late PIN callback cannot create child");
            ChildManager racing = manager(ChildManager.class, new Factory("success"), scheduler);
            GoogleTVConnectionManager replacement = manager(GoogleTVConnectionManager.class, new Factory("success"), scheduler);
            racing.next = replacement; racing.disposeDuringCreation = true;
            call(racing, "startChildConnectionManager", new Class<?>[]{int.class, String.class}, 6467, "PIN");
            check((boolean) get(replacement, "disposing"), "dispose during child creation closes replacement");
            check(get(racing, "childConnectionManager") == null, "racing child cannot escape ownership");
            GoogleTVSocketLifecycle lifecycle = new GoogleTVSocketLifecycle();
            Factory factory = new Factory("success");
            GoogleTVSocketLifecycle.Attempt old = lifecycle.begin(factory);
            lifecycle.disconnect();
            GoogleTVSocketLifecycle.Attempt fresh = lifecycle.begin(factory);
            check(!lifecycle.release(old), "stale completion has no authority");
            check(lifecycle.isCurrent(fresh) && !fresh.socket.isClosed(), "stale failure cannot close replacement");
            lifecycle.stop();
            check(fresh.socket.isClosed() && lifecycle.begin(factory) == null, "stop closes and fences future attempts");
        } finally { scheduler.shutdownNow(); check(scheduler.awaitTermination(2, TimeUnit.SECONDS), "child scheduler closed"); }
    }
    static void absoluteDeadline() throws Exception {
        Scheduler scheduler = new Scheduler(), independent = new Scheduler();
        ExecutorService executor = Executors.newSingleThreadExecutor();
        Factory factory = new Factory("tls");
        GoogleTVConnectionManager manager = manager(GoogleTVConnectionManager.class, factory, scheduler);
        set(manager, "sockets", new GoogleTVSocketLifecycle(independent));
        try {
            Future<?> task = executor.submit(manager::connect);
            long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
            while (factory.sockets.isEmpty() && System.nanoTime() < until) Thread.yield();
            SocketStub socket = factory.sockets.get(0);
            check(socket.entered.await(2, TimeUnit.SECONDS), "TLS reached independent deadline boundary");
            check(independent.delayMillis == 15000, "absolute deadline covers TCP plus TLS");
            independent.last.run(); // No thingHandler worker or wall-clock sleep needed to expire it.
            task.get(2, TimeUnit.SECONDS);
            check(socket.isClosed(), "absolute deadline closes even non-interruptible TLS");
            check(independent.getQueue().isEmpty(), "deadline queue purged on completion");
            check(scheduler.getQueue().size() == 1, "deadline failure schedules only one later retry");
        } finally {
            manager.dispose(); executor.shutdownNow(); scheduler.shutdownNow(); independent.shutdownNow();
            check(executor.awaitTermination(2, TimeUnit.SECONDS), "deadline connect worker closed");
            check(scheduler.awaitTermination(2, TimeUnit.SECONDS), "deadline retry worker closed");
            check(independent.awaitTermination(2, TimeUnit.SECONDS), "deadline executor closed");
        }
    }
    static void embeddedProvider() throws Exception {
        java.security.Provider provider = new org.bouncycastle.jce.provider.BouncyCastleProvider();
        javax.crypto.Cipher cipher = javax.crypto.Cipher.getInstance("AES/ECB/PKCS5Padding", provider);
        cipher.init(javax.crypto.Cipher.ENCRYPT_MODE, new javax.crypto.spec.SecretKeySpec(new byte[16], "AES"));
        check(cipher.doFinal(new byte[16]).length == 32, "embedded unsigned provider remains usable on Java21");
    }
    static void stalePinCompletion() throws Exception {
        Scheduler scheduler = new Scheduler();
        Factory factory = new Factory("success");
        GoogleTVConnectionManager parent = manager(GoogleTVConnectionManager.class, factory, scheduler);
        GoogleTVConnectionManager old = manager(GoogleTVConnectionManager.class, new Factory("success"), scheduler);
        GoogleTVConnectionManager current = manager(GoogleTVConnectionManager.class, new Factory("success"), scheduler);
        set(parent, "childConnectionManager", current);
        set(old, "connectionManager", parent); set(current, "connectionManager", parent);
        ((GoogleTVConfiguration) get(old, "config")).mode = "PIN";
        ((GoogleTVConfiguration) get(current, "config")).mode = "PIN";
        try {
            set(old, "disposing", true);
            old.finishPinProcess();
            check(get(parent, "childConnectionManager") == current && !(boolean) get(current, "disposing"),
                    "disposed PIN child cannot complete a newer child's handshake");
            set(old, "disposing", false);
            old.finishPinProcess();
            check(get(parent, "childConnectionManager") == current && !(boolean) get(current, "disposing"),
                    "replaced PIN child has no completion authority even before its dispose flag is visible");
            check(factory.sockets.isEmpty(), "stale PIN completion cannot reconnect parent");
            current.finishPinProcess();
            check(get(parent, "childConnectionManager") == null && (boolean) get(current, "disposing"),
                    "current PIN child is retired on success");
            check(factory.sockets.size() == 1, "current successful PIN child reconnects parent once");
            current.finishPinProcess();
            check(factory.sockets.size() == 1, "duplicate successful child callback ignored");
        } finally {
            parent.dispose(); old.dispose(); current.dispose();
            for (String name : List.of("readerThread", "senderThread")) {
                Thread thread = (Thread) get(parent, name);
                if (thread != null) { thread.join(2000); check(!thread.isAlive(), "PIN parent thread closed"); }
            }
            scheduler.shutdownNow(); check(scheduler.awaitTermination(2, TimeUnit.SECONDS), "PIN scheduler closed");
        }
    }
    public static void main(String[] args) throws Exception {
        System.setSecurityManager(new SecurityManager() {
            public void checkPermission(java.security.Permission permission) { }
            public void checkConnect(String host, int port) { throw new AssertionError("real network prohibited"); }
            public void checkListen(int port) { throw new AssertionError("listeners prohibited"); }
            public void checkAccept(String host, int port) { throw new AssertionError("accept prohibited"); }
        });
        blocked("connect"); blocked("tls"); failuresAndSuccess(); childAndStale(); absoluteDeadline(); embeddedProvider();
        stalePinCompletion();
        System.out.println("PASS " + checks + " lifecycle assertions; fake sockets only; no native runtime");
    }
}
