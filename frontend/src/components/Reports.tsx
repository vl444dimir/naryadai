import {useEffect, useMemo, useRef, useState, type FormEvent} from 'react';
import {api, ApiError} from '../lib/api';
import {useResource} from '../lib/hooks';
import type {CatalogItem, User} from '../lib/types';

type PeriodMode = 'day' | 'week' | 'month' | 'history' | 'custom' | 'shift';
type Tab = 'overview' | 'ratings' | 'materials' | 'downtime' | 'anomalies' | 'orders';
type ExportKind = 'shift' | 'ratings' | 'materials' | 'downtime' | 'anomalies';
type ComponentKey = 'quality' | 'on_time' | 'first_pass' | 'complexity' | 'acceptance';
type Items<T> = {items: T[]};
type ShiftOption = {id: string; name: string; starts_at: string; ends_at: string};
type EquipmentOption = CatalogItem & {section_id: string; inventory_number?: string};
type PersonOption = {id: string; full_name: string; role: string};
type Filters = {section_id: string; equipment_id: string; employee_id: string; brigade_id: string};
type Draft = Filters & {mode: PeriodMode; from: string; to: string; shift_id: string};
type Summary = {text: string; engine: string; model_status: string; model?: string | null};
type Component = {weight: number; ratio: number | null; points: number | null};
type Rating = {
  employee_id?: string; employee_name?: string; brigade_id?: string | null; brigade_name?: string;
  score: number | null; earned_points: number; available_weight: number; provisional: boolean;
  components: Record<ComponentKey, Component>; counts: Record<string, number>;
  quality_average: number | null; complexity: {completed_norm_minutes: number; assigned_norm_minutes: number};
  explanation: string; evidence_order_ids: string[];
};
type Workload = {employee_id: string; employee_name: string; assigned_orders: number; closed_orders: number; active_minutes: number; paused_minutes: number; in_selected_shift_roster: boolean | null};
type Downtime = {equipment_id: string; equipment_name: string; section_id: string | null; minutes: number; planned_minutes: number; unplanned_minutes: number; overlap_minutes: number; unplanned_share: number | null; causes: string[]; intervals: {started_at: string; ended_at: string}[]; evidence_order_ids: string[]};
type MaterialRow = {order_id: string; number: number; material_id: string; material_name: string; unit: string; quantity: number; norm_quantity: number | null; deviation: number | null; section_id: string; equipment_id: string; employee_id: string | null; brigade_id: string | null; submitted_at: string};
type MaterialTotal = {material_id: string; material_name: string; unit: string; quantity: number; order_ids: string[]};
type Finding = {kind: string; text: string; recommendation: string; engine: string; evidence_order_ids: string[]; count?: number; minutes?: number; quantity?: number; norm_quantity?: number; sample_size?: number; dimension?: string; entity_id?: string; assigned_order_count?: number};
type PeriodReport = {
  period: {from_at: string; to_at: string; as_of: string; shift_basis?: string | null};
  filters: Record<string, string | null>; scope: string; synthetic: boolean;
  roster: {shift_id: string | null; member_ids: string[]; includes_actual_activity_outside_roster: boolean};
  brigade_attribution: {unknown_execution_orders: number; estimates_used: boolean};
  counts: Record<string, number>; order_ids: string[]; overdue_order_ids: string[]; workload: Workload[];
  ratings: {employees: Rating[]; brigades: Rating[]; formula: string}; downtime: Downtime[];
  materials: {rows: MaterialRow[]; totals: MaterialTotal[]}; anomalies: Finding[]; summary: Summary;
};
type OrderMeta = {number: number | null; available: boolean; message?: string};
type Explanation = {period: PeriodReport['period']; filters: PeriodReport['filters']; scope: string; employee_id: string; rating: Rating; explanation: Summary};
type SummaryResponse = Pick<PeriodReport, 'period' | 'filters' | 'scope' | 'counts' | 'summary'>;
type ReportsProps = {user: User; onOpenOrder: (id: string) => void};

const OFFSET = 5 * 60 * 60 * 1000;
const COMPONENTS: {key: ComponentKey; label: string; symbol: string; weight: number}[] = [
  {key: 'quality', label: 'Качество', symbol: 'Q', weight: 40},
  {key: 'on_time', label: 'В срок', symbol: 'T', weight: 25},
  {key: 'first_pass', label: 'С первого раза', symbol: 'F', weight: 15},
  {key: 'complexity', label: 'Объём работы', symbol: 'C', weight: 15},
  {key: 'acceptance', label: 'Приём заданий', symbol: 'A', weight: 5},
];
const TABS: {id: Tab; label: string}[] = [{id: 'overview', label: 'Сводка'}, {id: 'ratings', label: 'Рейтинг'}, {id: 'materials', label: 'Материалы'}, {id: 'downtime', label: 'Простои'}, {id: 'anomalies', label: 'Проблемы'}, {id: 'orders', label: 'Наряды'}];
const EXPORTS: {id: ExportKind; label: string}[] = [{id: 'shift', label: 'Полный отчёт'}, {id: 'ratings', label: 'Рейтинг'}, {id: 'materials', label: 'Материалы'}, {id: 'downtime', label: 'Простои'}, {id: 'anomalies', label: 'Проблемы и основания'}];

