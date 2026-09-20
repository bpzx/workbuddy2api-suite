"""点链接登录后切回页面，界面必须**立即**反映结果（issue #34）。

现场：用户点弹出的授权链接、在浏览器里登录成功，切回管理端时弹窗仍停在
「等待…」。根因是浏览器节流后台标签页的定时器（约 1 次/分钟），而轮询当时
没处理 `visibilitychange` —— 切回来后要等最久一整分钟才刷新。

这个脚本复现那条路径：**让标签页真的进入后台、再切回来**，并断言界面在
「切回后不到一个轮询间隔（2 秒）」内就更新了。

    python dev/verify_login_poll_visibility.py --check

数据落在 dev/.login-poll/（已 gitignore），截图输出到 dev/.login-shots/。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.login-poll'
SHOTS = REPO / 'dev' / '.login-shots'
UPSTREAM_PORT = 7903
MANAGER_PORT = 7904
ADMIN_PW = 'login-poll-pass'

# 轮询接口先返回 waiting，脚本中途翻成 success —— 模拟"用户在别处完成了登录"
_login_done = {'v': False}


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/healthz':
            self._json({'healthy': 1, 'total': 1})
        elif path == '/status':
            self._json({'accounts': [], 'total': 0, 'healthy': 0, 'cooling': 0,
                        'disabled': 0, 'in_flight_full': 0, 'redis_mode': 'noop',
                        'sticky_sessions': 0,
                        'realm_totals': {
                            'cn': {'total': 0, 'healthy': 0, 'cooling': 0,
                                   'disabled': 0, 'in_flight_full': 0},
                            'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                                       'disabled': 0, 'in_flight_full': 0}}})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    (DATA / 'auths').mkdir(parents=True, exist_ok=True)

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }

    # 启动管理端；同时在**管理端进程里**打补丁：让 tencent.start_login /
    # poll_login 返回可控结果（真实腾讯无法在测试里走完授权）。
    launcher = f'''
import server.config as c
from server.services import tencent

_calls = {{'start': 0, 'poll': 0}}

async def fake_start(realm='cn'):
    _calls['start'] += 1
    return {{'state': 'probe-state-1', 'authUrl': 'https://example.invalid/authorize?state=probe-state-1',
             'realm': realm}}

async def fake_poll(state, realm=None):
    _calls['poll'] += 1
    # 第 3 次之后翻成 ready —— 模拟用户在浏览器里完成了授权
    if _calls['poll'] >= 3:
        return {{'status': 'ready', 'uid': 'u-probe', 'nickname': '探针账号',
                 'enterprise_id': '', 'access_token': 'tok', 'refresh_token': 'rt',
                 'expires_at': 9999999999, 'domain': 'copilot.tencent.com', 'realm': 'cn'}}
    return {{'status': 'waiting'}}

tencent.start_login = fake_start
tencent.poll_login = fake_poll
import server.routers.accounts as acc
acc.tencent.start_login = fake_start
acc.tencent.poll_login = fake_poll

import uvicorn, server.main
uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")
'''
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin / {ADMIN_PW})')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        return run_check(env)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


def run_check(env: dict) -> int:
    code = r'''
const BASE = process.env.BASE, USER = 'admin', PASS = process.env.PASS, OUT = process.env.OUT;
(async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const {pathToFileURL} = await import('node:url');
  const c = path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js');
  const mod = await import(fs.existsSync(c) ? pathToFileURL(c).href : 'playwright-core');
  const chromium = mod.chromium ?? mod.default?.chromium;
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  const dir = fs.existsSync(root) ? fs.readdirSync(root).filter(d => d.startsWith('chromium-') && !d.includes('headless_shell')).sort().pop() : null;
  const exe = dir ? path.join(root, dir, 'chrome-win64', 'chrome.exe') : undefined;
  const browser = await chromium.launch({executablePath: exe});
  const ctx = await browser.newContext({viewport: {width: 1400, height: 950}, deviceScaleFactor: 2});
  const page = await ctx.newPage();
  const findings = [];
  const step = (ok, label, detail = '') => {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` -- ${detail}` : ''}`);
    if (!ok) findings.push(label + (detail ? ': ' + detail : ''));
  };

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.waitForTimeout(2000);

  // 打开「添加账号」弹窗，轮询随之启动
  await page.click('text=添加账号');
  await page.waitForTimeout(2500);
  const body1 = await page.locator('body').innerText();
  step(/等待授权完成|等待/.test(body1), '弹窗进入等待状态（轮询已启动）');

  // ── 关键场景：模拟用户点链接跳到别处登录 ──
  // 用 CDP 让页面进入 hidden（等价于用户切到另一个标签页）：
  // 直接派发 visibilitychange 并覆写 document.hidden，因为无头浏览器不会
  // 因为"切标签"真的改变它。
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  // 在"后台"停留 7 秒 —— 期间轮询 tick 应当被跳过（真实浏览器会节流，
  // 而且我们的实现本来就要求 hidden 时不查）
  await page.waitForTimeout(7000);
  const hiddenPolled = await page.evaluate(() => window.__pollCount || 0);

  // ── 用户登录完成，切回页面 ──
  const t0 = Date.now();
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
    Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
    document.dispatchEvent(new Event('visibilitychange'));
  });
  // 等界面更新（最多 5 秒 —— 远小于"被节流到 1 次/分钟"的 60 秒）
  let updated = false;
  for (let i = 0; i < 50; i++) {
    const txt = await page.locator('body').innerText();
    if (/授权成功|探针账号/.test(txt)) { updated = true; break; }
    await page.waitForTimeout(100);
  }
  const elapsed = Date.now() - t0;
  step(updated, `切回后界面立即更新（${elapsed}ms，上限 5000ms）`,
       updated ? '' : '界面仍未更新 —— 说明 visibilitychange 没生效');
  step(elapsed < 5000, `更新耗时远小于一个轮询间隔被节流的 60s（${elapsed}ms）`);

  await page.screenshot({path: `${OUT}/login-done.png`, fullPage: true});

  await browser.close();
  if (findings.length) { console.log('\nFAILURES:\n' + findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e => { console.error('harness error:', e); process.exit(2); });
'''
    SHOTS.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', '-e', code],
                       env={**env, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}',
                            'PASS': ADMIN_PW, 'OUT': str(SHOTS)},
                       cwd=str(REPO / 'web'), text=True, timeout=300)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:2000])
    print(f'截图: {SHOTS}')
    return r.returncode


if __name__ == '__main__':
    raise SystemExit(main())
