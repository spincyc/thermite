// Native API adapters for the existing in-memory protocol fixtures. The
// independent native I/O regression test also executes _io.js on real JVM I/O.
'use strict';
function install(classes, openhab, options) {
  let elapsed = 0;
  classes['java.lang.System'] = { nanoTime: () => elapsed * 1000000 };
  classes['java.util.concurrent.TimeUnit'] = { MILLISECONDS: 'milliseconds' };
  classes['java.net.SocketTimeoutException'] = class extends Error {};
  classes['java.lang.Thread'] = { currentThread: () => ({ isInterrupted: () => false }) };
  const hooks = [];
  if (options.client) {
    const workers = {};
    const common = { getExecutor: () => workers, getScheduler: () => workers, getByteBufferPool: () => workers };
    openhab.osgi = { getService: () => ({ getCommonHttpClient: () => common }) };
    class HttpClient {
      setExecutor() {} setScheduler() {} setByteBufferPool() {} setCookieStore() {}
      setFollowRedirects() {} setMaxConnectionsPerDestination() {} setMaxRequestsQueuedPerDestination() {}
      setConnectTimeout(ms) { if (options.connect) options.connect(ms); }
      getProtocolHandlers() { return { remove() {} }; }
      start() {} stop() {}
      newRequest(uri) {
        const request = { uri, method: 'GET', body: null, headers: {} };
        return {
          method(v) { request.method = v; return this; },
          timeout(v) { request.timeout = v; return this; },
          header(n,v) { request.headers[n] = v; return this; },
          content(v) { request.body = v.text; return this; },
          send(listener) {
            try { listener.reply = options.client.send(request, 'string'); }
            catch (error) { listener.error = error; }
          },
        };
      }
    }
    class Listener {
      constructor(request, max) { this.max = max; }
      get(timeout) {
        if (this.error) throw this.error;
        const reply = this.reply;
        const body = String(reply.body());
        if (Buffer.byteLength(body) > this.max) throw Error('response exceeds limit');
        return { getStatus: () => reply.statusCode(), getContentAsString: () => body,
          getHeaders: () => ({ getValuesList: n => reply.headers ? reply.headers().allValues(n) : {size:()=>0,get:()=>null} }) };
      }
      cancel() {}
    }
    Object.assign(classes, {
      'org.eclipse.jetty.client.HttpClient': HttpClient,
      'org.eclipse.jetty.client.http.HttpClientTransportOverHTTP': class {},
      'org.eclipse.jetty.util.HttpCookieStore$Empty': class {},
      'org.eclipse.jetty.client.util.StringContentProvider': class { constructor(text) { this.text=text; } },
      'org.eclipse.jetty.client.util.FutureResponseListener': Listener,
    });
  }
  if (options.Socket) {
    class Buffer {
      constructor(bytes) { this.bytes=bytes; this.pos=0; this.end=bytes.length; }
      static allocate(size) { return new Buffer(new Array(size)); }
      static wrap(bytes) { return new Buffer(bytes); }
      hasRemaining() { return this.pos < this.end; }
      remaining() { return this.end - this.pos; }
      get() { return this.bytes[this.pos++]; }
      flip() { this.end=this.pos; this.pos=0; }
    }
    class Channel {
      constructor() { this.socket=new options.Socket(); this.pending=''; }
      static open() { return new Channel(); }
      configureBlocking() {}
      connect(address) { this.socket.connect(address, 2000); return true; }
      register() {}
      read(buffer) {
        if (!this.pending) {
          if (this.socket.replies.length) this.pending=this.socket.replies.shift()+'\r\n';
          else if (this.socket.eof) return -1;
          else return 0;
        }
        let count=0;
        while (this.pending.length && buffer.hasRemaining()) {
          buffer.bytes[buffer.pos++]=this.pending.charCodeAt(0); this.pending=this.pending.slice(1); count++;
        }
        return count;
      }
      write(buffer) {
        let text='';
        while(buffer.hasRemaining()) text+=String.fromCharCode(buffer.get());
        this.socket.receive(text); return text.length;
      }
      close() { this.socket.close(); }
    }
    Object.assign(classes, {
      'java.nio.channels.SocketChannel': Channel,
      'java.nio.channels.Selector': { open:()=>({ selectedKeys:()=>({clear(){}}), select:ms=>{elapsed+=ms;},close(){} }) },
      'java.net.InetAddress': { getByAddress:bytes=>bytes.map(v=>(v+256)%256).join('.') },
      'java.nio.ByteBuffer': Buffer,
      'java.nio.charset.StandardCharsets': { US_ASCII: { encode: text => Buffer.wrap([...text].map(c=>c.charCodeAt(0))) } },
      'java.lang.String': class { constructor(s) { this.text=s; } getBytes(){return [...this.text].map(c=>c.charCodeAt(0));} },
    });
  }
  return { runtime:{ lifecycleTracker:{addDisposeHook:hook=>hooks.push(hook)} }, hooks };
}
module.exports = { install };
