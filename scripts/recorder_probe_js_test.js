/**
 * v3.6.0 —— 在 Node 里「真实执行」录制器注入页面的探测脚本。
 *
 * 为什么要有它：探测逻辑（pre 快照 / 动作后 probe / 元素回读 / 反馈捕获）是本次
 * 改动里风险最高的部分——它跑在真实网页的 JS 上下文里，一旦抛错会静默吞掉，
 * 表现为「录制器忽然录不到事件」，排查成本极高。本脚本用一层极简 DOM 桩把
 * RECORDER_JS 跑起来，确定性地驱动合成事件与定时器，从而在无浏览器、无网络、
 * 无 GPU 的情况下验证这段逻辑。
 *
 * 跑法： node recorder_probe_js_test.js <recorder_js_path>
 * 退出码：0 = 全过；1 = 有失败
 */

'use strict';
const fs = require('fs');

const jsPath = process.argv[2];
if (!jsPath) {
  console.error('用法: node recorder_probe_js_test.js <recorder_js_path>');
  process.exit(2);
}
const RECORDER_JS = fs.readFileSync(jsPath, 'utf8');

let PASS = 0, FAIL = 0;
function ok(name, cond, extra) {
  if (cond) { PASS++; console.log('  OK   ' + name + (extra ? '   [' + extra + ']' : '')); }
  else { FAIL++; console.log('  FAIL ' + name + (extra ? '   [' + extra + ']' : '')); }
}

// --------------------------------------------------------------------------
// 极简 DOM 桩
// --------------------------------------------------------------------------
const captured = [];          // apcRecord 收到的原始 JSON 字符串
const listeners = {};         // type -> [fn]
let timers = [];              // {fn, ms, seq}
let seq = 0;
const qsMap = {};             // selector -> [element]

function mkEl(tag, opts) {
  opts = opts || {};
  const el = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    id: opts.id || '',
    name: opts.name || '',
    value: opts.value !== undefined ? opts.value : '',
    innerText: opts.innerText || '',
    textContent: opts.innerText || '',
    placeholder: opts.placeholder || '',
    type: opts.type || '',
    isConnected: true,
    children: [],
    parentNode: null,
    _attrs: opts.attrs || {},
    getAttribute(k) { return this._attrs[k] !== undefined ? this._attrs[k] : null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 120, height: 24 }; },
    dispatchEvent() { return true; },
  };
  return el;
}

function setQuery(sel, els) { qsMap[sel] = els || []; }

globalThis.window = (function () { const w = { top: null }; w.top = w; return w; })();
globalThis.location = { href: 'http://erp.local/order/list' };
globalThis.document = {
  title: '订单列表',
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  querySelectorAll(sel) { return qsMap[sel] || []; },
  contains(el) { return !!(el && el.isConnected); },
};
globalThis.getComputedStyle = () => ({ display: 'block', visibility: 'visible', opacity: '1' });
globalThis.setTimeout = (fn, ms) => { timers.push({ fn: fn, ms: ms || 0, seq: seq++ }); return seq; };
globalThis.clearTimeout = (id) => {
  timers = timers.filter((t) => t.seq !== id - 1);
};
globalThis.apcRecord = (s) => { captured.push(s); };

function fire(type, ev) {
  (listeners[type] || []).forEach((fn) => fn(ev));
}

/** 触发所有 ms <= limit 的定时器（升序），不依赖真实时间。 */
function runTimers(limit) {
  const due = timers.filter((t) => t.ms <= limit).sort((a, b) => a.ms - b.ms || a.seq - b.seq);
  timers = timers.filter((t) => t.ms > limit);
  due.forEach((t) => t.fn());
  return due.length;
}

function reset() {
  captured.length = 0;
  timers = [];
  Object.keys(qsMap).forEach((k) => delete qsMap[k]);
  Object.keys(listeners).forEach((k) => delete listeners[k]);
  globalThis.window.__apcRec = false;
  globalThis.location.href = 'http://erp.local/order/list';
  globalThis.document.title = '订单列表';
}

function events() { return captured.map((s) => JSON.parse(s)); }
function byType(t) { return events().filter((e) => e.type === t); }

