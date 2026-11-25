// Native I/O only: no callback runs JavaScript on an I/O/deadline thread.
// The renderer embeds a copy in each device script; no shared Graal objects.
const thermiteIO = (() => {
  const System = Java.type('java.lang.System');
  const TimeUnit = Java.type('java.util.concurrent.TimeUnit');
  const MAX_BODY = 65536;
  const MAX_LINE = 4096;
  const MAX_SESSION = 65536;
  const SHARED_HTTP_KEY = 'thermite.io.http.v1';
  const SHARED_SELECTORS = 2;
  const SHARED_MAX_CONNECTIONS = 64;
  const SHARED_MAX_QUEUED = 128;
  const SHARED_CONNECT_TIMEOUT = 2000;
  let client = null;

  function deadline(ms) { return Number(System.nanoTime()) + ms * 1000000; }
  function remaining(end) {
    const ms = Math.ceil((end - Number(System.nanoTime())) / 1000000);
    if (ms <= 0) throw new (Java.type('java.net.SocketTimeoutException'))('device operation deadline exceeded');
    return ms;
  }

  function sharedStore() {
    try {
      const cache = require('@runtime/cache').sharedCache;
      if (cache && typeof cache.get === 'function') {
        const MapClass = Java.type('java.util.concurrent.ConcurrentHashMap');
        return cache.get(SHARED_HTTP_KEY, () => new MapClass());
      }
    } catch (_) { /* standalone harness without @runtime/cache */ }
    try {
      if (typeof System.getProperties === 'function') {
        const props = System.getProperties();
        const MapClass = Java.type('java.util.concurrent.ConcurrentHashMap');
        let store = props.get(SHARED_HTTP_KEY);
        if (!store) {
          const fresh = new MapClass();
          store = props.putIfAbsent(SHARED_HTTP_KEY, fresh) || fresh;
        }
        return store;
      }
    } catch (_) { /* minimal JS mock without System.getProperties */ }
    return null;
  }

  function startClient(common, selectors, maxConnections, maxQueued, connectTimeout) {
    const HttpClient = Java.type('org.eclipse.jetty.client.HttpClient');
    const Transport = Java.type('org.eclipse.jetty.client.http.HttpClientTransportOverHTTP');
    const EmptyCookies = Java.type('org.eclipse.jetty.util.HttpCookieStore$Empty');
    // Own only our connections/selectors. Reuse already-started native workers
    // and scheduler, without changing or stopping the common client.
    const owned = new HttpClient(new Transport(selectors));
    owned.setExecutor(common.getExecutor());
    owned.setScheduler(common.getScheduler());
    owned.setByteBufferPool(common.getByteBufferPool());
    owned.setCookieStore(new EmptyCookies());
    owned.setFollowRedirects(false);
    owned.setConnectTimeout(connectTimeout);
    owned.setMaxConnectionsPerDestination(maxConnections);
    owned.setMaxRequestsQueuedPerDestination(maxQueued);
    try { owned.start(); } catch (error) { try { owned.stop(); } catch (_) {} throw error; }
    // Sony's PIN API uses a bare 401 as a normal protocol response. The
    // integration alone owns Basic/Cookie authentication and retries; Jetty
    // must neither reject that challenge nor handle it automatically.
    owned.getProtocolHandlers().remove('www-authenticate');
    return owned;
  }

  function acquireClient(connectTimeout) {
    const common = require('openhab').osgi.getService('org.openhab.core.io.net.http.HttpClientFactory').getCommonHttpClient();
    const store = sharedStore();
    if (store === null) {
      const owned = startClient(common, 1, 1, 1, connectTimeout);
      client = owned;
      require('@runtime').lifecycleTracker.addDisposeHook(() => { client = null; owned.stop(); });
      return owned;
    }
    const Lock = Java.type('java.util.concurrent.locks.ReentrantLock');
    const AtomicInteger = Java.type('java.util.concurrent.atomic.AtomicInteger');
    store.putIfAbsent('lock', new Lock());
    store.putIfAbsent('refs', new AtomicInteger(0));
    const lock = store.get('lock');
    const refs = store.get('refs');
    let owned;
    lock.lock();
    try {
      const existing = store.get('client');
      const reusable = existing && store.get('common') === common
        && (typeof existing.isStarted !== 'function' || existing.isStarted());
      if (reusable) {
        owned = existing;
      } else {
        owned = startClient(common, SHARED_SELECTORS, SHARED_MAX_CONNECTIONS, SHARED_MAX_QUEUED, SHARED_CONNECT_TIMEOUT);
        store.put('client', owned);
        store.put('common', common);
        refs.set(0);
      }
      refs.incrementAndGet();
      client = owned;
    } finally {
      lock.unlock();
    }
    let disposed = false;
    require('@runtime').lifecycleTracker.addDisposeHook(() => {
      if (disposed) return;
      disposed = true;
      client = null;
      lock.lock();
      try {
        if (store.get('client') === owned && refs.decrementAndGet() <= 0) {
          store.remove('client');
          store.remove('common');
          try { owned.stop(); } catch (_) {}
        }
      } finally {
        lock.unlock();
      }
    });
    return owned;
  }

  function http(url, method, body, headers, timeout, connectTimeout) {
    const end = deadline(timeout);
    if (client === null) acquireClient(connectTimeout);
    const Listener = Java.type('org.eclipse.jetty.client.util.FutureResponseListener');
    const Content = Java.type('org.eclipse.jetty.client.util.StringContentProvider');
    const request = client.newRequest(url).method(method).timeout(remaining(end), TimeUnit.MILLISECONDS);
    for (const [name, value] of Object.entries(headers)) request.header(name, value);
    if (body !== null) request.content(new Content(body));
    const listener = new Listener(request, MAX_BODY);
    try {
      request.send(listener);
      // Unlike JDK21 HttpRequest.timeout, this waits for the complete bounded
      // body. Cancellation aborts the native exchange on any failure.
      return listener.get(remaining(end), TimeUnit.MILLISECONDS);
    } catch (error) {
      listener.cancel(true);
      throw error;
    }
  }

  // Whether a failed http() exchange provably sent no request byte: the TCP
  // connection was refused, unroutable or timed out while connecting. Pinned
  // Jetty 9.4 reports a connect timeout as SocketTimeoutException "Connect
  // Timeout" (or "Concurrent Connect Timeout"); a total, idle or read timeout
  // is a TimeoutException and may follow a written request.
  function unreached(error) {
    const Throwable = Java.type('java.lang.Throwable');
    const Refused = Java.type('java.net.ConnectException');
    const NoRoute = Java.type('java.net.NoRouteToHostException');
    const Timeout = Java.type('java.net.SocketTimeoutException');
    let cause = error;
    for (let depth = 0; depth < 8 && cause instanceof Throwable; ++depth) {
      if (cause instanceof Refused || cause instanceof NoRoute) return true;
      if (cause instanceof Timeout && /^(?:Concurrent )?Connect Timeout$/.test(String(cause.getMessage()))) return true;
      const next = cause.getCause();
      if (next === cause) break;
      cause = next;
    }
    return false;
  }

  function close(link) {
    if (link && !link.closed) {
      link.closed = true;
      try { link.channel.close(); } finally { link.selector.close(); }
    }
  }

  function wait(link, operation, end) {
    if (Java.type('java.lang.Thread').currentThread().isInterrupted()) throw new Error('device operation interrupted');
    link.channel.register(link.selector, operation);
    link.selector.selectedKeys().clear();
    // select(0) would wait indefinitely. remaining() always returns >=1.
    link.selector.select(remaining(Math.min(end, link.deadline)));
  }

  function open(host, port, connectTimeout, end) {
    const Channel = Java.type('java.nio.channels.SocketChannel');
    const Selector = Java.type('java.nio.channels.Selector');
    const Address = Java.type('java.net.InetSocketAddress');
    const InetAddress = Java.type('java.net.InetAddress');
    const parts = host.split('.');
    if (parts.length !== 4 || !parts.every(p => /^\d+$/.test(p) && Number(p) <= 255)) throw new Error('device needs an IPv4 address');
    // Inventory validates IPv4; construct it without an unbounded DNS lookup.
    const address = InetAddress.getByAddress(Java.to(parts.map(p => Number(p) > 127 ? Number(p) - 256 : Number(p)), 'byte[]'));
    const channel = Channel.open();
    let selector;
    try { selector = Selector.open(); } catch (error) { channel.close(); throw error; }
    const link = { channel, selector, deadline: end, closed: false, buffer: '', bytes: 0 };
    try {
      channel.configureBlocking(false);
      const connectEnd = Math.min(end, deadline(connectTimeout));
      if (!channel.connect(new Address(address, port))) {
        while (!channel.finishConnect()) wait(link, 8, connectEnd); // SelectionKey.OP_CONNECT
      }
      return link;
    } catch (error) { close(link); throw error; }
  }

  function decode(buffer, count) {
    if (typeof buffer.hasArray === 'function' && buffer.hasArray()) {
      const JString = Java.type('java.lang.String');
      const Charset = Java.type('java.nio.charset.StandardCharsets');
      return String(new JString(buffer.array(), Number(buffer.arrayOffset()), count, Charset.UTF_8));
    }
    buffer.flip();
    const chars = [];
    while (buffer.hasRemaining()) chars.push(Number(buffer.get()) & 255);
    return String.fromCharCode(...chars);
  }

  // Non-blocking poll for the next complete line already readable on `link`.
  // Returns the line, or null when idle (count === 0) or peer closed
  // (link.closed === true), never throwing SocketTimeoutException when idle.
  // Without `keep`, an incomplete partial line left behind on idle closes `link`.
  function pollLine(link, keep = false) {
    const ByteBuffer = Java.type('java.nio.ByteBuffer');
    try {
      while (!link.closed) {
        if (Java.type('java.lang.Thread').currentThread().isInterrupted()) throw new Error('device operation interrupted');
        if (Number(System.nanoTime()) >= link.deadline) { close(link); return null; }
        const match = /[\r\n]/.exec(link.buffer);
        if (match) {
          if (match.index > MAX_LINE) throw new Error('device line exceeds limit');
          const result = link.buffer.slice(0, match.index);
          link.buffer = link.buffer.slice(match.index + 1);
          if (result.length) return result;
          continue; // CRLF may straddle native reads; ignore its empty half.
        }
        if (link.buffer.length > MAX_LINE) throw new Error('device line exceeds limit');
        const buffer = ByteBuffer.allocate(Math.min(1024, MAX_LINE + 1 - link.buffer.length));
        const count = Number(link.channel.read(buffer));
        if (count < 0) { close(link); return null; }
        if (count === 0) {
          if (!keep && link.buffer.length > 0) close(link);
          return null;
        }
        link.bytes += count;
        if (link.bytes > MAX_SESSION) throw new Error('device response exceeds limit');
        link.buffer += decode(buffer, count);
      }
      return null;
    } catch (error) {
      close(link);
      throw error;
    }
  }

  // The next complete line within `timeout` ms. A timeout closes the
  // connection unless it was a quiet one (quietTimeout); with `keep`, any
  // timeout before the connection's own deadline keeps it and the bytes of
  // a line not yet complete, for a later call (waitedOut).
  function line(link, timeout, keep = false) {
    if (keep && timeout <= 1) return pollLine(link, true);
    const ByteBuffer = Java.type('java.nio.ByteBuffer');
    const end = Math.min(link.deadline, deadline(timeout));
    try { while (!link.closed) {
      remaining(end);
      const match = /[\r\n]/.exec(link.buffer);
      if (match) {
        if (match.index > MAX_LINE) throw new Error('device line exceeds limit');
        const result = link.buffer.slice(0, match.index);
        link.buffer = link.buffer.slice(match.index + 1);
        if (result.length) return result;
        continue; // CRLF may straddle native reads; ignore its empty half.
      }
      if (link.buffer.length > MAX_LINE) throw new Error('device line exceeds limit');
      const buffer = ByteBuffer.allocate(Math.min(1024, MAX_LINE + 1 - link.buffer.length));
      const count = Number(link.channel.read(buffer));
      if (count < 0) { close(link); return null; }
      if (count === 0) { wait(link, 1, end); continue; } // SelectionKey.OP_READ
      link.bytes += count;
      if (link.bytes > MAX_SESSION) throw new Error('device response exceeds limit');
      link.buffer += decode(buffer, count);
    }
    return null;
    } catch (error) {
      if (!(keep ? waitedOut(link, error) : quietTimeout(link, error))) close(link);
      throw error;
    }
  }

  // A kept connection's next use: its own deadline and byte budget.
  function renew(link, end) {
    link.deadline = end;
    link.bytes = 0;
  }

  // A short wait that ran out before the connection's deadline, on a
  // connection still open, with nothing interrupted.
  function waitedOut(link, error) {
    const Timeout = Java.type('java.net.SocketTimeoutException');
    return error instanceof Timeout && !link.closed
      && Number(System.nanoTime()) < link.deadline
      && !Java.type('java.lang.Thread').currentThread().isInterrupted();
  }

  // One that also left no part line behind.
  function quietTimeout(link, error) {
    return waitedOut(link, error) && link.buffer.length === 0;
  }

  function write(link, text) {
    const Charset = Java.type('java.nio.charset.StandardCharsets');
    const buffer = Charset.US_ASCII.encode(text);
    if (Number(buffer.remaining()) > MAX_LINE) throw new Error('device command exceeds limit');
    while (buffer.hasRemaining()) {
      remaining(link.deadline);
      if (Number(link.channel.write(buffer)) === 0) wait(link, 4, link.deadline); // SelectionKey.OP_WRITE
    }
  }
  return { deadline, remaining, http, unreached, open, renew, line, pollLine, write, close, quietTimeout, waitedOut };
})();
