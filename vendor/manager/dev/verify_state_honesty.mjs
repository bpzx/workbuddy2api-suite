/**
 * 「取不到 ≠ 没有」的浏览器断言（静态导出产物）。
 *
 * 安全页（PR #93）：
 *   A 全挂     → 整页「数据加载失败」+ 重试；**不**出现「暂无规则」，**不**渲染开关
 *   B 只 config 挂 → 策略卡说「未取到」，**一个开关都不渲染**，其余内容仍在
 *   C 恢复     → 规则与开关回来
 *
 * 任务记录页（PR #97）：
 *   D 慢加载   → 有骨架、**不**出现「暂无签到记录 / 暂无自动任务记录」
 *   E 签到记录挂 → 该面板说「签到记录加载失败」，**不**出现「共 0 条」，另一块照常
 *   F 恢复     → 两块内容与页脚都回来
 *
 * 密钥页（PR #100）：
 *   G 全挂     → 整页「数据加载失败」+ 重试；**不**出现「暂无 API 密钥」
 *   H 只密钥列表挂 → 顶部「部分数据加载失败」，**不**出现「暂无 API 密钥」
 *                    （手上这份列表不知道有没有，就不能说「没有」）
 *   I 只上游列表挂 → 顶部「部分数据加载失败」，空列表**照常**说「暂无 API 密钥」
 *                    （这一份确实取到了、确实为空。用它反过来证明 H 用的判据是
 *                     「**这一份**失败了」，而不是「有任一份失败」——后者会把一个
 *                     正常的空列表也藏起来）
 *   J 恢复     → 回到正常的空列表
 *
 * 设置页（批次 1 收尾）：
 *   K 配置挂   → 整页「数据加载失败」+ 重试；**一个 Tab 都不渲染** —— 那张表单填的是
 *                内置默认值，渲染出来等于把「默认值」说成「你现在的配置」
 *   L 只用户列表挂 → 表单照常；用户 Tab **不**说「暂无管理用户」，其余内容仍在
 *   M 恢复     → 设置页正常
 *
 * 账号管理 / 模型中心 / 红包（批次 1 收尾三页）：
 *   N 慢加载   → 三页都出骨架，**不**出现「暂无账号 / 暂无模型 / 暂无红包」
 *   O 账号池挂  → 整页错误态 + 重试；**不**说「暂无账号」；
 *                **分组切换条必须仍在**（它不依赖账号池，用户得能切回默认分组自救）
 *   P 只上游状态挂 → 列表照常渲染；账号状态标「未知」；顶部有「上游状态没取到」说明；
 *                    **不**升级成整页错误（这一条是 P0-2：原来 `upRes` 失败无 else，完全无声）
 *   Q 模型目录挂 → 整页错误态 + 重试；**不**说「暂无模型」
 *   R 红包列表挂 → 整页错误态 + 重试；**不**说「暂无红包」
 *   S 恢复     → 三页都正常
 *
 * S 对模型中心是**正向**断言（要找得到模型行），因此 fixture 的上游必须给出
 * 非空模型清单：目录为空时「暂无模型」是如实的空态，只断言「错误态消失了」的话，
 * 「页面根本没恢复」与「恢复了但确实没有模型」长得一样，这条断言就没有判别力。
 *
 * 批次 3（P1-3 / P1-4）：
 *   P 日志页筛选 → 改完即生效：下拉改完**不点任何按钮**就重查；文本输入 4 个字符
 *                  只发 **1** 次请求（400ms 防抖）；「重置」清空全部筛选且只重取一次
 *   Q 账号页     → 4 组状态摆在明面上（不再只靠 hover）；筛不到时说「没有匹配的账号」
 *                  并给「清除筛选」；切到没有账号的版本时说「国际版没有账号」而**不是**
 *                  「暂无账号」（账号在池子里，只是版本不对）
 *
 * P 的判据是**请求次数**（见下面 `hits`），因为「筛选生效了吗」在界面上看不出来。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE;
const PASS = process.env.WB_PASS;
const OUT = process.env.WB_SHOTS;

async function loadPlaywright() {
  const candidates = [
    ...(process.env.WB_PLAYWRIGHT ? [process.env.WB_PLAYWRIGHT] : []),
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

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓ ' : '✗ '} ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
// 断言全是中文文案，所以语言必须钉死：应用的 detectLocale() 会读 navigator.languages，
// 在浏览器语言不是中文的机器上，下面每一条正向断言都会失败——而页面其实完全正常。
// 更坏的是负向断言（「不许出现『暂无规则』」这类）照样是绿的，于是整份报告看起来像
// 「应用坏了」，方向完全错。钉住之后在哪台机器上跑结果都一样。
const ctx = await browser.newContext({viewport: {width: 1400, height: 1000}, locale: 'zh-CN'});
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
// 等表单真的被 React 接管再填。dev 冷编译时 hydration 会晚于首次输入：那时 fill
// 只改了 DOM，React 的受控 state 仍是空的，提交上去就是**空密码**。这个坑的表现
// 特别误导——登录失败后每一页都被弹回登录页，于是正向断言全红、负向断言全绿。
// React 会给它接管过的 DOM 节点挂一个 `__react*` 内部键，可当作 hydration 信号。
await page.waitForFunction(() => {
  const el = document.querySelector('#password');
  return !!el && Object.keys(el).some((k) => k.startsWith('__react'));
}, null, {timeout: 20000}).catch(() => {});
await page.fill('#username', 'admin');
await page.fill('#password', PASS);
await Promise.all([page.waitForURL(/dashboard/).catch(() => {}),
                   page.click('button[type=submit]')]);

// 没登录成功就立刻退出，别往下跑。后面三十多条断言都是「页面上应该有这句话」，
// 而未登录时每一页都会被弹回登录页 —— 于是正向断言全红、负向断言（「不许出现
// 『暂无规则』」这类）反而全绿，整份报告看起来像「应用坏了」，方向完全错。
if (!/dashboard/.test(page.url())) {
  console.error(`✗ 登录没成功（当前停在 ${page.url()}）——后面的断言没有意义，直接退出`);
  await browser.close();
  process.exit(1);
}

// 运行时开关：0=全放行 1=安全页全挂 2=只 config 挂 3=签到记录延迟 4=签到记录 500
//              5=密钥+上游全挂 6=只密钥列表挂 7=只上游列表挂
//              8=设置页配置挂（主数据） 9=只设置页用户列表挂
//             10=账号池挂 11=只上游状态挂 12=模型目录挂 13=红包列表挂 14=三页都延迟
const MODE = {value: 0};

/**
 * 每个接口被打了多少次（键是 `/api/` 之后、去掉查询串的那一段）。
 *
 * 「筛选改完生效了吗」「防抖有没有生效」这两件事的判据都是**请求次数**——在界面上
 * 看不出来（列表看起来一样，只是内容不同）。所以计数放在拦截器里，失败的那次也算。
 */
