// Native I/O only: no callback runs JavaScript on an I/O/deadline thread.
// The renderer embeds a copy in each device script; no shared Graal objects.
const thermiteIO = (() => {
  const System = Java.type('java.lang.System');
  const TimeUnit = Java.type('java.util.concurrent.TimeUnit');
  const MAX_BODY = 65536;
  const MAX_LINE = 4096;
  const MAX_SESSION = 65536;
  let client = null;

  function deadline(ms) { return Number(System.nanoTime()) + ms * 1000000; }
  function remaining(end) {
    const ms = Math.ceil((end - Number(System.nanoTime())) / 1000000);
    if (ms <= 0) throw new (Java.type('java.net.SocketTimeoutException'))('device operation deadline exceeded');
    return ms;
  }

  function http(url, method, body, headers, timeout, connectTimeout) {
    const end = deadline(timeout);
    if (client === null) {
      const HttpClient = Java.type('org.eclipse.jetty.client.HttpClient');
      const Transport = Java.type('org.eclipse.jetty.client.http.HttpClientTransportOverHTTP');
      const EmptyCookies = Java.type('org.eclipse.jetty.util.HttpCookieStore$Empty');
      const common = require('openhab').osgi.getService('org.openhab.core.io.net.http.HttpClientFactory').getCommonHttpClient();
      // Own only our connections/selector. Reuse already-started native workers
      // and scheduler, without changing or stopping the common client.
      const owned = new HttpClient(new Transport(1));
      owned.setExecutor(common.getExecutor());
      owned.setScheduler(common.getScheduler());
      owned.setByteBufferPool(common.getByteBufferPool());
      owned.setCookieStore(new EmptyCookies());
      owned.setFollowRedirects(false);
      owned.setConnectTimeout(connectTimeout);
      owned.setMaxConnectionsPerDestination(1);
      owned.setMaxRequestsQueuedPerDestination(1);
      try { owned.start(); } catch (error) { try { owned.stop(); } catch (_) {} throw error; }
      // Sony's PIN API uses a bare 401 as a normal protocol response. The
      // integration alone owns Basic/Cookie authentication and retries; Jetty
      // must neither reject that challenge nor handle it automatically.
      owned.getProtocolHandlers().remove('www-authenticate');
      client = owned;
      require('@runtime').lifecycleTracker.addDisposeHook(() => { owned.stop(); });
    }
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

  function line(link, timeout) {
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
      buffer.flip();
      while (buffer.hasRemaining()) link.buffer += String.fromCharCode(Number(buffer.get()) & 255);
    }
    return null;
    } catch (error) {
      if (!quietTimeout(link, error)) close(link);
      throw error;
    }
  }

  function quietTimeout(link, error) {
    const Timeout = Java.type('java.net.SocketTimeoutException');
    return error instanceof Timeout && !link.closed && link.buffer.length === 0
      && Number(System.nanoTime()) < link.deadline
      && !Java.type('java.lang.Thread').currentThread().isInterrupted();
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
  return { deadline, remaining, http, unreached, open, line, write, close, quietTimeout };
})();
