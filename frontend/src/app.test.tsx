import {act,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {beforeEach,describe,expect,it,vi} from 'vitest';
import App from './App';
import {dateTime,Order,User} from './lib/types';

const mocks=vi.hoisted(()=>({get:vi.fn(),post:vi.fn(),put:vi.fn(),setToken:vi.fn(),download:vi.fn()}));
vi.mock('./lib/api',()=>({api:mocks,ApiError:class extends Error{status=0;}}));
vi.mock('./components/OrderDetail',()=>({default:({orderId}:{orderId:string})=><h2>Карточка {orderId}</h2>}));
vi.mock('./components/CreateOrder',()=>({default:()=> <h2>Выдача</h2>}));
vi.mock('./components/Reports',()=>({default:()=> <h2>Отчёты</h2>}));
vi.mock('./components/CatalogAdmin',()=>({default:()=> <h2>Управление</h2>}));

const person:User={id:'w1',login:'worker',full_name:'Рабочий Тестовый',role:'worker',specialty:'слесарь',grade:3,brigade_id:null,on_shift:true,active:true};
const order=(number:number,status:string,changes:Partial<Order>={}):Order=>({id:'o'+number,number,status,version:1,description:'Проверить соединение насоса',work_type:'planned',priority:'normal',equipment_id:'e1',section_id:'s1',executor_id:'w1',working_executor_id:null,brigade_id:null,master_id:'m1',due_at:'2026-10-08T15:00:00Z',norm_minutes:60,overdue:false,overdue_minutes:0,result_version:0,equipment:{id:'e1',name:'Насос '+number,inventory_number:'n1'},photos:[],events:[],assessment:null,...changes});
let currentUser=person;let availableOrders:Order[]=[];let availableBrigades:{id:string;name:string}[]=[];
beforeEach(()=>{
  vi.clearAllMocks();currentUser={...person};availableOrders=[];availableBrigades=[];history.replaceState(null,'','#/work');sessionStorage.setItem('naryadai.session','session');
  mocks.get.mockImplementation(async(path:string)=>{
    if(path==='/auth/me')return currentUser;
    if(path.startsWith('/events'))return {items:[],cursor:0};
    if(path==='/notifications/status')return {telegram:{configured:true,link_available:true,linked:false}};
    if(path.startsWith('/notifications'))return {items:[],unread_count:0};
    if(path.startsWith('/catalog/users'))return {items:[person]};
    if(path.startsWith('/catalog/brigades'))return {items:availableBrigades};
    if(path.startsWith('/catalog/'))return {items:[]};
    if(path.startsWith('/orders')){const params=new URLSearchParams(path.split('?')[1]);const status=params.get('status'),brigade=params.get('brigade_id');const items=availableOrders.filter(row=>(!status||row.status===status)&&(!brigade||row.brigade_id===brigade));const counts:Record<string,number>={};for(const row of items)counts[row.status]=(counts[row.status]||0)+1;return {items,total:items.length,counts_by_status:counts,overdue_count:items.filter(row=>row.overdue).length,limit:200,offset:0};}
    throw new Error('Unexpected GET '+path);
  });
  mocks.post.mockResolvedValue({});
});

describe('Оболочка рабочего приложения',()=>{
  it('мастер фильтрует оперативные наряды и счётчики по бригаде',async()=>{
    currentUser={...person,id:'m1',role:'master'};availableBrigades=[{id:'b1',name:'Бригада первая'},{id:'b2',name:'Бригада вторая'}];
    availableOrders=[order(11,'issued',{brigade_id:'b1'}),order(12,'issued',{brigade_id:'b2'})];
    const user=userEvent.setup();render(<App/>);await screen.findByText('Наряд №12');
    await user.selectOptions(screen.getByLabelText('Бригада'),'b1');
    await waitFor(()=>expect(screen.queryByText('Наряд №12')).toBeNull());expect(screen.getByText('Наряд №11')).toBeTruthy();
    expect(within(screen.getByText('Активные наряды').closest('button')!).getByText('1')).toBeTruthy();
    expect(mocks.get.mock.calls.some(([path])=>path.startsWith('/orders?limit=1&brigade_id=b1'))).toBe(true);
  });
  it('обновляет просрочку без событий наряда, когда срок наступил на открытом экране',async()=>{
    availableOrders=[order(5,'issued')];let view:ReturnType<typeof render>|undefined;vi.useFakeTimers();
    try{
      await act(async()=>{view=render(<App/>);});
      expect(screen.getByText('Наряд №5')).toBeTruthy();expect(screen.queryByText('Просрочен на 1 мин.')).toBeNull();
      availableOrders=[order(5,'issued',{overdue:true,overdue_minutes:1})];
      await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
      expect(screen.getByText('Просрочен на 1 мин.')).toBeTruthy();
      const metric=screen.getByText('Просрочены').closest('button')!;
      expect(within(metric).getByText('1')).toBeTruthy();
    }finally{view?.unmount();vi.useRealTimers();}
  });
  it('даёт рабочему путь назад при чужой ссылке на выдачу вместо пустого экрана',async()=>{
    history.replaceState(null,'','#/new');const user=userEvent.setup();render(<App/>);
    const back=await screen.findByRole('button',{name:'Вернуться к нарядам'});
    expect(screen.queryByRole('heading',{name:'Выдача'})).toBeNull();
    await user.click(back);await screen.findByRole('heading',{name:'Мои наряды'});
  });
  it('находит активный наряд отдельной выборкой и сохраняет FIFO очереди вместо порядка срока',async()=>{
    availableOrders=[order(9,'closed'),order(2,'queued',{queue_position:2,due_at:'2026-10-08T13:00:00Z'}),order(1,'queued',{queue_position:1,due_at:'2026-10-08T18:00:00Z'}),order(3,'issued')];
    render(<App/>);await screen.findByText('Наряд №3');
    const cards=screen.getAllByRole('article');expect(within(cards[0]).getByText('Наряд №3')).toBeTruthy();
    expect(within(cards[1]).getByText('Наряд №1')).toBeTruthy();expect(within(cards[2]).getByText('Наряд №2')).toBeTruthy();expect(screen.queryByText('Наряд №9')).toBeNull();
    expect(mocks.get.mock.calls.some(([path])=>path.includes('status=issued'))).toBe(true);
  });
  it('рабочий не получает выдачу и администрирование; мастер видит служебные действия, а не кнопку начала чужой работы',async()=>{
    currentUser={...person,id:'m1',role:'master'};availableOrders=[order(7,'accepted')];render(<App/>);
    await screen.findByText('Наряд №7');expect(screen.getByRole('button',{name:'Выдать наряд'})).toBeTruthy();
    expect(screen.queryByRole('button',{name:'Перейти к работе'})).toBeNull();
    expect(screen.getByRole('button',{name:'Открыть наряд'})).toBeTruthy();
  });
  it('в рабочем входе отсутствуют выдача наряда и управление сотрудниками',async()=>{
    render(<App/>);await screen.findByRole('heading',{name:'Мои наряды'});
    expect(screen.queryByRole('button',{name:'Выдать наряд'})).toBeNull();
    expect(screen.queryByRole('button',{name:'Управление'})).toBeNull();
    expect(screen.queryByRole('button',{name:'Состав смены'})).toBeNull();
  });
  it('выход очищает оба вида черновиков своего пользователя и сохраняет чужой namespace',async()=>{
    sessionStorage.setItem('naryadai.draft.w1.o1','photo bytes');localStorage.setItem('naryadai.draft.w1.create','text');localStorage.setItem('naryadai.draft.w2.create','other');
    const user=userEvent.setup();render(<App/>);await screen.findByRole('heading',{name:'Мои наряды'});
    await user.click(screen.getByRole('button',{name:'Открыть профиль'}));await user.click(screen.getByRole('button',{name:'Выйти из приложения'}));
    await screen.findByRole('heading',{name:'Войти в приложение'});expect(sessionStorage.getItem('naryadai.draft.w1.o1')).toBeNull();expect(localStorage.getItem('naryadai.draft.w1.create')).toBeNull();expect(localStorage.getItem('naryadai.draft.w2.create')).toBe('other');
  });
  it('профиль удерживает фокус и возвращает его после Escape',async()=>{
    const user=userEvent.setup();render(<App/>);await screen.findByRole('heading',{name:'Мои наряды'});const opener=screen.getByRole('button',{name:'Открыть профиль'});await user.click(opener);
    const dialog=screen.getByRole('dialog');expect(dialog.contains(document.activeElement)).toBe(true);const last=within(dialog).getByRole('button',{name:'Выйти из приложения'});last.focus();fireEvent.keyDown(document,{key:'Tab'});expect(document.activeElement).toBe(within(dialog).getByRole('button',{name:'Закрыть профиль'}));
    fireEvent.keyDown(document,{key:'Escape'});await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());expect(document.activeElement).toBe(opener);
  });
  it('для Telegram предлагает явную ссылку после ответа и не зависит от popup',async()=>{
    mocks.post.mockResolvedValue({url:'https://t.me/demo_bot?start=synthetic'});history.replaceState(null,'','#/notifications');const user=userEvent.setup();render(<App/>);
    await user.click(await screen.findByRole('button',{name:'Подключить Telegram'}));const link=await screen.findByRole('link',{name:'Открыть Telegram'});expect(link.getAttribute('href')).toBe('https://t.me/demo_bot?start=synthetic');expect(screen.queryByText(/Ссылка открыта/)).toBeNull();
  });
  it('показывает срок в UTC+5 независимо от часового пояса браузера',()=>{expect(dateTime('2026-10-08T00:00:00Z')).toContain('05:00');});
});
