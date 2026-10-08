import {useCallback, useEffect, useRef, useState} from 'react';
import type {ChangeEvent, FormEvent, ReactNode} from 'react';
import {api, ApiError} from '../lib/api';
import {useResource} from '../lib/hooks';
import {PRIORITY, STATUS} from '../lib/types';
import type {User} from '../lib/types';

type Props = {orderId: string; user: User; onBack: () => void; onChanged: () => void};
type Photo = {id: string; kind: 'before' | 'after'; result_version: number | null; source: string; synthetic?: boolean; captured_at?: string | null; uploaded_at: string};
type OrderEvent = {id: number; actor_id: string | null; action: string; occurred_at: string; from_status?: string | null; to_status?: string | null; reason?: string | null; details: Record<string, unknown>};
type Assessment = {engine: string; score: number | null; verdict: string; explanation: string; good: string[]; improve: string[]; findings: {code: string; message: string; severity: string; evidence?: string[]}[]; needs_master_review: boolean; model?: string | null};
type OrderData = {id: string; number: number; version: number; status: string; work_type: string; priority: string; description: string; comment?: string | null; equipment_id: string; section_id: string; executor_id: string | null; brigade_id: string | null; working_executor_id: string | null; master_id: string; created_at: string; closed_at?: string | null; due_at: string; norm_minutes: number; overdue: boolean; overdue_minutes: number; result_version: number; work_description?: string | null; fault_code_id?: string | null; materials_none?: boolean; master_score?: number | null; master_verdict?: string | null; master_comment?: string | null; final_score?: number | null; rejection_justified?: boolean | null; synthetic?: boolean; equipment?: {name: string; inventory_number: string}; materials: {material_id: string; quantity: number; unit: string}[]; photos: Photo[]; events: OrderEvent[]; assessment: Assessment | null};
type Item = {id: string; name: string; active?: boolean};
type Fault = Item & {code: string};
type Material = Item & {unit: string};
type Employee = User & {availability?: string; current_order_number?: number | null; queue_count?: number};
type List<T> = {items: T[]};
type Report = {audience: string; loaded_order_version?: number; loaded_result_version?: number; timing?: {active_minutes: number; norm_minutes: number; on_time: boolean | null}; quality?: {score: number | null; source: string; verdict?: string | null; explanation: string; good: string[]; improve: string[]}; downtime?: {minutes: number; intervals: {id: string; ended_at: string | null; started_at: string; cause: string}[]}};
type MaterialRow = {key: string; materialId: string; quantity: string};
type JsonPending = {kind: 'json'; method: 'post' | 'patch'; path: string; body: Record<string, unknown>; label: string; finish: 'result' | 'comment' | 'dialog' | 'none'};
type UploadPending = {kind: 'upload'; path: string; operationId: string; version: number; label: string; photoKind: 'before' | 'after'; source: 'camera' | 'gallery'; fileData: string; beforePhotoIds: string[]; versionConflict?: boolean};
type Pending = JsonPending | UploadPending;
type Draft = {key: string; work: string; faultId: string; materials: MaterialRow[]; none: boolean; photoIds: string[]; resultComment: string; discussion: string; dirty: boolean; sourceVersion: number | null; pending: Pending | null};
type DialogMode = 'pause' | 'reject' | 'decision' | 'edit' | 'cancel' | 'downtime-start' | 'downtime-end' | 'rejection' | 'repeat';
type DialogFields = {reason: string; score: string; verdict: string; priority: string; due: string; assignment: 'person' | 'brigade'; executorId: string; brigadeId: string; justified: 'yes' | 'no'; previousOrderId: string};

const tap = {minHeight: 52};
const terminal = new Set(['closed', 'cancelled']);
const editable = new Set(['issued', 'accepted', 'queued', 'rejected', 'in_progress', 'paused', 'rework']);
const resultStates = new Set(['in_progress', 'paused', 'rework']);
const actionNames: Record<string, string> = {issued: 'Наряд выдан', accepted: 'Принят исполнителем', queued: 'Поставлен в очередь', started: 'Работа начата', paused: 'Работа приостановлена', resumed: 'Работа продолжена', rejected: 'Отказ от задания', performed: 'Результат отправлен', review_requested: 'Передан на проверку', review_completed: 'Проверка завершена', review_failed: 'Нужна ручная проверка', rework: 'Возвращён на доработку', closed: 'Принят мастером', cancelled: 'Наряд отменён', reassigned: 'Исполнитель изменён', updated: 'Условия наряда изменены', photo_uploaded: 'Добавлено фото', commented: 'Добавлен комментарий', downtime_started: 'Оборудование остановлено', downtime_ended: 'Оборудование запущено', rejection_reviewed: 'Причина отказа рассмотрена', repeat_confirmed: 'Повторный отказ подтверждён'};
const verdicts: Record<string, string> = {accepted: 'Принято', accepted_with_remarks: 'Принято с замечаниями', requires_rework: 'Нужна доработка'};
const blankDialog: DialogFields = {reason: '', score: '', verdict: 'accepted', priority: 'normal', due: '', assignment: 'person', executorId: '', brigadeId: '', justified: 'yes', previousOrderId: ''};

function operationId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const h = Array.from(bytes, n => n.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function date(value?: string | null): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString('ru-RU', {timeZone: 'Asia/Qyzylorda', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'});
}
function dueInput(value: string): string {
  const d = new Date(new Date(value).getTime() + 5 * 3600_000);
  return Number.isNaN(d.getTime()) ? '' : d.toISOString().slice(0, 16);
}
function emptyDraft(key: string): Draft {
  return {key, work: '', faultId: '', materials: [], none: false, photoIds: [], resultComment: '', discussion: '', dirty: false, sourceVersion: null, pending: null};
}
function readDraft(key: string): Draft {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (value?.key === key && typeof value.work === 'string' && Array.isArray(value.materials) && Array.isArray(value.photoIds)) return {...emptyDraft(key), ...value};
  } catch { /* Invalid or unavailable storage must not break the work screen. */ }
  return emptyDraft(key);
}
function latestEvent(order: OrderData): number { return Math.max(0, ...order.events.map(e => e.id)); }
function blobData(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error('Не удалось подготовить фото')); reader.readAsDataURL(blob);});
}
function dataBlob(value: string): Blob {
  const [header, data] = value.split(',');
  if (header !== 'data:image/jpeg;base64' || !data) throw new Error('Сохранённое фото повреждено. Выберите его заново.');
  const binary = atob(data), bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], {type: 'image/jpeg'});
}
async function compress(file: File): Promise<Blob> {
  const imageUrl = URL.createObjectURL(file);
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => {image.onload = () => resolve(); image.onerror = () => reject(new Error('Не удалось открыть фото. Выберите JPEG, PNG или WebP.')); image.src = imageUrl;});
    const ratio = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * ratio)); canvas.height = Math.max(1, Math.round(image.naturalHeight * ratio));
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Камера недоступна в этом браузере. Попробуйте выбрать фото из галереи.');
    context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height); context.drawImage(image, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('Не удалось сжать фото')), 'image/jpeg', 0.82));
  } finally { URL.revokeObjectURL(imageUrl); }
}

