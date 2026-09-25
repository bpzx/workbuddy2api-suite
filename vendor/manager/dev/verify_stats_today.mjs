/**
 * 用量页时段选择的界面验收（开发工具，不参与构建）。issue #53。
 *
 * 为什么要真跑一遍：这次改的是**默认值**与**同屏口径**——默认从「近 30 天」
 * 改成「今日」，趋势图与两张分解表要一起跟随，而且副标题不能还写着「按天聚合」。
 * 这些只有真渲染出来才看得见；单测能钉住源码文本，钉不住「界面上到底显示什么」。
 *
 *   node dev/verify_stats_today.mjs
 *
 * 前置：本机已装 playwright-core 与 ms-playwright 里的 chromium（与
 * dev/keys-credit-ui.mjs 共用同一套依赖）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7885';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || 'testpw123';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-stats-today');

async function loadPlaywright() {
  // WB_PLAYWRIGHT 优先：调用方（dev/verify_stats_today_ui.py）用 Python 算好
  // 真实路径传进来。为什么要这样：在 MSYS/Git-Bash 下 `TEMP` 是 `/tmp`，
  // 而 Node 是原生 Windows 进程，把它当路径拼出来会指向 D:\tmp 之类的错位置。
  const explicit = process.env.WB_PLAYWRIGHT;
  const candidates = [
    ...(explicit ? [explicit] : []),
    path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js'),
    'playwright-core',
  ];
  for (const c of candidates) {
    try {
      const mod = await import(c.startsWith('/') || /^[A-Za-z]:/.test(c) ? pathToFileURL(c).href : c);
      return mod.chromium ?? mod.default?.chromium;
    } catch { /* 试下一个 */ }
  }
  throw new Error(`找不到 playwright-core（试过：${candidates.join(', ')}）`);
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root)
    .filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell'))
    .sort().pop();
  if (!dir) return undefined;
  const exe = path.join(root, dir, 'chrome-win64', 'chrome.exe');
  return fs.existsSync(exe) ? exe : undefined;
}

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const ctx = await browser.newContext({viewport: {width: 1440, height: 1000}});
const page = await ctx.newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

// 记录页面实际发出的统计请求，用来核对「界面选了什么」与「后端收到什么」一致
const seen = [];
page.on('request', (r) => {
  const u = r.url();
  if (/\/api\/stats\/(daily|by-model|by-key)/.test(u)) seen.push(u);
});

console.log('=== 登录 ===');
await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);
step(page.url().includes('dashboard'), '登录成功', page.url());

console.log('\n=== 打开用量统计页 ===');
seen.length = 0;
await page.goto(`${BASE}/stats`, {waitUntil: 'load'});
await page.waitForTimeout(1500);
await page.screenshot({path: path.join(OUT, 's1_default.png'), fullPage: true});

const bodyText = await page.evaluate(() => document.body.innerText);

/**
 * 时段选择器（不是语言切换器）。
 *
 * 踩过的坑：页面顶部还有语言切换器与版本切换器，同样是 combobox，按
 * `button[role=combobox]` 取第一个拿到的是**语言**——后面的断言全都在问
 * 「语言选择器里有没有今日」，看起来像功能没做。这里按**当前文本**定位：
 * 时段选择器显示的是 今日 / 近 7 天 / Today 之类。
 */
const RANGE_WORDS = /今日|近 \d+ 天|Today|Last \d+ days|直近 \d+ 日|최근 \d+일|依天彙總/;
async function rangeTrigger() {
  const boxes = page.locator('button[role="combobox"]');
  const n = await boxes.count();
  for (let i = 0; i < n; i++) {
    const txt = ((await boxes.nth(i).innerText().catch(() => '')) || '').trim();
    if (RANGE_WORDS.test(txt)) return {box: boxes.nth(i), text: txt, index: i};
  }
  return {box: null, text: '', index: -1};
}