function number(value: number | null | undefined, digits = 1): string {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('ru-RU', {maximumFractionDigits: digits});
}
function duration(value: number | null | undefined): string {
  return value == null ? '—' : value >= 60 ? `${number(value / 60)} ч` : `${number(value)} мин`;
}
function date(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return '—';
  return new Date(value).toLocaleString('ru-RU', {timeZone: 'Asia/Qyzylorda', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'});
}
function utcInput(value: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) return null;
  const parsed = new Date(`${value.length === 16 ? `${value}:00` : value}+05:00`);
  if (!Number.isFinite(parsed.getTime()) || new Date(parsed.getTime() + OFFSET).toISOString().slice(0, value.length) !== value) return null;
  return parsed.toISOString();
}
export function datesFor(mode: PeriodMode, now = new Date()): {from: string; to: string} {
  const local = new Date(now.getTime() + OFFSET);
  const day = new Date(Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate()));
  let start = day.getTime(), end = start + 86400000;
  if (mode === 'week') { start -= ((day.getUTCDay() + 6) % 7) * 86400000; end = start + 7 * 86400000; }
  if (mode === 'month') { start = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), 1); end = Date.UTC(day.getUTCFullYear(), day.getUTCMonth() + 1, 1); }
  if (mode === 'history') start -= 89 * 86400000;
  return {from: new Date(start).toISOString().slice(0, 16), to: new Date(end).toISOString().slice(0, 16)};
}
function initialDraft(user: User): Draft { return {...datesFor('day'), mode: 'day', shift_id: '', section_id: '', equipment_id: '', employee_id: user.role === 'worker' ? user.id : '', brigade_id: ''}; }

export function buildReportQuery(draft: Draft, user: Pick<User, 'id' | 'role'>): {query: string; error: string | null} {
  const params = new URLSearchParams();
  if (draft.mode === 'shift') {
    if (!draft.shift_id) return {query: '', error: 'Выберите смену или укажите период датами.'};
    params.set('shift_id', draft.shift_id);
  } else {
    const from = utcInput(draft.from), to = utcInput(draft.to);
    if (!from || !to || Date.parse(to) <= Date.parse(from)) return {query: '', error: 'Укажите начало и конец периода. Конец должен быть позже начала.'};
    params.set('from_at', from); params.set('to_at', to);
  }
  for (const key of ['section_id', 'equipment_id'] as const) if (draft[key]) params.set(key, draft[key]);
  const employee = user.role === 'worker' ? user.id : draft.employee_id;
  if (employee) params.set('employee_id', employee);
  if (user.role !== 'worker' && draft.brigade_id) params.set('brigade_id', draft.brigade_id);
  return {query: params.toString(), error: null};
}

export function reportMatches(report: Pick<PeriodReport, 'period' | 'filters' | 'scope'>, query: string, user: User): boolean {
  const params = new URLSearchParams(query);
  if (user.role === 'worker' && (report.scope !== 'own' || report.filters.employee_id !== user.id)) return false;
  for (const key of ['section_id', 'equipment_id', 'employee_id', 'brigade_id', 'shift_id']) if ((report.filters[key] || '') !== (params.get(key) || '')) return false;
  for (const key of ['from_at', 'to_at'] as const) if (params.has(key) && Date.parse(report.period[key]) !== Date.parse(params.get(key)!)) return false;
  return true;
}
function errorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 0) return 'Нет связи с сервером. Проверьте соединение и повторите.';
  return error instanceof Error ? error.message : 'Не удалось выполнить действие. Повторите позже.';
}
function engineLabel(summary: Summary): string {
  return summary.engine === 'llm' && summary.model_status === 'completed' ? 'Пояснение ИИ' : 'Сводка по расчётам';
}
function engineNotice(summary: Summary): string {
  if (summary.engine === 'llm' && summary.model_status === 'completed') return summary.model ? `Подготовлено моделью ${summary.model}. Числа взяты из отчёта.` : 'Подготовлено моделью по числам этого отчёта.';
  const statuses: Record<string, string> = {disabled: 'ИИ отключён. Показан расчёт системы.', not_requested: 'Модель ещё не вызывалась.', blocked_data_policy: 'Вызов ИИ для этих данных не разрешён. Показан расчёт системы.', failed: 'ИИ не смог подготовить проверяемое пояснение. Показан расчёт системы.', insufficient_data: 'Для пояснения ИИ пока недостаточно данных.'};
  return statuses[summary.model_status] || 'Модель не использована. Показан расчёт системы.';
}

function BarChart({title, rows, unit = 'мин', secondary = false, maximum}: {title: string; rows: {label: string; value: number; extra?: number}[]; unit?: string; secondary?: boolean; maximum?: number}) {
  const visible = rows.slice(0, 8), max = Math.max(1, maximum ?? Math.max(...visible.map(row => row.value + (row.extra || 0)))), height = 38 + visible.length * 42;
  if (!visible.length) return <p className="empty">Пока нет данных для графика.</p>;
  return <figure style={{margin: 0}}>
    <figcaption className="muted">{title}{rows.length > visible.length ? ' · первые 8' : ''}</figcaption>
    {secondary && <p className="muted"><span style={{color: '#0f766e'}}>● Работа</span> · <span style={{color: '#a16207'}}>● Ожидание</span></p>}
    <svg viewBox={`0 0 570 ${height}`} role="img" aria-label={title} style={{display: 'block', width: '100%', height: 'auto'}}>
      <title>{title}</title>
      {visible.map((row, index) => {
        const y = 15 + index * 42, width = row.value / max * 275, extra = (row.extra || 0) / max * 275;
        return <g key={`${row.label}-${index}`}><title>{`${row.label}: ${number(row.value)} ${unit}${secondary ? ` работы, ${number(row.extra || 0)} ${unit} ожидания` : ''}`}</title><text x="0" y={y + 16} fontSize="14" fill="currentColor">{row.label.length > 23 ? `${row.label.slice(0, 22)}…` : row.label}</text><rect x="188" y={y} width="275" height="24" rx="4" fill="#e2e8f0"/><rect x="188" y={y} width={width} height="24" rx="4" fill="#0f766e"/>{secondary && <rect x={188 + width} y={y} width={extra} height="24" fill="#d97706"/>}<text x="474" y={y + 16} fontSize="13" fill="currentColor">{number(row.value + (row.extra || 0))} {unit}</text></g>;
      })}
    </svg>
  </figure>;
}