console.log('='.repeat(74));
console.log('注入脚本真实执行验证（DOM 桩 + 合成事件）');
console.log('='.repeat(74));
console.log('\n--- 装载 RECORDER_JS ---');

let loadErr = null;
try {
  reset();
  eval(RECORDER_JS);
} catch (e) { loadErr = e; }
ok('JS1 注入脚本可无错执行', loadErr === null, loadErr ? String(loadErr) : '');
if (loadErr) { console.log('\n汇总\n  通过 ' + PASS + ' 项，失败 ' + FAIL + ' 项'); process.exit(1); }

ok('JS2 装载即上报首个导航事件',
  byType('navigate').length === 1 && byType('navigate')[0].initial === true);
ok('JS3 事件带 url 与 ts',
  !!byType('navigate')[0].url && typeof byType('navigate')[0].ts === 'number');
ok('JS4 顶层框架 frame 为空（非 iframe）', byType('navigate')[0].frame === null);
ok('JS5 已注册鼠标/键盘/输入监听',
  ['mousedown', 'mouseup', 'mousemove', 'change', 'keydown'].every((k) => listeners[k]));

// --------------------------------------------------------------------------
console.log('\n--- A. 点击：动作前快照 + 动作后探测 ---');
reset();
eval(RECORDER_JS);
captured.length = 0;

const btn = mkEl('button', { id: 'save', innerText: '保存' });
setQuery('.el-message', []);
fire('mousedown', { button: 0, clientX: 10, clientY: 10, target: btn });
fire('mouseup', { button: 0, clientX: 11, clientY: 11, target: btn });

let clicks = byType('click');
ok('A1 合成点击被捕获', clicks.length === 1, 'click=' + clicks.length);
ok('A2 点击事件带 pre 快照', clicks.length === 1 && !!clicks[0].pre);
if (clicks.length) {
  const pre = clicks[0].pre;
  ok('A3 pre 快照含 feedback/dialogs/loading 三类',
    Array.isArray(pre.feedback) && Array.isArray(pre.dialogs) && Array.isArray(pre.loading),
    Object.keys(pre).join(','));
  ok('A4 动作前无反馈元素时 pre.feedback 为空', pre.feedback.length === 0);
}
ok('A5 点击后已排定两次探测（700/1800）', timers.length === 2,
  timers.map((t) => t.ms).join('/'));

// 模拟「点完后出现 toast」
setQuery('.el-message', [mkEl('div', { id: 'msg1', innerText: '保存成功' })]);
runTimers(700);

let probes = byType('probe');
ok('A6 700ms 探测到货', probes.length === 1, 'probe=' + probes.length);
if (probes.length) {
  const p = probes[0];
  ok('A7 探测带 for_ts 回指动作事件', p.for_ts === clicks[0].ts,
    'for_ts=' + p.for_ts + ' click.ts=' + clicks[0].ts);
  ok('A8 探测 delay 标记为 700', p.delay === 700, String(p.delay));
  ok('A9 探测带 post 快照', !!p.post && Array.isArray(p.post.feedback));
  ok('A10 探测捕获到新出现的反馈文案',
    p.post.feedback.some((f) => f.text === '保存成功'),
    JSON.stringify(p.post.feedback));
  ok('A11 反馈元素带可回放的选择器',
    p.post.feedback.length > 0 && !!p.post.feedback[0].selector,
    p.post.feedback.length ? p.post.feedback[0].selector : '');
  ok('A12 探测带当前 URL', p.url === 'http://erp.local/order/list', p.url);
  ok('A13 探测带页面标题', p.title === '订单列表', p.title);
}

// 第二次探测
runTimers(1800);
probes = byType('probe');
ok('A14 1800ms 第二次探测到货', probes.length === 2, 'probe=' + probes.length);
ok('A15 两次探测的 delay 分别为 700/1800',
  probes.map((p) => p.delay).join(',') === '700,1800',
  probes.map((p) => p.delay).join(','));
ok('A16 探测完成后清理了待探测元素（不泄漏）',
  Object.keys(globalThis.window).indexOf('__apcProbeLeftover') === -1);

// --------------------------------------------------------------------------
console.log('\n--- B. 输入：回读字段值 ---');
reset();
eval(RECORDER_JS);
captured.length = 0;

