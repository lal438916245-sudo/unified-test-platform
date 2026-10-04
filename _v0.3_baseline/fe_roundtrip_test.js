/* eslint-disable */
/**
 * 前端「旧计划 载入 → 渲染 → 不修改 → 保存」往返保真测试台。
 *
 * 与"手搓步骤对象"不同：真实调用 index.html 的 renderPlanEditor 渲染表单，
 * 再从**渲染出的 HTML**里解析各控件值（模拟用户什么都没改），随后调用 plPayload()
 * 得到待保存体，最后与数据库里的 steps 原文逐字节比对。
 *
 * 用法： node fe_roundtrip_test.js [fe_input.json 路径]
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = process.env.PLATFORM_ROOT || path.resolve(__dirname, '..');
const INPUT_PATH = process.argv[2] || path.join(ROOT, '_v0.3_baseline', 'fe_input.json');
const RAW = JSON.parse(fs.readFileSync(INPUT_PATH, 'utf8'));
const HTML = fs.readFileSync(path.join(ROOT, 'frontend', 'index.html'), 'utf8');
const SCRIPT = HTML.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];

// 输入文件里的计划（id<9000）全部按"真实计划"对待，对加载→保存做逐字节比对；
// id≥9000 的合成用例在 JS 里定义，避免文本转义环节损坏反斜杠（也避免与内置用例撞号）。
const REAL = RAW.plans.filter((p) => p.id < 9000);
const SYN = [
  {
    id: 9001, name: 'SYN-unmanaged', description: '', environment_id: 1, executor_id: 1,
    asset_source_id: 0, fail_fast: true, owner: 'local', revision: 1,
    created_at: '-', updated_at: '-',
    steps: [{
      engine: 'pytest', name: 's', requires_exclusive: false,
      params: { timeout_sec: 60, cwd: 'X:\\probe\\dir', args: ['a.py'], asset_source_id: 7 },
    }],
  },
  {
    id: 9002, name: 'SYN-missing-defaults', description: '', environment_id: 1, executor_id: 1,
    asset_source_id: 0, fail_fast: true, owner: 'local', revision: 1,
    created_at: '-', updated_at: '-',
    steps: [
      { engine: 'locust', name: 'l', requires_exclusive: false,
        params: { locustfile: 'http-fixture' } },
      { engine: 'matcheval', name: 'm', requires_exclusive: false,
        params: { dataset: 'fixture-small', algorithm: ['tpl', 'orb'] } },
    ],
  },
  {
    id: 9003, name: 'SYN-explicit-nondefault', description: '', environment_id: 1, executor_id: 1,
    asset_source_id: 0, fail_fast: true, owner: 'local', revision: 1,
    created_at: '-', updated_at: '-',
    steps: [
      { engine: 'locust', name: 'l', requires_exclusive: false,
        params: { locustfile: 'http-fixture' } },
      { engine: 'matcheval', name: 'm', requires_exclusive: false,
        params: { dataset: 'fixture-small' } },
    ],
  },
];
const PLANS = REAL.concat(SYN);

let pass = 0;
let fail = 0;
const failures = [];
function ck(name, ok, detail) {
  if (ok) { pass++; console.log('  ✓ ' + name); }
  else { fail++; failures.push(name); console.log('  ✗ ' + name + (detail ? '\n      ' + detail : '')); }
}

function unesc(s) {
  return String(s).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

function parseAttrs(body) {
  const a = {};
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*"([^"]*)")?/g;
  let m;
  while ((m = re.exec(body)) !== null) {
    const k = m[1].toLowerCase();
    if (k === 'input' || k === 'select' || k === 'option' || k === 'label') continue;
    a[k] = m[2] === undefined ? '' : unesc(m[2]);
  }
  return a;
}

/** 渲染出的 HTML → 「控件 id → 值 / 勾选态」，模拟浏览器默认选中行为 */
function parseControls(src) {
  const vals = {}; const checks = {}; const algs = {};
  let m;
  const selRe = /<select\b([^>]*)>([\s\S]*?)<\/select>/gi;
  while ((m = selRe.exec(src)) !== null) {
    const sa = parseAttrs(m[1]);
    if (!sa.id) continue;
    const optRe = /<option\b([^>]*)>([\s\S]*?)<\/option>/gi;
    let om; let first = null; let sel = null;
    while ((om = optRe.exec(m[2])) !== null) {
      const oa = parseAttrs(om[1]);
      const v = 'value' in oa ? oa.value : unesc(om[2]);
      if (first === null) first = v;
      if ('selected' in oa) sel = v;
    }
    vals[sa.id] = sel !== null ? sel : (first !== null ? first : '');
  }
  const inRe = /<input\b([^>]*?)\/?>/gi;
  while ((m = inRe.exec(src)) !== null) {
    const ia = parseAttrs(m[1]);
    const alg = ia.class && /pl-alg-(\d+)/.exec(ia.class);
    if (alg) {
      const idx = Number(alg[1]);
      if ('checked' in ia) (algs[idx] = algs[idx] || []).push(ia.value);
      continue;
    }
    if (!ia.id) continue;
    if ((ia.type || '').toLowerCase() === 'checkbox') checks[ia.id] = 'checked' in ia;
    else vals[ia.id] = 'value' in ia ? ia.value : '';
  }
  return { vals, checks, algs };
}

/**
 * 一次「载入 → 渲染 → (可选用户改动) → 保存」。
 * override: { 控件id: 值 }，值为 {checked:true/false} 表示复选框；[] 表示取消全部算法勾选
 */
function roundTrip(plan, override) {
  let appHTML = '';
  const appEl = {};
  Object.defineProperty(appEl, 'innerHTML', {
    get() { return appHTML; }, set(v) { appHTML = String(v); },
  });
  const dom = { vals: {}, checks: {}, algs: {} };

  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    document: {
      getElementById(id) {
        if (id === 'app') return appEl;
        return {
          get value() { return dom.vals[id] !== undefined ? dom.vals[id] : ''; },
          get checked() { return !!dom.checks[id]; },
        };
      },
      querySelectorAll(sel) {
        const mm = /\.pl-alg-(\d+):checked/.exec(sel);
        if (mm) return (dom.algs[Number(mm[1])] || []).map((v) => ({ value: v }));
        return [];
      },
      addEventListener() {},
    },
    window: { addEventListener() {} },
    location: { hash: '' },
    sessionStorage: { getItem: () => null, setItem: () => {} },
    alert() {}, confirm: () => true,
    fetch: () => Promise.reject(new Error('no-net')),
    URLSearchParams, JSON, Math, Date, Number, String, Object, Array, Boolean,
    isNaN, parseInt, parseFloat, encodeURIComponent,
  };
  vm.createContext(sandbox);
  vm.runInContext(SCRIPT, sandbox, { filename: 'index.html.js' });

  sandbox.j = async (p) => {
    if (p === '/api/environments') return RAW.envs;
    if (p === '/api/executors') return RAW.execs;
    if (p === '/api/asset-sources') return RAW.assets;
    if (p.indexOf('/api/test-cases') === 0) return [];
    if (p.indexOf('/api/suites') === 0) return [];
    if (p === '/api/config/catalog') return RAW.catalog;
    const mm = /^\/api\/plans\/(\d+)$/.exec(p);
    if (mm) {
      const found = PLANS.filter((x) => String(x.id) === mm[1])[0];
      if (!found) throw new Error('no plan ' + mm[1]);
      return found;
    }
    throw new Error('unexpected path ' + p);
  };

  return vm.runInContext(`renderPlanEditor(${JSON.stringify(String(plan.id))})`, sandbox)
    .then(() => {
      const parsed = parseControls(appHTML);
      dom.vals = parsed.vals; dom.checks = parsed.checks; dom.algs = parsed.algs;
      // 模拟用户在界面上的改动
      if (override) {
        Object.keys(override).forEach((k) => {
          const v = override[k];
          if (v && typeof v === 'object' && 'checked' in v) dom.checks[k] = !!v.checked;
          else if (Array.isArray(v)) dom.algs[Number(/pl-alg-(\d+)/.exec(k)[1])] = v;
          else dom.vals[k] = String(v);
        });
      }
      return vm.runInContext('plPayload()', sandbox);
    });
}

