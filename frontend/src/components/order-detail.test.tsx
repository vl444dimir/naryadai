import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {api, ApiError} from '../lib/api';
import type {User} from '../lib/types';
import OrderDetail from './OrderDetail';

vi.mock('../lib/api', () => {
  class MockApiError extends Error {
    constructor(public status: number, message: string, public detail: unknown = null) {super(message);}
  }
  return {ApiError: MockApiError, api: {get: vi.fn(), post: vi.fn(), patch: vi.fn(), upload: vi.fn(), getBlob: vi.fn(), download: vi.fn()}};
});

const orderId = '00000000-0000-4000-8000-000000000071';
const workerId = '00000000-0000-4000-8000-000000000001';
const masterId = '00000000-0000-4000-8000-000000000002';
const faultId = '00000000-0000-4000-8000-000000000003';
const brigadeId = '00000000-0000-4000-8000-000000000004';
const otherWorkerId = '00000000-0000-4000-8000-000000000005';
const materialId = '00000000-0000-4000-8000-000000000006';
const worker: User = {id: workerId, login: 'worker', full_name: 'Иван Рабочий', role: 'worker', specialty: 'Слесарь', grade: 4, brigade_id: brigadeId, on_shift: true, active: true};
const master: User = {...worker, id: masterId, login: 'master', full_name: 'Мастер Смены', role: 'master'};
type FixtureOrder = Record<string, any>;
let serverOrder: FixtureOrder;
let signedInUser: User;
let revoked: boolean;
const onChanged = vi.fn();
const onBack = vi.fn();

function order(overrides: FixtureOrder = {}): FixtureOrder {
  return {id: orderId, number: 71, version: 1, status: 'issued', work_type: 'unplanned', priority: 'normal', description: 'Заменить подшипник привода', equipment_id: 'equipment-1', section_id: 'section-1', executor_id: workerId, brigade_id: null, working_executor_id: null, master_id: masterId, created_at: '2026-10-08T04:00:00Z', due_at: '2026-10-08T06:35:27Z', norm_minutes: 60, overdue: false, overdue_minutes: 0, result_version: 0, work_description: null, fault_code_id: null, materials_none: false, equipment: {name: 'Конвейер 7', inventory_number: 'К-007'}, photos: [], events: [], materials: [], assessment: null, ...overrides};
}

function event(id: number, action: string, actorId: string | null = workerId, details = {}) {
  return {id, actor_id: actorId, action, occurred_at: '2026-10-08T05:00:00Z', details};
}

function photo(id: string, resultVersion: number | null, kind = 'after') {
  return {id, kind, result_version: resultVersion, source: 'camera', synthetic: false, uploaded_at: '2026-10-08T05:00:00Z'};
}

function fileData(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsDataURL(file);});
}

async function applyMutation(path: string, body: any) {
  if (body.expected_version !== serverOrder.version) throw new ApiError(409, 'Наряд изменился. Обновите карточку');
  if (path.endsWith('/actions')) {
    const stages: Record<string, string> = {accept: 'accepted', start: 'in_progress', pause: 'paused', resume: 'in_progress', queue: 'queued', reject: 'rejected'};
    serverOrder = {...serverOrder, status: stages[body.action], working_executor_id: workerId, version: serverOrder.version + 1};
  } else if (path.endsWith('/decision')) {
    serverOrder = {...serverOrder, status: body.verdict === 'requires_rework' ? 'rework' : 'closed', version: serverOrder.version + 1, master_verdict: body.verdict, master_score: body.score, master_comment: body.reason};
  } else if (path.endsWith('/result')) {
    const nextVersion = serverOrder.result_version + 1;
    serverOrder = {...serverOrder, version: serverOrder.version + 1, status: 'ai_review', result_version: nextVersion, work_description: body.work_description, fault_code_id: body.fault_code_id, materials_none: body.materials_none, assessment: null, photos: serverOrder.photos.map((item: any) => body.photo_ids.includes(item.id) ? {...item, result_version: nextVersion} : item)};
  }
  return structuredClone(serverOrder);
}

