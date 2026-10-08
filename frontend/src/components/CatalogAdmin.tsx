import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { useResource } from '../lib/hooks';
import type { User } from '../lib/types';
import { clearDraft, dateInput, errorStatus, errorText, inputToIso, readDraft, storeDraft } from './formTools';

type RecordItem = { id: string; [key: string]: unknown };
type Items = { items: RecordItem[] };
type Values = Record<string, string | boolean>;
type FieldSpec = { key: string; label: string; type?: 'number' | 'boolean' | 'select' | 'list'; required?: boolean; reference?: string; min?: number; max?: number; step?: number; help?: string };
type Resource = { key: string; label: string; fields: FieldSpec[] };
const active: FieldSpec = { key: 'active', label: 'Доступна для новых работ', type: 'boolean' };
const name: FieldSpec = { key: 'name', label: 'Название', required: true };
const external: FieldSpec = { key: 'external_id', label: 'Код во внешней системе', help: 'Необязательно. Для согласованного обмена с системой предприятия.' };
const eqType: FieldSpec = { key: 'equipment_type', label: 'Тип оборудования', required: true, help: 'Должен совпадать с типом в карточке оборудования.' };
const fault: FieldSpec = { key: 'fault_code_id', label: 'Шифр неисправности', type: 'select', reference: 'fault-codes', required: true };
const resources: Resource[] = [
  { key: 'sections', label: 'Участки', fields: [name, external, active] },
  { key: 'equipment', label: 'Оборудование', fields: [name, { key: 'inventory_number', label: 'Инвентарный номер', required: true }, { key: 'section_id', label: 'Участок', type: 'select', reference: 'sections', required: true }, eqType, { key: 'criticality', label: 'Критичность · 1–5', type: 'number', min: 1, max: 5, step: 1, required: true }, external, active] },
  { key: 'brigades', label: 'Бригады', fields: [name, active] },
  { key: 'fault-codes', label: 'Неисправности', fields: [{ key: 'code', label: 'Шифр', required: true, help: 'Например: М-02. Использованный шифр сохраняет историю.' }, name, { key: 'specialty', label: 'Специальность исполнителя' }, active] },
  { key: 'materials', label: 'Материалы', fields: [name, { key: 'unit', label: 'Единица измерения', required: true, help: 'Например: шт., кг, л, м.' }, { key: 'allowed_fault_prefixes', label: 'Группы неисправностей', type: 'list', help: 'Коды через запятую, например: М, Г. Пусто — без ограничения.' }, external, active] },
  { key: 'time-norms', label: 'Нормативы времени', fields: [name, eqType, fault, { key: 'minutes', label: 'Норматив, минут', type: 'number', min: 1, max: 43200, step: 1, required: true }] },
  { key: 'material-norms', label: 'Нормы материалов', fields: [eqType, fault, { key: 'material_id', label: 'Материал', type: 'select', reference: 'materials', required: true }, { key: 'quantity', label: 'Нормальное количество', type: 'number', min: 0.001, max: 999999999.999, step: 0.001, required: true }] },
];
const roleLabels: Record<string, string> = { worker: 'Исполнитель', master: 'Мастер смены', leader: 'Руководитель', admin: 'Администратор' };
const { patch, post, put } = api;

function initialValues(resource: Resource): Values {
  return Object.fromEntries(resource.fields.map(field => [field.key, field.type === 'boolean' ? true : field.key === 'criticality' ? '1' : field.key === 'minutes' ? '60' : '']));
}

function valueFromItem(resource: Resource, item: RecordItem): Values {
  return Object.fromEntries(resource.fields.map(field => [field.key, field.type === 'boolean' ? Boolean(item[field.key]) : Array.isArray(item[field.key]) ? (item[field.key] as unknown[]).join(', ') : String(item[field.key] ?? '')]));
}

const emptyUser: Values = { login: '', full_name: '', password: '', pin: '', clear_pin: false, role: 'worker', specialty: 'слесарь', grade: '3', brigade_id: '', active: true };

