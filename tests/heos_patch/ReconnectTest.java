package org.openhab.binding.heos.internal.api;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;

import org.openhab.binding.heos.internal.json.dto.HeosEventObject;
import org.openhab.binding.heos.internal.json.dto.HeosResponseObject;
import org.openhab.binding.heos.internal.json.payload.Media;
import org.openhab.binding.heos.internal.resources.HeosConstants;
import org.openhab.binding.heos.internal.resources.HeosEventListener;

/**
 * Runs the rebuilt production HeosSystem against two loopback fake receivers that never send events.
 *
 * The scheduler compresses every delay by SCALE, and the event stream's last-event time is backdated past
 * LAST_EVENT_THRESHOLD, so the quiet-house path runs in seconds. The listener re-establishes the connection
 * on CONNECTION_RESTORED exactly as HeosBridgeHandler.initialize/connectBridge does. A network guard
 * refuses every non-loopback connection. The unmodified 5.2.3 classes must FAIL this test.
 */
public class ReconnectTest {
    static final long SCALE = 200; // 30 s start delay -> 150 ms; 60 s heartbeat -> 300 ms; 15 s restore -> 75 ms
    static final int HEARTBEATS = 6; // heartbeat cycles that must follow the single reset
    static final long DEADLINE_MS = 20_000;
    static final List<String> failures = new ArrayList<>();
    static int checks;

    static void check(boolean value, String name) {
        checks++;
        if (!value) {
            failures.add(name);
        }
    }