const input = mkEl('input', { id: 'cust', value: '', name: 'customerCode' });
input.value = 'C001';
input.isConnected = true;
fire('change', { target: input });

let changes = byType('change');
ok('B1 输入被捕获且带值', changes.length === 1 && changes[0].value === 'C001',
  JSON.stringify(changes[0] || {}));
ok('B2 输入事件带 pre 快照', !!changes[0].pre);
runTimers(700);
probes = byType('probe');
ok('B3 输入后探测到货', probes.length === 1);
ok('B4 探测回读到字段值（抓「输入没进去」）',
  probes.length === 1 && probes[0].acted_value === 'C001',
  probes.length ? JSON.stringify(probes[0].acted_value) : '');
ok('B5 探测标记元素仍存在',
  probes.length === 1 && probes[0].acted_exists === true);

// 元素被移除（页面跳走）时也不该抛错。
// 注意：不能清 timers —— 700ms 那次已消费，这里要跑的是排好队的 1800ms 那次。
input.isConnected = false;
runTimers(1800);
probes = byType('probe');
ok('B6 元素已销毁时不抛错且如实标记 acted_exists=false',
  probes.length === 2 && probes[1].acted_exists === false,
  'probe=' + probes.length + ' acted_exists=' + (probes.length ? probes[probes.length - 1].acted_exists : null));

// --------------------------------------------------------------------------
console.log('\n--- C. 弹窗 / 加载遮罩 ---');
reset();
eval(RECORDER_JS);
captured.length = 0;

const btn2 = mkEl('button', { id: 'del' });
setQuery('.ant-modal-content', []);
setQuery('.el-loading-mask', [mkEl('div', { id: 'mask' })]);
fire('mousedown', { button: 0, clientX: 1, clientY: 1, target: btn2 });
fire('mouseup', { button: 0, clientX: 1, clientY: 1, target: btn2 });

clicks = byType('click');
ok('C1 动作前的加载遮罩被记进 pre', clicks[0].pre.loading.length === 1,
  JSON.stringify(clicks[0].pre.loading));

// 动作后遮罩消失、弹窗出现
setQuery('.el-loading-mask', []);
setQuery('.ant-modal-content', [mkEl('div', { id: 'dlg', innerText: '确认删除' })]);
runTimers(700);
probes = byType('probe');
ok('C2 探测捕获到弹窗出现',
  probes[0].post.dialogs.some((d) => d.selector === '#dlg'),
  JSON.stringify(probes[0].post.dialogs));
ok('C3 探测显示加载遮罩已消失', probes[0].post.loading.length === 0);

// --------------------------------------------------------------------------
console.log('\n--- D. 键盘 / 悬停 / 不猜 ---');
reset();
eval(RECORDER_JS);
captured.length = 0;

const input2 = mkEl('input', { id: 'kw', value: '' });
globalThis.document.activeElement = input2;
fire('keydown', { key: 'Enter', ctrlKey: false, shiftKey: false, altKey: false, metaKey: false });
let keys = byType('keys');
ok('D1 特殊键被捕获', keys.length === 1 && keys[0].keys[0] === 'Enter',
  JSON.stringify(keys.map((k) => k.keys)));
ok('D2 按键事件带 pre 快照（可推断跳转/反馈）', !!keys[0].pre);
runTimers(700);
ok('D3 按键后探测到货', byType('probe').length === 1);

// 组合键
captured.length = 0; timers = [];
fire('keydown', { key: 'c', ctrlKey: true, shiftKey: false, altKey: false, metaKey: false });
keys = byType('keys');
ok('D4 组合键还原为 Control+c',
  keys.length === 1 && keys[0].keys.join('+') === 'Control+c',
  JSON.stringify(keys.length ? keys[0].keys : null));

// 普通字符不产生 keys 事件（由 change 负责）
captured.length = 0; timers = [];
fire('keydown', { key: 'a', ctrlKey: false, shiftKey: false, altKey: false, metaKey: false });
ok('D5 普通字符不重复记为按键', byType('keys').length === 0);

// 悬停：要等 600ms 且不该排探测（悬停无结果语义）
captured.length = 0; timers = [];
const link = mkEl('a', { id: 'lnk', innerText: '更多' });
fire('mousemove', { target: link });
ok('D6 悬停不立即上报（需停留 >600ms）', captured.length === 0);
runTimers(600);
ok('D7 停留后上报 hover', byType('hover').length === 1);
ok('D8 悬停不排定动作后探测（省开销）', timers.filter((t) => t.ms === 700).length === 0);

// 拖拽 vs 点击的阈值
reset();
eval(RECORDER_JS);
captured.length = 0;
const src = mkEl('div', { id: 'card' });
const dst = mkEl('div', { id: 'trash' });
fire('mousedown', { button: 0, clientX: 0, clientY: 0, target: src });
fire('mouseup', { button: 0, clientX: 60, clientY: 0, target: dst });
ok('D9 位移 >12px 记为拖拽', byType('drag').length === 1);
ok('D10 拖拽也带 pre 快照', !!byType('drag')[0].pre);

// --------------------------------------------------------------------------
console.log('\n--- E. 健壮性 ---');
reset();
eval(RECORDER_JS);
captured.length = 0;

// querySelectorAll 抛错的选择器不应中断
globalThis.document.querySelectorAll = (sel) => {
  if (sel.indexOf('[class*=') === 0) throw new Error('bad selector');
  return qsMap[sel] || [];
};
const b3 = mkEl('button', { id: 'b3' });
setQuery('.el-message', [mkEl('div', { id: 'm3', innerText: '完成' })]);
fire('mousedown', { button: 0, clientX: 0, clientY: 0, target: b3 });
fire('mouseup', { button: 0, clientX: 0, clientY: 0, target: b3 });
ok('E1 坏选择器不中断录制', byType('click').length === 1);
runTimers(700);
probes = byType('probe');
ok('E2 坏选择器下探测仍能产出', probes.length === 1);
ok('E3 坏选择器不影响其它选择器采集',
  probes[0].post.feedback.some((f) => f.text === '完成'),
  JSON.stringify(probes[0].post.feedback));

// 扫描上限：给 100 个节点，采集数必须被压住
setQuery('.el-message', Array.from({ length: 100 },
  (_, i) => mkEl('div', { id: 'm' + i, innerText: '提示' + i })));
captured.length = 0; timers = [];
fire('mousedown', { button: 0, clientX: 0, clientY: 0, target: b3 });
fire('mouseup', { button: 0, clientX: 0, clientY: 0, target: b3 });
runTimers(700);
probes = byType('probe');
ok('E4 反馈采集有上限（默认最多 6 条）',
  probes[0].post.feedback.length <= 6, String(probes[0].post.feedback.length));

// 不可见元素不计入
globalThis.getComputedStyle = () => ({ display: 'none', visibility: 'visible', opacity: '1' });
captured.length = 0; timers = [];
fire('mousedown', { button: 0, clientX: 0, clientY: 0, target: b3 });
fire('mouseup', { button: 0, clientX: 0, clientY: 0, target: b3 });
runTimers(700);
probes = byType('probe');
ok('E5 不可见元素不采集（避免误判为提示）',
  probes[0].post.feedback.length === 0, String(probes[0].post.feedback.length));

// 页面标题/URL 变化能被探测带上
globalThis.getComputedStyle = () => ({ display: 'block', visibility: 'visible', opacity: '1' });
setQuery('.el-message', []);
globalThis.location.href = 'http://erp.local/order/detail/88';
globalThis.document.title = '订单详情';
captured.length = 0; timers = [];
fire('mousedown', { button: 0, clientX: 0, clientY: 0, target: b3 });
fire('mouseup', { button: 0, clientX: 0, clientY: 0, target: b3 });
runTimers(700);
probes = byType('probe');
ok('E6 探测带上变化后的 URL',
  probes[0].url === 'http://erp.local/order/detail/88', probes[0].url);
ok('E7 探测带上变化后的标题', probes[0].title === '订单详情', probes[0].title);

console.log('\n' + '='.repeat(74));
console.log('汇总');
console.log('='.repeat(74));
console.log('  通过 ' + PASS + ' 项，失败 ' + FAIL + ' 项');
console.log('  ' + (FAIL === 0 ? '✅ 注入脚本逻辑全部通过' : '❌ 有失败项'));
process.exit(FAIL === 0 ? 0 : 1);
