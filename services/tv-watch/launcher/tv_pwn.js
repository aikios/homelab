// SCPL client for Vizio M55-D0 (2016, plain-HTTP API on 7345, TLS fallback)
// Commands:
//   pair [name] [id]      interactive pairing (PIN via /tmp/tv_pair_pin.txt)
//   launch <url> [ns] [appid]
//   current               GET /app/current
//   home                  restore home bundle
//   enum                  route existence sweep
//   get <path>            raw GET
//   put <path> <json>     raw PUT
const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// TV_IP/TV_HOST and the token file come from the environment / the service dir;
// no real address or token lives in this public tree.
const TV = process.env.TV_IP || process.env.TV_HOST || '';
if (!TV) { console.error('set TV_IP (or TV_HOST) to the TV LAN address'); process.exit(1); }
const TOKEN_FILE = process.env.TV_TOKEN_FILE || path.join(__dirname, '..', 'tv_token.json');
let AUTH = '';
try { AUTH = JSON.parse(fs.readFileSync(TOKEN_FILE, 'utf8')).token; } catch (e) {}

function req(m, p, body, auth, tls) {
  return new Promise(r => {
    const d = body ? JSON.stringify(body) : '';
    const mod = tls ? https : http;
    const o = { hostname: TV, port: 7345, path: p, method: m, rejectUnauthorized: false,
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(d) } };
    if (auth) o.headers.AUTH = auth;
    const rr = mod.request(o, res => { let s = ''; res.on('data', c => s += c); res.on('end', () => r({ code: res.statusCode, body: s })); });
    rr.on('error', e => r({ code: 0, body: 'ERR:' + e.message }));
    rr.setTimeout(8000, () => { rr.destroy(); r({ code: 0, body: 'ERR:timeout' }); });
    if (d) rr.write(d);
    rr.end();
  });
}
async function req2(m, p, body, auth) { // TLS first (verified post-reset), plain fallback
  let r = await req(m, p, body, auth, true);
  if (r.code === 0) r = await req(m, p, body, auth, false);
  return r;
}

async function pair(name, id) {
  name = name || 'ResearchBox'; id = id || 'deadbeef1234';
  const PIN_FILE = '/tmp/tv_pair_pin.txt';
  try { fs.unlinkSync(PIN_FILE); } catch (e) {}
  const start = await req2('PUT', '/pairing/start', { DEVICE_NAME: name, DEVICE_ID: id });
  console.log('start:', start.code, start.body.slice(0, 200));
  if (!start.body.includes('SUCCESS')) process.exit(1);
  const token = JSON.parse(start.body).ITEM.PAIRING_REQ_TOKEN;
  execSync('bash ' + path.join(__dirname, 'alert_user.sh'));
  console.log('PIN shown on TV — enter it: echo <PIN> > ' + PIN_FILE);
  const t0 = Date.now(); let pin = null;
  while (Date.now() - t0 < 300000) {
    try { pin = fs.readFileSync(PIN_FILE, 'utf8').trim(); if (pin) break; } catch (e) {}
    await new Promise(r => setTimeout(r, 300));
  }
  if (!pin) { console.log('no pin'); process.exit(1); }
  const r = await req2('PUT', '/pairing/pair', { DEVICE_NAME: name, DEVICE_ID: id,
    CHALLENGE_TYPE: 1, RESPONSE_VALUE: pin, PAIRING_REQ_TOKEN: token });
  console.log('pair:', r.code, r.body.slice(0, 300));
  try { const tok = JSON.parse(r.body).ITEM.AUTH_TOKEN;
    fs.writeFileSync(TOKEN_FILE, JSON.stringify({ token: tok, name, id }));
    console.log('saved token:', tok);
  } catch (e) { console.log('parse fail'); process.exit(1); }
}

async function launch(url, ns, appid) {
  ns = ns ? parseInt(ns) : 4; appid = appid || '3001';
  const r = await req2('PUT', '/app/launch', { VALUE: { MESSAGE: url, NAME_SPACE: ns, APP_ID: appid } }, AUTH);
  console.log('launch:', r.code, r.body.slice(0, 300));
}

async function current() {
  const r = await req2('GET', '/app/current', null, AUTH);
  console.log('current:', r.code, r.body.slice(0, 500));
}

async function home() {
  const r = await req2('PUT', '/app/launch', { VALUE: { MESSAGE: '{"app":"home","bundle":"bundles/home"}', NAME_SPACE: 4, APP_ID: '1' } }, AUTH);
  console.log('home:', r.code, r.body.slice(0, 200));
}

const ROUTES = ['/app/launch', '/app/current', '/app/list', '/app/state', '/app/store/get',
  '/app/store/set', '/app/get_auth_token', '/app/key/register', '/install', '/fw_update',
  '/update', '/upgrade', '/pairing/start', '/system/capabilities', '/system/dynamic/iptolocation',
  '/state/device/power_mode', '/menu_native/dynamic/tv_settings', '/cloud/firmware_update',
  '/app/exit', '/app/close', '/state', '/system', '/state/device', '/app'];

async function enum_() {
  for (const p of ROUTES) {
    const g = await req2('GET', p, null, AUTH);
    const put = await req2('PUT', p, { VALUE: {} }, AUTH);
    const tag = (c, b) => {
      if (c === 0) return 'DEAD';
      if (b.includes('URI_NOT_FOUND')) return 'ROUTE_ABSENT';
      if (c === 200 || c === 400 || c === 401 || c === 403) return 'LIVE(' + c + ')';
      if (c === 500) return 'LIVE(500)';
      return c + '?';
    };
    console.log(p.padEnd(36), 'GET=' + tag(g.code, g.body), 'PUT=' + tag(put.code, put.body),
      g.body ? (g.body.slice(0, 90).replace(/\s+/g, ' ')) : '');
    await new Promise(r => setTimeout(r, 150));
  }
}

(async () => {
  const cmd = process.argv[2];
  if (cmd === 'pair') await pair(process.argv[3], process.argv[4]);
  else if (cmd === 'launch') await launch(process.argv[3], process.argv[4], process.argv[5]);
  else if (cmd === 'current') await current();
  else if (cmd === 'home') await home();
  else if (cmd === 'enum') await enum_();
  else if (cmd === 'get') { const r = await req2('GET', process.argv[3], null, AUTH); console.log(r.code, r.body.slice(0, 2000)); }
  else if (cmd === 'put') { const r = await req2('PUT', process.argv[3], JSON.parse(process.argv[4]), AUTH); console.log(r.code, r.body.slice(0, 2000)); }
  else console.log('usage: node tv_pwn.js pair|launch|current|home|enum|get|put ...');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });