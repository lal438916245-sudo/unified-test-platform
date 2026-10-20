/* Exercise real editor rendering/collection with browser-like first-ID lookup. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const script = fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8').match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
let appHTML = '';
let duplicates = [];
const unesc = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
function attrs(src) {
  const out = {};
  for (const m of src.matchAll(/([\w:-]+)(?:\s*=\s*"([^"]*)")?/g)) out[m[1]] = m[2] === undefined ? true : unesc(m[2]);
  return out;
}
function options(html) {
  let first, selected;
  for (const m of html.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/gi)) {
    const a = attrs(m[1]), value = a.value ?? unesc(m[2]);
    if (first === undefined) first = value;
    if (a.selected) selected = value;
  }
  return selected ?? first ?? '';
}
function renderDOM(html) {
  appHTML = html; elements.clear(); duplicates = [];
  // Preserve DOM order: getElementById returns the first matching element.
  const controls = [...html.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>|<input\b([^>]*?)>/gi)];
  for (const m of controls) {
    const a = attrs(m[1] ?? m[3]);
    if (!a.id) continue;
    if (elements.has(a.id)) { duplicates.push(a.id); continue; }
    const el = {value: m[1] !== undefined ? options(m[2]) : (a.value ?? ''), checked: !!a.checked};
    Object.defineProperty(el, 'innerHTML', {set(v) { this.value = options(v); }, get() { return ''; }});
    elements.set(a.id, el);
  }
}
const app = {};
Object.defineProperty(app, 'innerHTML', {get: () => appHTML, set: renderDOM});
const sandbox = {
  console, document: {getElementById: id => id === 'app' ? app : elements.get(id) || null,
    querySelectorAll: () => [], addEventListener() {}},
  window: {addEventListener() {}}, location: {hash: ''},
  sessionStorage: {getItem: () => null, setItem() {}},
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  fetch: () => Promise.reject(Error('No network in editor regression')),
  alert() {}, confirm: () => true, URLSearchParams,
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
sandbox.fixtureInput = input;
vm.runInContext(`
  plCatalog=fixtureInput.catalog;
  plRefs=fixtureInput.refs;
  plForm={id:null,revision:1,data:{name:'reference',description:'',environment_id:fixtureInput.refs.envs[0].id,
    executor_id:fixtureInput.refs.execs[0].id,asset_source_id:0,fail_fast:true,steps:[]}};
`, sandbox);
const run = s => vm.runInContext(s, sandbox);
const plain = value => JSON.parse(JSON.stringify(value));
// Use the actual Suite insertion path, not a hand-built inline step.
elements.set('pl-suite-pick', {value:String(input.refs.suites[0].id)});
run('plAddSuiteSteps()');
assert.deepEqual(duplicates, [], 'All rendered control IDs must be unique');
let body = plain(run('plPayload()'));
assert.equal(body.steps.length, 1);
assert.equal(body.steps[0].case_ref.id, input.refs.cases[0].id);
assert.deepEqual(body.steps[0].override_params, {}, 'Unchanged reference must not invent script overrides');
const refId = `pl-s0-case-ref`;
assert.equal(elements.get(refId).value, String(input.refs.cases[0].id));
assert.equal(elements.get('pl-s0-case').value, input.refs.cases[0].params.case);
// Reusable requirements survive unchanged saves and can be overridden/cleared.
const originalRequirement = input.refs.cases[0].params.requirements || '';
assert.equal(elements.get('pl-s0-requirements').value, originalRequirement);
elements.get('pl-s0-requirements').value = 'unity-common';
body = plain(run('plPayload()'));
assert.equal(body.steps[0].override_params.requirements, 'unity-common');
elements.get('pl-s0-requirements').value = '';
body = plain(run('plPayload()'));
if (originalRequirement) assert.equal(body.steps[0].override_params.requirements, '');
else assert.ok(!('requirements' in body.steps[0].override_params));
elements.get('pl-s0-requirements').value = originalRequirement;
// A script change must affect only the script parameter, never the TestCase ID.
elements.get('pl-s0-case').value = 'test_cases/test_full_link_loop.py';
body = plain(run('plPayload()'));
assert.equal(body.steps[0].case_ref.id, input.refs.cases[0].id);
assert.equal(body.steps[0].override_params.case, 'test_cases/test_full_link_loop.py');
assert.equal(elements.get(refId).value, String(input.refs.cases[0].id));
// Changing suite must replace only the script options, preserving the reference.
elements.get('pl-s0-suite').value = 'unity-smoke';
run("plUIChange('0')");
assert.equal(elements.get('pl-s0-case').value, 'test_unity_smoke.py');
assert.equal(elements.get(refId).value, String(input.refs.cases[0].id));
// Render a second indexed reference and verify that an unchanged save is stable.
run(`plForm.data.steps=[0,1].map(()=>({kind:'case',case_ref:{id:fixtureInput.refs.cases[0].id,
 revision:fixtureInput.refs.cases[0].revision,fingerprint:fixtureInput.refs.cases[0].fingerprint},
 override_params:{},params:{},name:fixtureInput.refs.cases[0].name,requires_exclusive:true}));plRenderEditor()`);
assert.deepEqual(duplicates, []);
body = plain(run('plPayload()'));
assert.ok(body.steps.every(s => Object.keys(s.override_params).length === 0));
const checksHTML=run(`runtimeChecksHtml([
 {name:'dependency',status:'failed',detail:'<secret>',next_step:'install'},
 {name:'window',status:'not_checked'}, {name:'poco',status:'busy'},
 {name:'mongo',status:'passed'}, {name:'gameview',status:'not_applicable'}])`);
for(const label of ['失败','未检查','资源占用','通过','不适用'])assert.ok(checksHTML.includes(label));
assert.ok(checksHTML.includes('&lt;secret&gt;')&&!checksHTML.includes('<secret>'));
fs.writeFileSync(process.argv[3], JSON.stringify(body));
console.log('UI reference rendering, Suite insertion, script overrides and DOM ID uniqueness passed');