const hits = {};

await page.route('**/api/**', async (route) => {
  const url = route.request().url();
  const fail = (code = 500) => route.fulfill({
    status: code, contentType: 'application/json',
    body: JSON.stringify({detail: '模拟后端故障'})});
  // 路径要钉到结尾（`(\?|$)`）：`/api/accounts` 是 `/api/accounts-xxx` 的前缀，
  // 宽松匹配会在将来加接口时悄悄多拦一个。
  const is = (p) => new RegExp(`/api/${p}(\\?|$)`).test(url);
  {
    const path = url.split('?')[0].split('/api/').pop();
    hits[path] = (hits[path] ?? 0) + 1;
  }
  if (MODE.value === 1 && /\/api\/security/.test(url)) return fail();
  if (MODE.value === 2 && /\/api\/security\?|config/.test(url)
      && /\/api\/security/.test(url)) return fail();
  if (MODE.value === 3 && /\/api\/checkin-logs/.test(url)) {
    await new Promise((r) => setTimeout(r, 3500));
  }
  if (MODE.value === 4 && /\/api\/checkin-logs/.test(url)) return fail();
  // 结尾用 (\?|$) 锚住：`/api/keys/import/status` 这类子路径是另一份数据
  // （弹窗打开时才探测的配件），不该被这里的「列表挂了」一起打掉。
  if (MODE.value === 5 && /\/api\/(keys|upstreams)(\?|$)/.test(url)) return fail();
  if (MODE.value === 6 && /\/api\/keys(\?|$)/.test(url)) return fail();
  if (MODE.value === 7 && /\/api\/upstreams(\?|$)/.test(url)) return fail();
  // 设置页：主数据是配置（那张表单就是它的副本）；用户列表是另一个 Tab 的配件
  if (MODE.value === 8 && /\/api\/settings\/upstream/.test(url)) return fail();
  if (MODE.value === 9 && /\/api\/users/.test(url)) return fail();
  // 账号页：主数据是账号池（`/api/accounts`）；上游状态是另一个 hook（`/api/status`）
  if (MODE.value === 10 && is('accounts')) return fail();
  if (MODE.value === 11 && is('status')) return fail();
  if (MODE.value === 12 && is('model-catalog')) return fail();
  if (MODE.value === 13 && is('red-packets')) return fail();
  if (MODE.value === 14 && (is('accounts') || is('model-catalog') || is('red-packets'))) {
    await new Promise((r) => setTimeout(r, 3500));
  }
  return route.continue();
});