function SecureImage({photo, caption}: {photo: Photo; caption: string}) {
  const [url, setUrl] = useState<string | null>(null), [error, setError] = useState(false);
  useEffect(() => {
    let active = true, objectUrl: string | null = null;
    setUrl(null); setError(false);
    api.getBlob(`/photos/${photo.id}/file`).then(blob => {if (!active) return; objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);}).catch(() => {if (active) setError(true);});
    return () => {active = false; if (objectUrl) URL.revokeObjectURL(objectUrl);};
  }, [photo.id]);
  return <figure style={{margin: 0}}>
    {url ? <a href={url} target="_blank" rel="noreferrer" aria-label={`Открыть фото: ${caption}`}><img src={url} alt={caption} style={{width: '100%', maxHeight: 300, objectFit: 'contain', borderRadius: 12, background: '#f1f5f9'}} /></a> : <div className="empty" style={{minHeight: 90}}>{error ? 'Фото не удалось загрузить' : 'Загружаем фото…'}</div>}
    <figcaption className="muted">{caption} · {date(photo.uploaded_at)}{photo.synthetic || photo.source === 'seed' ? ' · Тестовое изображение' : ''}</figcaption>
  </figure>;
}

function Modal({title, children, busy, onClose}: {title: string; children: ReactNode; busy: boolean; onClose: () => void}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {const node = ref.current; node?.showModal(); return () => {if (node?.open) node.close();};}, []);
  return <dialog ref={ref} className="panel" style={{width: 'min(640px, calc(100vw - 24px))', maxHeight: '90dvh', overflow: 'auto'}} aria-label={title} onCancel={e => {e.preventDefault(); if (!busy) onClose();}}>
    <div className="toolbar"><h2 style={{margin: 0}}>{title}</h2><button className="button secondary" style={tap} type="button" disabled={busy} onClick={onClose} aria-label="Закрыть диалог">Закрыть</button></div>
    {children}
  </dialog>;
}

