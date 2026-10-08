import {beforeEach,describe,expect,it,vi} from 'vitest';
import {api,ApiError} from './api';
beforeEach(()=>api.setToken(null));
describe('Транспорт приложения',()=>{
  it('дочитывает следующую страницу справочника с исходными фильтрами и авторизацией',async()=>{
    api.setToken('test-session');const fetcher=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({items:[{id:'1'},{id:'2'}]}))).mockResolvedValueOnce(new Response(JSON.stringify({items:[{id:'3'}]})));
    vi.stubGlobal('fetch',fetcher);const result=await api.get('/catalog/materials?limit=2&active_only=true');expect(result.items.map((item:any)=>item.id)).toEqual(['1','2','3']);const next=new URL(String(fetcher.mock.calls[1][0]),'http://localhost');expect(next.searchParams.get('offset')).toBe('2');expect(next.searchParams.get('active_only')).toBe('true');expect(fetcher.mock.calls[1][1].headers.get('Authorization')).toBe('Bearer test-session');vi.unstubAllGlobals();
  });
  it('поздний 401 старого запроса не завершает новую сессию',async()=>{
    let resolve!:(response:Response)=>void;vi.stubGlobal('fetch',vi.fn(()=>new Promise<Response>(done=>{resolve=done;})));const listener=vi.fn();window.addEventListener('naryadai:unauthorized',listener);api.setToken('old');const request=api.get('/auth/me');api.setToken('new');resolve(new Response(JSON.stringify({detail:'Сессия истекла'}),{status:401}));await expect(request).rejects.toBeInstanceOf(ApiError);expect(listener).not.toHaveBeenCalled();window.removeEventListener('naryadai:unauthorized',listener);vi.unstubAllGlobals();
  });
  it('потеря ответа не выдаётся за отсутствие сохранения',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new TypeError('network')));try{await api.post('/orders',{operation_id:'same'});throw new Error('unexpected success');}catch(error){expect(error).toBeInstanceOf(ApiError);expect((error as ApiError).status).toBe(0);expect((error as Error).message).toContain('не подтверждён');expect((error as Error).message).not.toContain('не отправлены');}finally{vi.unstubAllGlobals();}
  });
});