const bodyText = () => page.locator('body').innerText();
/** 取这一阶段新增的前端异常（用于把异常定位到具体步骤） */
let errMark = 0;
const phaseErrors = () => {
  const fresh = errors.slice(errMark);
  errMark = errors.length;
  return fresh;
};

// ══ 安全页 ══════════════════════════════════════════════════════════
console.log('\n安全页（PR #93）');
MODE.value = 1;
await page.goto(`${BASE}/security`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
let text = await bodyText();
step(/数据加载失败/.test(text), '全部取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/暂无规则/.test(text), '不许说「暂无规则」（那等于说没有任何网段被放行或拦截）');
step(!/暂无访问记录/.test(text), '不许说「暂无访问记录」（那等于说没人被拦过）');
step((await page.locator('[role=switch]').count()) === 0,
     '一个开关都不渲染（把「不知道」显示成「关着」比空白更糟）',
     `页面上的开关数：${await page.locator('[role=switch]').count()}`);
await page.screenshot({path: `${OUT}/01-security-all-failed.png`, fullPage: true});

MODE.value = 2;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/未取到/.test(text), '只 config 没取到时，策略卡如实说「未取到」');
step((await page.locator('[role=switch]').count()) === 0, '仍然不渲染开关');
step(!/请检查网络连接或后端服务是否正常/.test(text),
     '其余数据取到了 → 不升级成整页错误（只有顶部那条常驻提示）',
     /部分数据加载失败/.test(text) ? '顶部有「部分数据加载失败」常驻提示' : '（没有常驻提示）');
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/02-security-config-missing.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step((await page.locator('[role=switch]').count()) > 0, '恢复后开关回来（正常态没有被搞坏）');
step(!/数据加载失败/.test(text) && !/未取到/.test(text), '正常态不出现错误态文案');
await page.screenshot({path: `${OUT}/03-security-ok.png`, fullPage: true});

// ══ 任务记录页 ══════════════════════════════════════════════════════
console.log('\n任务记录页（PR #97）');
MODE.value = 3;
await page.goto(`${BASE}/tasks`, {waitUntil: 'load'});
await page.waitForTimeout(1200);            // 数据还在路上（签到记录被延迟 3.5s）
text = await bodyText();
step(!/暂无签到记录/.test(text),
     '加载期不说「暂无签到记录」（看起来像账号从没签过到）',
     (text.match(/暂无[^\n]{0,10}/) || ['（无）'])[0]);
step(!/暂无自动任务记录/.test(text), '加载期不说「暂无自动任务记录」（看起来像采集器没在跑）');
await page.screenshot({path: `${OUT}/04-tasks-loading.png`, fullPage: true});
await page.waitForTimeout(4000);
text = await bodyText();
step(/共 \d+ 条/.test(text), '数据到位后两块正片与页脚都出来',
     (text.match(/共 \d+ 条/) || [''])[0]);

MODE.value = 4;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(4000);
text = await bodyText();
step(/签到记录加载失败/.test(text), '签到记录挂了：该面板说「加载失败」而不是「暂无」');
step(!/暂无签到记录/.test(text), '不说「暂无签到记录」');
// 两个面板各有自己的页脚（种子数据：签到 2 条 / 自动任务 1 条）。挂掉的那一块必须
// 连页脚一起收起来 —— 取不到时总数就是 0，「共 0 条」挂在「加载失败」下面等于自己
// 打自己；而另一块的 1 条必须照常留着（否则就是「整块都没了」而不是「如实收起」）。
step(!/共 2 条/.test(text),
     '挂掉的那一块连页脚一起收起来了（它自己的 2 条不该露面）',
     (text.match(/共 \d+ 条/g) || ['（没有页脚）']).join(' / '));
step(/共 1 条/.test(text), '另一块（自动任务）照常带着自己的页脚');
step(!/暂无签到记录/.test(text), '也不出现「共 0 条」式的空态文案');
step(!/数据加载失败/.test(text), '只有一块挂 → 不升级成整页错误');
await page.screenshot({path: `${OUT}/05-tasks-one-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(!/加载失败/.test(text) && /共 2 条/.test(text) && /共 1 条/.test(text),
     '恢复后两块内容与页脚都回来');

// ══ 密钥页 ══════════════════════════════════════════════════════════
console.log('\n密钥页（PR #100）');
MODE.value = 5;
await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '全部取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
// 这一句是这一页最要紧的一条：密钥是**凭据**，说「暂无 API 密钥」读起来是
// 「我的密钥被删了」，用户会顺手点旁边的「新建密钥」重建一个 —— 于是建出重复密钥，
// 而重复密钥会让「按密钥限额 / 按密钥统计用量」全都对不上。
step(!/暂无 API 密钥/.test(text),
     '不许说「暂无 API 密钥」（那是「确实没有」，而事实是不知道有没有）',
     (text.match(/暂无[^\n]{0,12}/) || ['（无）'])[0]);
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/06-keys-all-failed.png`, fullPage: true});

MODE.value = 6;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/部分数据加载失败/.test(text), '只密钥列表没取到时，顶部给常驻提示');
step(!/请检查网络连接或后端服务是否正常/.test(text),
     '上游列表取到了 → 不升级成整页错误（只有顶部那条常驻提示）');