function mount(person: User = worker) {
  signedInUser = person;
  return render(<OrderDetail orderId={orderId} user={person} onBack={onBack} onChanged={onChanged} />);
}

async function ready(person: User = worker) {
  const view = mount(person);
  await screen.findByRole('heading', {name: 'Наряд №71'});
  return view;
}

function resultFields(work = 'Подшипник заменён. Вращение и нагрев проверены.') {
  fireEvent.change(screen.getByRole('textbox', {name: 'Что выполнено'}), {target: {value: work}});
  fireEvent.change(screen.getByRole('combobox', {name: 'Шифр неисправности'}), {target: {value: faultId}});
  fireEvent.click(screen.getByRole('checkbox', {name: 'Материалы не использовались'}));
}

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  serverOrder = order(); signedInUser = worker; revoked = false;
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {configurable: true, value: function (this: HTMLDialogElement) {this.setAttribute('open', '');}});
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {configurable: true, value: function (this: HTMLDialogElement) {this.removeAttribute('open');}});
  Object.defineProperty(URL, 'createObjectURL', {configurable: true, value: vi.fn(() => 'blob:authenticated-photo')});
  Object.defineProperty(URL, 'revokeObjectURL', {configurable: true, value: vi.fn()});
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === `/orders/${orderId}`) {if (revoked) throw new ApiError(404, 'Наряд не найден'); return structuredClone(serverOrder);}
    if (path === '/auth/me') return signedInUser;
    if (path.startsWith('/catalog/materials')) return {items: [{id: materialId, name: 'Подшипник 204', unit: 'шт', active: true}]};
    if (path.startsWith('/catalog/fault-codes')) return {items: [{id: faultId, code: 'М-01', name: 'Износ подшипника', active: true}]};
    if (path.startsWith('/catalog/users')) return {items: [worker, master]};
    if (path.startsWith('/catalog/brigades')) return {items: [{id: brigadeId, name: 'Бригада 1', active: true}]};
    if (path.startsWith('/catalog/sections')) return {items: [{id: 'section-1', name: 'Дробление'}]};
    if (path.startsWith('/reports/orders/')) return {audience: signedInUser.role, timing: {active_minutes: 32, norm_minutes: 60, on_time: true}, ...(signedInUser.role === 'worker' ? {} : {downtime: {minutes: 0, intervals: []}})};
    throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(api.post).mockImplementation(applyMutation);
  vi.mocked(api.patch).mockImplementation(async (_path, body: any) => {serverOrder = {...serverOrder, ...body, version: serverOrder.version + 1}; return structuredClone(serverOrder);});
  vi.mocked(api.getBlob).mockResolvedValue(new Blob(['image'], {type: 'image/jpeg'}));
  vi.mocked(api.download).mockResolvedValue(undefined);
});

afterEach(() => {cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers();});