export function RatingComparison({user, rows, kind}: {user: Pick<User, 'id' | 'role'>; rows: Rating[]; kind: 'employees' | 'brigades'}) {
  const worker = user.role === 'worker';
  if (kind === 'brigades' && !['master', 'leader', 'admin'].includes(user.role)) return null;
  const visible = kind === 'employees' && worker ? rows.filter(row => row.employee_id === user.id) : rows;
  const label = kind === 'brigades' ? 'Бригада' : 'Исполнитель';
  const name = (row: Rating) => kind === 'brigades' ? row.brigade_name || 'Историческая бригада неизвестна' : row.employee_name || 'Исполнитель';
  const graphTitle = kind === 'brigades' ? 'Баллы бригад' : 'Баллы исполнителей';
  return <section className="panel stack">
    <h2>{kind === 'brigades' ? 'Рейтинг бригад' : worker ? 'Моя оценка за период' : 'Рейтинг исполнителей'}</h2>
    {kind === 'employees' && <p className="muted">40Q + 25T + 15F + 15C + 5A. Качество и сроки считаются по закрытым нарядам. Для повторных отказов требуется полное наблюдение 7 дней и подтверждение мастера.</p>}
    <BarChart title={graphTitle} rows={visible.filter(row => row.score != null).map(row => ({label: name(row), value: row.score!}))} unit="баллов" maximum={100}/>
    <p className="muted">Шкала графика — 100 баллов. Предварительные оценки отмечены в таблице и карточках; отсутствующие показатели показаны прочерком.</p>
    {visible.length > 0 && <div className="reports-desktop table-wrap"><table>
      <caption style={{textAlign: 'left', padding: '12px 14px'}}>Баллы и составляющие рейтинга {kind === 'brigades' ? 'бригад' : 'исполнителей'}</caption>
      <thead><tr><th scope="col">{label}</th><th scope="col">Баллы / 100</th>{COMPONENTS.map(item => <th scope="col" key={item.key}>{item.symbol} · {item.label} / {item.weight}</th>)}<th scope="col">Закрыто</th><th scope="col">Доступный вес / 100</th><th scope="col">Статус оценки</th></tr></thead>
      <tbody>{visible.map((row, index) => <tr key={row.employee_id || row.brigade_id || `${kind}-${index}`}><th scope="row">{name(row)}</th><td><strong>{number(row.score, 2)}</strong></td>{COMPONENTS.map(item => <td key={item.key}>{number(row.components[item.key]?.points, 2)}</td>)}<td>{number(row.counts.closed, 0)}</td><td>{number(row.available_weight, 0)}</td><td>{row.score == null ? 'Недостаточно данных' : row.provisional ? 'Предварительно' : 'Все показатели учтены'}</td></tr>)}</tbody>
    </table></div>}
    {!visible.length && <p className="empty">За этот период нет данных для сравнительной оценки.</p>}
  </section>;
}

export function downtimeShare(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) || value < 0 || value > 1 ? '—' : `${number(value * 100)}%`;
}

function Evidence({ids, onOpen, cache, load, exportOrder, worker}: {ids: string[]; onOpen: (id: string) => void; cache: Record<string, OrderMeta>; load: (ids: string[]) => void; exportOrder: (id: string) => void; worker: boolean}) {
  const [open, setOpen] = useState(false), [limit, setLimit] = useState(8);
  const visible = ids.slice(0, limit);
  useEffect(() => { if (open) load(visible); }, [open, limit, ids.join('|'), load]);
  if (!ids.length) return <p className="muted">Наряды-основания ещё не появились.</p>;
  return <details open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Наряды-основания · {number(ids.length, 0)}</summary>
    {open && <div className="stack" style={{paddingTop: 10}}>{visible.map(id => {
      const meta = cache[id], disabled = meta?.available === false;
      return <div className="toolbar" key={id}>
        <button type="button" className="button secondary" onClick={() => onOpen(id)} disabled={disabled}>{meta?.number != null ? `Наряд №${meta.number}` : disabled ? 'Наряд недоступен' : 'Открыть наряд'}</button>
        {!disabled && <button type="button" className="button secondary" onClick={() => exportOrder(id)} aria-label={meta?.number ? `Excel наряда №${meta.number}` : 'Скачать отчёт по наряду'}>Excel</button>}
        {disabled && <span className="muted">{worker ? 'Доступ ограничен вашей ролью.' : meta.message || 'Доступ к этому наряду ограничен.'}</span>}
      </div>;
    })}{visible.length < ids.length && <button type="button" className="button secondary" onClick={() => setLimit(value => value + 8)}>Показать ещё {Math.min(8, ids.length - visible.length)}</button>}</div>}
  </details>;
}