step(!/暂无 API 密钥/.test(text),
     '列表这一份没取到 → 不说「暂无 API 密钥」',
     (text.match(/暂无[^\n]{0,12}/) || ['（无）'])[0]);
// 同一句谎话的另一半：空状态挡住了，tab 上却还挂着「普通密钥 · 0」——列表取不到时
// `keys` 就是空的，那个 0 没有依据，读起来仍是「一个密钥都没有」。（任务记录页的
// 「共 0 条」是同一处，PR #97 才补上。）
step(!/普通密钥 · 0/.test(text),
     'tab 上的数字也一起收起来（否则等于说「一个密钥都没有」）',
     (text.match(/普通密钥[^\n]{0,8}/) || ['（无）'])[0]);
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/07-keys-list-failed.png`, fullPage: true});

MODE.value = 7;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/部分数据加载失败/.test(text), '只上游列表没取到时，同样给常驻提示');
step(/暂无 API 密钥/.test(text),
     '密钥列表这一份**取到了且确实为空** → 照常显示空态（判据是「这一份失败没」，'
     + '不是「有任一份失败」）');
step(/普通密钥 · 0/.test(text),
     '数字也照常显示 0 —— 这一份确实取到了、确实为空，那个 0 是如实的',
     (text.match(/普通密钥[^\n]{0,8}/) || ['（无）'])[0]);
step(!/请检查网络连接或后端服务是否正常/.test(text), '仍然不升级成整页错误');
step(phaseErrors().length === 0, '这一步没有前端异常', phaseErrors().join(' | '));
await page.screenshot({path: `${OUT}/08-keys-upstreams-failed.png`, fullPage: true});

// 恢复要**回到这一页**再断言。原来这一步只写「reload 当前页」——它跟在最后一节
// 后面，于是后面的批次往脚本末尾追加新页面时，这条「恢复后……」就会在**别的页面**
// 上求值：`/settings` 上当然找不到「暂无 API 密钥」，断言恒真、形同没有。
MODE.value = 0;
await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(/暂无 API 密钥/.test(text) && !/数据加载失败/.test(text),
     '恢复后回到正常的空列表');

// ══ 设置页 ══════════════════════════════════════════════════════════
console.log('\n设置页（批次 1 收尾：配置没取到时不拿默认值冒充）');
MODE.value = 8;
await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '配置取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
// 判据用**文本**而不是 `[role=tab]` 计数：页面底部那张「菜单栏引导」提示卡自己
// 也带 2 个 role=tab（圆点指示器与箭头），按计数会把它算进来，永远不为 0。
const settingsTabs = () => page.locator('[role=tab]').filter({hasText: '上游配置'}).count();
step((await settingsTabs()) === 0,
     '一个 Tab 都不渲染（那张表单填的是内置默认值，不是「你现在的配置」）',
     `「上游配置」Tab 数：${await settingsTabs()}`);
step(!/暂无管理用户/.test(text), '不许说「暂无管理用户」');
await page.screenshot({path: `${OUT}/09-settings-all-failed.png`, fullPage: true});

MODE.value = 9;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step((await settingsTabs()) > 0,
     '配置取到了 → 表单照常渲染（正常态没被搞坏）');
step(!/数据加载失败/.test(text), '只有用户列表挂 → 不升级成整页错误');
await page.locator('[role=tab]').filter({hasText: '管理用户'}).click().catch(() => {});
await page.waitForTimeout(800);
text = await bodyText();
step(!/暂无管理用户/.test(text),
     '用户列表挂了：不说「暂无管理用户」（那等于说系统里一个账号都没有）',
     (text.match(/暂无[^\n]{0,12}/) || ['（无）'])[0]);
await page.screenshot({path: `${OUT}/10-settings-users-failed.png`, fullPage: true});

MODE.value = 0;
await page.goto(`${BASE}/settings`, {waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step((await settingsTabs()) > 0 && !/数据加载失败/.test(text),
     '恢复后设置页正常');

// ══ 加载期：三页都要出骨架，且都不能先摆空态 ═══════════════════════
// 骨架的选择器用 `[data-slot=skeleton]`（ui/skeleton.tsx 上的稳定标记），
// 不用 `.animate-pulse`：后者页面上别的地方也可能用（如按钮图标在转圈）。
console.log('\n批次 1 收尾三页（加载期）');
MODE.value = 14;                       // 三页的接口都延迟 3.5s
const skeletonCount = () => page.locator('[data-slot=skeleton]').count();

await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(1200);       // 数据还在路上
text = await bodyText();
step(!/暂无账号/.test(text), '账号管理：加载期不说「暂无账号」（看起来像号池是空的）',
     (text.match(/暂无[^\n]{0,10}/) || ['（无）'])[0]);
step((await skeletonCount()) > 0, '账号管理：加载期渲染骨架',
     `骨架条数：${await skeletonCount()}`);
await page.screenshot({path: `${OUT}/11-accounts-loading.png`, fullPage: true});

await page.goto(`${BASE}/models`, {waitUntil: 'load'});
await page.waitForTimeout(1200);
text = await bodyText();
step(!/暂无模型/.test(text), '模型中心：加载期不说「暂无模型」');
step((await skeletonCount()) > 0, '模型中心：加载期渲染骨架',
     `骨架条数：${await skeletonCount()}`);
await page.screenshot({path: `${OUT}/12-models-loading.png`, fullPage: true});

await page.goto(`${BASE}/red-packets`, {waitUntil: 'load'});
await page.waitForTimeout(1200);
text = await bodyText();
step(!/还没有红包/.test(text), '红包：加载期不说「还没有红包」');
step((await skeletonCount()) > 0, '红包：加载期渲染骨架',
     `骨架条数：${await skeletonCount()}`);
await page.screenshot({path: `${OUT}/13-redpackets-loading.png`, fullPage: true});

// ══ 账号管理：主数据挂 / 只配件挂 ══════════════════════════════════
console.log('\n账号管理（批次 1 收尾：账号池取不到时不拿空列表冒充）');
/** 分组切换条上的按钮。**它不依赖账号池**，所以账号池挂掉时它必须还在 */
const groupChips = () => page.locator('button').filter({hasText: '默认分组'}).count();

MODE.value = 10;
await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '账号池取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/暂无账号/.test(text), '不说「暂无账号」');
step((await groupChips()) > 0,
     '分组切换条仍在（账号池挂了也得能切回默认分组自救，所以守卫不能早返回整页）',
     `「默认分组」按钮数：${await groupChips()}`);
await page.screenshot({path: `${OUT}/14-accounts-pool-failed.png`, fullPage: true});

// 只把上游状态打挂：账号列表是好的，必须照常渲染。这一条同时是 P0-2 的回归
// ——原实现 `if (upRes.status === 'fulfilled')` 后面**没有 else**，上游状态挂了
// 界面上一点痕迹都没有，每个号的状态看起来还跟正常一样。
MODE.value = 11;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(!/数据加载失败/.test(text), '只上游状态挂 → 不升级成整页错误（账号列表照常）');
step(/上游状态没取到/.test(text), '顶部如实说明「上游状态没取到」（原来这里是完全无声的）');
step(/状态未知/.test(text), '账号状态标成「未知」而不是「未加载」');
step(!/暂无账号/.test(text), '也不落进「暂无账号」');
await page.screenshot({path: `${OUT}/15-accounts-status-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(!/数据加载失败/.test(text) && !/上游状态没取到/.test(text) && !/暂无账号/.test(text),
     '恢复后账号页正常');