    static boolean await(BooleanSupplier condition, long millis) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(millis);
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                return false;
            }
            Thread.sleep(20);
        }
        return true;
    }

    /** openHAB's thingHandler pool stand-in with every delay compressed. */
    static final class CompressedScheduler extends ScheduledThreadPoolExecutor {
        CompressedScheduler() {
            super(4, runnable -> {
                Thread thread = new Thread(runnable, "compressed-thingHandler");
                thread.setDaemon(true);
                return thread;
            });
        }

        static long scaled(long delay, TimeUnit unit) {
            return Math.max(0, unit.toMicros(delay) / SCALE);
        }

        @Override
        public ScheduledFuture<?> schedule(Runnable command, long delay, TimeUnit unit) {
            return super.schedule(command, scaled(delay, unit), TimeUnit.MICROSECONDS);
        }

        @Override
        public <V> ScheduledFuture<V> schedule(Callable<V> callable, long delay, TimeUnit unit) {
            return super.schedule(callable, scaled(delay, unit), TimeUnit.MICROSECONDS);
        }

        @Override
        public ScheduledFuture<?> scheduleWithFixedDelay(Runnable command, long initial, long delay, TimeUnit unit) {
            return super.scheduleWithFixedDelay(command, scaled(initial, unit), Math.max(1, scaled(delay, unit)),
                    TimeUnit.MICROSECONDS);
        }

        @Override
        public ScheduledFuture<?> scheduleAtFixedRate(Runnable command, long initial, long period, TimeUnit unit) {
            return super.scheduleAtFixedRate(command, scaled(initial, unit), Math.max(1, scaled(period, unit)),
                    TimeUnit.MICROSECONDS);
        }
    }

    /** The bridge handler's connection lifecycle, without the openHAB Thing framework. */
    static final class Receiver implements HeosEventListener {
        final String name;
        final FakeHeosServer server;
        final CompressedScheduler scheduler;
        final HeosSystem system;
        final AtomicInteger streamTimeouts = new AtomicInteger();
        final AtomicInteger connectionLost = new AtomicInteger();
        final AtomicInteger restored = new AtomicInteger();
        final AtomicInteger failedConnects = new AtomicInteger();
        volatile HeosFacade facade;

        Receiver(String name, CompressedScheduler scheduler) throws Exception {
            this.name = name;
            this.server = new FakeHeosServer(0);
            this.scheduler = scheduler;
            this.system = new HeosSystem(scheduler);
        }

        void connectBridge() throws Exception {
            HeosFacade connection = system.establishConnection("127.0.0.1", server.port(), 60);
            connection.registerForChangeEvents(this);
            facade = connection;
        }

        void delayedInitialize() {
            try {
                connectBridge();
            } catch (Exception e) {
                failedConnects.incrementAndGet();
                scheduler.schedule(this::delayedInitialize, 30, TimeUnit.SECONDS);
            }
        }

        @Override
        public void bridgeChangeEvent(String event, boolean success, Object command) {
            if (HeosConstants.EVENT_STREAM_TIMEOUT.equals(command)) {
                streamTimeouts.incrementAndGet();
            } else if (HeosConstants.CONNECTION_LOST.equals(command)) {
                connectionLost.incrementAndGet();
            } else if (HeosConstants.CONNECTION_RESTORED.equals(command)) {
                restored.incrementAndGet();
                scheduler.submit(this::delayedInitialize);
            }
        }

        @Override
        public void playerStateChangeEvent(HeosEventObject eventObject) {
        }

        @Override
        public void playerStateChangeEvent(HeosResponseObject<?> responseObject) {
        }

        @Override
        public void playerMediaChangeEvent(String pid, Media media) {
        }

        /** Pretend the event stream has been quiet for longer than LAST_EVENT_THRESHOLD. */
        void backdateLastEvent() throws Exception {
            Field threshold = HeosSystem.class.getDeclaredField("LAST_EVENT_THRESHOLD");
            threshold.setAccessible(true);
            Field controller = HeosSystem.class.getDeclaredField("eventController");
            controller.setAccessible(true);
            Field lastEventTime = HeosEventController.class.getDeclaredField("lastEventTime");
            lastEventTime.setAccessible(true);
            lastEventTime.setLong(controller.get(system),
                    System.currentTimeMillis() - threshold.getLong(null) - TimeUnit.MINUTES.toMillis(1));
        }

        String describe() {
            return name + ": resets=" + streamTimeouts.get() + " lost=" + connectionLost.get() + " restored="
                    + restored.get() + " failedConnects=" + failedConnects.get() + " " + server.status();
        }
    }

    static int readerThreads() {
        int count = 0;
        for (Map.Entry<Thread, StackTraceElement[]> entry : Thread.getAllStackTraces().entrySet()) {
            for (StackTraceElement frame : entry.getValue()) {
                if (frame.getClassName().equals("org.apache.commons.net.telnet.TelnetInputStream")
                        && frame.getMethodName().equals("run")) {
                    count++;
                    break;
                }
            }
        }
        return count;
    }

    static boolean loopback(String host) {
        return host.equals("127.0.0.1") || host.equals("localhost") || host.equals("::1")
                || host.equals("0:0:0:0:0:0:0:1");
    }

    @SuppressWarnings("removal")
    static void forbidRealNetwork() {
        System.setSecurityManager(new SecurityManager() {
            @Override
            public void checkPermission(java.security.Permission permission) {
            }

            @Override
            public void checkConnect(String host, int port) {
                if (!loopback(host)) {
                    throw new SecurityException("real network prohibited: " + host);
                }
            }

            @Override
            public void checkConnect(String host, int port, Object context) {
                checkConnect(host, port);
            }

            @Override
            public void checkAccept(String host, int port) {
                checkConnect(host, port);
            }

            @Override
            public void checkListen(int port) {
                if (port != 0) {
                    throw new SecurityException("only ephemeral loopback listeners");
                }
            }
        });
    }

    public static void main(String[] args) throws Exception {
        forbidRealNetwork();
        CompressedScheduler scheduler = new CompressedScheduler();
        List<Receiver> receivers = List.of(new Receiver("receiver-a", scheduler), new Receiver("receiver-b", scheduler));
        try {
            for (Receiver receiver : receivers) {
                receiver.connectBridge();
            }
            check(await(() -> receivers.stream().allMatch(r -> r.server.open() == 2 && r.server.registrations.get() == 1),
                    5_000), "each receiver holds one command and one event socket after connecting");
            check(await(() -> readerThreads() == 4, 5_000), "one Telnet reader thread per socket after connecting");

            for (Receiver receiver : receivers) {
                receiver.backdateLastEvent();
            }
            // Every receiver must reset its quiet event stream once, reconnect, and then keep answering heartbeats
            // on the new session. A reconnect storm never sends another heartbeat, so this waits until the deadline.
            boolean steady = await(() -> receivers.stream()
                    .allMatch(r -> r.streamTimeouts.get() >= 1 && r.server.heartbeatsSince(2) >= HEARTBEATS), DEADLINE_MS);
            check(steady, HEARTBEATS + " heartbeat cycles follow the quiet-stream reset on every receiver");
            // After a failed steady state, settle briefly rather than waiting out every remaining condition.
            long settle = steady ? 5_000 : 500;
            for (Receiver receiver : receivers) {
                String name = receiver.name;
                check(receiver.streamTimeouts.get() == 1, name + ": no repeated reconnects (exactly one quiet-stream reset)");
                check(receiver.connectionLost.get() == 0, name + ": heartbeats never reported the connection lost");
                check(receiver.server.accepted() == 4, name + ": exactly one reconnect (two sessions accepted in total)");
                check(await(() -> receiver.server.open() == 2, settle),
                        name + ": exactly one live session (command + event socket) per receiver");
                check(await(() -> receiver.server.closedBefore(2), settle), name + ": previous session sockets are closed");
            }
            check(await(() -> readerThreads() == 4, settle), "previous reader threads ended; one per live socket remains");

            for (Receiver receiver : receivers) {
                HeosFacade facade = receiver.facade;
                if (facade != null) {
                    facade.closeConnection();
                }
            }
            check(await(() -> receivers.stream().allMatch(r -> r.server.open() == 0), settle),
                    "closing the connection closes every socket");
            check(await(() -> readerThreads() == 0, settle), "closing the connection ends every reader thread");
        } finally {
            for (Receiver receiver : receivers) {
                System.out.println(receiver.describe() + " readers=" + readerThreads());
                receiver.server.close();
            }
            scheduler.shutdownNow();
        }
        if (!failures.isEmpty()) {
            for (String failure : failures) {
                System.out.println("FAIL " + failure);
            }
            System.out.println("FAILED " + failures.size() + " of " + checks + " reconnect assertions");
            System.exit(1);
        }
        System.out.println("PASS " + checks + " reconnect assertions; two loopback fake receivers; no real network");
        System.exit(0);
    }
}
