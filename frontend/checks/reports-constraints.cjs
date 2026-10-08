// Run from frontend: node checks/reports-constraints.cjs. No API/model calls.
const assert = require('node:assert/strict');
const path = require('node:path');
const vm = require('node:vm');
const {build} = require('rolldown');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

(async () => {
  const bundle = await build({
    input: path.resolve(__dirname, '../src/components/Reports.tsx'),
    external: ['react', 'react/jsx-runtime'],
    transform: {define: {'import.meta.env.VITE_API_BASE_URL': '""'}},
    output: {format: 'cjs'},
    write: false,
    logLevel: 'silent',
  });
  const output = bundle.output.find(item => item.type === 'chunk');
  assert.ok(output, 'Reports bundle must exist');
  const module = {exports: {}};
  vm.runInNewContext(output.code, {module, exports: module.exports, require, URLSearchParams, Date, Number, Set, console}, {filename: 'reports-test.cjs'});
  const {buildReportQuery, datesFor, reportMatches, Reports, RatingComparison, downtimeShare} = module.exports;
  const master = {id: 'master', role: 'master', full_name: 'Мастер'};
  const worker = {id: 'own-worker', role: 'worker', full_name: 'Исполнитель'};
  const draft = {mode: 'custom', from: '2026-10-08T00:00', to: '2026-10-09T00:00', shift_id: '', section_id: '', equipment_id: '', employee_id: '', brigade_id: ''};
  let checked = 0;
  function check(name, fn) { fn(); checked++; console.log(`PASS ${name}`); }
  check('UTC+5 crosses the UTC calendar boundary exactly', () => {
    const result = buildReportQuery(draft, master);
    assert.equal(result.error, null);
    const params = new URLSearchParams(result.query);
    assert.equal(params.get('from_at'), '2026-10-07T19:00:00.000Z');
    assert.equal(params.get('to_at'), '2026-10-08T19:00:00.000Z');
  });
  check('shift and dates are mutually exclusive', () => {
    const params = new URLSearchParams(buildReportQuery({...draft, mode: 'shift', shift_id: 'shift-1', from: '', to: '', section_id: 'section-1'}, master).query);
    assert.equal(params.get('shift_id'), 'shift-1');
    assert.equal(params.has('from_at'), false);
    assert.equal(params.has('to_at'), false);
    assert.equal(params.get('section_id'), 'section-1');
  });
  check('worker scope overrides foreign employee and removes brigade', () => {
    const params = new URLSearchParams(buildReportQuery({...draft, employee_id: 'foreign-worker', brigade_id: 'foreign-brigade'}, worker).query);
    assert.equal(params.get('employee_id'), worker.id);
    assert.equal(params.has('brigade_id'), false);
  });
  check('missing shift is rejected', () => assert.ok(buildReportQuery({...draft, mode: 'shift'}, master).error));
  check('equal or reversed dates are rejected', () => {
    assert.ok(buildReportQuery({...draft, to: draft.from}, master).error);
    assert.ok(buildReportQuery({...draft, to: '2026-10-01T00:00'}, master).error);
  });
  check('invalid calendar date is rejected instead of normalised', () => assert.ok(buildReportQuery({...draft, from: '2026-02-30T00:00'}, master).error));
  check('arbitrary periods longer than a year remain supported', () => assert.equal(buildReportQuery({...draft, from: '2024-01-01T00:00', to: '2026-10-08T00:00'}, master).error, null));
  check('presets use Kostanay dates including Monday and month boundaries', () => {
    const now = new Date('2026-10-07T20:00:00Z');
    assert.equal(datesFor('day', now).from, '2026-10-08T00:00');
    assert.equal(datesFor('week', now).from, '2026-10-05T00:00');
    assert.equal(datesFor('week', now).to, '2026-10-12T00:00');
    assert.equal(datesFor('month', now).from, '2026-10-01T00:00');
    assert.equal(datesFor('month', now).to, '2026-11-01T00:00');
  });
  check('mixed role or old filter report cannot be displayed for worker', () => {
    const query = buildReportQuery(draft, worker).query;
    const packet = {scope: 'own', filters: {employee_id: worker.id}, period: {from_at: '2026-10-07T19:00:00Z', to_at: '2026-10-08T19:00:00Z'}};
    assert.equal(reportMatches(packet, query, worker), true);
    assert.equal(reportMatches({...packet, scope: 'enterprise'}, query, worker), false);
    assert.equal(reportMatches({...packet, filters: {employee_id: 'foreign-worker'}}, query, worker), false);
    assert.equal(reportMatches({...packet, filters: {...packet.filters, equipment_id: 'old-filter'}}, query, worker), false);
    assert.equal(reportMatches({...packet, period: {...packet.period, from_at: '2026-10-06T19:00:00Z'}}, query, worker), false);
  });
  check('role UI hides enterprise people and brigade filters for worker', () => {
    const own = renderToStaticMarkup(React.createElement(Reports, {user: worker, onOpenOrder() {}}));
    const enterprise = renderToStaticMarkup(React.createElement(Reports, {user: master, onOpenOrder() {}}));
    assert.ok(own.includes('Мои отчёты'));
    assert.ok(!own.includes('Все исполнители'));
    assert.ok(!own.includes('Все бригады'));
    assert.ok(enterprise.includes('Все исполнители'));
    assert.ok(enterprise.includes('Все бригады'));
  });
  const rating = {score: 80, earned_points: 68, available_weight: 85, provisional: true, counts: {closed: 6}, components: {quality: {weight: 40, ratio: 0.8, points: 32}, on_time: {weight: 25, ratio: 0.8, points: 20}, first_pass: {weight: 15, ratio: null, points: null}, complexity: {weight: 15, ratio: 0.8, points: 12}, acceptance: {weight: 5, ratio: 0.8, points: 4}}};
  check('manager rating table has score and all five components, plus brigade graph', () => {
    for (const role of ['master', 'leader', 'admin']) {
      const html = renderToStaticMarkup(React.createElement(RatingComparison, {user: {...master, role}, kind: 'brigades', rows: [{...rating, brigade_id: 'team-1', brigade_name: 'Ремонтная бригада'}]}));
      assert.ok(html.includes('<table>'));
      for (const label of ['Баллы / 100', 'Q · Качество / 40', 'T · В срок / 25', 'F · С первого раза / 15', 'C · Объём работы / 15', 'A · Приём заданий / 5']) assert.ok(html.includes(label), `missing ${label} for ${role}`);
      assert.ok(html.includes('aria-label="Баллы бригад"'));
      assert.ok(html.includes('Предварительно'));
      assert.ok(html.includes('Ремонтная бригада'));
    }
  });
  check('worker cannot render brigade graph or a foreign rating row', () => {
    const brigades = renderToStaticMarkup(React.createElement(RatingComparison, {user: worker, kind: 'brigades', rows: [{...rating, brigade_id: 'team-1', brigade_name: 'Чужая бригада'}]}));
    assert.equal(brigades, '');
    const own = renderToStaticMarkup(React.createElement(RatingComparison, {user: worker, kind: 'employees', rows: [{...rating, employee_id: worker.id, employee_name: 'Моя строка'}, {...rating, employee_id: 'foreign-worker', employee_name: 'Чужая оценка'}]}));
    assert.ok(own.includes('Моя строка'));
    assert.ok(!own.includes('Чужая оценка'));
    assert.ok(!own.includes('Баллы бригад'));
  });
  check('missing ratings and components remain dashes, not manufactured zeros', () => {
    const components = Object.fromEntries(Object.entries(rating.components).map(([key, item]) => [key, {...item, points: null, ratio: null}]));
    const html = renderToStaticMarkup(React.createElement(RatingComparison, {user: worker, kind: 'employees', rows: [{...rating, employee_id: worker.id, employee_name: 'Моя строка', score: null, available_weight: 0, earned_points: 0, components}]}));
    assert.ok(html.includes('<strong>—</strong>'));
    assert.equal((html.match(/<td>—<\/td>/g) || []).length, 5);
    assert.ok(html.includes('Недостаточно данных'));
  });
  check('downtime percentage uses actual backend ratio and preserves unknown values', () => {
    assert.equal(downtimeShare(0), '0%');
    assert.equal(downtimeShare(0.25), '25%');
    assert.equal(downtimeShare(1), '100%');
    assert.equal(downtimeShare(null), '—');
    assert.equal(downtimeShare(undefined), '—');
    assert.equal(downtimeShare(NaN), '—');
  });
  console.log(`${checked} report constraints passed; no network/model calls.`);
})().catch(error => {console.error(error); process.exitCode = 1;});