// ══ 模型中心 ══════════════════════════════════════════════════════
console.log('\n模型中心（批次 1 收尾）');
MODE.value = 12;
await page.goto(`${BASE}/models`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '模型目录取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/暂无模型/.test(text), '不说「暂无模型」（那等于说腾讯那边没有可用模型）');
step(!/没有匹配/.test(text), '也不落进「没有匹配的模型」（筛选结果为空的前提是清单已取到）');
await page.screenshot({path: `${OUT}/16-models-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(!/数据加载失败/.test(text) && /glm-5\.2/.test(text),
     '恢复后模型中心正常（模型行真的渲染出来，不只是错误态消失）');

// ══ 红包 ══════════════════════════════════════════════════════════
console.log('\n红包（批次 1 收尾）');
MODE.value = 13;
await page.goto(`${BASE}/red-packets`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
step(/数据加载失败/.test(text), '红包列表取不到时给整页错误态');
step(/重试/.test(text), '错误态里有「重试」');
step(!/还没有红包/.test(text), '不说「还没有红包」（那等于说红包发完了 / 被清了）');
await page.screenshot({path: `${OUT}/17-redpackets-failed.png`, fullPage: true});

MODE.value = 0;
await page.reload({waitUntil: 'load'});
await page.waitForTimeout(3000);
text = await bodyText();
step(!/数据加载失败/.test(text), '恢复后红包页正常');

// ══ 日志页筛选：改完即生效（批次 3 修 P1-4）══════════════════════════
//
// 这一段的判据是**请求次数**，不是页面上的一句话——「筛选改完生效了吗」在界面上
// 看不出来（列表看起来一样，只是内容不同）。所以先记一份每个接口被打了多少次。
console.log('\n日志页筛选（批次 3：同一排控件不再有两种脾气）');
MODE.value = 0;
await page.goto(`${BASE}/logs`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
const logsHits = () => hits.logs ?? 0;

step((await page.getByRole('button', {name: '重置'}).count()) > 0,
     '按钮从「查询」换成了「重置」（改完即生效，再留一个查询按钮只会把同一份查询再打一次）');

// ① 下拉类：改完立刻重查，**不用点任何按钮**
{
  const before = logsHits();
  // ⚠️ 必须把范围限在筛选区（`section`）里再数：页面顶部还有一个语言选择器，
  // 它是**第 0 个** combobox。按全页 `.nth(2)` 会点到「密钥」而不是「状态」——
  // 而失败的样子特别误导：那个菜单能正常打开，只是里面没有「失败」这个选项，
  // 于是断言在「等一个不存在的选项」上超时，看起来像下拉坏了。
  await page.locator('section [data-slot=select-trigger]').nth(2).click();   // 状态
  await page.getByRole('option', {name: '失败'}).click();
  await page.waitForTimeout(1500);
  step(logsHits() > before,
       '改状态筛选后立刻重查（原来只在「查询」按钮里生效，且在第 1 页时点了没反应）',
       `请求数 ${before} → ${logsHits()}`);
}

// ② 文本类：输入 4 个字符只发 1 次请求（400ms 防抖）。
//    这里必须逐字符输入：`fill()` 一次就填完，只产生一个 input 事件，
//    测不出防抖——那样这条断言是**空转**的（不防抖也会通过）。
{
  const box = page.locator('section input').nth(0);          // 模型
  const before = logsHits();
  await box.pressSequentially('kimi', {delay: 40});
  // 先确认字符真的进去了：若上一步的菜单没关干净，Radix 的焦点陷阱会把按键
  // 吃在浮层里，输入框仍是空的 —— 那时「只发了 1 次请求」会因为「压根没输入」
  // 而通过，断言变成空转。
  step((await box.inputValue()) === 'kimi', '关键词真的输进了输入框',
       `输入框里是 ${JSON.stringify(await box.inputValue())}`);
  await page.waitForTimeout(1500);
  const delta = logsHits() - before;
  step(delta === 1, '文本筛选输入 4 个字符只发 1 次请求（400ms 防抖）',
       `请求数 +${delta}（不防抖会是 +4）`);
}

// ③ 重置：一键清掉全部筛选，且只重取一次
{
  const box = page.locator('section input').nth(0);
  const before = logsHits();
  await page.getByRole('button', {name: '重置'}).click();
  await page.waitForTimeout(1200);
  step((await box.inputValue()) === '', '「重置」清掉了输入框里的关键词');
  step(logsHits() - before === 1,
       '重置只重取一次（文本类若走防抖计时器，会先按「旧文本 + 新下拉」查一次、再查一次）',
       `请求数 +${logsHits() - before}`);
}
await page.screenshot({path: `${OUT}/13-logs-filters.png`, fullPage: true});

// ══ 账号管理检索与状态分组（批次 3 修 P1-3）══════════════════════════
console.log('\n账号管理检索与状态分组（批次 3）');
MODE.value = 0;
await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(3500);
text = await bodyText();
// 4 组状态**摆在明面上**：原先 9 档只挂在徽章的 hover 提示里，移动端没有 hover，
// 等于状态信息不可见。
step(['可用', '需处理', '冷却中', '已停用'].every((s) => text.includes(s)),
     '4 组状态筛选摆在明面上（不再只靠悬浮提示）');

// 状态筛选**真的在筛**：fixture 里只有一个「可用」的账号，点「需处理」必然筛空。
// 这条比「按钮在不在」有用得多——四个按钮都加上去而列表仍然渲染全量，是最容易
// 「看起来做完、其实没用」的形态（源码层那条断言盯的就是这个）。
await page.getByRole('button', {name: '需处理'}).click();
await page.waitForTimeout(800);
text = await bodyText();
step(/没有匹配的账号/.test(text),
     '按状态筛空时说「没有匹配的账号」（状态筛选真的在筛，不是装饰）');
step(!/暂无账号/.test(text), '不说「暂无账号」（账号在池子里，只是被筛选挡住了）');
step(/清除筛选/.test(text), '给了「清除筛选」的出口（不用自己回忆刚才改了什么）');
await page.screenshot({path: `${OUT}/14-accounts-no-match.png`, fullPage: true});

await page.getByRole('button', {name: '清除筛选'}).click();
await page.waitForTimeout(800);
text = await bodyText();
step(!/没有匹配的账号/.test(text), '清除筛选后账号回来（正常态没被搞坏）');

// 搜索一个必然不存在的关键词：同上，但走的是搜索那条路
await page.locator('input[placeholder*="搜索昵称"]').fill('zzz-不存在的账号');
await page.waitForTimeout(800);
text = await bodyText();
step(/没有匹配的账号/.test(text), '搜不到时说「没有匹配的账号」');
step(!/暂无账号/.test(text), '不说「暂无账号」（搜索词不匹配 ≠ 没有账号）');
await page.getByRole('button', {name: '清除筛选'}).click();
await page.waitForTimeout(800);

// 切到国际版：池子里只有国内版账号 → 这是**版本**筛空，不是「一个账号都没有」
await page.getByRole('tab', {name: '国际版'}).first().click();
await page.waitForTimeout(1500);
text = await bodyText();
step(/国际版没有账号/.test(text),
     '切到没有账号的版本时如实说「国际版没有账号」（原来这里说「暂无账号」，读起来像号池是空的）');
step(!/暂无账号/.test(text), '不说「暂无账号」（账号就在池子里，只是不属于这个版本）');
await page.screenshot({path: `${OUT}/15-accounts-realm-empty.png`, fullPage: true});

// 切回国内版：后面的断言与人工复看都默认是国内版
await page.getByRole('tab', {name: '国内版'}).first().click();
await page.waitForTimeout(800);

step(errors.length === 0, '全程没有未捕获的前端异常', errors.slice(0, 2).join(' | '));

await browser.close();
console.log('\n=== 结果 ===');
if (findings.length) {
  console.log('FAILED:\n  - ' + findings.join('\n  - '));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