function RatingDetails({row}: {row: Rating}) {
  const counts = row.counts;
  const basis: Record<ComponentKey, string> = {
    quality: `Средняя оценка ${number(row.quality_average, 2)} / 5 · оценено ${counts.scored || 0} из ${counts.closed || 0} закрытых`,
    on_time: `${counts.on_time || 0} из ${counts.closed || 0} закрытых сданы в срок`,
    first_pass: `${counts.first_pass_mature || 0} из ${counts.mature_7day || 0} с полным наблюдением 7 дней · ещё наблюдаем ${counts.pending_7day || 0}`,
    complexity: `${duration(row.complexity.completed_norm_minutes)} закрытых нормативных работ из ${duration(row.complexity.assigned_norm_minutes)} назначенных`,
    acceptance: `Необоснованных отказов ${counts.unjustified_rejections || 0} · назначено ${counts.assigned || 0} · не рассмотрено ${counts.unreviewed_rejections || 0}`,
  };
  return <div className="stack">
    {COMPONENTS.map(item => {
      const component = row.components[item.key];
      return <div key={item.key}>
        <div className="toolbar" style={{justifyContent: 'space-between'}}><strong>{item.symbol} · {item.label}</strong><span>{number(component?.points, 2)} / {component?.weight ?? item.weight}</span></div>
        <progress value={component?.points ?? 0} max={component?.weight ?? item.weight} style={{width: '100%', accentColor: '#0f766e'}} aria-label={`${item.label}: ${number(component?.points, 2)} из ${component?.weight ?? item.weight}`}/>
        <small className="muted">{component?.ratio == null ? 'Пока недостаточно данных. ' : `${number(component.ratio * 100)}%. `}{basis[item.key]}</small>
      </div>;
    })}
    <p className="muted">{row.explanation}</p>
    {row.provisional && <div className="notice">Предварительный балл: доступны показатели с весом {row.available_weight} из 100. Подтверждённые составляющие дали {number(row.earned_points, 2)} балла; показанная оценка пересчитана на доступный вес.</div>}
    <p className="muted">Доработок: {counts.rework || 0}. Подтверждённых повторных отказов: {counts.confirmed_repeat || 0}. Ожидание запчастей и совпадение событий сами по себе не доказывают вину человека.</p>
  </div>;
}

// A new identity or role gets a fresh catalogue, evidence cache and request state.
export function Reports(props: ReportsProps) {
  return <ReportsView key={`${props.user.id}|${props.user.role}`} {...props}/>;
}

