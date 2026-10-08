import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { api } from '../lib/api';
import type { Order, User } from '../lib/types';
import { CreateOrder } from './CreateOrder';
import { CatalogAdmin } from './CatalogAdmin';
import { compressImage } from './formTools';

vi.mock('../lib/api', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), upload: vi.fn() } }));
vi.mock('./formTools', async importOriginal => ({
  ...await importOriginal<typeof import('./formTools')>(),
  // Canvas codecs belong to the browser; these tests exercise the save protocol.
  compressImage: vi.fn(async (file: File) => file),
}));

const worker: User = { id: 'worker-1', login: 'worker.test', full_name: 'Слесарь Тест', role: 'worker', specialty: 'слесарь', grade: 3, brigade_id: null, on_shift: true, active: true };
const master: User = { ...worker, id: 'master-1', login: 'master.test', full_name: 'Мастер Тест', role: 'master' };
const admin: User = { ...worker, id: 'admin-1', login: 'admin.test', full_name: 'Администратор Тест', role: 'admin' };
const section = { id: 'section-1', name: 'Насосная', external_id: null, active: true };
const equipment = { id: 'pump-1', name: 'Насос №1', section_id: section.id, inventory_number: 'Н-001', equipment_type: 'насос', criticality: 3, external_id: null, active: true };
const brigade = { id: 'brigade-1', name: 'Ремонтная бригада', active: true };
const norm = { id: 'norm-1', name: 'Ремонт насоса', equipment_type: 'насос', fault_code_id: 'fault-1', minutes: 45 };
const shift = { id: 'shift-1', name: 'Дневная смена', starts_at: '2030-01-02T03:00:00Z', ends_at: '2030-01-02T15:00:00Z' };
const order: Order = { id: 'order-1', number: 42, status: 'issued', version: 1, description: 'Устранить течь', work_type: 'unplanned', priority: 'normal', equipment_id: equipment.id, section_id: section.id, executor_id: worker.id, working_executor_id: null, brigade_id: null, master_id: master.id, due_at: '2030-01-02T04:00:00Z', norm_minutes: 60, overdue: false, overdue_minutes: 0, result_version: 0, photos: [], events: [], assessment: null };
const catalogs: Record<string, unknown[]> = {
  sections: [section], equipment: [equipment], brigades: [brigade], users: [{ ...worker, availability: 'free', queue_count: 0 }],
  'time-norms': [norm], shifts: [shift], 'fault-codes': [{ id: 'fault-1', code: 'М-02', name: 'Течь', specialty: 'слесарь', active: true }], materials: [], 'material-norms': [],
};
const failed = (status: number, message: string) => Object.assign(new Error(message), { status });
const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);
const mockPut = vi.mocked(api.put);
const mockPatch = vi.mocked(api.patch);
const mockUpload = vi.mocked(api.upload);

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(compressImage).mockImplementation(async file => file);
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:test-photo') });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
  mockGet.mockImplementation(async path => {
    if (path.startsWith('/orders/')) return order;
    const key = path.match(/^\/catalog\/([^?]+)/)?.[1];
    return { items: catalogs[key ?? ''] ?? [] };
  });
  mockPost.mockResolvedValue(order);
  mockPut.mockResolvedValue({});
  mockPatch.mockResolvedValue({});
});

async function basicOrder(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole('option', { name: /Насос №1/ });
  await user.selectOptions(screen.getByLabelText('Оборудование *'), equipment.id);
  await user.type(screen.getByLabelText('Что произошло и что нужно сделать *'), 'Устранить течь');
  await user.selectOptions(screen.getByLabelText('Исполнитель *'), worker.id);
}

