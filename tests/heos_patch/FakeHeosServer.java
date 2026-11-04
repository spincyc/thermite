package org.openhab.binding.heos.internal.api;

import java.io.BufferedReader;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * A loopback HEOS CLI stand-in: it answers every request with success and never sends a change event,
 * like a receiver in a quiet house. It records each accepted connection and whether its peer closed it.
 * Run standalone as {@code FakeHeosServer PORT} for a native-runtime proof; it prints a status line
 * every five seconds.
 */
public final class FakeHeosServer implements Closeable {
    final ServerSocket server;
    final List<Connection> connections = new CopyOnWriteArrayList<>();
    final AtomicInteger heartbeats = new AtomicInteger();
    final AtomicInteger registrations = new AtomicInteger();

    final class Connection {
        final Socket socket;
        final int index;
        volatile boolean closed;
        final AtomicInteger heartbeats = new AtomicInteger();

        Connection(Socket socket, int index) {
            this.socket = socket;
            this.index = index;
        }

        void serve() {
            try (socket; BufferedReader in = new BufferedReader(
                    new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8))) {
                OutputStream out = socket.getOutputStream();
                for (String line; (line = in.readLine()) != null;) {
                    String request = line.trim();
                    if (request.isEmpty()) {
                        continue;
                    }
                    String body = request.startsWith("heos://") ? request.substring(7) : request;
                    int query = body.indexOf('?');
                    String command = query < 0 ? body : body.substring(0, query);
                    String message = query < 0 ? "" : body.substring(query + 1);
                    if (command.equals("system/heart_beat")) {
                        heartbeats.incrementAndGet();
                        FakeHeosServer.this.heartbeats.incrementAndGet();
                    } else if (command.equals("system/register_for_change_events") && message.equals("enable=on")) {
                        registrations.incrementAndGet();
                    }
                    String payload = command.equals("player/get_players") || command.equals("group/get_groups")
                            ? ", \"payload\": []"
                            : "";
                    String response = "{\"heos\": {\"command\": \"" + json(command)
                            + "\", \"result\": \"success\", \"message\": \"" + json(message) + "\"}" + payload + "}\r\n";
                    out.write(response.getBytes(StandardCharsets.UTF_8));
                    out.flush();
                }
            } catch (IOException ignored) {
                // A reset by the client is a close as well.
            } finally {
                closed = true;
            }
        }
    }

    public FakeHeosServer(int port) throws IOException {
        server = new ServerSocket(port, 512, InetAddress.getLoopbackAddress());
        Thread acceptor = new Thread(this::accept, "fake-heos-accept-" + server.getLocalPort());
        acceptor.setDaemon(true);
        acceptor.start();
    }

    private void accept() {
        try {
            while (!server.isClosed()) {
                Connection connection = new Connection(server.accept(), connections.size());
                connections.add(connection);
                Thread handler = new Thread(connection::serve, "fake-heos-conn-" + connection.index);
                handler.setDaemon(true);
                handler.start();
            }
        } catch (IOException ignored) {
            // Closed.
        }
    }

    static String json(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    int port() {
        return server.getLocalPort();
    }

    int accepted() {
        return connections.size();
    }

    int open() {
        return (int) connections.stream().filter(c -> !c.closed).count();
    }

    /** Heartbeats received on connections accepted at or after {@code firstIndex}. */
    int heartbeatsSince(int firstIndex) {
        return connections.stream().filter(c -> c.index >= firstIndex).mapToInt(c -> c.heartbeats.get()).sum();
    }

    boolean closedBefore(int firstIndex) {
        return connections.stream().filter(c -> c.index < firstIndex).allMatch(c -> c.closed);
    }

    String status() {
        return "accepted=" + accepted() + " open=" + open() + " heartbeats=" + heartbeats.get() + " registrations="
                + registrations.get();
    }

    @Override
    public void close() throws IOException {
        server.close();
        for (Connection connection : connections) {
            connection.socket.close();
        }
    }

    public static void main(String[] args) throws Exception {
        try (FakeHeosServer server = new FakeHeosServer(Integer.parseInt(args[0]))) {
            System.out.println("listening 127.0.0.1:" + server.port());
            while (true) {
                Thread.sleep(5000);
                System.out.println(System.currentTimeMillis() / 1000 + " " + server.status());
            }
        }
    }
}