describe('OrderDetail: рабочий и мастер', () => {
  it('проводит рабочего через принятие, начало и паузу с причиной и актуальными версиями', async () => {
    const user = userEvent.setup();
    await ready();
    await user.click(screen.getByRole('button', {name: 'Принять наряд'}));
    await user.click(await screen.findByRole('button', {name: 'Начать работу'}));
    await user.click(await screen.findByRole('button', {name: 'Приостановить'}));
    const dialog = screen.getByRole('dialog', {name: 'Почему приостановили работу?'});
    await user.type(within(dialog).getByRole('textbox', {name: 'Причина / пояснение'}), 'Ожидаем подшипник со склада');
    await user.click(within(dialog).getByRole('button', {name: 'Сохранить'}));
    await screen.findByRole('button', {name: 'Продолжить работу'});
    const calls = vi.mocked(api.post).mock.calls;
    expect(calls.map(([, body]: any) => [body.action, body.expected_version])).toEqual([['accept', 1], ['start', 2], ['pause', 3]]);
    expect(calls[2][1]).toMatchObject({reason: 'Ожидаем подшипник со склада'});
    expect(new Set(calls.map(([, body]: any) => body.operation_id)).size).toBe(3);
    expect(onChanged).toHaveBeenCalledTimes(3);
  });

  it('не отправляет паузу без объяснения и сохраняет доступный отказ вне смены', async () => {
    serverOrder = order({status: 'in_progress', working_executor_id: workerId});
    const user = userEvent.setup();
    await ready();
    await user.click(screen.getByRole('button', {name: 'Приостановить'}));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', {name: 'Сохранить'}));
    expect(screen.getByText('Укажите причину или пояснение — минимум 2 символа.')).toBeTruthy();
    expect(api.post).not.toHaveBeenCalled();
    cleanup(); serverOrder = order();
    await ready({...worker, on_shift: false});
    expect((screen.getByRole('button', {name: 'Принять наряд'}) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', {name: 'Не могу выполнить'}) as HTMLButtonElement).disabled).toBe(false);
  });

  it('требует человеческую оценку и пояснение при приёмке результата проверки по правилам', async () => {
    serverOrder = order({status: 'ai_review', result_version: 1, work_description: 'Подшипник заменён', fault_code_id: faultId, materials_none: true, assessment: {engine: 'rules', score: null, verdict: 'accepted', explanation: 'Обязательные поля заполнены', good: [], improve: [], findings: [], needs_master_review: true}});
    const originalGet = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async (path: string) => path.startsWith('/catalog/fault-codes') ? {items: [{id: 'another-fault', code: 'Э-02', name: 'Повреждение изоляции', active: true}, {id: faultId, code: 'М-01', name: 'Износ подшипника', active: true}]} : originalGet(path));
    const user = userEvent.setup();
    await ready(master);
    const resultSection = screen.getByRole('heading', {name: 'Результат и проверка'}).closest('section')!;
    expect(await within(resultSection).findByText('М-01 — Износ подшипника')).toBeTruthy();
    expect(within(resultSection).queryByText('Э-02 — Повреждение изоляции')).toBeNull();
    expect(within(resultSection).queryByText(faultId)).toBeNull();
    expect(screen.getByText('Проверка по правилам')).toBeTruthy();
    expect(screen.queryByText('Рекомендация ИИ')).toBeNull();
    await user.click(screen.getByRole('button', {name: 'Проверить и принять решение'}));
    const dialog = screen.getByRole('dialog', {name: 'Приёмка мастером'});
    await user.click(within(dialog).getByRole('button', {name: 'Сохранить решение'}));
    expect(api.post).not.toHaveBeenCalled();
    await user.selectOptions(within(dialog).getByRole('combobox', {name: 'Оценка мастера'}), '5');
    await user.type(within(dialog).getByRole('textbox', {name: 'Пояснение к решению'}), 'Фото и результат проверены мастером');
    await user.click(within(dialog).getByRole('button', {name: 'Сохранить решение'}));
    await screen.findByText('Решение мастера: Принято');
    expect(api.post).toHaveBeenCalledWith(`/orders/${orderId}/decision`, expect.objectContaining({expected_version: 1, verdict: 'accepted', score: 5, reason: 'Фото и результат проверены мастером'}));
  });

  it('скрывает действия мастера у рабочего и разрешает просмотр чужого исполнителя бригаде', async () => {
    serverOrder = order({executor_id: null, brigade_id: brigadeId, working_executor_id: otherWorkerId, status: 'in_progress'});
    await ready();
    expect(screen.getByText('Этот наряд уже принят другим исполнителем. Доступен просмотр.')).toBeTruthy();
    for (const name of ['Приостановить', 'Переназначить / изменить срок', 'Отменить наряд', 'Проверить и принять решение', 'Снять фото неисправности', 'Снять фото результата']) expect(screen.queryByRole('button', {name})).toBeNull();
    expect(screen.queryByRole('textbox', {name: 'Что выполнено'})).toBeNull();
  });

  it.each(['brigade', 'reassigned'])('не раскрывает оценку A рабочему B при чтении старого результата (%s)', async assignment => {
    const personB = {...worker, id: otherWorkerId, full_name: 'Другой рабочий'};
    serverOrder = order({executor_id: assignment === 'brigade' ? null : otherWorkerId, brigade_id: assignment === 'brigade' ? brigadeId : null, working_executor_id: assignment === 'brigade' ? workerId : otherWorkerId, status: 'rework', result_version: 1, work_description: 'Работа исполнителя A', master_verdict: 'accepted_with_remarks', master_score: 2, master_comment: 'Личный комментарий мастера об исполнителе A', events: [event(1, 'performed', workerId, {result_version: 1})], assessment: {engine: 'llm', score: 2, verdict: 'requires_rework', explanation: 'Персональная оценка работы исполнителя A', good: ['Персональное достижение A'], improve: ['Персональное замечание A'], findings: [], needs_master_review: true}});
    const originalGet = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async (path: string) => path.startsWith('/reports/orders/') ? {audience: 'worker', quality: {score: null, source: 'unavailable', verdict: null, explanation: 'Оценка вашего результата ещё не готова', good: [], improve: []}} : originalGet(path));
    await ready(personB);
    await screen.findByText('Персональная оценка этого результата доступна его исполнителю и мастеру.');
    expect(screen.getAllByText('Работа исполнителя A').length).toBeGreaterThan(0);
    expect(screen.queryByText(/Предварительная оценка:/)).toBeNull();
    for (const text of ['Рекомендация ИИ', 'Персональная оценка работы исполнителя A', 'Персональное достижение A', 'Персональное замечание A', 'Личный комментарий мастера об исполнителе A', 'Решение мастера: Принято с замечаниями']) expect(screen.queryByText(text)).toBeNull();
  });

  it('показывает рабочему оценку собственной отправленной версии результата', async () => {
    serverOrder = order({status: 'ai_review', result_version: 2, working_executor_id: workerId, events: [event(1, 'performed', otherWorkerId, {result_version: 1}), event(2, 'performed', workerId, {result_version: 2})], assessment: {engine: 'llm', score: 4, verdict: 'accepted', explanation: 'Проверен ваш результат', good: ['Подтверждение результата приложено'], improve: [], findings: [], needs_master_review: true}});
    await ready();
    expect(screen.getByText('Проверен ваш результат')).toBeTruthy();
    expect(screen.getByText('Подтверждение результата приложено')).toBeTruthy();
    expect(screen.getByText(/Предварительная оценка:/)).toBeTruthy();
    expect(screen.queryByText('Персональная оценка этого результата доступна его исполнителю и мастеру.')).toBeNull();
  });

  it('показывает автору правила без балла для исторического события без result_version только после собственного отчёта', async () => {
    serverOrder = order({status: 'ai_review', result_version: 1, events: [event(1, 'performed', workerId)], assessment: {engine: 'rules', score: null, verdict: 'accepted', explanation: 'Проверены обязательные поля', good: [], improve: [], findings: [], needs_master_review: true}});
    const originalGet = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async (path: string) => path.startsWith('/reports/orders/') ? {audience: 'worker', quality: {score: null, source: 'unavailable', verdict: 'accepted', explanation: 'Проверены обязательные поля', good: [], improve: []}} : originalGet(path));
    await ready();
    await screen.findByText('Проверка по правилам');
    expect(screen.queryByText(/Предварительная оценка:/)).toBeNull();
    expect(screen.queryByText('Рекомендация ИИ')).toBeNull();
  });

  it('не меняет секунды срока при изменении одного приоритета', async () => {
    const user = userEvent.setup();
    await ready(master);
    await user.click(screen.getByRole('button', {name: 'Переназначить / изменить срок'}));
    const dialog = screen.getByRole('dialog');
    await user.selectOptions(within(dialog).getByRole('combobox', {name: 'Приоритет'}), 'high');
    await user.type(within(dialog).getByRole('textbox', {name: 'Причина / пояснение'}), 'Работа стала срочной');
    await user.click(within(dialog).getByRole('button', {name: 'Сохранить'}));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.patch).mock.calls[0][1] as Record<string, unknown>;
    expect(body).toMatchObject({priority: 'high', reason: 'Работа стала срочной'});
    expect(body).not.toHaveProperty('due_at');
  });

  it('находит подходящий предыдущий ремонт на следующей странице и подтверждает именно его', async () => {
    serverOrder = order({status: 'rework', result_version: 1, fault_code_id: faultId});
    const previousId = '00000000-0000-4000-8000-000000000070';
    const firstPage = Array.from({length: 200}, (_, index) => order({id: `old-${index}`, number: index + 100, status: 'closed', fault_code_id: faultId, closed_at: '2026-09-20T04:00:00Z'}));
    const previous = order({id: previousId, number: 70, status: 'closed', fault_code_id: faultId, closed_at: '2026-10-07T04:00:00Z'});
    const originalGet = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.startsWith('/orders?')) {
        const params = new URLSearchParams(path.split('?')[1]);
        expect(params.get('equipment_id')).toBe('equipment-1');
        expect(params.get('status')).toBe('closed');
        expect(params.get('limit')).toBe('200');
        return {items: params.get('offset') === '200' ? [previous] : firstPage, total: 201};
      }
      return originalGet(path);
    });
    const user = userEvent.setup();
    await ready(master);
    await user.click(screen.getByRole('button', {name: 'Связать с повторной неисправностью'}));
    const dialog = screen.getByRole('dialog', {name: 'Подтвердить повторную неисправность'});
    const option = await within(dialog).findByRole('option', {name: /^№70 ·/});
    expect(option.getAttribute('value')).toBe(previousId);
    expect(within(dialog).queryByText('Нет подходящего закрытого ремонта с этим шифром за предыдущие 7 дней.')).toBeNull();
    expect(vi.mocked(api.get).mock.calls.some(([path]) => path.includes('status=closed') && path.includes('offset=200'))).toBe(true);
    expect(within(dialog).getAllByRole('option')).toHaveLength(2);
    await user.selectOptions(within(dialog).getByRole('combobox', {name: 'Предыдущий ремонт'}), previousId);
    await user.type(within(dialog).getByRole('textbox', {name: 'Причина / пояснение'}), 'Повторное повреждение подшипника подтверждено');
    await user.click(within(dialog).getByRole('button', {name: 'Сохранить'}));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(`/orders/${orderId}/confirm-repeat`, expect.objectContaining({expected_version: 1, previous_order_id: previousId, reason: 'Повторное повреждение подшипника подтверждено'})));
  });
});