describe('Выдача наряда', () => {
  it('выдаёт исполнителю точный серверный DTO без выдуманного времени фото', async () => {
    const user = userEvent.setup(); const onCreated = vi.fn();
    render(<CreateOrder user={master} onCreated={onCreated} onCancel={vi.fn()} />);
    await basicOrder(user);
    await user.selectOptions(screen.getByLabelText('Норматив из справочника'), norm.id);
    await user.click(screen.getByText('Смена, комментарий и простой'));
    await user.selectOptions(screen.getByLabelText('Смена', { exact: true }), shift.id);
    await user.type(screen.getByLabelText('Комментарий'), '  Проверить уплотнение  ');
    await user.click(screen.getByLabelText('Оборудование остановлено — начать учёт простоя'));
    await user.click(screen.getByRole('button', { name: 'Выдать наряд' }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(order));
    expect(mockPost).toHaveBeenCalledExactlyOnceWith('/orders', {
      operation_id: expect.stringMatching(/^[a-f0-9-]{36}$/), description: 'Устранить течь', equipment_id: equipment.id, section_id: section.id,
      executor_id: worker.id, brigade_id: null, work_type: 'unplanned', priority: 'normal', norm_minutes: 45,
      time_norm_id: norm.id, due_at: null, shift_id: shift.id, comment: 'Проверить уплотнение', equipment_stopped: true,
    });
    expect(mockUpload).not.toHaveBeenCalled();
    expect(localStorage.getItem(`naryadai.draft.${master.id}.create_order`)).toBeNull();
  });

  it('выдаёт бригаде и переводит явно указанный срок UTC+5 в UTC', async () => {
    const user = userEvent.setup();
    render(<CreateOrder user={master} onCreated={vi.fn()} onCancel={vi.fn()} />);
    await basicOrder(user);
    await user.selectOptions(screen.getByLabelText('Кому поручить'), 'brigade');
    await user.selectOptions(screen.getByLabelText('Бригада *'), brigade.id);
    await user.selectOptions(screen.getByLabelText('Приоритет'), 'emergency');
    await user.selectOptions(screen.getByLabelText('Срок', { exact: true }), 'date');
    fireEvent.change(screen.getByLabelText('Дата и время · UTC+5'), { target: { value: '2030-01-02T13:30' } });
    await user.click(screen.getByRole('button', { name: 'Выдать наряд' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/orders', {
      operation_id: expect.any(String), description: 'Устранить течь', equipment_id: equipment.id, section_id: section.id,
      executor_id: null, brigade_id: brigade.id, work_type: 'unplanned', priority: 'emergency', norm_minutes: 60,
      time_norm_id: null, due_at: '2030-01-02T08:30:00.000Z', shift_id: null, comment: null, equipment_stopped: false,
    }]);
  });

  it('после потери ответа и повторного открытия отправляет ту же операцию и данные', async () => {
    const user = userEvent.setup(); const onCreated = vi.fn();
    mockPost.mockRejectedValueOnce(failed(0, 'Ответ сервера не получен')).mockResolvedValueOnce(order);
    const view = render(<CreateOrder user={master} onCreated={onCreated} onCancel={vi.fn()} />);
    await basicOrder(user);
    await user.click(screen.getByRole('button', { name: 'Выдать наряд' }));
    await screen.findByRole('button', { name: 'Повторить ту же выдачу' });
    const original = structuredClone(mockPost.mock.calls[0][1]);
    expect(onCreated).not.toHaveBeenCalled();
    view.unmount();
    render(<CreateOrder user={master} onCreated={onCreated} onCancel={vi.fn()} />);
    await screen.findByRole('option', { name: /Насос №1/ });
    expect((screen.getByLabelText('Что произошло и что нужно сделать *') as HTMLTextAreaElement).value).toBe('Устранить течь');
    await waitFor(() => expect((screen.getByRole('button', { name: 'Повторить ту же выдачу' }) as HTMLButtonElement).disabled).toBe(false));
    await user.click(screen.getByRole('button', { name: 'Повторить ту же выдачу' }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(order));
    expect(mockPost.mock.calls).toEqual([['/orders', original], ['/orders', original]]);
  });

  it('после частичной загрузки повторяет только неподтверждённое фото с прежней версией и operation_id', async () => {
    const user = userEvent.setup(); const onCreated = vi.fn();
    const firstPhoto = { id: 'photo-1', kind: 'before' };
    const secondPhoto = { id: 'photo-2', kind: 'before' };
    const withOne = { ...order, version: 2, photos: [firstPhoto] };
    const withBoth = { ...order, version: 3, photos: [firstPhoto, secondPhoto] };
    mockUpload.mockResolvedValueOnce(withOne).mockRejectedValueOnce(failed(0, 'Ответ о фото не получен')).mockResolvedValueOnce(withBoth);
    const { container } = render(<CreateOrder user={master} onCreated={onCreated} onCancel={vi.fn()} />);
    await basicOrder(user);
    await user.upload(container.querySelector<HTMLInputElement>('input[type="file"][multiple]')!, [new File(['one'], 'one.jpg', { type: 'image/jpeg' }), new File(['two'], 'two.jpg', { type: 'image/jpeg' })]);
    await waitFor(() => expect(screen.getAllByAltText('Фото неисправности')).toHaveLength(2));
    await user.click(screen.getByRole('button', { name: 'Выдать наряд' }));
    await screen.findByRole('button', { name: 'Дозагрузить фото' });
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockUpload).toHaveBeenCalledTimes(2);
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByRole('status').textContent).toContain('уже выдан');
    const first = mockUpload.mock.calls[0][1], lost = mockUpload.mock.calls[1][1];
    expect(first.get('expected_version')).toBe('1');
    expect(lost.get('expected_version')).toBe('2');
    expect(first.get('operation_id')).not.toBe(lost.get('operation_id'));
    expect(Array.from(lost.keys()).sort()).toEqual(['expected_version', 'file', 'kind', 'operation_id', 'source']);
    expect(lost.get('source')).toBe('gallery');
    expect(lost.get('kind')).toBe('before');
    await user.click(screen.getByRole('button', { name: 'Дозагрузить фото' }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(withBoth));
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockUpload).toHaveBeenCalledTimes(3);
    const retry = mockUpload.mock.calls[2][1];
    expect(mockUpload.mock.calls.every(([path]) => path === '/orders/order-1/photos')).toBe(true);
    expect(retry.get('operation_id')).toBe(lost.get('operation_id'));
    expect(retry.get('expected_version')).toBe('2');
    expect((retry.get('file') as File).name).toBe('two.jpg');
  });

  it('сохранённый наряд восстанавливает по ID и не выдаёт заново после перезагрузки', async () => {
    const user = userEvent.setup();
    mockUpload.mockRejectedValueOnce(failed(503, 'Фото временно недоступно'));
    const view = render(<CreateOrder user={master} onCreated={vi.fn()} onCancel={vi.fn()} />);
    await basicOrder(user);
    await user.upload(view.container.querySelector<HTMLInputElement>('input[type="file"][multiple]')!, new File(['one'], 'one.jpg', { type: 'image/jpeg' }));
    await screen.findByAltText('Фото неисправности');
    await user.click(screen.getByRole('button', { name: 'Выдать наряд' }));
    await screen.findByRole('button', { name: 'Дозагрузить фото' });
    view.unmount();
    const onCreated = vi.fn();
    render(<CreateOrder user={master} onCreated={onCreated} onCancel={vi.fn()} />);
    await screen.findByRole('button', { name: 'Открыть сохранённый' });
    await user.click(screen.getByRole('button', { name: 'Открыть сохранённый' }));
    expect(onCreated).toHaveBeenCalledWith(order);
    expect(mockGet).toHaveBeenCalledWith('/orders/order-1');
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('рабочему не предлагает выдачу и не вызывает мутаций', async () => {
    const user = userEvent.setup(); const onCancel = vi.fn();
    render(<CreateOrder user={worker} onCreated={vi.fn()} onCancel={onCancel} />);
    expect(screen.queryByRole('button', { name: 'Выдать наряд' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Назад' }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(mockPost).not.toHaveBeenCalled(); expect(mockUpload).not.toHaveBeenCalled();
  });
});

describe('Справочники и состав смены', () => {
  it('передаёт единицу измерения и уникальные группы неисправностей в точном DTO', async () => {
    const user = userEvent.setup(); const onChanged = vi.fn();
    render(<CatalogAdmin user={admin} onChanged={onChanged} />);
    await user.click(screen.getByRole('button', { name: 'Материалы' }));
    await user.type(screen.getByLabelText('Название *'), '  Масло  ');
    await user.type(screen.getByLabelText(/Единица измерения/), ' л ');
    await user.type(screen.getByLabelText(/Группы неисправностей/), 'М, Г; М');
    await user.click(screen.getByRole('button', { name: 'Добавить' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(mockPost).toHaveBeenCalledExactlyOnceWith('/catalog/materials', { name: 'Масло', unit: 'л', allowed_fault_prefixes: ['М', 'Г'], external_id: null, active: true });
  });

  it('ошибка серверной проверки единицы сохраняет черновик и позволяет исправить его', async () => {
    const user = userEvent.setup(); const onChanged = vi.fn();
    mockPost.mockRejectedValueOnce(failed(422, 'Проверьте единицу измерения')).mockResolvedValueOnce({ id: 'material-1' });
    render(<CatalogAdmin user={admin} onChanged={onChanged} />);
    await user.click(screen.getByRole('button', { name: 'Материалы' }));
    await user.type(screen.getByLabelText('Название *'), 'Смазка');
    await user.type(screen.getByLabelText(/Единица измерения/), 'ошибочная единица');
    await user.click(screen.getByRole('button', { name: 'Добавить' }));
    await screen.findByRole('alert');
    expect((screen.getByLabelText('Название *') as HTMLInputElement).value).toBe('Смазка');
    expect((screen.getByLabelText(/Единица измерения/) as HTMLInputElement).value).toBe('ошибочная единица');
    expect(JSON.parse(localStorage.getItem(`naryadai.draft.${admin.id}.catalog.materials`)!)).toMatchObject({ values: { name: 'Смазка', unit: 'ошибочная единица' } });
    expect(onChanged).not.toHaveBeenCalled();
    await user.clear(screen.getByLabelText(/Единица измерения/));
    await user.type(screen.getByLabelText(/Единица измерения/), 'кг');
    await user.click(screen.getByRole('button', { name: 'Добавить' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(mockPost.mock.calls[1][1]).toMatchObject({ name: 'Смазка', unit: 'кг' });
  });

  it('пробелы в единице проверяются до сети и не блокируют исправление локальной ошибки', async () => {
    const user = userEvent.setup();
    render(<CatalogAdmin user={admin} onChanged={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Материалы' }));
    await user.type(screen.getByLabelText('Название *'), 'Смазка');
    await user.type(screen.getByLabelText(/Единица измерения/), '   ');
    await user.click(screen.getByRole('button', { name: 'Добавить' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Единица измерения');
    expect(mockPost).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: 'Добавить' }) as HTMLButtonElement).disabled).toBe(false);
    await user.clear(screen.getByLabelText(/Единица измерения/));
    await user.type(screen.getByLabelText(/Единица измерения/), 'кг');
    await user.click(screen.getByRole('button', { name: 'Добавить' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).toMatchObject({ name: 'Смазка', unit: 'кг' });
  });

  it.each(['worker', 'leader'] as const)('%s видит справочники без административных и сменных мутаций', async role => {
    const user = userEvent.setup();
    render(<CatalogAdmin user={{ ...worker, role }} onChanged={vi.fn()} />);
    for (const label of ['Участки', 'Оборудование', 'Бригады', 'Неисправности', 'Материалы', 'Нормативы времени', 'Нормы материалов']) {
      await user.click(screen.getByRole('button', { name: label }));
      expect(screen.queryByRole('button', { name: 'Добавить' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Изменить' })).toBeNull();
    }
    expect(screen.queryByRole('button', { name: 'Сотрудники' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'На смене' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Состав смены' })).toBeNull();
    expect(mockPost).not.toHaveBeenCalled(); expect(mockPatch).not.toHaveBeenCalled(); expect(mockPut).not.toHaveBeenCalled();
  });

  it('потеря ответа о новом материале сохраняет данные и блокирует слепой повтор', async () => {
    const user = userEvent.setup(); const onChanged = vi.fn();
    mockPost.mockRejectedValueOnce(failed(0, 'Соединение прервано'));
    render(<CatalogAdmin user={admin} onChanged={onChanged} />);
    await user.click(screen.getByRole('button', { name: 'Материалы' }));
    await user.type(screen.getByLabelText('Название *'), 'Масло');
    await user.type(screen.getByLabelText(/Единица измерения/), 'л');
    await user.click(screen.getByRole('button', { name: 'Добавить' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('status').textContent).toContain('повторное создание может добавить копию');
    expect((screen.getByRole('button', { name: 'Добавить' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText('Название *') as HTMLInputElement).value).toBe('Масло');
    expect((screen.getByLabelText(/Единица измерения/) as HTMLInputElement).value).toBe('л');
    await user.click(screen.getByRole('button', { name: 'Добавить' }));
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('локальная проверка имени сотрудника не требует сверки после несуществующего запроса', async () => {
    const user = userEvent.setup();
    render(<CatalogAdmin user={admin} onChanged={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Сотрудники' }));
    await user.type(screen.getByLabelText('Логин *'), 'worker.new');
    await user.type(screen.getByLabelText('Имя сотрудника *'), '   ');
    await user.type(screen.getByLabelText('Пароль *'), 'Test-local-validation!');
    await user.click(screen.getByRole('button', { name: 'Создать сотрудника' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Укажите имя');
    expect(mockPost).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: 'Создать сотрудника' }) as HTMLButtonElement).disabled).toBe(false);
    await user.clear(screen.getByLabelText('Имя сотрудника *'));
    await user.type(screen.getByLabelText('Имя сотрудника *'), 'Новый Сотрудник');
    await user.click(screen.getByRole('button', { name: 'Создать сотрудника' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][0]).toBe('/catalog/users');
  });

  it('ошибочный период смены можно исправить без обновления списка, пока запрос не отправлялся', async () => {
    const user = userEvent.setup();
    render(<CatalogAdmin user={master} onChanged={vi.fn()} />);
    await screen.findByRole('button', { name: 'Снять со смены' });
    await user.click(screen.getByRole('button', { name: 'Состав смены' }));
    await user.type(screen.getByLabelText('Название *'), 'Ночная смена');
    fireEvent.change(screen.getByLabelText('Начало · UTC+5'), { target: { value: '2030-01-02T20:00' } });
    fireEvent.change(screen.getByLabelText('Окончание · UTC+5'), { target: { value: '2030-01-02T08:00' } });
    await user.click(screen.getByLabelText('Слесарь Тест · Исполнитель'));
    await user.click(screen.getByRole('button', { name: 'Создать смену' }));
    expect((await screen.findByRole('alert')).textContent).toContain('позже начала');
    expect(mockPost).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: 'Создать смену' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(screen.getByLabelText('Окончание · UTC+5'), { target: { value: '2030-01-03T08:00' } });
    await user.click(screen.getByRole('button', { name: 'Создать смену' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][0]).toBe('/catalog/shifts');
  });

  it('мастер отмечает выход сотрудника и создаёт смену, без изменения справочников или учётных записей', async () => {
    const user = userEvent.setup(); const onChanged = vi.fn();
    render(<CatalogAdmin user={master} onChanged={onChanged} />);
    await user.click(await screen.findByRole('button', { name: 'Снять со смены' }));
    await waitFor(() => expect(mockPut).toHaveBeenCalledExactlyOnceWith('/catalog/users/worker-1/attendance', { on_shift: false }));
    await user.click(screen.getByRole('button', { name: 'Участки' }));
    expect(screen.queryByRole('button', { name: 'Добавить' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Сотрудники' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Состав смены' }));
    await user.type(screen.getByLabelText('Название *'), 'Ночная смена');
    fireEvent.change(screen.getByLabelText('Начало · UTC+5'), { target: { value: '2030-01-02T20:00' } });
    fireEvent.change(screen.getByLabelText('Окончание · UTC+5'), { target: { value: '2030-01-03T08:00' } });
    await user.click(screen.getByLabelText('Слесарь Тест · Исполнитель'));
    await user.click(screen.getByRole('button', { name: 'Создать смену' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledExactlyOnceWith('/catalog/shifts', { name: 'Ночная смена', starts_at: '2030-01-02T15:00:00.000Z', ends_at: '2030-01-03T03:00:00.000Z', user_ids: [worker.id] }));
    expect(mockPatch).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it('изменение имени отправляет только имя без неизменной роли или реквизитов', async () => {
    const user = userEvent.setup(); const onChanged = vi.fn();
    render(<CatalogAdmin user={admin} onChanged={onChanged} />);
    await user.click(screen.getByRole('button', { name: 'Сотрудники' }));
    const row = (await screen.findByText(worker.full_name, { selector: 'strong' })).closest('article')!;
    await user.click(within(row).getByRole('button', { name: 'Изменить' }));
    await user.clear(screen.getByLabelText('Имя сотрудника *'));
    await user.type(screen.getByLabelText('Имя сотрудника *'), 'Слесарь Исправленный');
    await user.click(screen.getByRole('button', { name: 'Сохранить сотрудника' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(mockPatch).toHaveBeenCalledExactlyOnceWith('/catalog/users/worker-1', { full_name: 'Слесарь Исправленный' });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('пароль и ПИН остаются только в открытой форме, включая серверную ошибку и восстановление черновика', async () => {
    const user = userEvent.setup(); const onChanged = vi.fn();
    mockPost.mockRejectedValueOnce(failed(422, 'Логин уже занят'));
    const view = render(<CatalogAdmin user={admin} onChanged={onChanged} />);
    await user.click(screen.getByRole('button', { name: 'Сотрудники' }));
    await user.type(screen.getByLabelText('Логин *'), 'worker.new');
    await user.type(screen.getByLabelText('Имя сотрудника *'), 'Новый Сотрудник');
    await user.type(screen.getByLabelText('Пароль *'), 'Test-secret-928!');
    await user.type(screen.getByLabelText('ПИН · необязательно'), '582739');
    await user.click(screen.getByRole('button', { name: 'Создать сотрудника' }));
    await screen.findByRole('alert');
    expect(mockPost).toHaveBeenCalledExactlyOnceWith('/catalog/users', { login: 'worker.new', full_name: 'Новый Сотрудник', password: 'Test-secret-928!', pin: '582739', role: 'worker', specialty: 'слесарь', grade: 3, brigade_id: null, active: true });
    expect((screen.getByLabelText('Пароль *') as HTMLInputElement).value).toBe('Test-secret-928!');
    expect((screen.getByLabelText('ПИН · необязательно') as HTMLInputElement).value).toBe('582739');
    for (const storage of [localStorage, sessionStorage]) for (let index = 0; index < storage.length; index++) {
      const value = storage.getItem(storage.key(index)!)!;
      expect(value).not.toContain('Test-secret-928!'); expect(value).not.toContain('582739');
      expect(value).not.toMatch(/"(?:password|pin|clear_pin)"\s*:/);
    }
    expect(onChanged).not.toHaveBeenCalled();
    view.unmount();
    render(<CatalogAdmin user={admin} onChanged={onChanged} />);
    await user.click(screen.getByRole('button', { name: 'Сотрудники' }));
    expect((screen.getByLabelText('Имя сотрудника *') as HTMLInputElement).value).toBe('Новый Сотрудник');
    expect((screen.getByLabelText('Пароль *') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('ПИН · необязательно') as HTMLInputElement).value).toBe('');
  });
});