// ① 默认就是「今日」，且请求带 days=1
const cur = await rangeTrigger();
step(cur.box !== null, '找到时段选择器（按文本定位，不按位置）',
     `候选数=${await page.locator('button[role="combobox"]').count()}，命中第 ${cur.index + 1} 个`);
step(/今日|Today/.test(cur.text), '时段选择器默认显示「今日」', `实际=${JSON.stringify(cur.text)}`);
step(seen.some((u) => /[?&]days=1(&|$)/.test(u)),
     '页面用 days=1 拉取统计（与「今日」口径一致）',
     seen.slice(0, 2).join(' | '));
step(!seen.some((u) => /[?&]days=30(&|$)/.test(u)),
     '没有残留的 days=30 请求（默认值确实改了）');

// ② 副标题随窗口变（只有一天时不能写「按天聚合」）
step(/当日汇总|Today only|当日集計|오늘 집계|當日彙總/.test(bodyText),
     '图表副标题是「当日汇总」而不是「按天聚合」',
     bodyText.split('\n').filter((l) => /聚合|汇总|彙總|Today/.test(l)).slice(0, 2).join(' | '));

// ③ 选项里四项齐全
await cur.box.click();
await page.waitForTimeout(700);
await page.screenshot({path: path.join(OUT, 's2_options.png')});
const opts = await page.evaluate(() =>
  Array.from(document.querySelectorAll('[role="option"]')).map((e) => e.textContent.trim()));
step(opts.length === 4, '时段选项有四项', `实际=${JSON.stringify(opts)}`);
step(/今日|Today/.test(opts[0] || ''), '「今日」排在第一位', `实际=${JSON.stringify(opts[0])}`);

// ④ 切到近 7 天：三处一起跟随，副标题变回「按天聚合」
seen.length = 0;
const seven = page.locator('[role="option"]').filter({hasText: /近 7 天|Last 7 days|直近 7 日|최근 7일/}).first();
await seven.click();
await page.waitForTimeout(1600);
await page.screenshot({path: path.join(OUT, 's3_last7.png'), fullPage: true});
const paths = seen.map((u) => u.replace(/^.*\/api\/stats\//, '').split('?')[0]);
step(['daily', 'by-model', 'by-key'].every((p) => paths.includes(p)),
     '趋势图与两张分解表都跟随时段变化', `实际请求=${JSON.stringify([...new Set(paths)])}`);
step(seen.every((u) => /[?&]days=7(&|$)/.test(u)),
     '三处请求都带 days=7', seen.slice(0, 3).join(' | '));
const after7 = await page.evaluate(() => document.body.innerText);
step(/按天聚合|Aggregated daily|日次集計|依天彙總|일별 집계/.test(after7),
     '切回多天窗口后副标题恢复为「按天聚合」');
const afterCur = await rangeTrigger();
step(/近 7 天|Last 7|直近 7|최근 7/.test(afterCur.text),
     '选择器上显示的是「近 7 天」', `实际=${JSON.stringify(afterCur.text)}`);

// ⑤ 切到英文：新加的文案要真的跟着语言走（不能只有中文能看）
console.log('\n=== 切到英文界面 ===');
await page.locator('button[role="combobox"]').first().click();
await page.waitForTimeout(600);
await page.locator('[role="option"]').filter({hasText: /English/}).first().click();
await page.waitForTimeout(1300);
await page.screenshot({path: path.join(OUT, 's4_en.png'), fullPage: true});
const enCur = await rangeTrigger();
step(/Today|Last 7|Last 30/.test(enCur.text),
     '英文界面下时段选择器已翻译', `实际=${JSON.stringify(enCur.text)}`);
await enCur.box.click();
await page.waitForTimeout(600);
const enOpts = await page.evaluate(() =>
  Array.from(document.querySelectorAll('[role="option"]')).map((e) => e.textContent.trim()));
await page.keyboard.press('Escape');
step(enOpts.includes('Today'), '英文界面下选项里有 Today', `实际=${JSON.stringify(enOpts)}`);

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
console.log(`截图目录：${OUT}`);
