import { useEffect, useId, useRef, useState } from 'react';
import { api } from '../lib/api';
import { useResource } from '../lib/hooks';
import type { Order, User } from '../lib/types';
import { clearDraft, compressImage, dateInput, errorStatus, errorText, inputToIso, operationId, readDraft, storeDraft } from './formTools';

type Item = { id: string; name: string; active?: boolean; section_id?: string; equipment_type?: string; inventory_number?: string; minutes?: number; starts_at?: string; ends_at?: string };
type Employee = User & { availability?: string; queue_count?: number; current_order_number?: number | null };
type Items<T> = { items: T[] };
type Draft = { section: string; equipment: string; description: string; assignment: 'person' | 'brigade'; executor: string; brigade: string; priority: string; workType: string; deadlineMode: 'norm' | 'date'; minutes: string; norm: string; deadline: string; shift: string; comment: string; stopped: boolean };
type PendingCreate = { operation_id: string; [key: string]: unknown };
type PhotoTask = { id: string; file: File; preview: string; source: 'camera' | 'gallery'; uploaded: boolean; operationId?: string; expectedVersion?: number };
type Persisted = { draft: Draft; request: PendingCreate | null; orderId: string | null; photoCount?: number };
const defaults: Draft = { section: '', equipment: '', description: '', assignment: 'person', executor: '', brigade: '', priority: 'normal', workType: 'unplanned', deadlineMode: 'norm', minutes: '60', norm: '', deadline: dateInput(new Date(Date.now() + 60 * 60 * 1000)), shift: '', comment: '', stopped: false };
const { get, post, upload } = api;

function availability(employee: Employee): string {
  if (employee.availability === 'off_shift' || !employee.on_shift) return 'не на смене';
  if (employee.availability === 'busy') return `выполняет №${employee.current_order_number ?? '…'} · очередь ${employee.queue_count ?? 0}`;
  if (employee.availability === 'queued') return `в очереди ${employee.queue_count ?? 0}`;
  return 'свободен';
}