export function CatalogAdmin({ user, onChanged }: { user: User; onChanged: () => void }) {
  const isAdmin = user.role === 'admin';
  const managesShifts = isAdmin || user.role === 'master';
  const [tab, setTab] = useState(isAdmin ? 'sections' : managesShifts ? 'attendance' : 'sections');
  const [editing, setEditing] = useState<string | null>(null);
  const [values, setValues] = useState<Values>(initialValues(resources[0]));
  const [userValues, setUserValues] = useState<Values>(emptyUser);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [ambiguousCreate, setAmbiguousCreate] = useState(false);
  const [shiftName, setShiftName] = useState('');
  const [shiftStart, setShiftStart] = useState(dateInput());
  const [shiftEnd, setShiftEnd] = useState(dateInput(new Date(Date.now() + 12 * 60 * 60 * 1000)));
  const [roster, setRoster] = useState<string[]>([]);
  const sections = useResource<Items>('/catalog/sections?limit=500');
  const equipment = useResource<Items>('/catalog/equipment?limit=500');
  const brigades = useResource<Items>('/catalog/brigades?limit=500');
  const faults = useResource<Items>('/catalog/fault-codes?limit=500');
  const materials = useResource<Items>('/catalog/materials?limit=500');
  const timeNorms = useResource<Items>('/catalog/time-norms?limit=500');
  const materialNorms = useResource<Items>('/catalog/material-norms?limit=500');
  const users = useResource<Items>('/catalog/users');
  const shifts = useResource<Items>('/catalog/shifts');
  const loaded: Record<string, typeof sections> = { sections, equipment, brigades, 'fault-codes': faults, materials, 'time-norms': timeNorms, 'material-norms': materialNorms, users, shifts, attendance: users };
  const resource = resources.find(item => item.key === tab);
  const selected = loaded[tab];
  const draftKey = `naryadai.draft.${user.id}.catalog.${tab}`;

  useEffect(() => {
    const definition = resources.find(item => item.key === tab);
    setEditing(null); setError(''); setNotice(''); setAmbiguousCreate(false);
    if (definition) { const restored = readDraft<{ values: Values; editing: string | null }>(`naryadai.draft.${user.id}.catalog.${tab}`, { values: initialValues(definition), editing: null }); setValues(restored.values); setEditing(restored.editing); }
    if (tab === 'users') { const restored = readDraft<{ values: Values; editing: string | null }>(`naryadai.draft.${user.id}.catalog.users`, { values: emptyUser, editing: null }); setUserValues({ ...emptyUser, ...restored.values, password: '', pin: '', clear_pin: false }); setEditing(restored.editing); }
  }, [tab, user.id]);

  useEffect(() => {
    if (resource) storeDraft(draftKey, { values, editing });
    if (tab === 'users') {
      const { password: _password, pin: _pin, clear_pin: _clearPin, ...safeValues } = userValues;
      storeDraft(draftKey, { values: safeValues, editing });
    }
  }, [resource, draftKey, values, userValues, tab, editing]);

  function update(field: string, value: string | boolean) { setValues(previous => ({ ...previous, [field]: value })); }
  function updateUser(field: string, value: string | boolean) { setUserValues(previous => ({ ...previous, [field]: value })); }
  function referenceLabel(kind: string, id: unknown): string {
    const item = loaded[kind]?.data?.items.find(candidate => candidate.id === id);
    if (!item) return 'Не выбрано';
    return String(item.name ?? item.full_name ?? item.code ?? 'Запись');
  }
  function recordLabel(item: RecordItem): string {
    return String(item.name ?? (item.material_id ? `${referenceLabel('materials', item.material_id)} · ${referenceLabel('fault-codes', item.fault_code_id)}` : item.full_name ?? item.code ?? 'Запись'));
  }
  function newItem() {
    setEditing(null); setError(''); setNotice(''); setAmbiguousCreate(false);
    if (resource) setValues(initialValues(resource));
    else setUserValues(emptyUser);
  }
  function edit(item: RecordItem) {
    setEditing(item.id); setError(''); setNotice(''); setAmbiguousCreate(false);
    if (resource) setValues(valueFromItem(resource, item));
    if (tab === 'users') setUserValues({ ...emptyUser, login: String(item.login ?? ''), full_name: String(item.full_name ?? ''), role: String(item.role ?? 'worker'), specialty: String(item.specialty ?? ''), grade: String(item.grade ?? 3), brigade_id: String(item.brigade_id ?? ''), active: Boolean(item.active) });
  }

  async function saved() { clearDraft(draftKey); await selected.reload(); onChanged(); setNotice(editing ? 'Изменения сохранены.' : 'Запись добавлена.'); setEditing(null); if (resource) setValues(initialValues(resource)); else setUserValues(emptyUser); }

  async function saveResource() {
    if (!isAdmin || !resource || busy || ambiguousCreate) return;
    setBusy(true); setError('');
    let attemptedMutation = false;
    try {
      const payload: Record<string, unknown> = {};
      for (const field of resource.fields) {
        const value = values[field.key];
        if (field.type === 'boolean') payload[field.key] = Boolean(value);
        else if (field.type === 'number') {
          const number = Number(value);
          if (!String(value).trim() || !Number.isFinite(number) || field.min !== undefined && number < field.min || field.max !== undefined && number > field.max || field.step === 1 && !Number.isInteger(number)) throw new Error(`Проверьте поле «${field.label}».`);
          payload[field.key] = number;
        } else if (field.type === 'list') {
          const prefixes = [...new Set(String(value ?? '').split(/[,;\n]/).map(item => item.trim()).filter(Boolean))];
          if (prefixes.length > 20) throw new Error('Можно указать не больше 20 групп неисправностей.');
          payload[field.key] = prefixes;
        } else {
          const text = String(value ?? '').trim();
          if (field.required && !text) throw new Error(`Заполните поле «${field.label}».`);
          payload[field.key] = text || null;
        }
      }
      attemptedMutation = true;
      if (editing) await put(`/catalog/${resource.key}/${editing}`, payload);
      else await post(`/catalog/${resource.key}`, payload);
      await saved();
    } catch (failure) {
      setError(errorText(failure));
      if (!editing && attemptedMutation && errorStatus(failure) === 0) { setAmbiguousCreate(true); setNotice('Ответ о сохранении не получен. Сначала обновите список и проверьте, появилась ли запись; повторное создание может добавить копию.'); }
    } finally { setBusy(false); }
  }

  async function saveUser() {
    if (!isAdmin || busy || ambiguousCreate) return;
    setBusy(true); setError('');
    let attemptedMutation = false;
    try {
      const payload: Record<string, unknown> = { full_name: String(userValues.full_name).trim(), role: userValues.role, specialty: String(userValues.specialty).trim(), grade: Number(userValues.grade), brigade_id: userValues.brigade_id || null, active: Boolean(userValues.active) };
      if (!payload.full_name || !payload.specialty || !Number.isInteger(payload.grade) || Number(payload.grade) < 1 || Number(payload.grade) > 8) throw new Error('Укажите имя, специальность и разряд от 1 до 8.');
      const password = String(userValues.password ?? '');
      const pin = String(userValues.pin ?? '');
      if (editing) {
        const original = users.data?.items.find(item => item.id === editing);
        if (!original) throw new Error('Обновите список сотрудников перед изменением записи.');
        // Merely including role in PATCH revokes sessions on the server.
        // Send only changed public fields; credential changes remain explicit.
        for (const key of Object.keys(payload)) if (payload[key] === original[key]) delete payload[key];
      }
      if (!editing) { payload.login = String(userValues.login).trim(); payload.password = password; if (!payload.login || password.length < 8) throw new Error('Для нового сотрудника нужны логин и пароль от 8 символов.'); }
      else if (password) { if (password.length < 8) throw new Error('Новый пароль должен содержать не меньше 8 символов.'); payload.password = password; }
      if (pin && !/^\d{4,8}$/.test(pin)) throw new Error('ПИН должен содержать от 4 до 8 цифр.');
      if (editing && userValues.clear_pin) payload.pin = null;
      else if (pin) payload.pin = pin;
      attemptedMutation = true;
      if (editing) await patch(`/catalog/users/${editing}`, payload);
      else await post('/catalog/users', payload);
      await saved();
    } catch (failure) {
      setError(errorText(failure));
      if (!editing && attemptedMutation && errorStatus(failure) === 0) { setAmbiguousCreate(true); setNotice('Ответ не получен. Обновите список и проверьте сотрудника по логину перед повторной отправкой.'); }
    } finally { setBusy(false); }
  }

  async function attendance(item: RecordItem) {
    if (!managesShifts || busy) return;
    setBusy(true); setError('');
    try { await put(`/catalog/users/${item.id}/attendance`, { on_shift: !item.on_shift }); await users.reload(); onChanged(); setNotice('Статус смены обновлён.'); }
    catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  }

  async function createShift() {
    if (!managesShifts || busy || ambiguousCreate) return;
    setBusy(true); setError('');
    let attemptedMutation = false;
    try {
      const starts = inputToIso(shiftStart); const ends = inputToIso(shiftEnd);
      if (new Date(ends) <= new Date(starts)) throw new Error('Окончание смены должно быть позже начала.');
      if (shiftName.trim().length < 2 || !roster.length) throw new Error('Назовите смену и выберите её участников.');
      attemptedMutation = true;
      await post('/catalog/shifts', { name: shiftName.trim(), starts_at: starts, ends_at: ends, user_ids: roster });
      await shifts.reload(); onChanged(); setNotice('Смена создана. Выход сотрудников отмечается отдельно.'); setShiftName(''); setRoster([]);
    } catch (failure) { setError(errorText(failure)); if (attemptedMutation && errorStatus(failure) === 0) { setAmbiguousCreate(true); setNotice('Ответ о создании смены не получен. Проверьте список перед повтором.'); } }
    finally { setBusy(false); }
  }

  function fieldControl(field: FieldSpec) {
    if (field.type === 'boolean') return <label className="field" style={{ minHeight: 52 }} key={field.key}><span><input type="checkbox" checked={Boolean(values[field.key])} onChange={event => update(field.key, event.target.checked)} /> {field.label}</span></label>;
    return <label className="field" key={field.key}>{field.label}{field.required ? ' *' : ''}
      {field.type === 'select' ? <select required={field.required} value={String(values[field.key] ?? '')} onChange={event => update(field.key, event.target.value)}><option value="">Выберите значение</option>{loaded[field.reference!]?.data?.items.map(item => <option key={item.id} value={item.id}>{item.code ? `${item.code} · ` : ''}{String(item.name ?? item.full_name ?? '')}{item.active === false ? ' · отключено' : ''}</option>)}</select> : <input required={field.required} type={field.type === 'number' ? 'number' : 'text'} inputMode={field.type === 'number' ? 'decimal' : undefined} min={field.min} max={field.max} step={field.step} maxLength={field.key === 'code' || field.key === 'unit' ? 30 : field.key === 'name' ? 200 : field.type === 'list' ? 500 : 100} value={String(values[field.key] ?? '')} onChange={event => update(field.key, event.target.value)} />}
      {field.help && <small className="muted">{field.help}</small>}
    </label>;
  }

  return <section className="panel stack" aria-labelledby="catalog-title">
    <h2 id="catalog-title">{isAdmin ? 'Справочники и сотрудники' : managesShifts ? 'Смена и сотрудники' : 'Справочники'}</h2>
    <div className="toolbar" role="group" aria-label="Разделы справочников">{resources.map(item => <button type="button" aria-pressed={tab === item.key} className={`button ${tab === item.key ? 'primary' : 'secondary'}`} disabled={busy} key={item.key} onClick={() => setTab(item.key)}>{item.label}</button>)}{isAdmin && <button type="button" aria-pressed={tab === 'users'} className={`button ${tab === 'users' ? 'primary' : 'secondary'}`} disabled={busy} onClick={() => setTab('users')}>Сотрудники</button>}{managesShifts && <><button aria-pressed={tab === 'shifts'} className={`button ${tab === 'shifts' ? 'primary' : 'secondary'}`} disabled={busy} onClick={() => setTab('shifts')}>Состав смены</button><button aria-pressed={tab === 'attendance'} className={`button ${tab === 'attendance' ? 'primary' : 'secondary'}`} disabled={busy} onClick={() => setTab('attendance')}>На смене</button></>}</div>
    {error && <div className="error" role="alert">{error}</div>}{notice && <div className="notice" role="status">{notice}</div>}
    {selected.loading && <p className="muted" role="status">Загружаем данные…</p>}
    {selected.error && <div className="error" role="alert">{selected.error}</div>}
    <div className="toolbar"><button className="button secondary" type="button" disabled={busy} onClick={async () => { await selected.reload(); setAmbiguousCreate(false); }}>Обновить список</button>{isAdmin && tab !== 'shifts' && tab !== 'attendance' && <button className="button secondary" type="button" disabled={busy} onClick={newItem}>Новая запись</button>}</div>
    {resource && isAdmin && <form className="stack panel" onSubmit={event => { event.preventDefault(); void saveResource(); }}><h3>{editing ? 'Изменить запись' : `Добавить: ${resource.label.toLowerCase()}`}</h3><fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}><div className="form-grid">{resource.fields.map(fieldControl)}</div></fieldset><button className="button primary" disabled={busy || ambiguousCreate} type="submit">{busy ? 'Сохраняем…' : editing ? 'Сохранить изменения' : 'Добавить'}</button></form>}
    {resource && !isAdmin && <p className="muted">Изменения справочников доступны администратору.</p>}
    {tab === 'users' && isAdmin && <form className="panel stack" onSubmit={event => { event.preventDefault(); void saveUser(); }}><h3>{editing ? 'Изменить сотрудника' : 'Добавить сотрудника'}</h3><fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}><div className="form-grid">
      <label className="field">Логин *<input value={String(userValues.login)} required={!editing} disabled={Boolean(editing)} minLength={2} maxLength={100} autoComplete="off" onChange={event => updateUser('login', event.target.value)} /></label>
      <label className="field">Имя сотрудника *<input required minLength={2} maxLength={200} value={String(userValues.full_name)} onChange={event => updateUser('full_name', event.target.value)} /></label>
      <label className="field">Роль<select value={String(userValues.role)} disabled={editing === user.id} onChange={event => updateUser('role', event.target.value)}>{Object.entries(roleLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      <label className="field">Специальность *<input required maxLength={100} value={String(userValues.specialty)} onChange={event => updateUser('specialty', event.target.value)} /></label>
      <label className="field">Разряд *<input type="number" required min={1} max={8} step={1} value={String(userValues.grade)} onChange={event => updateUser('grade', event.target.value)} /></label>
      <label className="field">Бригада<select value={String(userValues.brigade_id)} onChange={event => updateUser('brigade_id', event.target.value)}><option value="">Без бригады</option>{brigades.data?.items.map(item => <option value={item.id} key={item.id}>{String(item.name)}</option>)}</select></label>
      <label className="field">{editing ? 'Новый пароль · необязательно' : 'Пароль *'}<input type="password" minLength={8} maxLength={200} required={!editing} autoComplete="new-password" value={String(userValues.password)} onChange={event => updateUser('password', event.target.value)} /></label>
      <label className="field">{editing ? 'Новый ПИН · необязательно' : 'ПИН · необязательно'}<input type="password" inputMode="numeric" pattern="[0-9]{4,8}" minLength={4} maxLength={8} disabled={Boolean(userValues.clear_pin)} autoComplete="new-password" value={String(userValues.pin)} onChange={event => updateUser('pin', event.target.value)} /></label>
      {editing && <label className="field" style={{ minHeight: 52 }}><span><input type="checkbox" checked={Boolean(userValues.clear_pin)} onChange={event => updateUser('clear_pin', event.target.checked)} /> Удалить действующий ПИН</span></label>}
      <label className="field" style={{ minHeight: 52 }}><span><input type="checkbox" disabled={editing === user.id} checked={Boolean(userValues.active)} onChange={event => updateUser('active', event.target.checked)} /> Доступ сотрудника включён</span></label>
    </div></fieldset><p className="muted">Изменение пароля, ПИН или роли завершает действующие сеансы сотрудника. Пароль и ПИН не сохраняются в черновике.</p><button className="button primary" type="submit" disabled={busy || ambiguousCreate}>{busy ? 'Сохраняем…' : editing ? 'Сохранить сотрудника' : 'Создать сотрудника'}</button></form>}
    {tab === 'shifts' && managesShifts && <form className="panel stack" onSubmit={event => { event.preventDefault(); void createShift(); }}><h3>Создать смену</h3><fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}><div className="form-grid"><label className="field">Название *<input required minLength={2} maxLength={200} value={shiftName} onChange={event => setShiftName(event.target.value)} /></label><label className="field">Начало · UTC+5<input type="datetime-local" required value={shiftStart} onChange={event => setShiftStart(event.target.value)} /></label><label className="field">Окончание · UTC+5<input type="datetime-local" required value={shiftEnd} onChange={event => setShiftEnd(event.target.value)} /></label></div><p>Участники: {roster.length}</p><div className="form-grid">{users.data?.items.filter(item => item.active).map(item => <label className="field" style={{ minHeight: 52 }} key={item.id}><span><input type="checkbox" checked={roster.includes(item.id)} onChange={event => setRoster(previous => event.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id))} /> {String(item.full_name)} · {roleLabels[String(item.role)]}</span></label>)}</div></fieldset><button className="button primary" disabled={busy || ambiguousCreate} type="submit">{busy ? 'Создаём…' : 'Создать смену'}</button></form>}
    {!selected.loading && selected.data?.items.length === 0 && <div className="empty">Пока нет записей.</div>}
    <div className="stack">{selected.data?.items.map(item => <article className="panel" key={item.id}><div className="toolbar"><div><strong>{recordLabel(item)}</strong>{item.active === false && <span className="badge">Отключено</span>}<div className="muted">{tab === 'attendance' || tab === 'users' ? `${roleLabels[String(item.role)] ?? ''} · ${String(item.specialty ?? '')} · разряд ${String(item.grade ?? '')} · ${item.on_shift ? 'на смене' : 'не на смене'}` : tab === 'shifts' ? `${new Date(String(item.starts_at)).toLocaleString('ru-RU', { timeZone: 'Asia/Qyzylorda' })} — ${new Date(String(item.ends_at)).toLocaleString('ru-RU', { timeZone: 'Asia/Qyzylorda' })}` : resource?.fields.filter(field => !['name', 'active', 'external_id'].includes(field.key)).map(field => `${field.label}: ${field.reference ? referenceLabel(field.reference, item[field.key]) : Array.isArray(item[field.key]) ? (item[field.key] as unknown[]).join(', ') || 'без ограничения' : String(item[field.key] ?? '—')}`).join(' · ')}</div></div>{tab === 'attendance' && managesShifts ? <button className={`button ${item.on_shift ? 'secondary' : 'primary'}`} disabled={busy || item.active === false} onClick={() => void attendance(item)}>{item.on_shift ? 'Снять со смены' : 'Отметить на смене'}</button> : isAdmin && tab !== 'shifts' && <button className="button secondary" disabled={busy} onClick={() => edit(item)}>Изменить</button>}</div></article>)}</div>
  </section>;
}

export default CatalogAdmin;