(async () => {
  console.log('==============================================================');
  console.log(' A. 输入计划：GET → 编辑器载入 → 不修改 → 保存 → 逐字节比对');
  console.log('==============================================================');
  for (const p of REAL) {
    const payload = await roundTrip(p);
    const got = JSON.stringify(payload.steps);
    const want = JSON.stringify(p.steps);
    const nLoc = p.steps.filter((s) => s.engine === 'locust').length;
    ck(`plan#${p.id} steps 逐字节一致（${p.steps.length} 步${nLoc ? ' / 含 ' + nLoc + ' 个 locust' : ''}）`,
      got === want, got === want ? '' : `\n      原: ${want}\n      新: ${got}`);
  }

  console.log('\n==============================================================');
  console.log(' B. 输入中每个 Locust 步骤 params：键集合 / 键序 / 值');
  console.log('==============================================================');
  const locBases = [];
  for (const p of REAL) for (const s of p.steps) if (s.engine === 'locust') locBases.push({ plan: p.id, step: s });
  ck(`输入含 Locust 步骤（实测 ${locBases.length} 个，应 ≥1）`, locBases.length >= 1);
  for (const item of locBases) {
    const plan = REAL.filter((p) => p.id === item.plan)[0];
    const payload = await roundTrip(plan);
    const idx = plan.steps.indexOf(item.step);
    const gotP = payload.steps[idx].params;
    const wantP = item.step.params;
    const wantKeys = Object.keys(wantP);
    const gotKeys = Object.keys(gotP);
    ck(`plan#${item.plan} locust params 键集合一致`, JSON.stringify(gotKeys) === JSON.stringify(wantKeys),
      `原=${JSON.stringify(wantKeys)} 新=${JSON.stringify(gotKeys)}`);
    ck(`plan#${item.plan} locust params 键序一致（timeout_sec 未被前移）`,
      JSON.stringify(gotKeys) === JSON.stringify(wantKeys));
    ck(`plan#${item.plan} locust params 值一致`, JSON.stringify(gotP) === JSON.stringify(wantP),
      `原=${JSON.stringify(wantP)} 新=${JSON.stringify(gotP)}`);
    ck(`plan#${item.plan} 未破坏 asset_source_id 键态（本步本无 → 保持无）`,
      ('asset_source_id' in wantP) === ('asset_source_id' in gotP));
  }

  console.log('\n==============================================================');
  console.log(' C. 表单不管理的键必须保留（cwd 绝对路径 + asset_source_id）');
  console.log('==============================================================');
  const syn1 = SYN[0];
  {
    const payload = await roundTrip(syn1);
    const got = payload.steps[0].params;
    ck('未管理键 cwd 原样保留（含反斜杠）', got.cwd === 'X:\\probe\\dir', JSON.stringify(got));
    ck('未管理键 asset_source_id 原样保留（=7）', got.asset_source_id === 7, JSON.stringify(got));
    ck('SYN-unmanaged 键序一致',
      JSON.stringify(Object.keys(got)) === JSON.stringify(Object.keys(syn1.steps[0].params)),
      `原=${JSON.stringify(Object.keys(syn1.steps[0].params))} 新=${JSON.stringify(Object.keys(got))}`);
    ck('SYN-unmanaged steps 逐字节一致',
      JSON.stringify(payload.steps) === JSON.stringify(syn1.steps),
      `\n      原: ${JSON.stringify(syn1.steps)}\n      新: ${JSON.stringify(payload.steps)}`);
  }

  console.log('\n==============================================================');
  console.log(' D. 原步骤缺失的键 → 用户未改动时不得被凭空注入默认值');
  console.log('==============================================================');
  const syn2 = SYN[1];
  {
    const payload = await roundTrip(syn2);
    const l = payload.steps[0].params;
    const m = payload.steps[1].params;
    ck('locust 原无 timeout_sec → 不注入', !('timeout_sec' in l), JSON.stringify(l));
    ck('locust 原无 users → 不注入', !('users' in l), JSON.stringify(l));
    ck('locust 原无 spawn_rate → 不注入', !('spawn_rate' in l), JSON.stringify(l));
    ck('locust 原无 run_time → 不注入', !('run_time' in l), JSON.stringify(l));
    ck('locust 原无 csv_full_history → 不注入', !('csv_full_history' in l), JSON.stringify(l));
    ck('matcheval 原无 timeout_sec → 不注入', !('timeout_sec' in m), JSON.stringify(m));
    ck('matcheval 原无 threshold → 不注入', !('threshold' in m), JSON.stringify(m));
    ck('matcheval 原无 output_level → 不注入', !('output_level' in m), JSON.stringify(m));
    ck('matcheval 已声明 algorithm 顺序保留',
      JSON.stringify(m.algorithm) === JSON.stringify(['tpl', 'orb']), JSON.stringify(m.algorithm));
    ck('SYN-missing-defaults steps 逐字节一致',
      JSON.stringify(payload.steps) === JSON.stringify(syn2.steps),
      `\n      原: ${JSON.stringify(syn2.steps)}\n      新: ${JSON.stringify(payload.steps)}`);
  }

  console.log('\n==============================================================');
  console.log(' E. 正向对照：用户显式改成非默认值 → 必须写入');
  console.log('==============================================================');
  const syn3 = SYN[2];
  {
    const payload = await roundTrip(syn3, { 'pl-s0-timeout': '500', 'pl-s1-threshold': '0.9' });
    const l = payload.steps[0].params;
    const m = payload.steps[1].params;
    ck('locust 显式设 timeout_sec=500 → 写入', l.timeout_sec === 500, JSON.stringify(l));
    ck('matcheval 显式设 threshold=0.9 → 写入', m.threshold === 0.9, JSON.stringify(m));
    ck('正向对照不引入其它未声明键（locust 仅多 timeout_sec）',
      Object.keys(l).sort().join(',') === 'locustfile,timeout_sec', JSON.stringify(Object.keys(l)));
    ck('正向对照不引入其它未声明键（matcheval 仅多 threshold）',
      Object.keys(m).sort().join(',') === 'dataset,threshold', JSON.stringify(Object.keys(m)));
  }
  {
    // 清空可清空的键（args / cwd_root / locustfile）→ 应删除
    const plan = {
      id: 9100, name: 'SYN-clear', description: '', environment_id: 1, executor_id: 1,
      asset_source_id: 0, fail_fast: true, owner: 'local', revision: 1,
      created_at: '-', updated_at: '-',
      steps: [
        { engine: 'pytest', name: 'p', requires_exclusive: false,
          params: { timeout_sec: 60, cwd_root: 'backend-demo', args: ['a.py'] } },
        { engine: 'locust', name: 'l', requires_exclusive: false,
          params: { locustfile: 'http-fixture', users: 5 } },
      ],
    };
    PLANS.push(plan);
    const payload = await roundTrip(plan, {
      'pl-s0-args': '', 'pl-s0-cwd-root': '', 'pl-s1-locustfile': '',
    });
    const p0 = payload.steps[0].params;
    const p1 = payload.steps[1].params;
    ck('清空 args → 键被删除', !('args' in p0), JSON.stringify(p0));
    ck('清空 cwd_root → 键被删除', !('cwd_root' in p0), JSON.stringify(p0));
    ck('清空 locustfile → 键被删除', !('locustfile' in p1), JSON.stringify(p1));
    ck('清空后仍保留未改动键（timeout_sec / users）',
      p0.timeout_sec === 60 && p1.users === 5, JSON.stringify(p0) + ' ' + JSON.stringify(p1));
  }

  console.log('\n==============================================================');
  console.log(` 结果: ${pass} passed, ${fail} failed`);
  if (fail) console.log(' 失败项: ' + JSON.stringify(failures, null, 1));
  console.log('==============================================================');
  process.exit(fail ? 1 : 0);
})();
