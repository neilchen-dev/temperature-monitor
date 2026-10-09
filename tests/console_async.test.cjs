// Run with node tests/console_async.test.cjs; no DOM or external requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../static/console.html'), 'utf8');
const source = html.slice(html.indexOf('  var trendRequestId = 0;'), html.indexOf('  function renderTrend('));
const pending = [], rendered = [];
const state = { trendSelection: { device_id: 'A', source: 'ha' }, trendWindow: 'day', key: 'key' };
const els = { trendMeta: {textContent:''}, tempChartBox: {}, humChartBox: {} };
const context = { state, els, WINDOW_MS: {day:86400000}, Date, encodeURIComponent,
  apiGet: url => new Promise((resolve, reject) => pending.push({url, resolve, reject})),
  renderTrend: items => rendered.push({device: state.trendSelection.device_id, items}),
  chartFallback: () => {}
};
vm.createContext(context);
vm.runInContext(source, context);
(async () => {
  const first = context.loadTrend();
  state.trendSelection = { device_id:'B', source:'ha' };
  const second = context.loadTrend();
  pending[1].resolve({ok:true, data:{count:1, items:[{temperature:24}]}});
  await second;
  pending[0].resolve({ok:true, data:{count:1, items:[{temperature:40}]}});
  await first;
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].device, 'B');
  const fallback = context.loadTrend();
  pending[2].resolve({ok:true, data:{count:0}});
  await Promise.resolve();
  pending[3].reject(new Error('injected fallback failure'));
  await fallback;
  assert.equal(els.trendMeta.textContent, '趋势加载失败：网络错误');
  const staleFallback = context.loadTrend();
  pending[4].resolve({ok:true, data:{count:0}});
  await Promise.resolve();
  state.trendSelection = {device_id:'C', source:'ha'};
  const latest = context.loadTrend();
  pending[6].resolve({ok:true, data:{count:1, items:[{temperature:25}]}});
  await latest;
  pending[5].reject(new Error('old fallback failure'));
  await staleFallback;
  assert.equal(rendered.at(-1).device, 'C');
  assert.notEqual(els.trendMeta.textContent, '趋势加载失败：网络错误');
  console.log('Trend request ordering and fallback error tests passed');
})().catch(error => { console.error(error); process.exitCode=1; });