describe('OrderDetail: сбой, конфликт и черновик', () => {
  it.each([0, 503])('повторяет неподтверждённое действие с теми же operation_id и expected_version (status %s)', async status => {
    vi.mocked(api.post).mockRejectedValueOnce(new ApiError(status, 'Отправка не подтверждена'));
    const user = userEvent.setup();
    await ready();
    await user.click(screen.getByRole('button', {name: 'Принять наряд'}));
    await screen.findByRole('button', {name: 'Повторить отправку'});
    const firstBody = vi.mocked(api.post).mock.calls[0][1];
    expect(JSON.parse(sessionStorage.getItem(`naryadai.draft.${workerId}.${orderId}`)!)).toMatchObject({pending: {body: firstBody}});
    await user.click(screen.getByRole('button', {name: 'Обновить и проверить'}));
    await screen.findByText('Карточка обновлена. Неподтверждённую отправку можно повторить тем же запросом.');
    expect(screen.getByRole('button', {name: 'Повторить отправку'})).toBeTruthy();
    await user.click(screen.getByRole('button', {name: 'Повторить отправку'}));
    await screen.findByRole('button', {name: 'Начать работу'});
    expect(api.post).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.post).mock.calls[1]).toEqual(vi.mocked(api.post).mock.calls[0]);
    expect(JSON.parse(sessionStorage.getItem(`naryadai.draft.${workerId}.${orderId}`)!)).toMatchObject({pending: null});
  });

  it('оставляет заполненный результат после 409 и создаёт новую операцию только после явного обновления', async () => {
    serverOrder = order({status: 'in_progress', version: 4, working_executor_id: workerId});
    vi.mocked(api.post).mockImplementationOnce(async () => {serverOrder = {...serverOrder, version: 5, priority: 'high'}; throw new ApiError(409, 'Наряд изменился. Обновите карточку');});
    const user = userEvent.setup();
    await ready();
    await screen.findByRole('option', {name: 'М-01 — Износ подшипника'});
    resultFields();
    fireEvent.change(screen.getByRole('textbox', {name: 'Комментарий к результату'}), {target: {value: 'Проверено под нагрузкой'}});
    await user.click(screen.getByRole('button', {name: 'Исполнено — отправить на проверку'}));
    await screen.findByRole('button', {name: 'Обновить и проверить'});
    expect((screen.getByRole('textbox', {name: 'Что выполнено'}) as HTMLTextAreaElement).value).toContain('Вращение и нагрев');
    expect(screen.getByRole('button', {name: 'Исполнено — отправить на проверку'}).matches(':disabled')).toBe(true);
    await user.click(screen.getByRole('button', {name: 'Обновить и проверить'}));
    await screen.findByText('Карточка обновлена. Ваш ввод сохранён: проверьте данные перед новой отправкой.');
    expect((screen.getByRole('textbox', {name: 'Комментарий к результату'}) as HTMLTextAreaElement).value).toBe('Проверено под нагрузкой');
    await user.click(screen.getByRole('button', {name: 'Исполнено — отправить на проверку'}));
    await screen.findByText('Результат отправлен на проверку. Решение примет мастер.');
    const [first, second] = vi.mocked(api.post).mock.calls.map(([, body]) => body as Record<string, unknown>);
    expect(first.expected_version).toBe(4); expect(second.expected_version).toBe(5);
    expect(first.operation_id).not.toBe(second.operation_id);
    expect(second).toMatchObject({work_description: 'Подшипник заменён. Вращение и нагрев проверены.', fault_code_id: faultId, materials_none: true, comment: 'Проверено под нагрузкой'});
  });

  it('восстанавливает черновик после повторного открытия без смешивания пользователей', async () => {
    serverOrder = order({status: 'in_progress', working_executor_id: workerId});
    const view = await ready();
    await screen.findByRole('option', {name: 'М-01 — Износ подшипника'});
    resultFields('Частично заменена муфта');
    view.unmount(); await ready();
    expect((screen.getByRole('textbox', {name: 'Что выполнено'}) as HTMLTextAreaElement).value).toBe('Частично заменена муфта');
    expect((screen.getByRole('combobox', {name: 'Шифр неисправности'}) as HTMLSelectElement).value).toBe(faultId);
    expect((screen.getByRole('checkbox', {name: 'Материалы не использовались'}) as HTMLInputElement).checked).toBe(true);
    cleanup(); serverOrder = order({status: 'in_progress', executor_id: otherWorkerId, working_executor_id: otherWorkerId});
    await ready({...worker, id: otherWorkerId, full_name: 'Другой рабочий'});
    expect((screen.getByRole('textbox', {name: 'Что выполнено'}) as HTMLTextAreaElement).value).toBe('');
  });

  it('убирает карточку после 404 переназначения и сохраняет локальный ввод', async () => {
    vi.useFakeTimers(); serverOrder = order({status: 'in_progress', working_executor_id: workerId});
    await act(async () => {mount();});
    expect(screen.getByRole('heading', {name: 'Наряд №71'})).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', {name: 'Что выполнено'}), {target: {value: 'Неотправленная запись ремонта'}});
    revoked = true;
    await act(async () => {vi.advanceTimersByTime(3000);});
    expect(screen.queryByRole('heading', {name: 'Наряд №71'})).toBeNull();
    expect(screen.getByText('Наряд переназначен или больше недоступен. Черновик сохранён.')).toBeTruthy();
    expect(JSON.parse(sessionStorage.getItem(`naryadai.draft.${workerId}.${orderId}`)!)).toMatchObject({work: 'Неотправленная запись ремонта'});
  });
});

