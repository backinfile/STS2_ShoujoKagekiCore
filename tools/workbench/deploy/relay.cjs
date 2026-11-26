'use strict';
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const {Readable} = require('node:stream');
const {pipeline} = require('node:stream/promises');
const {createGzip} = require('node:zlib');
const {getMod} = require('../mods.cjs');

const IMAGE_LIMIT = 16 * 1024 * 1024;
const MCP_LIMIT = 128 * 1024 * 1024;
const SMALL_REQUEST_LIMIT = 1024 * 1024;
const allowed = new Set(['workbench_list_mods','workbench_status','workbench_query','workbench_get_entity',
  'workbench_get_state','workbench_get_assets','workbench_history','workbench_commit','workbench_restore']);
const methods = new Set(['initialize','ping','tools/list','tools/call','notifications/initialized','notifications/cancelled']);

class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function* boundedBody(req, limit) {
  let size = 0;
  for await (const chunk of req.iterator({destroyOnReturn: false})) {
    size += chunk.length;
    if (size > limit) throw new RequestError(413, '提交过大');
    yield chunk;
  }
}

function createRelay(config, directory = __dirname) {
  let largeRequest = false;
  const expected = Buffer.from('Bearer ' + config.secret);
  return http.createServer(async (req, res) => {
    const reply = (status, value, headers = {}) => {
      res.writeHead(status, {'Content-Type':'application/json; charset=utf-8', ...headers});
      res.end(JSON.stringify(value));
    };
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Referrer-Policy', 'no-referrer');
    if (req.headers.origin && req.headers.origin !== config.origin) return reply(403, {error:'来源无效'});
    const match = /^\/mods\/([a-z0-9_-]+)(\/.*)$/.exec(req.url);
    const modId = match?.[1] || 'junna';
    const route = match?.[2] || req.url;
    try { getMod(modId); }
    catch { return reply(404, {error:'Mod 不存在'}); }
    const relative = modId === 'junna' ? '' : path.join('mods', modId);
    const modPath = modId === 'junna' ? '' : '/mods/' + modId;
    if ((req.method === 'GET' && route === '/') ||
        (['GET', 'HEAD'].includes(req.method) && route === '/app.webmanifest')) {
      const manifest = route === '/app.webmanifest';
      try {
        const file = path.join(directory, relative, manifest ? 'app.webmanifest' : 'editor.html');
        const content = await fs.promises.readFile(file);
        res.writeHead(200, {'Content-Type': manifest ? 'application/manifest+json; charset=utf-8' : 'text/html; charset=utf-8'});
        return res.end(req.method === 'HEAD' ? undefined : content);
      } catch (error) {
        return reply(error.code === 'ENOENT' ? 404 : 500, {error:'页面无法读取'});
      }
    }
    if (req.method !== 'POST' || !['/mcp', '/upload'].includes(route)) return reply(404, {error:'路径无效'});
    const auth = Buffer.from(String(req.headers.authorization || ''));
    if (auth.length !== expected.length || !crypto.timingSafeEqual(auth, expected)) {
      return reply(401, {error:'连接无效'});
    }

    const upload = route === '/upload';
    const limit = upload ? IMAGE_LIMIT : MCP_LIMIT;
    const length = Number(req.headers['content-length'] ?? Infinity);
    if (req.headers['content-length'] !== undefined && length > limit) {
      return reply(413, {error:'提交过大'}, {Connection:'close'});
    }
    if (upload && !/^image\/(png|jpeg|webp|gif)$/.test(req.headers['content-type'] || '')) {
      return reply(400, {error:'图片类型无效'});
    }
    const large = upload || length > SMALL_REQUEST_LIMIT;
    if (large && largeRequest) return reply(503, {error:'工作台繁忙'}, {'Retry-After':'2', Connection:'close'});
    if (large) largeRequest = true;

    const disconnected = new AbortController();
    const timeout = AbortSignal.timeout(90000);
    const signal = AbortSignal.any([disconnected.signal, timeout]);
    const closed = () => { if (!res.writableFinished) disconnected.abort(); };
    res.once('close', closed);
    let streamError;
    try {
      const local = JSON.parse(await fs.promises.readFile(config.backendConfig, 'utf8'));
      const url = 'http://127.0.0.1:' + local.port;
      const headers = {Authorization:'Bearer ' + local.token, Origin:url};
      if (upload) {
        async function* imageBody() {
          try { yield* boundedBody(req, limit); }
          catch (error) { streamError = error; throw error; }
        }
        const upstream = await fetch(url + modPath + route, {
          method:'POST',
          headers:{...headers, 'Content-Type':req.headers['content-type'], 'X-Asset-Id':req.headers['x-asset-id'],
            'X-Request-Id':req.headers['x-request-id'], 'X-Client-Id':req.headers['x-client-id']},
          body:imageBody(), duplex:'half', signal
        });
        return reply(upstream.status, await upstream.json());
      }

      const chunks = [];
      for await (const chunk of boundedBody(req, limit)) chunks.push(chunk);
      if (signal.aborted) throw signal.reason;
      let body;
      try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { return reply(400, {error:'JSON 格式无效'}); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) return reply(400, {error:'MCP 请求格式无效'});
      if (!methods.has(body.method)) return reply(403, {error:'方法无效'});
      if (body.method === 'tools/call' && !allowed.has(body.params?.name)) return reply(403, {error:'工具无效'});
      const upstream = await fetch(url + modPath + route, {
        method:'POST', headers:{...headers, 'Content-Type':'application/json',
          Accept:'application/json, text/event-stream', 'MCP-Protocol-Version':'2025-03-26'},
        body:JSON.stringify(body), signal
      });
      if (!upstream.body) return res.writeHead(upstream.status).end();
      if (body.method === 'tools/list') {
        const value = await upstream.json();
        if (value.result?.tools) value.result.tools = value.result.tools.filter(tool => allowed.has(tool.name));
        return reply(upstream.status, value);
      }
      if (body.method === 'tools/call' && body.params.name === 'workbench_status') {
        const value = await upstream.json();
        const status = value.result?.structuredContent;
        if (status) {
          status.url = config.origin + config.prefix + modPath + '/';
          value.result.content = [{type:'text', text:JSON.stringify(status)}];
        }
        return reply(upstream.status, value);
      }
      const acceptGzip = /\bgzip\b/.test(req.headers['accept-encoding'] || '');
      res.writeHead(upstream.status, {'Content-Type':upstream.headers.get('content-type') || 'application/json',
        ...(acceptGzip ? {'Content-Encoding':'gzip', Vary:'Accept-Encoding'} : {})});
      const stream = Readable.fromWeb(upstream.body);
      if (acceptGzip) await pipeline(stream, createGzip({level:3}), res, {signal});
      else await pipeline(stream, res, {signal});
    } catch (cause) {
      if (disconnected.signal.aborted || res.destroyed) return;
      if (res.headersSent) return res.destroy();
      const error = streamError || cause;
      if (error instanceof RequestError) return reply(error.status, {error:error.message}, {Connection:'close'});
      if (timeout.aborted) return reply(504, {error:'工作台上游超时'});
      return reply(502, {error:'工作台连接中断'});
    } finally {
      if (large) largeRequest = false;
      res.off('close', closed);
    }
  });
}

function start(config) {
  const server = createRelay(config);
  server.listen(config.relayPort, '127.0.0.1', () => {
    fs.writeFileSync(path.join(path.dirname(config.backendConfig), 'relay.runtime.json'),
      JSON.stringify({pid:process.pid, port:config.relayPort, startedAt:new Date().toISOString()}) + '\n');
    console.log('Shoujo relay ready');
  });
  return server;
}

if (require.main === module) start(JSON.parse(fs.readFileSync(path.join(__dirname, 'relay.config.json'), 'utf8')));
module.exports = {createRelay, start};
