/*
 * Copyright (c) 2026 Contributors to the thermite project
 * SPDX-License-Identifier: EPL-2.0
 */
package org.openhab.binding.androidtv.internal.protocol.googletv;

import java.io.IOException;
import java.net.SocketAddress;
import java.net.SocketTimeoutException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/** Owns an attempt before any network I/O; cancellation never waits for its handshake lock. */
final class GoogleTVSocketLifecycle {
    static final int CONNECT_TIMEOUT_MILLIS = 5000;
    static final int HANDSHAKE_TIMEOUT_MILLIS = 10000;
    static final int TOTAL_TIMEOUT_MILLIS = 15000;
    // Independent of thingHandler: a saturated binding scheduler cannot delay
    // cancellation. No queued canceled tasks or permanent classloader-owning thread.
    private static final ScheduledThreadPoolExecutor DEADLINES = deadlines();
    private final ScheduledExecutorService deadlines;

    private static ScheduledThreadPoolExecutor deadlines() {
        ScheduledThreadPoolExecutor executor = new ScheduledThreadPoolExecutor(1, task -> {
            Thread thread = new Thread(task, "OH-androidtv-connect-deadline");
            thread.setDaemon(true);
            thread.setContextClassLoader(null);
            return thread;
        });
        executor.setRemoveOnCancelPolicy(true);
        executor.setKeepAliveTime(1, TimeUnit.SECONDS);
        executor.allowCoreThreadTimeOut(true);
        return executor;
    }

    GoogleTVSocketLifecycle() {
        this(DEADLINES);
    }

    GoogleTVSocketLifecycle(ScheduledExecutorService deadlines) {
        this.deadlines = deadlines;
    }

    static final class Attempt {
        final SSLSocket socket;
        final AtomicInteger deadlineState = new AtomicInteger(); // 0 opening; 1 finished; 2 expired
        volatile ScheduledFuture<?> deadline;

        Attempt(SSLSocket socket) {
            this.socket = socket;
        }
    }

    private Attempt current;
    private boolean stopped;

    synchronized Attempt begin(SSLSocketFactory factory) throws IOException {
        if (stopped || current != null) {
            return null;
        }
        // The no-argument factory method creates an UNCONNECTED socket.
        current = new Attempt((SSLSocket) factory.createSocket());
        return current;
    }

    boolean open(Attempt attempt, SocketAddress address) throws IOException {
        if (!isCurrent(attempt) || attempt.deadlineState.get() != 0) {
            return false;
        }
        ScheduledFuture<?> timeout = deadlines.schedule(() -> {
            if (attempt.deadlineState.compareAndSet(0, 2)) {
                close(attempt);
            }
        }, TOTAL_TIMEOUT_MILLIS, TimeUnit.MILLISECONDS);
        attempt.deadline = timeout;
        try {
            if (attempt.deadlineState.get() != 0) {
                if (attempt.deadlineState.get() == 2) {
                    throw new SocketTimeoutException("Google TV connection deadline expired");
                }
                return false;
            }
            attempt.socket.connect(address, CONNECT_TIMEOUT_MILLIS);
            attempt.socket.setSoTimeout(HANDSHAKE_TIMEOUT_MILLIS);
            attempt.socket.startHandshake();
            // Established quiet devices retain the binding's existing reader behavior.
            attempt.socket.setSoTimeout(0);
            if (!attempt.deadlineState.compareAndSet(0, 1) && attempt.deadlineState.get() == 2) {
                throw new SocketTimeoutException("Google TV connection deadline expired");
            }
            return isCurrent(attempt);
        } finally {
            attempt.deadlineState.compareAndSet(0, 1);
            timeout.cancel(false);
        }
    }

    synchronized boolean isCurrent(Attempt attempt) {
        return !stopped && current == attempt && attempt.deadlineState.get() != 2;
    }

    boolean release(Attempt attempt) {
        boolean owned;
        synchronized (this) {
            owned = !stopped && current == attempt;
            if (current == attempt) {
                current = null;
            }
        }
        close(attempt);
        return owned;
    }

    void disconnect() {
        Attempt previous;
        synchronized (this) {
            previous = current;
            current = null;
        }
        close(previous);
    }

    void stop() {
        synchronized (this) {
            stopped = true;
        }
        disconnect();
    }

    private static void close(Attempt attempt) {
        if (attempt != null) {
            attempt.deadlineState.compareAndSet(0, 1);
            ScheduledFuture<?> deadline = attempt.deadline;
            if (deadline != null) {
                deadline.cancel(false);
            }
            try {
                attempt.socket.close();
            } catch (IOException ignored) {
                // The attempt has already been detached, including on close failure.
            }
        }
    }
}