describe('OrderDetail: фотографии', () => {
  it('получает фото через getBlob и отзывает временный URL при закрытии карточки', async () => {
    serverOrder = order({photos: [photo('before-photo', 0, 'before')]});
    const view = await ready();
    const image = await screen.findByRole('img', {name: 'До ремонта'});
    expect(api.getBlob).toHaveBeenCalledWith('/photos/before-photo/file');
    expect(image.getAttribute('src')).toBe('blob:authenticated-photo');
    expect(image.getAttribute('src')).not.toContain('/api/photos/');
    view.unmount(); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:authenticated-photo');
  });

  it('сжимает снимок один раз, повторяет те же байты и отправляет только собственное новое фото в результат', async () => {
    serverOrder = order({status: 'in_progress', working_executor_id: workerId, result_version: 1, photos: [photo('previous-result', 1), photo('other-pending', null)], events: [event(1, 'photo_uploaded', otherWorkerId, {photo_id: 'other-pending'})]});
    vi.stubGlobal('Image', class {
      naturalWidth = 2400; naturalHeight = 1600; onload: (() => void) | null = null; onerror: (() => void) | null = null;
      set src(_value: string) {queueMicrotask(() => this.onload?.());}
    });
    const context = {fillStyle: '', fillRect: vi.fn(), drawImage: vi.fn()};
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D);
    const compression = vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => {callback(new Blob(['same-compressed-bytes'], {type: 'image/jpeg'}));});
    vi.mocked(api.upload).mockRejectedValueOnce(new ApiError(0, 'Сохранение фото не подтверждено')).mockImplementationOnce(async () => {
      serverOrder = {...serverOrder, version: 2, photos: [...serverOrder.photos, photo('own-new-photo', null)], events: [...serverOrder.events, event(2, 'photo_uploaded', workerId, {photo_id: 'own-new-photo'})]};
      return structuredClone(serverOrder);
    });
    const view = await ready();
    const input = view.container.querySelector<HTMLInputElement>('input[type="file"][capture="environment"]')!;
    fireEvent.change(input, {target: {files: [new File(['camera-original'], 'camera.png', {type: 'image/png', lastModified: 1234567})]}});
    await screen.findByRole('button', {name: 'Повторить отправку'});
    const user = userEvent.setup(); await user.click(screen.getByRole('button', {name: 'Повторить отправку'}));
    await screen.findByRole('checkbox', {name: 'Включить в результат'});
    expect(compression).toHaveBeenCalledTimes(1);
    const forms = vi.mocked(api.upload).mock.calls.map(([, body]) => body);
    for (const field of ['operation_id', 'expected_version', 'kind', 'source']) expect(forms[1].get(field)).toBe(forms[0].get(field));
    expect(forms[0].get('expected_version')).toBe('1'); expect(forms[0].get('kind')).toBe('after'); expect(forms[0].get('source')).toBe('camera');
    expect(forms[0].has('captured_at')).toBe(false); expect(forms[1].has('captured_at')).toBe(false);
    expect((forms[0].get('file') as File).size).toBe((forms[1].get('file') as File).size);
    expect(await fileData(forms[0].get('file') as File)).toBe(await fileData(forms[1].get('file') as File));
    expect((screen.getByRole('checkbox', {name: 'Включить в результат'}) as HTMLInputElement).checked).toBe(true);
    await screen.findByRole('option', {name: 'М-01 — Износ подшипника'});
    resultFields(); await user.click(screen.getByRole('button', {name: 'Исполнено — отправить на проверку'}));
    await screen.findByText('Результат отправлен на проверку. Решение примет мастер.');
    expect(api.post).toHaveBeenCalledWith(`/orders/${orderId}/result`, expect.objectContaining({expected_version: 2, photo_ids: ['own-new-photo']}));
  });

  it('сохраняет снимок и текст с материалами после upload 409, затем явно отправляет те же байты с новой операцией и версией', async () => {
    serverOrder = order({status: 'in_progress', version: 4, working_executor_id: workerId});
    vi.stubGlobal('Image', class {
      naturalWidth = 2400; naturalHeight = 1600; onload: (() => void) | null = null; onerror: (() => void) | null = null;
      set src(_value: string) {queueMicrotask(() => this.onload?.());}
    });
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({fillStyle: '', fillRect: vi.fn(), drawImage: vi.fn()} as unknown as CanvasRenderingContext2D);
    const compression = vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['photo-preserved-after-conflict'], {type: 'image/jpeg'})));
    vi.mocked(api.upload).mockImplementationOnce(async () => {
      serverOrder = {...serverOrder, version: 5, events: [event(1, 'commented', masterId)]};
      throw new ApiError(409, 'Наряд изменился. Обновите карточку');
    }).mockImplementationOnce(async () => {
      serverOrder = {...serverOrder, version: 6, photos: [photo('photo-after-conflict', null)], events: [...serverOrder.events, event(2, 'photo_uploaded', workerId, {photo_id: 'photo-after-conflict'})]};
      return structuredClone(serverOrder);
    });
    const user = userEvent.setup(), view = await ready();
    await screen.findByRole('option', {name: 'М-01 — Износ подшипника'});
    fireEvent.change(screen.getByRole('textbox', {name: 'Что выполнено'}), {target: {value: 'Заменён подшипник. Проверено вращение.'}});
    fireEvent.change(screen.getByRole('combobox', {name: 'Шифр неисправности'}), {target: {value: faultId}});
    await user.click(screen.getByRole('button', {name: '+ Добавить материал'}));
    await user.selectOptions(screen.getByRole('combobox', {name: 'Материал 1'}), materialId);
    fireEvent.change(screen.getByRole('textbox', {name: /^Количество/}), {target: {value: '2'}});
    fireEvent.change(view.container.querySelector<HTMLInputElement>('input[type="file"][capture="environment"]')!, {target: {files: [new File(['camera-original'], 'camera.png', {type: 'image/png'})]}});
    await screen.findByText('Наряд изменился. Обновите карточку');
    expect(api.upload).toHaveBeenCalledTimes(1);
    expect((screen.getByRole('button', {name: 'Повторить отправку'}) as HTMLButtonElement).disabled).toBe(true);
    const saved = JSON.parse(sessionStorage.getItem(`naryadai.draft.${workerId}.${orderId}`)!);
    expect(saved).toMatchObject({work: 'Заменён подшипник. Проверено вращение.', faultId, materials: [{materialId, quantity: '2'}], pending: {kind: 'upload', version: 4, versionConflict: true}});
    expect(screen.getByRole('img', {name: 'Неотправленное фото в черновике'}).getAttribute('src')).toBe(saved.pending.fileData);
    view.unmount(); await ready();
    expect(screen.getByRole('img', {name: 'Неотправленное фото в черновике'}).getAttribute('src')).toBe(saved.pending.fileData);
    expect((screen.getByRole('button', {name: 'Повторить отправку'}) as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole('button', {name: 'Обновить и проверить'}));
    await screen.findByText('Карточка обновлена. Снимок сохранён в черновике; отправьте его повторно.');
    expect(api.upload).toHaveBeenCalledTimes(1);
    expect(JSON.parse(sessionStorage.getItem(`naryadai.draft.${workerId}.${orderId}`)!).pending.operationId).toBe(saved.pending.operationId);
    await user.click(screen.getByRole('button', {name: 'Повторить отправку'}));
    await screen.findByRole('checkbox', {name: 'Включить в результат'});
    expect(compression).toHaveBeenCalledTimes(1);
    const [original, retried] = vi.mocked(api.upload).mock.calls.map(([, form]) => form);
    expect(original.get('expected_version')).toBe('4'); expect(retried.get('expected_version')).toBe('5');
    expect(original.get('operation_id')).not.toBe(retried.get('operation_id'));
    for (const field of ['kind', 'source']) expect(retried.get(field)).toBe(original.get(field));
    expect(await fileData(original.get('file') as File)).toBe(await fileData(retried.get('file') as File));
    expect((screen.getByRole('textbox', {name: 'Что выполнено'}) as HTMLTextAreaElement).value).toBe('Заменён подшипник. Проверено вращение.');
    expect((screen.getByRole('combobox', {name: 'Материал 1'}) as HTMLSelectElement).value).toBe(materialId);
    expect((screen.getByRole('textbox', {name: /^Количество/}) as HTMLInputElement).value).toBe('2');
    expect(JSON.parse(sessionStorage.getItem(`naryadai.draft.${workerId}.${orderId}`)!)).toMatchObject({pending: null, photoIds: ['photo-after-conflict']});
  });
});