export function CreateOrder({ user, onCreated, onCancel }: { user: User; onCreated: (order: Order) => void; onCancel: () => void }) {
  const key = `naryadai.draft.${user.id}.create_order`;
  const [initial] = useState(() => readDraft<Persisted>(key, { draft: defaults, request: null, orderId: null }));
  const [draft, setDraft] = useState<Draft>({ ...defaults, ...initial.draft });
  const [request, setRequest] = useState<PendingCreate | null>(initial.request);
  const [order, setOrder] = useState<Order | null>(null);
  const [photos, setPhotos] = useState<PhotoTask[]>([]);
  const [busy, setBusy] = useState(false);
  const [photoBusy, setPhotoBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState(initial.photoCount ? 'Текст черновика сохранён. Фото после перезагрузки нужно выбрать заново.' : '');
  const [restoring, setRestoring] = useState(Boolean(initial.orderId));
  const currentOrder = useRef<Order | null>(null);
  const currentPhotos = useRef<PhotoTask[]>([]);
  const requestRef = useRef<PendingCreate | null>(initial.request);
  const running = useRef(false);
  const finished = useRef(false);
  const cameraInput = useRef<HTMLInputElement>(null);
  const galleryInput = useRef<HTMLInputElement>(null);
  const cameraId = useId();
  const galleryId = useId();
  const sections = useResource<Items<Item>>('/catalog/sections?active_only=true&limit=500');
  const equipment = useResource<Items<Item>>('/catalog/equipment?active_only=true&limit=500');
  const people = useResource<Items<Employee>>('/catalog/users?role=worker', 5000);
  const brigades = useResource<Items<Item>>('/catalog/brigades?active_only=true&limit=500');
  const norms = useResource<Items<Item>>('/catalog/time-norms?limit=500');
  const shifts = useResource<Items<Item>>('/catalog/shifts');
  const selectedEquipment = equipment.data?.items.find(item => item.id === draft.equipment);
  const selectedNorm = norms.data?.items.find(item => item.id === draft.norm);
  const locked = Boolean(request || order || restoring || initial.orderId);
  const uploadedCount = Number(order?.photos?.filter((photo: { kind: string }) => photo.kind === 'before').length ?? 0);
  const queuedCount = photos.filter(photo => !photo.uploaded).length;
  const remaining = Math.max(0, 5 - uploadedCount - queuedCount);
  const essentialError = sections.error || equipment.error || people.error || brigades.error;

  useEffect(() => {
    if (!finished.current) storeDraft(key, { draft, request, orderId: order?.id ?? initial.orderId, photoCount: queuedCount });
  }, [key, draft, request, order, queuedCount, initial.orderId]);

  useEffect(() => {
    if (!initial.orderId) return;
    let alive = true;
    get<Order>(`/orders/${initial.orderId}`).then(value => {
      if (!alive) return;
      currentOrder.current = value;
      setOrder(value);
      setNotice(`Наряд №${value.number} уже сохранён. Можно открыть его или добавить оставшиеся фото.`);
    }).catch(failure => { if (alive) setError(errorText(failure)); }).finally(() => { if (alive) setRestoring(false); });
    return () => { alive = false; };
  }, [initial.orderId]);

  useEffect(() => () => { currentPhotos.current.forEach(photo => URL.revokeObjectURL(photo.preview)); }, []);
  useEffect(() => { const timer = window.setInterval(() => { void people.reload(); }, 4000); return () => window.clearInterval(timer); }, [people.reload]);

  function change<K extends keyof Draft>(field: K, value: Draft[K]) { setDraft(previous => ({ ...previous, [field]: value })); }

  async function addPhotos(files: FileList | null, source: 'camera' | 'gallery') {
    if (!files?.length || busy || photoBusy || request && !order) return;
    if (files.length > remaining) { setError(`Можно добавить ещё ${remaining} фото. Всего — не больше пяти.`); return; }
    setPhotoBusy(true); setError('');
    const added: PhotoTask[] = [];
    try {
      for (const file of Array.from(files)) {
        const compressed = await compressImage(file);
        added.push({ id: operationId(), file: compressed, preview: URL.createObjectURL(compressed), source, uploaded: false });
      }
      currentPhotos.current = [...currentPhotos.current, ...added];
      setPhotos([...currentPhotos.current]);
    } catch (failure) {
      added.forEach(photo => URL.revokeObjectURL(photo.preview));
      setError(errorText(failure));
    } finally { setPhotoBusy(false); }
  }

  function removePhoto(id: string) {
    const photo = currentPhotos.current.find(item => item.id === id);
    if (!photo || photo.uploaded || busy) return;
    URL.revokeObjectURL(photo.preview);
    currentPhotos.current = currentPhotos.current.filter(item => item.id !== id);
    setPhotos([...currentPhotos.current]);
  }

  function buildRequest(): PendingCreate {
    if (!selectedEquipment || !draft.description.trim() || draft.description.trim().length < 3) throw new Error('Выберите оборудование и опишите проблему минимум тремя символами.');
    if (draft.assignment === 'person' && !draft.executor || draft.assignment === 'brigade' && !draft.brigade) throw new Error('Выберите исполнителя или бригаду.');
    const minutes = selectedNorm?.minutes ?? Number(draft.minutes);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 43200) throw new Error('Норматив должен быть от 1 до 43200 минут.');
    const due = draft.deadlineMode === 'date' ? inputToIso(draft.deadline) : null;
    if (due && new Date(due).getTime() <= Date.now()) throw new Error('Срок должен быть в будущем.');
    return { operation_id: operationId(), description: draft.description.trim(), equipment_id: draft.equipment, section_id: selectedEquipment.section_id, executor_id: draft.assignment === 'person' ? draft.executor : null, brigade_id: draft.assignment === 'brigade' ? draft.brigade : null, work_type: draft.workType, priority: draft.priority, norm_minutes: minutes, time_norm_id: draft.norm || null, due_at: due, shift_id: draft.shift || null, comment: draft.comment.trim() || null, equipment_stopped: draft.stopped };
  }

  function finish(value: Order) { finished.current = true; clearDraft(key); onCreated(value); }

  async function save() {
    if (running.current || photoBusy || restoring) return;
    running.current = true; setBusy(true); setError('');
    try {
      if (initial.orderId && !currentOrder.current) {
        const restored: Order = await get<Order>(`/orders/${initial.orderId}`);
        currentOrder.current = restored; setOrder(restored);
      }
      if (!currentOrder.current) {
        const payload = requestRef.current ?? buildRequest();
        requestRef.current = payload; setRequest(payload);
        storeDraft(key, { draft, request: payload, orderId: null, photoCount: currentPhotos.current.length });
        try {
          const value = await post<Order>('/orders', payload);
          currentOrder.current = value; setOrder(value);
          storeDraft(key, { draft, request: payload, orderId: value.id, photoCount: currentPhotos.current.length });
          setNotice(`Наряд №${value.number} сохранён. Загружаем фото…`);
        } catch (failure) {
          const status = errorStatus(failure);
          if (status >= 400 && status < 500 && ![408, 429].includes(status)) {
            requestRef.current = null; setRequest(null);
          } else {
            setNotice('Ответ о создании не получен. Повторите отправку: сохранённый идентификатор защищает от повторной выдачи.');
          }
          throw failure;
        }
      }
      for (const photo of currentPhotos.current.filter(item => !item.uploaded)) {
        const saved: Order = currentOrder.current!;
        photo.operationId ??= operationId();
        photo.expectedVersion ??= saved.version;
        const body = new FormData();
        body.append('operation_id', photo.operationId);
        body.append('expected_version', String(photo.expectedVersion));
        body.append('kind', 'before'); body.append('source', photo.source); body.append('file', photo.file);
        // A capture input does not supply a trustworthy capture timestamp.
        try {
          const value: Order = await upload<Order>(`/orders/${saved.id}/photos`, body);
          photo.uploaded = true;
          currentOrder.current = value; setOrder(value); setPhotos([...currentPhotos.current]);
        } catch (failure) {
          if (errorStatus(failure) === 409) {
            const latest = await get<Order>(`/orders/${saved.id}`);
            currentOrder.current = latest; setOrder(latest);
            photo.operationId = undefined; photo.expectedVersion = undefined;
          }
          throw failure;
        }
      }
      finish(currentOrder.current!);
    } catch (failure) {
      setError(errorText(failure));
      if (currentOrder.current) setNotice(`Наряд №${currentOrder.current.number} уже выдан. Не загруженные фото остались в форме. Повторная попытка не создаст новый наряд.`);
    } finally { running.current = false; setBusy(false); }
  }

  if (!['master', 'admin'].includes(user.role)) return <section className="panel"><p className="notice">Наряды выдаёт мастер смены.</p><button className="button secondary" onClick={onCancel}>Назад</button></section>;

  return <section className="panel stack" aria-labelledby="create-order-title">
    <div className="toolbar"><h2 id="create-order-title">{order ? `Наряд №${order.number} сохранён` : 'Выдать наряд'}</h2><button type="button" className="button secondary" disabled={busy} onClick={() => order ? finish(order) : onCancel()}>{order ? 'Открыть сохранённый' : 'Назад'}</button></div>
    {notice && <div className="notice" role="status">{notice}</div>}
    {error && <div className="error" role="alert">{error}</div>}
    {essentialError && <div className="error" role="alert">Не удалось загрузить справочники: {essentialError}<button className="button secondary" onClick={() => { void sections.reload(); void equipment.reload(); void people.reload(); void brigades.reload(); }}>Повторить загрузку</button></div>}
    {restoring && <p className="muted" role="status">Находим сохранённый наряд…</p>}
    <form className="stack" onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={busy || locked} style={{ border: 0, padding: 0, margin: 0 }}>
        <div className="form-grid">
          <label className="field">Участок<select value={draft.section} onChange={event => setDraft(previous => ({ ...previous, section: event.target.value, equipment: '', norm: '' }))}><option value="">Все участки</option>{sections.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <label className="field">Оборудование *<select required value={draft.equipment} onChange={event => { const item = equipment.data?.items.find(candidate => candidate.id === event.target.value); setDraft(previous => ({ ...previous, equipment: event.target.value, section: item?.section_id ?? previous.section, norm: '' })); }}><option value="">Выберите оборудование</option>{equipment.data?.items.filter(item => !draft.section || item.section_id === draft.section).map(item => <option key={item.id} value={item.id}>{item.name} · {item.inventory_number}</option>)}</select></label>
        </div>
        <label className="field">Что произошло и что нужно сделать *<textarea required minLength={3} maxLength={6000} rows={3} value={draft.description} onChange={event => change('description', event.target.value)} placeholder="Например: течь масла на насосе. Проверить соединение и устранить течь." /></label>
        <div className="form-grid">
          <label className="field">Кому поручить<select value={draft.assignment} onChange={event => change('assignment', event.target.value as Draft['assignment'])}><option value="person">Исполнителю</option><option value="brigade">Бригаде</option></select></label>
          {draft.assignment === 'person' ? <label className="field">Исполнитель *<select required value={draft.executor} onChange={event => change('executor', event.target.value)}><option value="">Выберите сотрудника</option>{people.data?.items.filter(item => item.active).map(item => <option key={item.id} value={item.id}>{item.full_name} · {item.specialty} · {availability(item)}</option>)}</select></label> : <label className="field">Бригада *<select required value={draft.brigade} onChange={event => change('brigade', event.target.value)}><option value="">Выберите бригаду</option>{brigades.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
          <label className="field">Приоритет<select value={draft.priority} onChange={event => setDraft(previous => ({ ...previous, priority: event.target.value, workType: event.target.value === 'emergency' ? 'unplanned' : previous.workType }))}><option value="emergency">Аварийный — срочно</option><option value="high">Высокий</option><option value="normal">Обычный</option><option value="planned">Плановый</option></select></label>
          <label className="field">Тип работ<select value={draft.workType} onChange={event => setDraft(previous => ({ ...previous, workType: event.target.value, priority: event.target.value === 'planned' && previous.priority === 'emergency' ? 'planned' : previous.priority }))}><option value="unplanned">Внеплановые</option><option value="planned">Плановые</option></select></label>
          <label className="field">Срок<select value={draft.deadlineMode} onChange={event => change('deadlineMode', event.target.value as Draft['deadlineMode'])}><option value="norm">По нормативу от выдачи</option><option value="date">К указанному времени</option></select></label>
          {draft.deadlineMode === 'date' && <label className="field">Дата и время · UTC+5<input type="datetime-local" required value={draft.deadline} onChange={event => change('deadline', event.target.value)} /></label>}
          <label className="field">Норматив из справочника<select value={draft.norm} onChange={event => { const item = norms.data?.items.find(candidate => candidate.id === event.target.value); setDraft(previous => ({ ...previous, norm: event.target.value, minutes: item?.minutes ? String(item.minutes) : previous.minutes })); }}><option value="">Указать минуты вручную</option>{norms.data?.items.filter(item => selectedEquipment && item.equipment_type === selectedEquipment.equipment_type).map(item => <option key={item.id} value={item.id}>{item.name} · {item.minutes} мин.</option>)}</select></label>
          <label className="field">Норматив, минут *<input type="number" inputMode="numeric" required min={1} max={43200} step={1} disabled={Boolean(draft.norm)} value={draft.minutes} onChange={event => change('minutes', event.target.value)} /></label>
        </div>
        <details><summary style={{ minHeight: 52, cursor: 'pointer' }}>Смена, комментарий и простой</summary><div className="stack">
          <label className="field">Смена<select value={draft.shift} onChange={event => change('shift', event.target.value)}><option value="">Без привязки к смене</option>{shifts.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <label className="field">Комментарий<textarea rows={2} maxLength={4000} value={draft.comment} onChange={event => change('comment', event.target.value)} /></label>
          <label className="field" style={{ minHeight: 52 }}><span><input type="checkbox" checked={draft.stopped} onChange={event => change('stopped', event.target.checked)} /> Оборудование остановлено — начать учёт простоя</span></label>
        </div></details>
      </fieldset>
      <div className="stack">
        <p className="muted">Фото неисправности — до 5 снимков. При выборе камеры можно понадобиться разрешение телефона.</p>
        <div className="toolbar">
          <button className="button secondary" type="button" style={{ minHeight: 52 }} disabled={busy || photoBusy || remaining === 0 || Boolean(request && !order)} onClick={() => cameraInput.current?.click()}>Снять фото</button>
          <button className="button secondary" type="button" style={{ minHeight: 52 }} disabled={busy || photoBusy || remaining === 0 || Boolean(request && !order)} onClick={() => galleryInput.current?.click()}>Выбрать из галереи</button>
          <input ref={cameraInput} id={cameraId} type="file" accept="image/jpeg,image/png,image/webp" capture="environment" hidden disabled={busy || photoBusy || remaining === 0 || Boolean(request && !order)} onChange={event => { void addPhotos(event.target.files, 'camera'); event.target.value = ''; }} />
          <input ref={galleryInput} id={galleryId} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden disabled={busy || photoBusy || remaining === 0 || Boolean(request && !order)} onChange={event => { void addPhotos(event.target.files, 'gallery'); event.target.value = ''; }} />
        </div>
        {photoBusy && <p role="status">Подготавливаем фото…</p>}
        {photos.length > 0 && <div className="form-grid">{photos.map(photo => <div className="panel stack" key={photo.id}><img src={photo.preview} alt="Фото неисправности" style={{ width: '100%', maxHeight: 220, objectFit: 'contain' }} /><span>{photo.uploaded ? 'Загружено' : 'Готово к загрузке'} · {Math.ceil(photo.file.size / 1024)} КБ</span>{!photo.uploaded && <button type="button" className="button secondary" disabled={busy} onClick={() => removePhoto(photo.id)}>Убрать фото</button>}</div>)}</div>}
      </div>
      <div className="toolbar"><button className="button primary" type="submit" disabled={busy || photoBusy || restoring || Boolean(essentialError) || equipment.loading || people.loading}>{busy ? 'Сохраняем…' : order ? 'Дозагрузить фото' : request ? 'Повторить ту же выдачу' : 'Выдать наряд'}</button>{order && <button type="button" className="button secondary" disabled={busy} onClick={() => finish(order)}>Открыть без оставшихся фото</button>}</div>
    </form>
  </section>;
}

export default CreateOrder;
