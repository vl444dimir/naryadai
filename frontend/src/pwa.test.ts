import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {describe,expect,it,vi} from 'vitest';

function worker(){
  const handlers:Record<string,Function>={};
  const addAll=vi.fn().mockResolvedValue(undefined),put=vi.fn().mockResolvedValue(undefined),fetcher=vi.fn().mockResolvedValue(new Response('public asset'));
  const cache={addAll,put};
  vm.runInNewContext(readFileSync('public/sw.js','utf8'),{
    self:{location:{origin:'http://local.test'},clients:{claim:()=>Promise.resolve()},addEventListener:(name:string,callback:Function)=>{handlers[name]=callback;}},
    caches:{open:()=>Promise.resolve(cache),keys:()=>Promise.resolve([]),delete:()=>Promise.resolve(true),match:()=>Promise.resolve(undefined)},fetch:fetcher,URL,Response,
  });return {handlers,fetcher,addAll,put};
}
describe('PWA и граница данных',()=>{
  it('не перехватывает API, запросы с Bearer и произвольный JSON на другом API-пути',()=>{
    const {handlers,fetcher}=worker();
    for(const [path,destination,authorization] of [['/api/orders','document',false],['/private-photo','image',true],['/v1/orders','',false]] as const){
      const respondWith=vi.fn();handlers.fetch({request:{url:'http://local.test'+path,method:'GET',mode:'cors',destination,headers:new Headers(authorization?{Authorization:'Bearer test'}:{})},respondWith});expect(respondWith).not.toHaveBeenCalled();
    }expect(fetcher).not.toHaveBeenCalled();
  });
  it('кеширует успешный публичный файл интерфейса',async()=>{
    const {handlers,fetcher,put}=worker();let result!:Promise<Response>;
    handlers.fetch({request:{url:'http://local.test/assets/app.js',method:'GET',mode:'cors',destination:'script',headers:new Headers()},respondWith:(value:Promise<Response>)=>{result=value;}});
    expect(await (await result).text()).toBe('public asset');expect(fetcher).toHaveBeenCalledOnce();await Promise.resolve();expect(put).toHaveBeenCalledOnce();
  });
});
