const BASE=(import.meta.env.VITE_API_BASE_URL||'/api').replace(/\/$/,'');
let token:string|null=null;
export class ApiError extends Error{constructor(public status:number,message:string,public detail:unknown=null){super(message);this.name='ApiError';}}
function message(detail:any):string{if(typeof detail==='string')return detail;if(Array.isArray(detail))return detail.map(item=>{const field=(item.loc||[]).filter((s:string)=>!['body','query'].includes(s)).join('.');return `${field?field+': ':''}${item.msg==='Field required'?'Заполните поле':item.msg||'Проверьте значение'}`;}).join('; ');return detail?.message||'Не удалось выполнить действие. Повторите позже.';}
async function request(path:string,options:RequestInit={}):Promise<Response>{
  const requestToken=token;const headers=new Headers(options.headers);if(requestToken)headers.set('Authorization',`Bearer ${requestToken}`);
  let response:Response;
  try{response=await fetch(BASE+path,{...options,headers,cache:'no-store'});}catch(error){if(error instanceof DOMException&&error.name==='AbortError')throw error;throw new ApiError(0,'Нет связи с сервером. Результат запроса не подтверждён. Повторите после восстановления соединения.');}
  if(!response.ok){let data:any=null;try{data=await response.json();}catch{/* no response body */}if(response.status===401&&path!=='/auth/login'&&requestToken===token)window.dispatchEvent(new Event('naryadai:unauthorized'));throw new ApiError(response.status,message(data?.detail),data?.detail??null);}return response;
}
const jsonBody=(method:string,body?:unknown):RequestInit=>({method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
async function parse<T>(response:Response):Promise<T>{return response.status===204?undefined as T:response.json();}
async function getJson<T>(path:string):Promise<T>{
  const first=await parse<any>(await request(path));
  if(!/^\/catalog\/(sections|equipment|brigades|fault-codes|materials|time-norms|material-norms)(?:\?|$)/.test(path)||!Array.isArray(first?.items))return first as T;
  const [pathname,search='']=path.split('?'),params=new URLSearchParams(search);
  const limit=Number(params.get('limit')||100),start=Number(params.get('offset')||0);
  if(!Number.isFinite(limit)||limit<1||first.items.length<limit)return first as T;
  const items=[...first.items];let offset=start+limit;
  while(true){params.set('offset',String(offset));params.set('limit',String(limit));const page=await parse<any>(await request(pathname+'?'+params.toString()));items.push(...page.items);if(page.items.length<limit)break;offset+=limit;}
  return {...first,items:Array.from(new Map(items.map(item=>[item.id,item])).values())} as T;
}
export const api={
  setToken(value:string|null){token=value;},
  get:async<T=any>(path:string)=>getJson<T>(path),
  post:async<T=any>(path:string,body?:unknown)=>parse<T>(await request(path,jsonBody('POST',body))),
  patch:async<T=any>(path:string,body:unknown)=>parse<T>(await request(path,jsonBody('PATCH',body))),
  put:async<T=any>(path:string,body:unknown)=>parse<T>(await request(path,jsonBody('PUT',body))),
  upload:async<T=any>(path:string,body:FormData)=>parse<T>(await request(path,{method:'POST',body})),
  getBlob:async(path:string)=>(await request(path)).blob(),
  async download(path:string,filename:string){const blob=await this.getBlob(path);const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);},
};