function ReportsView({user, onOpenOrder}: ReportsProps) {
  const worker = user.role === 'worker';
  const [draft, setDraft] = useState<Draft>(() => initialDraft(user));
  const [applied, setApplied] = useState<Draft>(() => initialDraft(user));
  const [tab, setTab] = useState<Tab>('overview'), [exportKind, setExportKind] = useState<ExportKind>('shift');
  const [formError, setFormError] = useState<string | null>(null), [actionError, setActionError] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null), [summaryBusy, setSummaryBusy] = useState(false);
  const [explanations, setExplanations] = useState<Record<string, Summary>>({}), [explanationBusy, setExplanationBusy] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false), [cache, setCache] = useState<Record<string, OrderMeta>>({});
  const pendingMeta = useRef(new Set<string>()), cacheRef = useRef(cache), queryRef = useRef('');
  const [refresh, setRefresh] = useState(0);
  const sections = useResource<Items<CatalogItem>>('/catalog/sections?limit=500');
  const equipment = useResource<Items<EquipmentOption>>('/catalog/equipment?limit=500');
  const people = useResource<Items<PersonOption>>('/catalog/users');
  const brigades = useResource<Items<CatalogItem>>('/catalog/brigades?limit=500');
  const shifts = useResource<Items<ShiftOption>>('/catalog/shifts');
  const built = useMemo(() => buildReportQuery(applied, user), [applied, user.id, user.role]);
  const query = built.query;
  const resource = useResource<PeriodReport>(built.error ? null : `/reports/shift?${query}`, refresh);
  queryRef.current = `${user.id}|${user.role}|${refresh}|${query}`; cacheRef.current = cache;
  const report = resource.data && reportMatches(resource.data, query, user) ? resource.data : null;
  const sectionName = (id: string | null) => sections.data?.items.find(item => item.id === id)?.name || '—';
  const equipmentName = (id: string | null) => equipment.data?.items.find(item => item.id === id)?.name || '—';
  const personName = (id: string | null) => id === user.id ? user.full_name : people.data?.items.find(item => item.id === id)?.full_name || '—';
  const brigadeName = (id: string | null) => id ? brigades.data?.items.find(item => item.id === id)?.name || 'Бригада' : 'Историческая бригада неизвестна';
  function findingText(finding: Finding): string {
    if (finding.kind !== 'assignment_association') return finding.text;
    const id = finding.entity_id || '';
    let group = 'Группа назначенных работ';
    if (finding.dimension === 'shift_id') {
      const shift = shifts.data?.items.find(item => item.id === id);
      group = shift ? `Смена «${shift.name}» (${date(shift.starts_at)})` : 'Группа работ одной смены';
    } else if (finding.dimension === 'executor_id') group = id === user.id ? 'Ваши назначенные работы' : worker ? 'Группа назначенных работ' : `Исполнитель: ${personName(id)}`;
    else if (finding.dimension === 'brigade_id') group = `Бригада: ${brigadeName(id)}`;
    return `${group}: ${number(finding.count, 0)} из ${number(finding.sample_size, 0)} внеплановых нарядов. Всего назначено группе: ${number(finding.assigned_order_count, 0)}.`;
  }

  useEffect(() => { setDraft(initialDraft(user)); setApplied(initialDraft(user)); setCache({}); pendingMeta.current.clear(); }, [user.id, user.role]);
  useEffect(() => { setSummary(null); setExplanations({}); setActionError(null); }, [query, user.id, refresh]);

  const metadataLoader = useMemo(() => (ids: string[]) => {
    const key = `${user.id}|${user.role}|`;
    for (const id of ids) {
      if (cacheRef.current[id] || pendingMeta.current.has(id)) continue;
      pendingMeta.current.add(id);
      void api.get<{number: number}>(`/reports/orders/${encodeURIComponent(id)}`).then(result => {
        if (!queryRef.current.startsWith(key)) return;
        setCache(previous => ({...previous, [id]: {number: result.number, available: true}}));
      }).catch((error: unknown) => {
        if (!queryRef.current.startsWith(key)) return;
        if (error instanceof ApiError && (error.status === 403 || error.status === 404)) setCache(previous => ({...previous, [id]: {number: null, available: false}}));
      }).finally(() => pendingMeta.current.delete(id));
    }
  }, [user.id, user.role]);

  function apply(event: FormEvent) {
    event.preventDefault();
    const result = buildReportQuery(draft, user);
    setFormError(result.error);
    if (!result.error) { setApplied({...draft}); setRefresh(value => value + 1); }
  }
  function preset(mode: PeriodMode) {
    const activeShift = shifts.data?.items.find(item => Date.parse(item.starts_at) <= Date.now() && Date.now() < Date.parse(item.ends_at)) || shifts.data?.items[0];
    const next = {...draft, mode, ...(mode === 'custom' || mode === 'shift' ? {} : datesFor(mode)), shift_id: mode === 'shift' ? draft.shift_id || activeShift?.id || '' : ''};
    setDraft(next); setFormError(null);
    if (mode !== 'custom' && (mode !== 'shift' || next.shift_id)) { setApplied(next); setRefresh(value => value + 1); }
  }
  async function requestSummary() {
    if (!report || summaryBusy) return;
    const key = queryRef.current;
    setSummaryBusy(true); setActionError(null);
    try {
      const result = await api.post<SummaryResponse>(`/reports/summary?${query}`);
      if (key !== queryRef.current) return;
      if (!reportMatches(result, query, user) || Object.keys(report.counts).some(name => report.counts[name] !== result.counts[name])) {
        setActionError('Показатели изменились во время подготовки пояснения. Обновите отчёт и повторите запрос.'); return;
      }
      setSummary(result.summary);
    }
    catch (error) { if (key === queryRef.current) setActionError(errorText(error)); }
    finally { setSummaryBusy(false); }
  }
  async function requestExplanation(row: Rating) {
    if (!row.employee_id || explanationBusy || (worker && row.employee_id !== user.id)) return;
    const key = queryRef.current;
    setExplanationBusy(row.employee_id); setActionError(null);
    const params = new URLSearchParams(query); params.delete('employee_id');
    try {
      const result = await api.post<Explanation>(`/reports/ratings/${encodeURIComponent(row.employee_id)}/explanation?${params}`);
      if (key !== queryRef.current) return;
      const selectedQuery = new URLSearchParams(params); selectedQuery.set('employee_id', row.employee_id);
      const fields = ['score', 'earned_points', 'available_weight', 'quality_average', 'counts', 'components', 'complexity', 'provisional'] as const;
      if (result.employee_id !== row.employee_id || !reportMatches(result, selectedQuery.toString(), user) || fields.some(name => JSON.stringify(row[name]) !== JSON.stringify(result.rating[name]))) {
        setActionError('Оценка изменилась во время подготовки пояснения. Обновите отчёт и повторите запрос.'); return;
      }
      setExplanations(previous => ({...previous, [row.employee_id!]: result.explanation}));
    }
    catch (error) { if (key === queryRef.current) setActionError(errorText(error)); }
    finally { setExplanationBusy(null); }
  }
  async function downloadReport() {
    if (!report || downloading) return;
    setDownloading(true); setActionError(null);
    try { await api.download(`/reports/export.xlsx?${query}&report=${exportKind}`, `НарядAI-${exportKind}-${report.period.from_at.slice(0, 10)}.xlsx`); }
    catch (error) { setActionError(errorText(error)); }
    finally { setDownloading(false); }
  }
  async function exportOrder(id: string) {
    if (downloading || cache[id]?.available === false) return;
    setDownloading(true); setActionError(null);
    try { await api.download(`/reports/orders/${encodeURIComponent(id)}/export.xlsx`, cache[id]?.number ? `Наряд-${cache[id].number}.xlsx` : 'Отчёт-по-наряду.xlsx'); }
    catch (error) { setActionError(errorText(error)); }
    finally { setDownloading(false); }
  }
  const evidence = (ids: string[]) => <Evidence ids={ids} onOpen={onOpenOrder} cache={cache} load={metadataLoader} exportOrder={id => void exportOrder(id)} worker={worker}/>;
  const rows = report?.ratings.employees.filter(row => !worker || row.employee_id === user.id) || [];
  const workload = report?.workload.filter(row => !worker || row.employee_id === user.id) || [];
  const materialRows = report?.materials.rows.filter(row => !worker || row.employee_id === user.id) || [];
  const shownSummary = summary || report?.summary;
  const catalogErrors = [sections.error, equipment.error, people.error, brigades.error, shifts.error].filter(Boolean);
  const filteredEquipment = equipment.data?.items.filter(item => !draft.section_id || item.section_id === draft.section_id) || [];

  function ratingCard(row: Rating, key: string) {
    const explanation = row.employee_id ? explanations[row.employee_id] : null;
    return <article className="panel stack" key={key}>
      <div className="toolbar" style={{justifyContent: 'space-between'}}><h3 style={{margin: 0}}>{row.employee_name || row.brigade_name || 'Бригада'}</h3><strong style={{fontSize: 28}}>{row.score == null ? 'Нет оценки' : `${number(row.score)} / 100`}</strong></div>
      <div className="toolbar"><span className="badge">{row.provisional ? 'Предварительно' : 'Все показатели учтены'}</span><span className="muted">Закрыто {row.counts.closed || 0} · в срок {row.counts.on_time || 0} · качество {number(row.quality_average, 2)} / 5</span></div>
      <details open={worker}><summary>Из чего сложилась оценка</summary><RatingDetails row={row}/></details>
      {evidence(row.evidence_order_ids)}
      {row.employee_id && <div className="toolbar"><button type="button" className="button secondary" disabled={!!explanationBusy || downloading} onClick={() => void requestExplanation(row)}>{explanationBusy === row.employee_id ? 'Готовим пояснение…' : 'Запросить пояснение ИИ'}</button></div>}
      {explanation && <div className="notice"><strong>{engineLabel(explanation)}</strong><p>{explanation.text}</p><small>{engineNotice(explanation)}</small></div>}
    </article>;
  }

  return <div className="reports stack">
    <header className="toolbar" style={{justifyContent: 'space-between'}}><div><h1>{worker ? 'Мои отчёты' : 'Отчёты и качество работы'}</h1><p className="muted">Время Костаная · UTC+5. {worker ? 'Показаны только ваши результаты и действия.' : 'Факты смены, загрузка людей и подтверждённые результаты.'}</p></div><button type="button" className="button secondary" onClick={() => setRefresh(value => value + 1)} disabled={resource.loading}>Обновить данные</button></header>

    <form className="panel stack" onSubmit={apply}>
      <div className="toolbar" aria-label="Период отчёта">{([{mode: 'day', label: 'Сутки'}, {mode: 'week', label: 'Неделя'}, {mode: 'month', label: 'Месяц'}, {mode: 'history', label: '90 дней'}, {mode: 'shift', label: 'Смена'}, {mode: 'custom', label: 'Свои даты'}] as {mode: PeriodMode; label: string}[]).map(item => <button type="button" key={item.mode} className={`button ${draft.mode === item.mode ? 'primary' : 'secondary'}`} aria-pressed={draft.mode === item.mode} onClick={() => preset(item.mode)}>{item.label}</button>)}</div>
      <div className="form-grid">
        {draft.mode === 'shift' ? <label className="field">Смена<select value={draft.shift_id} required onChange={event => setDraft(previous => ({...previous, shift_id: event.target.value}))}><option value="">Выберите смену</option>{shifts.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {date(item.starts_at)}</option>)}</select></label> : <><label className="field">С начала<input type="datetime-local" value={draft.from} required onChange={event => setDraft(previous => ({...previous, from: event.target.value, mode: 'custom', shift_id: ''}))}/></label><label className="field">До момента<input type="datetime-local" value={draft.to} required onChange={event => setDraft(previous => ({...previous, to: event.target.value, mode: 'custom', shift_id: ''}))}/></label></>}
        <label className="field">Участок<select value={draft.section_id} onChange={event => setDraft(previous => ({...previous, section_id: event.target.value, equipment_id: ''}))}><option value="">Все участки</option>{sections.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label className="field">Оборудование<select value={draft.equipment_id} onChange={event => setDraft(previous => ({...previous, equipment_id: event.target.value}))}><option value="">Всё оборудование</option>{filteredEquipment.map(item => <option key={item.id} value={item.id}>{item.name}{item.inventory_number ? ` · ${item.inventory_number}` : ''}</option>)}</select></label>
        {!worker && <><label className="field">Исполнитель<select value={draft.employee_id} onChange={event => setDraft(previous => ({...previous, employee_id: event.target.value}))}><option value="">Все исполнители</option>{people.data?.items.map(item => <option key={item.id} value={item.id}>{item.full_name}</option>)}</select></label><label className="field">Бригада<select value={draft.brigade_id} onChange={event => setDraft(previous => ({...previous, brigade_id: event.target.value}))}><option value="">Все бригады</option>{brigades.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></>}
      </div>
      <div className="toolbar"><button className="button primary" type="submit" disabled={resource.loading}>Показать отчёт</button><button className="button secondary" type="button" onClick={() => {const next = initialDraft(user); setDraft(next); setApplied(next); setRefresh(value => value + 1); setFormError(null);}}>Сбросить фильтры</button></div>
      {draft.mode === 'shift' && !shifts.loading && !shifts.data?.items.length && <p className="notice">Доступных смен пока нет. Можно выбрать период датами.</p>}
      {formError && <p className="error" role="alert">{formError}</p>}
      {!!catalogErrors.length && <p className="notice">Некоторые списки не загрузились. {catalogErrors[0]}</p>}
    </form>

    {resource.error && <div className="error" role="alert">{resource.error}<div className="toolbar"><button type="button" className="button secondary" onClick={() => setRefresh(value => value + 1)}>Повторить загрузку</button></div></div>}
    {actionError && <p className="error" role="alert">{actionError}</p>}
    {resource.loading && <p className="notice" role="status">Собираем отчёт за выбранный период…</p>}
    {!resource.loading && !resource.error && !report && <p className="empty">Отчёт пока не загружен. Выберите период и нажмите «Показать отчёт».</p>}

    {report && <>
      <div className="panel stack">
        <div className="toolbar" style={{justifyContent: 'space-between'}}><div><strong>{date(report.period.from_at)} — {date(report.period.to_at)}</strong><p className="muted">Данные на {date(report.period.as_of)}. Конец периода не включается.</p></div>{report.synthetic && <span className="badge">Демонстрационные данные</span>}</div>
        <div className="toolbar"><label className="field">Что выгрузить<select value={exportKind} onChange={event => setExportKind(event.target.value as ExportKind)}>{EXPORTS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><button type="button" className="button secondary" onClick={() => void downloadReport()} disabled={downloading}>{downloading ? 'Готовим файл…' : 'Скачать Excel'}</button></div>
      </div>
      <nav className="toolbar" aria-label="Разделы отчёта">{TABS.map(item => <button type="button" key={item.id} className={`button ${tab === item.id ? 'primary' : 'secondary'}`} aria-pressed={tab === item.id} onClick={() => {setTab(item.id); setExportKind(item.id === 'overview' || item.id === 'orders' ? 'shift' : item.id);}}>{item.label}{item.id === 'anomalies' && report.anomalies.length ? ` · ${report.anomalies.length}` : ''}</button>)}</nav>

      {tab === 'overview' && <>
        <div className="metric-grid">{[{key: 'issued', label: 'Выдано'}, {key: 'completed', label: 'Закрыто мастером'}, {key: 'performed', label: 'Сдано результатов'}, {key: 'overdue', label: 'Просрочено'}, {key: 'rejected', label: 'Отклонено'}, {key: 'pending_approval', label: 'Ожидают приёмки'}, {key: 'in_progress', label: 'В работе'}, {key: 'closed_late', label: 'Закрыто после срока'}].map(item => <div className="metric" key={item.key}><span className="muted">{item.label}</span><strong style={{fontSize: 30, display: 'block'}}>{number(report.counts[item.key] || 0, 0)}</strong></div>)}</div>
        <div className="metric-grid"><div className="metric"><span className="muted">Подтверждённый простой</span><strong style={{fontSize: 28, display: 'block'}}>{duration(report.counts.equipment_downtime_minutes)}</strong></div><div className="metric"><span className="muted">Оборудование стоит на момент отчёта</span><strong style={{fontSize: 28, display: 'block'}}>{number(report.counts.equipment_in_downtime || 0, 0)}</strong></div></div>
        {shownSummary && <section className="panel stack"><div className="toolbar" style={{justifyContent: 'space-between'}}><h2 style={{margin: 0}}>{engineLabel(shownSummary)}</h2><button type="button" className="button secondary" onClick={() => void requestSummary()} disabled={summaryBusy}>{summaryBusy ? 'Готовим пояснение…' : 'Запросить пояснение ИИ'}</button></div><p>{shownSummary.text}</p><small className="muted">{engineNotice(shownSummary)}</small></section>}
        <section className="panel stack"><h2>Загрузка {worker ? 'за период' : 'исполнителей'}</h2><BarChart title="Работа и ожидание по журналу действий" rows={workload.map(row => ({label: row.employee_name, value: row.active_minutes, extra: row.paused_minutes}))} secondary/>
          <div className="reports-desktop table-wrap"><table><thead><tr><th>Исполнитель</th><th>Работа</th><th>Ожидание</th><th>Назначено</th><th>Закрыто</th><th>Состав смены</th></tr></thead><tbody>{workload.map(row => <tr key={row.employee_id}><td>{row.employee_name}</td><td>{duration(row.active_minutes)}</td><td>{duration(row.paused_minutes)}</td><td>{row.assigned_orders}</td><td>{row.closed_orders}</td><td>{row.in_selected_shift_roster == null ? '—' : row.in_selected_shift_roster ? 'В составе' : 'Работа вне состава'}</td></tr>)}</tbody></table></div>
          <div className="reports-mobile stack">{workload.map(row => <article className="panel" key={row.employee_id}><strong>{row.employee_name}</strong><p>Работа {duration(row.active_minutes)} · ожидание {duration(row.paused_minutes)}</p><p className="muted">Назначено {row.assigned_orders}, закрыто {row.closed_orders}{row.in_selected_shift_roster === false ? ' · Работа вне состава выбранной смены' : ''}</p></article>)}</div>
          {!workload.length && <p className="empty">За этот период нет назначений и рабочих интервалов.</p>}<p className="muted">Ожидание показано отдельно и не означает вину исполнителя. В выбранной смене учитываются переходящие работы; сотрудники её состава без работы имеют нулевую загрузку.</p>
        </section>
        <section className="panel stack"><h2>Где теряется время</h2><BarChart title="Подтверждённый простой оборудования" rows={report.downtime.map(row => ({label: row.equipment_name, value: row.minutes}))}/><button type="button" className="button secondary" onClick={() => {setTab('downtime'); setExportKind('downtime');}}>Посмотреть причины и интервалы</button></section>
        {!!report.overdue_order_ids.length && <section className="panel stack"><h2>Просроченные наряды</h2>{evidence(report.overdue_order_ids)}</section>}
      </>}

      {tab === 'ratings' && <>
        <RatingComparison user={user} rows={rows} kind="employees"/>
        {rows.map(row => ratingCard(row, row.employee_id || 'employee'))}{!rows.length && <p className="empty">За этот период пока нет данных для оценки. Нулевой балл не подставляется.</p>}
        {!worker && <section className="stack"><RatingComparison user={user} rows={report.ratings.brigades} kind="brigades"/>{report.ratings.brigades.map((row, index) => ratingCard(row, row.brigade_id || `unknown-${index}`))}{!report.ratings.brigades.length && <p className="empty">Данных о бригадах за выбранный период нет.</p>}{report.brigade_attribution.unknown_execution_orders > 0 && <p className="notice">Для {report.brigade_attribution.unknown_execution_orders} нарядов историческая бригада неизвестна. Текущий состав сотрудников не используется для восстановления прошлого.</p>}</section>}
      </>}

      {tab === 'materials' && <>
        <section className="panel stack"><h2>Списанные материалы</h2><p className="muted">Учитывается последняя версия результата на конец периода. Количества с разными единицами не складываются.</p><div className="metric-grid">{report.materials.totals.map(item => <div className="metric" key={item.material_id}><span>{item.material_name}</span><strong style={{display: 'block', fontSize: 24}}>{number(item.quantity, 3)} {item.unit}</strong></div>)}</div></section>
        <div className="reports-desktop table-wrap panel"><table><thead><tr><th>Наряд</th><th>Материал</th><th>Количество</th><th>Норма</th><th>Отклонение</th><th>Оборудование</th><th>Исполнитель / бригада</th></tr></thead><tbody>{materialRows.map((row, index) => <tr key={`${row.order_id}-${row.material_id}-${index}`}><td><button className="button secondary" onClick={() => onOpenOrder(row.order_id)}>№{row.number}</button></td><td>{row.material_name}</td><td>{number(row.quantity, 3)} {row.unit}</td><td>{row.norm_quantity == null ? 'Не задана' : `${number(row.norm_quantity, 3)} ${row.unit}`}</td><td>{row.deviation == null ? '—' : `${row.deviation > 0 ? '+' : ''}${number(row.deviation, 3)} ${row.unit}`}</td><td>{equipmentName(row.equipment_id)}</td><td>{personName(row.employee_id)}<br/><small className="muted">{brigadeName(row.brigade_id)}</small></td></tr>)}</tbody></table></div>
        <div className="reports-mobile stack">{materialRows.map((row, index) => <article className="panel stack" key={`${row.order_id}-${row.material_id}-${index}`}><strong>{row.material_name}</strong><span>{number(row.quantity, 3)} {row.unit} · норма {row.norm_quantity == null ? 'не задана' : `${number(row.norm_quantity, 3)} ${row.unit}`}</span><span className="muted">{equipmentName(row.equipment_id)} · {personName(row.employee_id)} · {brigadeName(row.brigade_id)}</span>{row.deviation != null && row.deviation > 0 && <span className="badge">Выше нормы на {number(row.deviation, 3)} {row.unit}</span>}<button type="button" className="button secondary" onClick={() => onOpenOrder(row.order_id)}>Открыть наряд №{row.number}</button></article>)}</div>
        {!materialRows.length && <p className="empty">Списаний в выбранном периоде нет.</p>}
      </>}

      {tab === 'downtime' && <>
        <section className="panel stack"><h2>Подтверждённые простои</h2><p className="muted">Простой фиксируется явно. Пересекающиеся интервалы одного оборудования объединены; продолжительность наряда не подставляется вместо простоя.</p><BarChart title="Простой по оборудованию" rows={report.downtime.map(row => ({label: row.equipment_name, value: row.minutes}))}/></section>
        {report.downtime.map(row => <article className="panel stack" key={row.equipment_id}><div className="toolbar" style={{justifyContent: 'space-between'}}><h3 style={{margin: 0}}>{row.equipment_name}</h3><strong>{duration(row.minutes)}</strong></div><p className="muted">{sectionName(row.section_id)} · плановый {duration(row.planned_minutes)} · внеплановый {duration(row.unplanned_minutes)}</p><p className="muted">Доля внепланового простоя: {downtimeShare(row.unplanned_share)}</p><p>Причины: {row.causes.length ? row.causes.join('; ') : 'не указаны'}</p>{row.overlap_minutes > 0 && <p className="notice">Плановые и внеплановые интервалы пересекаются на {duration(row.overlap_minutes)}. В общем итоге это время учтено один раз.</p>}<details><summary>Интервалы простоя · {row.intervals.length}</summary><ul>{row.intervals.map((interval, index) => <li key={index}>{date(interval.started_at)} — {date(interval.ended_at)}<span className="muted"> · {duration((Date.parse(interval.ended_at) - Date.parse(interval.started_at)) / 60000)}</span></li>)}</ul></details>{evidence(row.evidence_order_ids)}</article>)}
        {!report.downtime.length && <p className="empty">Подтверждённых интервалов простоя за этот период нет.</p>}
      </>}

      {tab === 'anomalies' && <>
        <section className="panel"><h2>Сигналы для проверки</h2><p className="muted">Это статистика и проверки по правилам. Совпадение событий не доказывает причину или вину сотрудника.</p></section>
        {report.anomalies.map((finding, index) => <article className="panel stack" key={`${finding.kind}-${index}`}><span className="badge">{finding.engine === 'rules' ? 'Проверка по правилу' : 'Статистический сигнал'}</span><h3 style={{margin: 0}}>{findingText(finding)}</h3><p>{finding.recommendation}</p>{evidence(finding.evidence_order_ids)}</article>)}
        {!report.anomalies.length && <p className="empty">На выбранных данных выраженных сигналов не найдено. Это не подтверждение отсутствия неисправностей.</p>}
      </>}

      {tab === 'orders' && <section className="panel stack"><h2>Наряды выбранного периода · {number(report.order_ids.length, 0)}</h2><p className="muted">Откройте наряд для хронологии, результата и оценки. Excel по наряду доступен отдельно.</p>{evidence(report.order_ids)}</section>}
    </>}
  </div>;
}

export default Reports;
