const SHELL='naryadai-shell-v1';
self.addEventListener('install',e=>e.waitUntil(caches.open(SHELL).then(cache=>cache.addAll(['/','/icon.svg','/manifest.webmanifest']))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('naryadai-shell-')&&k!==SHELL).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
  const url=new URL(e.request.url);
  if(e.request.method!=='GET'||url.origin!==self.location.origin||e.request.headers.has('Authorization')||url.pathname.startsWith('/api')||url.pathname==='/health'||(!['document','script','style','image','font','manifest'].includes(e.request.destination)&&e.request.mode!=='navigate'))return;
  e.respondWith(fetch(e.request).then(response=>{if(response.ok){const copy=response.clone();caches.open(SHELL).then(cache=>cache.put(e.request,copy));}return response;}).catch(async()=>{const cached=await caches.match(e.request);if(cached)return cached;if(e.request.mode==='navigate')return(await caches.match('/'))||Response.error();return Response.error();}));
});