export function OrderDetail({orderId, user, onBack, onChanged}: Props) {
  const key = `naryadai.draft.${user.id}.${orderId}`;
  const [order, setOrder] = useState<OrderData | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState<string | null>(null), [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null), [busy, setBusy] = useState<string | null>(null), [conflict, setConflict] = useState(false), [lostAccess, setLostAccess] = useState(false);
  const [storageWarning, setStorageWarning] = useState(false), [draft, setDraftState] = useState<Draft>(() => readDraft(key));
  const [dialog, setDialog] = useState<DialogMode | null>(null), [dialogFields, setDialogFields] = useState<DialogFields>(blankDialog), [dialogError, setDialogError] = useState<string | null>(null);
  const [repeatOptions, setRepeatOptions] = useState<OrderData[]>([]), [repeatLoading, setRepeatLoading] = useState(false);
  const [currentUser, setCurrentUser] = useState<User>(user);
  const orderRef = useRef<OrderData | null>(null), draftRef = useRef<Draft>(draft), mounted = useRef(true), busyRef = useRef(false), sequence = useRef(0);
  const camera = useRef<HTMLInputElement>(null), gallery = useRef<HTMLInputElement>(null), photoKind = useRef<'before' | 'after'>('after');
  const currentId = useRef(orderId); currentId.current = orderId;
  const materialCatalog = useResource<List<Material>>('/catalog/materials?limit=500');
  const faultCatalog = useResource<List<Fault>>('/catalog/fault-codes?limit=500');
  const employees = useResource<List<Employee>>('/catalog/users');
  const brigades = useResource<List<Item>>('/catalog/brigades?limit=500');
  const sections = useResource<List<Item>>('/catalog/sections?limit=500');
  const canReadReport = user.role !== 'worker' || order?.executor_id === user.id || order?.working_executor_id === user.id;
  const loadReport = useCallback(async () => ({...await api.get<Report>(`/reports/orders/${orderId}`), loaded_order_version: order?.version, loaded_result_version: order?.result_version}), [orderId, order?.version, order?.result_version]);
  const report = useResource<Report>(canReadReport && order?.id === orderId ? `/reports/orders/${orderId}` : null, 0, loadReport);

  function writeDraft(next: Draft) {
    draftRef.current = next; setDraftState(next);
    try {sessionStorage.setItem(next.key, JSON.stringify(next)); setStorageWarning(false);} catch {setStorageWarning(true);}
  }
  function changeDraft(values: Partial<Draft>, dirty = true) {writeDraft({...draftRef.current, ...values, dirty: dirty || draftRef.current.dirty});}
  const acceptOrder = useCallback((incoming: OrderData) => {
    if (!mounted.current || incoming.id !== currentId.current) return;
    const existing = orderRef.current;
    if (existing && (incoming.version < existing.version || incoming.version === existing.version && latestEvent(incoming) < latestEvent(existing))) return;
    orderRef.current = incoming; setOrder(incoming); setLostAccess(false);
  }, []);
  const reload = useCallback(async () => {
    const requestId = ++sequence.current;
    try {
      const incoming = await api.get<OrderData>(`/orders/${orderId}`);
      if (requestId === sequence.current && mounted.current) {acceptOrder(incoming); setLoadError(null); return true;}
      return false;
    } catch (err) {
      if (requestId !== sequence.current || !mounted.current) return false;
      if (err instanceof ApiError && err.status === 404) {setLostAccess(true); orderRef.current = null; setOrder(null); setLoadError('Наряд переназначен или больше недоступен. Черновик сохранён.');}
      else setLoadError(err instanceof Error ? err.message : 'Не удалось обновить наряд');
      return false;
    } finally {if (mounted.current && requestId === sequence.current) setLoading(false);}
  }, [orderId, acceptOrder]);

  useEffect(() => {
    mounted.current = true; orderRef.current = null; setOrder(null); setLoading(true); setLostAccess(false); setError(null); setLoadError(null); setNotice(null); setConflict(false); setDialog(null);
    const saved = readDraft(key); draftRef.current = saved; setDraftState(saved);
    if (saved.pending) setError('Предыдущая отправка не подтверждена. Повторите её или сначала проверьте состояние наряда.');
    if (saved.pending?.kind === 'upload' && saved.pending.versionConflict) setConflict(true);
    void reload();
    const timer = window.setInterval(() => {
      if (!busyRef.current) {void reload(); void api.get<User>('/auth/me').then(value => {if (mounted.current) setCurrentUser(value);}).catch(() => {});}
    }, 3000);
    return () => {mounted.current = false; sequence.current++; window.clearInterval(timer);};
  }, [key, reload]);
  useEffect(() => {setCurrentUser(user);}, [user]);
  useEffect(() => {if (order?.id === orderId) void report.reload();}, [order?.version, order ? latestEvent(order) : 0, orderId, report.reload]);
  useEffect(() => {
    if (!order || draftRef.current.key !== key || draftRef.current.dirty || draftRef.current.pending) return;
    if (!resultStates.has(order.status)) return;
    const ownPending = order.photos.filter(photo => photo.kind === 'after' && photo.result_version === null && order.events.some(event => event.action === 'photo_uploaded' && event.actor_id === user.id && event.details.photo_id === photo.id)).map(photo => photo.id);
    if (draftRef.current.sourceVersion === order.result_version && draftRef.current.work) return;
    writeDraft({...draftRef.current, work: order.work_description || '', faultId: order.fault_code_id || '', materials: order.materials.map(row => ({key: operationId(), materialId: row.material_id, quantity: String(row.quantity)})), none: Boolean(order.materials_none), photoIds: ownPending, sourceVersion: order.result_version});
  }, [order?.id, order?.status, order?.result_version, key, user.id]);

  async function perform(pending: Pending) {
    if (!mounted.current || busyRef.current || !orderRef.current) return;
    if (pending.kind === 'upload' && pending.versionConflict) return;
    const operationKey = draftRef.current.key;
    busyRef.current = true; setBusy(pending.label); setError(null); setDialogError(null);
    writeDraft({...draftRef.current, pending});
    try {
      let response: OrderData;
      if (pending.kind === 'upload') {
        const data = new FormData(); data.set('operation_id', pending.operationId); data.set('expected_version', String(pending.version)); data.set('kind', pending.photoKind); data.set('source', pending.source);
        data.set('file', dataBlob(pending.fileData), 'repair.jpg');
        // capture-file cannot prove actual capture time: do not invent captured_at.
        response = await api.upload<OrderData>(pending.path, data);
      } else response = await api[pending.method]<OrderData>(pending.path, pending.body);
      if (!mounted.current || draftRef.current.key !== operationKey || response.id !== currentId.current) return;
      acceptOrder(response);
      let next = {...draftRef.current, pending: null};
      if (pending.kind === 'upload' && pending.photoKind === 'after') {
        const newIds = response.photos.filter(photo => photo.kind === 'after' && photo.result_version === null && !pending.beforePhotoIds.includes(photo.id)).map(photo => photo.id);
        next.photoIds = Array.from(new Set([...next.photoIds, ...newIds])); next.dirty = true;
      }
      if (pending.kind === 'json' && pending.finish === 'result') next = {...next, work: '', faultId: '', materials: [], none: false, photoIds: [], resultComment: '', dirty: false, sourceVersion: response.result_version};
      if (pending.kind === 'json' && pending.finish === 'comment') next.discussion = '';
      writeDraft(next); setConflict(false); setDialog(null); setNotice(pending.kind === 'upload' ? 'Фото сохранено.' : pending.kind === 'json' && pending.finish === 'result' ? 'Результат отправлен на проверку. Решение примет мастер.' : 'Изменение сохранено.');
      onChanged(); void reload(); void report.reload();
    } catch (err) {
      if (!mounted.current || draftRef.current.key !== operationKey) return;
      const message = err instanceof Error ? err.message : 'Не удалось выполнить действие';
      setError(message); setDialogError(message);
      const status = err instanceof ApiError ? err.status : 0;
      if (status === 409) {writeDraft({...draftRef.current, pending: pending.kind === 'upload' ? {...pending, versionConflict: true} : null}); setConflict(true);}
      else if (status > 0 && status < 500) writeDraft({...draftRef.current, pending: null});
      // Unknown network/server outcomes retain the exact operation and payload.
    } finally {if (draftRef.current.key === operationKey) {busyRef.current = false; if (mounted.current) setBusy(null);}}
  }
  function mutate(label: string, suffix: string, values: Record<string, unknown>, finish: JsonPending['finish'] = 'none', method: 'post' | 'patch' = 'post') {
    const current = orderRef.current;
    if (!current || conflict || draftRef.current.pending || busyRef.current) return;
    void perform({kind: 'json', method, path: `/orders/${current.id}${suffix}`, body: {operation_id: operationId(), expected_version: current.version, ...values}, label, finish});
  }
  function retryPending() {
    const pending = draftRef.current.pending, current = orderRef.current;
    if (!pending || !current || conflict || busyRef.current) return;
    // A known version conflict failed without saving. A new upload is an explicit
    // user action; an uncertain network outcome still reuses the original operation.
    const retry = pending.kind === 'upload' && pending.versionConflict ? {...pending, operationId: operationId(), version: current.version, beforePhotoIds: current.photos.map(photo => photo.id), versionConflict: false} : pending;
    void perform(retry);
  }
  async function reconcile() {
    if (busyRef.current) return;
    busyRef.current = true; setBusy('Обновляем карточку…');
    try {
      if (!await reload() || !mounted.current) return;
      setConflict(false); setError(null); setDialogError(null);
      setNotice(draftRef.current.pending?.kind === 'upload' && draftRef.current.pending.versionConflict ? 'Карточка обновлена. Снимок сохранён в черновике; отправьте его повторно.' : draftRef.current.pending ? 'Карточка обновлена. Неподтверждённую отправку можно повторить тем же запросом.' : 'Карточка обновлена. Ваш ввод сохранён: проверьте данные перед новой отправкой.');
    } finally {busyRef.current = false; if (mounted.current) setBusy(null);}
  }

  function openDialog(mode: DialogMode) {
    const current = orderRef.current; if (!current) return;
    setDialogError(null); setDialogFields({...blankDialog, score: String(current.master_score ?? current.assessment?.score ?? ''), priority: current.priority, due: dueInput(current.due_at), assignment: current.brigade_id ? 'brigade' : 'person', executorId: current.executor_id || '', brigadeId: current.brigade_id || ''}); setDialog(mode);
    if (mode === 'repeat') {
      setRepeatLoading(true); setRepeatOptions([]);
      const path = `/orders?equipment_id=${encodeURIComponent(current.equipment_id)}&status=closed&limit=200`;
      void (async () => {
        const first = await api.get<List<OrderData> & {total: number}>(path);
        const rows = [...first.items];
        for (let offset = 200; offset < first.total; offset += 200) {
          if (!mounted.current || current.id !== currentId.current) return;
          const page = await api.get<List<OrderData>>(`${path}&offset=${offset}`);
          rows.push(...page.items);
        }
        if (!mounted.current || current.id !== currentId.current) return;
        const unique = Array.from(new Map(rows.map(previous => [previous.id, previous])).values());
        setRepeatOptions(unique.filter(previous => previous.id !== current.id && previous.fault_code_id === current.fault_code_id && Boolean(previous.closed_at) && new Date(previous.closed_at as string).getTime() <= new Date(current.created_at).getTime() && new Date(current.created_at).getTime() - new Date(previous.closed_at as string).getTime() <= 7 * 86400_000));
      })().catch(err => {if (mounted.current && current.id === currentId.current) setDialogError(err instanceof Error ? err.message : 'Не удалось найти предыдущие ремонты');}).finally(() => {if (mounted.current && current.id === currentId.current) setRepeatLoading(false);});
    }
  }
  function field(values: Partial<DialogFields>) {setDialogFields(current => ({...current, ...values}));}
  function submitDialog(event: FormEvent) {
    event.preventDefault(); const current = orderRef.current; if (!current || !dialog) return;
    const reason = dialogFields.reason.trim();
    const reasonRequired = dialog !== 'decision' || dialogFields.verdict !== 'accepted' || Number(dialogFields.score) !== current.assessment?.score || dialogFields.verdict !== current.assessment?.verdict;
    if (reasonRequired && reason.length < 2) {setDialogError('Укажите причину или пояснение — минимум 2 символа.'); return;}
    if (dialog === 'pause' || dialog === 'reject') mutate(dialog === 'pause' ? 'Приостанавливаем…' : 'Отправляем отказ…', '/actions', {action: dialog, reason}, 'dialog');
    else if (dialog === 'cancel') mutate('Отменяем наряд…', '', {cancel: true, reason}, 'dialog', 'patch');
    else if (dialog === 'downtime-start' || dialog === 'downtime-end') mutate('Сохраняем состояние оборудования…', '/downtime', {action: dialog === 'downtime-start' ? 'start' : 'end', reason}, 'dialog');
    else if (dialog === 'decision') {
      const score = dialogFields.score ? Number(dialogFields.score) : null;
      if (dialogFields.verdict !== 'requires_rework' && (!score || score < 1 || score > 5)) {setDialogError('Выберите оценку качества от 1 до 5.'); return;}
      mutate('Сохраняем решение мастера…', '/decision', {verdict: dialogFields.verdict, score, reason: reason || null}, 'dialog');
    } else if (dialog === 'rejection') mutate('Сохраняем решение по отказу…', '/rejection-decision', {justified: dialogFields.justified === 'yes', reason}, 'dialog');
    else if (dialog === 'repeat') {
      if (!dialogFields.previousOrderId) {setDialogError('Выберите предыдущий наряд.'); return;}
      mutate('Подтверждаем связь ремонтов…', '/confirm-repeat', {previous_order_id: dialogFields.previousOrderId, reason}, 'dialog');
    } else if (dialog === 'edit') {
      const values: Record<string, unknown> = {reason};
      if (dialogFields.priority !== current.priority) values.priority = dialogFields.priority;
      const due = new Date(`${dialogFields.due}:00+05:00`);
      if (!dialogFields.due || Number.isNaN(due.getTime())) {setDialogError('Укажите корректный срок.'); return;}
      if (dialogFields.due !== dueInput(current.due_at)) values.due_at = due.toISOString();
      if (dialogFields.assignment === 'person') {
        if (!dialogFields.executorId) {setDialogError('Выберите исполнителя.'); return;}
        if (dialogFields.executorId !== current.executor_id || current.brigade_id) values.executor_id = dialogFields.executorId;
      } else {
        if (!dialogFields.brigadeId) {setDialogError('Выберите бригаду.'); return;}
        if (dialogFields.brigadeId !== current.brigade_id || current.executor_id) values.brigade_id = dialogFields.brigadeId;
      }
      if (Object.keys(values).length === 1) {setDialogError('Вы ещё ничего не изменили.'); return;}
      mutate('Обновляем наряд…', '', values, 'dialog', 'patch');
    }
  }
  async function selectedPhoto(event: ChangeEvent<HTMLInputElement>, source: 'camera' | 'gallery') {
    const file = event.target.files?.[0]; event.target.value = ''; if (!file || !orderRef.current || busyRef.current) return;
    busyRef.current = true; setBusy('Подготавливаем фото…'); setError(null);
    const current = orderRef.current, kind = photoKind.current;
    try {
      const compressed = await compress(file), fileData = await blobData(compressed);
      if (!mounted.current || current.id !== currentId.current) return;
      const pending: UploadPending = {kind: 'upload', path: `/orders/${current.id}/photos`, operationId: operationId(), version: current.version, label: 'Сохраняем фото…', photoKind: kind, source, fileData, beforePhotoIds: current.photos.map(photo => photo.id)};
      busyRef.current = false; setBusy(null); await perform(pending);
    } catch (err) {setError(err instanceof Error ? err.message : 'Не удалось подготовить фото');} finally {busyRef.current = false; if (mounted.current) setBusy(null);}
  }
  function choosePhoto(kind: 'before' | 'after', source: 'camera' | 'gallery') {photoKind.current = kind; (source === 'camera' ? camera : gallery).current?.click();}
  function submitResult(event: FormEvent) {
    event.preventDefault(); const current = orderRef.current, value = draftRef.current; if (!current) return;
    if (!value.work.trim() || !value.faultId) {setError('Опишите выполненные работы и выберите шифр неисправности.'); return;}
    const rows = value.materials.filter(row => row.materialId);
    if (!value.none && (rows.length === 0 || rows.length !== value.materials.length)) {setError('Заполните материалы или отметьте, что они не использовались.'); return;}
    if (new Set(rows.map(row => row.materialId)).size !== rows.length) {setError('Один материал нельзя добавить дважды.'); return;}
    const quantities = rows.map(row => row.quantity.trim().replace(',', '.'));
    if (quantities.some(quantity => !/^\d+(\.\d{1,3})?$/.test(quantity) || Number(quantity) <= 0 || Number(quantity) > 1_000_000)) {setError('Количество должно быть больше нуля; допускается до 3 знаков после запятой.'); return;}
    const available = new Set(current.photos.filter(photo => photo.kind === 'after' && photo.result_version === null && current.events.some(event => event.action === 'photo_uploaded' && event.actor_id === user.id && event.details.photo_id === photo.id)).map(photo => photo.id));
    const photoIds = value.photoIds.filter(id => available.has(id));
    mutate('Отправляем результат…', '/result', {work_description: value.work.trim(), fault_code_id: value.faultId, materials_none: value.none, materials: value.none ? [] : rows.map((row, index) => ({material_id: row.materialId, quantity: quantities[index]})), photo_ids: photoIds, comment: value.resultComment.trim() || null}, 'result');
  }
  async function downloadReport() {setBusy('Готовим файл…'); try {await api.download(`/reports/orders/${orderId}/export.xlsx`, `Наряд-${order?.number || orderId}.xlsx`);} catch (err) {setError(err instanceof Error ? err.message : 'Не удалось скачать отчёт');} finally {if (mounted.current) setBusy(null);}}

  const current = order?.id === orderId ? order : null;
  const locked = Boolean(busy || draft.pending || conflict);
  const master = user.role === 'master' || user.role === 'admin';
  const ownWorker = Boolean(current && user.role === 'worker' && (current.executor_id === user.id || current.brigade_id && current.brigade_id === currentUser.brigade_id || current.working_executor_id === user.id) && (!current.working_executor_id || current.working_executor_id === user.id));
  const workerResult = Boolean(current && ownWorker && resultStates.has(current.status));
  const ownDraftPhotos = current?.photos.filter(photo => photo.kind === 'after' && photo.result_version === null && current.events.some(event => event.action === 'photo_uploaded' && event.actor_id === user.id && event.details.photo_id === photo.id)) || [];
  const publishedPhotos = current?.photos.filter(photo => photo.kind === 'before' || photo.result_version === current.result_version && photo.result_version !== null) || [];
  const resultEvent = current?.events.filter(item => item.action === 'performed' && item.details.result_version === current.result_version).at(-1);
  const reportConfirmsOwnQuality = Boolean(report.data?.quality && (report.data.quality.source !== 'unavailable' || report.data.quality.verdict && verdicts[report.data.quality.verdict]));
  const ownFreshReport = Boolean(current && report.data?.audience === 'worker' && report.data.loaded_order_version === current.version && report.data.loaded_result_version === current.result_version && reportConfirmsOwnQuality);
  const canSeePersonalResult = user.role !== 'worker' || (resultEvent ? resultEvent.actor_id === user.id : ownFreshReport);
  const selectedFault = faultCatalog.data?.items.find(item => item.id === current?.fault_code_id);
  const userNames = new Map((employees.data?.items || []).map(person => [person.id, person.full_name])); userNames.set(user.id, user.full_name);
  const latestRejection = current?.events.filter(event => event.action === 'rejected').at(-1);
  const downtimeEvent = current?.events.filter(event => event.action === 'downtime_started' || event.action === 'downtime_ended').at(-1);
  const hasOpenDowntime = report.data?.downtime ? report.data.downtime.intervals.some(interval => interval.ended_at === null) : downtimeEvent ? downtimeEvent.action === 'downtime_started' : null;
  const dialogTitles: Record<DialogMode, string> = {pause: 'Почему приостановили работу?', reject: 'Почему не можете принять наряд?', decision: 'Приёмка мастером', edit: 'Изменить назначение и срок', cancel: 'Отменить наряд', 'downtime-start': 'Отметить остановку оборудования', 'downtime-end': 'Подтвердить фактический запуск', rejection: 'Рассмотреть причину отказа', repeat: 'Подтвердить повторную неисправность'};
  const currentDraft = draft.key === key ? draft : emptyDraft(key);

  if (!current) return <section className="panel stack" aria-busy={loading}><button type="button" className="button secondary" style={tap} onClick={onBack}>← Назад</button>{loading ? <p>Загружаем наряд…</p> : <div className="error" role="alert">{loadError || error || 'Наряд недоступен'}</div>}{!loading && !lostAccess && <button type="button" className="button" style={tap} onClick={() => void reload()}>Повторить загрузку</button>}</section>;
  return <section className="stack" aria-busy={Boolean(busy)}>
    <input ref={camera} type="file" accept="image/*" capture="environment" hidden onChange={event => void selectedPhoto(event, 'camera')} />
    <input ref={gallery} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={event => void selectedPhoto(event, 'gallery')} />
    <div className="toolbar"><button className="button secondary" style={tap} type="button" disabled={Boolean(busy)} onClick={onBack}>← К нарядам</button>{canReadReport && <button className="button secondary" style={tap} type="button" disabled={Boolean(busy)} onClick={() => void downloadReport()}>Скачать отчёт</button>}</div>
    <header className="panel stack">
      <div className="toolbar"><h1 style={{margin: 0}}>Наряд №{current.number}</h1><span className={`badge status-${current.status}`}>{STATUS[current.status] || current.status}</span></div>
      <h2 style={{margin: 0}}>{current.equipment?.name || 'Оборудование'}</h2>
      <div className="toolbar"><span className={`badge ${current.priority === 'emergency' ? 'status-overdue' : ''}`}>{PRIORITY[current.priority] || current.priority}</span><span className="muted">{sections.data?.items.find(section => section.id === current.section_id)?.name || 'Участок'} · {current.equipment?.inventory_number || ''}</span></div>
      <p style={{whiteSpace: 'pre-wrap', margin: 0}}>{current.description}</p>
      {current.comment && <p className="muted" style={{whiteSpace: 'pre-wrap', margin: 0}}>{current.comment}</p>}
      <div className="form-grid"><div><strong>Срок: {date(current.due_at)}</strong>{current.overdue && <p className="error" style={{marginBottom: 0}}>Просрочен на {current.overdue_minutes} мин.</p>}</div><div className="muted">Норматив: {current.norm_minutes} мин.<br />Исполнитель: {current.executor_id ? userNames.get(current.executor_id) || 'Сотрудник' : brigades.data?.items.find(item => item.id === current.brigade_id)?.name || 'Бригада'}</div></div>
      {current.synthetic && <span className="badge">Демонстрационные данные</span>}
    </header>
    <div aria-live="polite" className="stack">
      {busy && <div className="notice" role="status">{busy}</div>}
      {notice && <div className="notice" role="status">{notice}</div>}
      {storageWarning && <div className="notice">Черновик остаётся в открытом окне. Не закрывайте страницу до отправки.</div>}
      {(error || loadError) && <div className="error" role="alert">{error || loadError}</div>}
      {currentDraft.pending?.kind === 'upload' && currentDraft.pending.fileData.startsWith('data:image/jpeg;base64,') && <figure className="panel stack" style={{margin: 0}}><img src={currentDraft.pending.fileData} alt="Неотправленное фото в черновике" style={{width: '100%', maxHeight: 240, objectFit: 'contain', borderRadius: 12}} /><figcaption className="muted">Снимок сохранён в черновике до подтверждения загрузки.</figcaption></figure>}
      {(conflict || currentDraft.pending) && <div className="notice stack"><p style={{margin: 0}}>{conflict ? 'Наряд изменился. Обновите карточку: ваш ввод останется.' : currentDraft.pending?.kind === 'upload' && currentDraft.pending.versionConflict ? 'Снимок остался в черновике. Отправьте его повторно после проверки карточки.' : 'Отправка не подтверждена. Повтор использует тот же запрос и не создаёт вторую запись.'}</p><div className="toolbar">{currentDraft.pending && <button className="button primary" style={tap} type="button" disabled={Boolean(busy) || conflict} onClick={retryPending}>Повторить отправку</button>}<button className="button secondary" style={tap} type="button" disabled={Boolean(busy)} onClick={() => void reconcile()}>Обновить и проверить</button></div></div>}
    </div>
    {ownWorker && !terminal.has(current.status) && <div className="panel stack">
      {!currentUser.on_shift && <div className="notice">Вы отмечены как не на смене. Мастер может изменить отметку; отказ с причиной доступен.</div>}
      <div className="toolbar">
        {current.status === 'issued' && <button type="button" className="button primary" style={tap} disabled={locked || !currentUser.on_shift} onClick={() => mutate('Принимаем наряд…', '/actions', {action: 'accept'})}>Принять наряд</button>}
        {['accepted', 'queued', 'rework'].includes(current.status) && <button type="button" className="button primary" style={tap} disabled={locked || !currentUser.on_shift} onClick={() => mutate('Начинаем работу…', '/actions', {action: 'start'})}>{current.status === 'rework' ? 'Начать доработку' : 'Начать работу'}</button>}
        {['issued', 'accepted'].includes(current.status) && <button type="button" className="button secondary" style={tap} disabled={locked || !currentUser.on_shift} onClick={() => mutate('Ставим в очередь…', '/actions', {action: 'queue'})}>Поставить в очередь</button>}
        {current.status === 'in_progress' && <button type="button" className="button secondary" style={tap} disabled={locked} onClick={() => openDialog('pause')}>Приостановить</button>}
        {current.status === 'paused' && <button type="button" className="button primary" style={tap} disabled={locked || !currentUser.on_shift} onClick={() => mutate('Продолжаем работу…', '/actions', {action: 'resume'})}>Продолжить работу</button>}
        {['issued', 'accepted', 'queued'].includes(current.status) && <button type="button" className="button danger" style={tap} disabled={locked} onClick={() => openDialog('reject')}>Не могу выполнить</button>}
      </div>
      {current.status === 'queued' && <p className="muted" style={{margin: 0}}>Задание в вашей очереди. Начните его после текущей работы.</p>}
    </div>}
    {user.role === 'worker' && !ownWorker && !terminal.has(current.status) && <div className="notice">Этот наряд уже принят другим исполнителем. Доступен просмотр.</div>}
    {master && !terminal.has(current.status) && <div className="panel toolbar">{editable.has(current.status) && <button className="button secondary" style={tap} type="button" disabled={locked} onClick={() => openDialog('edit')}>Переназначить / изменить срок</button>}<button className="button danger" style={tap} type="button" disabled={locked} onClick={() => openDialog('cancel')}>Отменить наряд</button></div>}
    {master && ['issued', 'accepted', 'queued'].includes(current.status) && <div className="panel stack"><h2>Фото неисправности</h2><div className="toolbar"><button className="button primary" style={tap} type="button" disabled={locked || current.photos.filter(photo => photo.kind === 'before').length >= 5} onClick={() => choosePhoto('before', 'camera')}>Снять фото неисправности</button><button className="button secondary" style={tap} type="button" disabled={locked || current.photos.filter(photo => photo.kind === 'before').length >= 5} onClick={() => choosePhoto('before', 'gallery')}>Из галереи</button></div><p className="muted">Фото поможет исполнителю увидеть неисправность. До 5 снимков.</p></div>}
    {workerResult && <form className="panel stack" onSubmit={submitResult}>
      <h2>Результат работы</h2>
      {current.status === 'rework' && <div className="notice">Исправьте замечания и отправьте результат повторно. Для новой версии используйте новое подтверждающее фото.</div>}
      <fieldset disabled={locked} style={{border: 0, padding: 0, margin: 0, minWidth: 0}} className="stack">
        <label className="field">Что выполнено<textarea value={currentDraft.work} onChange={event => changeDraft({work: event.target.value})} rows={4} maxLength={6000} placeholder="Что заменили, отрегулировали и проверили" required /></label>
        <label className="field">Шифр неисправности<select value={currentDraft.faultId} onChange={event => changeDraft({faultId: event.target.value})} required><option value="">Выберите неисправность</option>{faultCatalog.data?.items.filter(item => item.active !== false).map(item => <option key={item.id} value={item.id}>{item.code} — {item.name}</option>)}</select></label>
        {faultCatalog.error && <div className="error">{faultCatalog.error}<button className="button secondary" style={tap} type="button" onClick={() => void faultCatalog.reload()}>Загрузить шифры</button></div>}
        <h3 style={{margin: 0}}>Материалы и запчасти</h3>
        <label style={{...tap, display: 'flex', alignItems: 'center', gap: 12}}><input type="checkbox" checked={currentDraft.none} disabled={currentDraft.materials.length > 0} onChange={event => changeDraft({none: event.target.checked})} />Материалы не использовались</label>
        {currentDraft.materials.map((row, index) => <div key={row.key} className="form-grid"><label className="field">Материал {index + 1}<select value={row.materialId} onChange={event => changeDraft({materials: currentDraft.materials.map(item => item.key === row.key ? {...item, materialId: event.target.value} : item)})} required><option value="">Выберите материал</option>{materialCatalog.data?.items.filter(item => item.active !== false).map(item => <option key={item.id} value={item.id} disabled={currentDraft.materials.some(other => other.key !== row.key && other.materialId === item.id)}>{item.name} · {item.unit}</option>)}</select></label><label className="field">Количество {materialCatalog.data?.items.find(item => item.id === row.materialId)?.unit || ''}<input inputMode="decimal" value={row.quantity} onChange={event => changeDraft({materials: currentDraft.materials.map(item => item.key === row.key ? {...item, quantity: event.target.value} : item)})} maxLength={14} required /></label><button className="button secondary" style={tap} type="button" onClick={() => changeDraft({materials: currentDraft.materials.filter(item => item.key !== row.key)})}>Убрать материал {index + 1}</button></div>)}
        <button className="button secondary" style={tap} type="button" disabled={currentDraft.materials.length >= 40} onClick={() => changeDraft({none: false, materials: [...currentDraft.materials, {key: operationId(), materialId: '', quantity: '1'}]})}>+ Добавить материал</button>
        {materialCatalog.error && <div className="error">{materialCatalog.error}<button className="button secondary" style={tap} type="button" onClick={() => void materialCatalog.reload()}>Загрузить материалы</button></div>}
        <h3 style={{margin: 0}}>Фото после работы</h3><div className="toolbar"><button className="button primary" style={tap} type="button" disabled={ownDraftPhotos.length >= 5} onClick={() => choosePhoto('after', 'camera')}>Снять фото результата</button><button className="button secondary" style={tap} type="button" disabled={ownDraftPhotos.length >= 5} onClick={() => choosePhoto('after', 'gallery')}>Из галереи</button></div>
        {current.work_type === 'unplanned' && !currentDraft.photoIds.length && <div className="notice">Для внеплановой работы нужно фото после. Без него мастер не сможет принять результат.</div>}
        <div className="form-grid">{ownDraftPhotos.map(photo => <div key={photo.id} className="stack"><SecureImage photo={photo} caption="Подтверждение результата" /><label style={{...tap, display: 'flex', alignItems: 'center', gap: 12}}><input type="checkbox" checked={currentDraft.photoIds.includes(photo.id)} onChange={event => changeDraft({photoIds: event.target.checked ? [...currentDraft.photoIds, photo.id] : currentDraft.photoIds.filter(id => id !== photo.id)})} />Включить в результат</label></div>)}</div>
        <label className="field">Комментарий к результату<textarea rows={2} maxLength={4000} value={currentDraft.resultComment} onChange={event => changeDraft({resultComment: event.target.value})} placeholder="Что важно знать мастеру" /></label>
        <button className="button primary" style={tap} type="submit">Исполнено — отправить на проверку</button><p className="muted" style={{margin: 0}}>Ввод сохраняется на этом устройстве. Наряд закроет мастер после проверки.</p>
      </fieldset>
    </form>}
    {(current.result_version > 0 || current.assessment) && <section className="panel stack"><h2>Результат и проверка</h2>{current.work_description && <p style={{whiteSpace: 'pre-wrap', margin: 0}}>{current.work_description}</p>}<p style={{margin: 0}}><strong>Шифр неисправности: </strong>{!current.fault_code_id ? 'Не указан' : selectedFault ? `${selectedFault.code} — ${selectedFault.name}` : faultCatalog.loading ? 'Загружаем справочник…' : 'Не найден в справочнике'}</p>{current.materials.length > 0 && <ul>{current.materials.map(row => <li key={row.material_id}>{materialCatalog.data?.items.find(item => item.id === row.material_id)?.name || 'Материал'} — {row.quantity} {row.unit}</li>)}</ul>}{current.materials_none && <p className="muted">Материалы не использовались.</p>}
      {!canSeePersonalResult ? <div className="notice">Персональная оценка этого результата доступна его исполнителю и мастеру.</div> : current.assessment ? <><div className="toolbar"><span className="badge">{current.assessment.engine === 'llm' ? 'Рекомендация ИИ' : current.assessment.engine === 'seed' ? 'Тестовая историческая оценка' : 'Проверка по правилам'}</span><strong>{verdicts[current.assessment.verdict] || current.assessment.verdict}</strong></div><p style={{whiteSpace: 'pre-wrap', margin: 0}}>{current.assessment.explanation}</p>{current.assessment.engine === 'rules' && <div className="notice">Правила проверяют заполнение и известные ограничения. Качество ремонта оценивает мастер.</div>}{current.assessment.score != null && <p>Предварительная оценка: <strong>{current.assessment.score} / 5</strong></p>}{current.assessment.good.length > 0 && <div><strong>Что сделано хорошо</strong><ul>{current.assessment.good.map((text, index) => <li key={index}>{text}</li>)}</ul></div>}{current.assessment.improve.length > 0 && <div><strong>Что проверить или улучшить</strong><ul>{current.assessment.improve.map((text, index) => <li key={index}>{text}</li>)}</ul></div>}{current.assessment.findings.map((finding, index) => <div className={finding.severity === 'error' ? 'error' : 'notice'} key={`${finding.code}-${index}`}>{finding.message}</div>)}</> : <div className="notice">Результат сохранён. Проверка ещё не завершена; решение мастера пока недоступно.</div>}
      {canSeePersonalResult && current.master_verdict && <div className="notice"><strong>Решение мастера: {verdicts[current.master_verdict] || current.master_verdict}</strong>{current.master_score != null && <p>Оценка: {current.master_score} / 5</p>}{current.master_comment && <p>{current.master_comment}</p>}</div>}
      {report.data?.timing && <p className="muted">Работа: {Math.round(report.data.timing.active_minutes)} мин. · Норматив: {report.data.timing.norm_minutes} мин. {report.data.timing.on_time === true ? '· Отправлено в срок' : report.data.timing.on_time === false ? '· Отправлено после срока' : ''}</p>}
      {master && current.assessment && ['ai_review', 'rework'].includes(current.status) && <button type="button" className="button primary" style={tap} disabled={locked} onClick={() => openDialog('decision')}>Проверить и принять решение</button>}
    </section>}
    {publishedPhotos.length > 0 && <section className="panel stack"><h2>Фото наряда</h2><div className="form-grid">{publishedPhotos.map(photo => <SecureImage key={photo.id} photo={photo} caption={photo.kind === 'before' ? 'До ремонта' : 'После ремонта'} />)}</div></section>}
    <details className="panel stack"><summary style={{...tap, cursor: 'pointer', paddingBlock: 12}}>Комментарии и состояние оборудования</summary>
      {(ownWorker || master) && <div className="stack"><h3>Оборудование</h3><p className="muted">{hasOpenDowntime === true ? 'Оборудование отмечено как остановленное.' : hasOpenDowntime === false ? 'Открытый простой не зарегистрирован.' : 'Отметка о простое уточняется отдельно от статуса наряда.'}</p><div className="toolbar">{!terminal.has(current.status) && hasOpenDowntime !== true && <button className="button secondary" style={tap} type="button" disabled={locked} onClick={() => openDialog('downtime-start')}>Оборудование остановлено</button>}{hasOpenDowntime !== false && <button className="button secondary" style={tap} type="button" disabled={locked} onClick={() => openDialog('downtime-end')}>Оборудование запущено</button>}</div>{report.data?.downtime && <p className="muted">Подтверждённый простой: {Math.round(report.data.downtime.minutes)} мин.</p>}
        <form className="stack" onSubmit={event => {event.preventDefault(); if (currentDraft.discussion.trim().length < 2) {setError('Напишите комментарий — минимум 2 символа.'); return;} mutate('Сохраняем комментарий…', '/comments', {text: currentDraft.discussion.trim()}, 'comment');}}><label className="field">Комментарий к наряду<textarea rows={3} value={currentDraft.discussion} maxLength={3000} disabled={locked} onChange={event => changeDraft({discussion: event.target.value}, false)} placeholder="Например: ждём подшипник со склада" /></label><button className="button secondary" style={tap} disabled={locked || currentDraft.discussion.trim().length < 2} type="submit">Добавить комментарий</button></form>
      </div>}
      {master && latestRejection && <div className="stack"><h3>Последний отказ</h3><p>{latestRejection.reason} · {date(latestRejection.occurred_at)}</p><button className="button secondary" style={tap} type="button" disabled={locked} onClick={() => openDialog('rejection')}>Рассмотреть причину отказа</button></div>}
      {master && current.work_type === 'unplanned' && current.fault_code_id && <button className="button secondary" style={tap} type="button" disabled={locked} onClick={() => openDialog('repeat')}>Связать с повторной неисправностью</button>}
    </details>
    <details className="panel"><summary style={{...tap, cursor: 'pointer', paddingBlock: 12}}>История наряда · {current.events.length} событий</summary><ol className="stack">{current.events.map(event => <li key={event.id}><strong>{actionNames[event.action] || event.action}</strong><div className="muted">{date(event.occurred_at)} · {event.actor_id ? userNames.get(event.actor_id) || (event.actor_id === current.master_id ? 'Мастер' : 'Сотрудник') : 'Система'}</div>{event.reason && <p style={{whiteSpace: 'pre-wrap', marginBlock: 8}}>{event.reason}</p>}{event.to_status && event.to_status !== event.from_status && <span className="badge">{STATUS[event.to_status] || event.to_status}</span>}</li>)}</ol></details>
    {dialog && <Modal title={dialogTitles[dialog]} busy={Boolean(busy)} onClose={() => setDialog(null)}><form className="stack" onSubmit={submitDialog}>
      {dialog === 'cancel' && <div className="notice">Отменённый наряд нельзя продолжить. Отмена не означает, что оборудование восстановлено.</div>}
      {dialog === 'decision' && <><label className="field">Решение<select value={dialogFields.verdict} disabled={locked} onChange={event => field({verdict: event.target.value})}>{Object.entries(verdicts).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="field">Оценка мастера<select value={dialogFields.score} disabled={locked} onChange={event => field({score: event.target.value})}><option value="">Выберите оценку</option>{[5, 4, 3, 2, 1].map(score => <option key={score} value={score}>{score} / 5</option>)}</select></label><p className="muted">Итоговое решение принимаете вы. Для замечаний, доработки или изменения оценки нужно пояснение.</p></>}
      {dialog === 'edit' && <><label className="field">Назначить<select value={dialogFields.assignment} disabled={locked} onChange={event => field({assignment: event.target.value as 'person' | 'brigade'})}><option value="person">Исполнителю</option><option value="brigade">Бригаде</option></select></label>{dialogFields.assignment === 'person' ? <label className="field">Исполнитель<select value={dialogFields.executorId} disabled={locked} onChange={event => field({executorId: event.target.value})}><option value="">Выберите сотрудника</option>{employees.data?.items.filter(item => item.role === 'worker' && item.active).map(item => <option key={item.id} value={item.id}>{item.full_name} · {item.specialty} · {!item.on_shift ? 'не на смене' : item.availability === 'busy' ? `занят${item.current_order_number ? ' №' + item.current_order_number : ''}` : item.queue_count ? `очередь ${item.queue_count}` : 'свободен'}</option>)}</select></label> : <label className="field">Бригада<select value={dialogFields.brigadeId} disabled={locked} onChange={event => field({brigadeId: event.target.value})}><option value="">Выберите бригаду</option>{brigades.data?.items.filter(item => item.active !== false).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}<label className="field">Приоритет<select value={dialogFields.priority} disabled={locked} onChange={event => field({priority: event.target.value})}>{Object.entries(PRIORITY).filter(([value]) => current.work_type === 'unplanned' || value !== 'emergency').map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="field">Срок · время Казахстана<input type="datetime-local" value={dialogFields.due} disabled={locked} onChange={event => field({due: event.target.value})} required /></label></>}
      {dialog === 'rejection' && <label className="field">Оценка причины<select value={dialogFields.justified} disabled={locked} onChange={event => field({justified: event.target.value as 'yes' | 'no'})}><option value="yes">Отказ обоснован</option><option value="no">Отказ не обоснован</option></select></label>}
      {dialog === 'repeat' && <><p className="muted">Связь с прошлым ремонтом подтверждает мастер. Один шифр сам по себе не доказывает вину исполнителя.</p><label className="field">Предыдущий ремонт<select value={dialogFields.previousOrderId} disabled={locked || repeatLoading} onChange={event => field({previousOrderId: event.target.value})}><option value="">{repeatLoading ? 'Ищем предыдущие ремонты…' : 'Выберите наряд'}</option>{repeatOptions.map(previous => <option key={previous.id} value={previous.id}>№{previous.number} · {date(previous.closed_at as string)}</option>)}</select></label>{!repeatLoading && !repeatOptions.length && <div className="empty">Нет подходящего закрытого ремонта с этим шифром за предыдущие 7 дней.</div>}</>}
      <label className="field">{dialog === 'decision' ? 'Пояснение к решению' : 'Причина / пояснение'}<textarea value={dialogFields.reason} rows={3} maxLength={dialog === 'decision' ? 3000 : 2000} disabled={locked} onChange={event => field({reason: event.target.value})} placeholder={dialog === 'pause' ? 'Например: нет запчасти, ждём остановки оборудования' : 'Коротко поясните действие'} /></label>
      {dialogError && <div className="error" role="alert">{dialogError}</div>}{conflict && <button className="button secondary" style={tap} type="button" disabled={Boolean(busy)} onClick={() => void reconcile()}>Обновить карточку, сохранив ввод</button>}{currentDraft.pending && <button className="button primary" style={tap} type="button" disabled={Boolean(busy) || conflict} onClick={retryPending}>Повторить ту же отправку</button>}
      <div className="toolbar"><button className={`button ${dialog === 'cancel' || dialog === 'reject' ? 'danger' : 'primary'}`} style={tap} type="submit" disabled={locked || dialog === 'repeat' && (repeatLoading || !repeatOptions.length)}>{dialog === 'cancel' ? 'Подтвердить отмену' : dialog === 'decision' ? 'Сохранить решение' : 'Сохранить'}</button><button className="button secondary" style={tap} type="button" disabled={Boolean(busy)} onClick={() => setDialog(null)}>Вернуться без изменений</button></div>
    </form></Modal>}
  </section>;
}

export default OrderDetail;
