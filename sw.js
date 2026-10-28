// v3: 目录按需分片加载。首屏只取 manifest(约 1KB),普通页面约 3KB 路由分片 + 13KB 记录块,
// 不再为了打开首页下载 3.85MB 的整包目录。分片键用 FNV-1a,必须和生成器 shard_catalog.py 一致。
const BASE='/xiasl-archive/';
const CAT=BASE+'c/';
const VERSION=3;
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CSP="default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; form-action 'self'; base-uri 'self'";

// 数据文件很小,重试三次并把解压后的结果缓冲起来;移动网络抖动时自动恢复。
const CAT_CACHE='xiasl-cat-v3';
async function fetchGzCached(path){
  let store=null;
  try{store=await caches.open(CAT_CACHE);const hit=await store.match(new URL(path,self.location.origin));if(hit){return new Uint8Array(await hit.arrayBuffer());}}catch(e){}
  const bytes=await fetchGz(path);
  try{if(!store)store=await caches.open(CAT_CACHE);await store.put(new URL(path,self.location.origin),new Response(bytes,{headers:{'Content-Type':'application/gzip'}}));}catch(e){}
  return bytes;
}
async function fetchGz(path){let lastErr;for(let attempt=0;attempt<3;attempt++){try{let r=await fetch(new URL(path,self.location.origin),{cache:'reload'});if(!r.ok)throw Error('HTTP '+r.status);let buf=await new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();return new Uint8Array(buf);}catch(e){lastErr=e;await new Promise(res=>setTimeout(res,400*(attempt+1)));}}throw Error('数据文件载入失败('+(lastErr&&lastErr.message?lastErr.message:lastErr)+'): '+path);}
const decode=bytes=>new TextDecoder().decode(bytes);

function fnv1a(s){let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}

let manifest;
function manifestGet(){return manifest??=(async()=>JSON.parse(decode(await fetchGzCached(CAT+'manifest.json.gz'))))().catch(e=>{manifest=null;throw e;});}

const bucketCache=new Map();
async function bucket(kind,key,shards){const s=fnv1a(key)%shards,ck=kind+':'+s;let b=bucketCache.get(ck);if(!b){b=JSON.parse(decode(await fetchGzCached(CAT+kind+'/'+s+'.json.gz')));bucketCache.set(ck,b);}return b;}

const recCache=new Map();
async function loadChunk(chunk,size){let rows=recCache.get(chunk);if(!rows){rows=JSON.parse(decode(await fetchGzCached(CAT+'rec/'+chunk+'.json.gz')));recCache.set(chunk,rows);}return rows;}
async function recordsFor(ids,size){const need=new Set();for(const id of ids)need.add(Math.floor(id/size));await Promise.all([...need].map(c=>loadChunk(c,size)));return ids.map(id=>{const c=Math.floor(id/size),rows=recCache.get(c);return rows?rows[id-c*size]:undefined;});}

// 分片取不到时的兜底:退回旧的整包目录。
let fullCatalog;
async function fullGet(){return fullCatalog??=(async()=>JSON.parse(decode(await fetchGz(BASE+'index.json.gz'))))().catch(e=>{fullCatalog=null;throw e;});}

self.addEventListener('install',e=>e.waitUntil(self.skipWaiting()));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
function key(u){let q=[...u.searchParams].filter(([k])=>!['__archive_ctx','__archive_year'].includes(k)).sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:a[1]<b[1]?-1:a[1]>b[1]?1:0);let s=new URLSearchParams(q).toString();return u.pathname.slice(BASE.length-1).toLowerCase()+(s?'?'+s:'');}
function canonical(u){let p=u.pathname.slice(BASE.length-1).toLowerCase(),host='xiasl.net';let h=p.match(/^\/__hosts__\/([^/]+)(\/.*)$/);if(h){host=h[1];p=h[2];}let q=u.searchParams,m,id;
 if((m=p.match(/\/article\/.*\/(\d+)\.html$/)))id='article:'+Number(m[1]);
 else if(/\/shownews(?:_2)?\.asp$/.test(p)&&/^\d+$/.test(q.get('newsid')||''))id='article:'+Number(q.get('newsid'));
 else if((m=p.match(/\/thread-(\d+)-(\d+)-\d+\.html$/)))id='thread:'+Number(m[1])+':page:'+Number(m[2]);
 else if((p.endsWith('/viewthread.php')||q.get('mod')==='viewthread')&&q.has('tid'))id='thread:'+Number(q.get('tid'))+':page:'+Number(q.get('page')||1);
 else if((m=p.match(/\/forum-(\d+)-(\d+)\.html$/)))id='forum:'+Number(m[1])+':page:'+Number(m[2]);
 return id?host+'|'+id:null;
}
function instant(t){return Date.parse(t.slice(0,4)+'-'+t.slice(4,6)+'-'+t.slice(6,8)+'T'+t.slice(8,10)+':'+t.slice(10,12)+':'+t.slice(12,14)+'Z')||0;}
function html(s,status=200){return new Response(s,{status,headers:{'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':CSP}});}
function shell(title,body){return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+escape(title)+'</title><script src="'+BASE+'client.js"></script><body>'+body+'</body></html>';}
function homeLink(y){return BASE+'?__archive_year='+encodeURIComponent(y);}
function picker(c,message=''){return shell('选择首页年份','<main style="max-width:850px;margin:40px auto;font:16px sans-serif"><h1>选择首页年份</h1><p>'+escape(message)+'</p>'+Object.keys(c.homes).sort().map(y=>'<p><a href="'+homeLink(y)+'">'+y+' 年首页</a></p>').join('')+'<h2>其他旧址</h2>'+[['zhusq.533.net/','2002 年早期入口'],['xiasl.com.cn/uchome/','天使的空间'],['wz.xiasl.com.cn/','风荷网址导航（Common Crawl）']].map(([u,t])=>'<p><a href="'+BASE+'__hosts__/'+u+'?__archive_year=2017">'+t+'</a></p>').join('')+'</main>');}

async function lookupIds(u,m,path){
  const k=key(u);
  const isAsset=/\.(css|js|gif|png|jpe?g|ico|bmp|woff2?|svg)$/i.test(path);
  try{
    // 静态资源在路由表里以"纯路径"登记,直接查它,省一次往返
    if(isAsset){
      const bare=key(new URL(u.pathname,u.origin));
      const b=await bucket('r',bare,m.shards);
      if(b[bare]?.length)return b[bare];
    }
    const r=await bucket('r',k,m.shards);if(r[k]?.length)return r[k];
    const can=canonical(u);
    if(can){const c=await bucket('c',can,m.shards);if(c[can]?.length)return c[can];}
    return null;
  }catch(e){
    // 兜底:整包目录(仅在分片不可用时)
    const full=await fullGet();
    return full.routes[k]||full.canonical[canonical(u)]||null;
  }
}

async function replay(request){
 if(!['GET','HEAD'].includes(request.method))return html(shell('只读历史存档','登录、发帖等原站后台功能未恢复。'),405);
 let u=new URL(request.url),m=await manifestGet(),path=u.pathname.slice(BASE.length-1);
 let year=u.searchParams.get('__archive_year')||(request.referrer?new URL(request.referrer).searchParams.get('__archive_year'):null)||'2017';
 if(!/^\d{4}$/.test(year))year='2017';
 if(path==='/__archive__/home'){let y=u.searchParams.get('year');return y&&m.homes[y]!==undefined?Response.redirect(new URL(homeLink(y),u.origin),302):html(picker(m,y?'该年没有可读首页':''));}
 if(/search\.php$/.test(path)||path.includes('/search.xiasl.net/'))return html(shell('存档搜索','<main id="search" style="max-width:1000px;margin:30px auto;font:16px sans-serif">正在加载搜索索引…</main><script src="'+BASE+'search.js"></script>'));
 let ids=null,preset=null;
 if(['/','/index.html','/index.htm','/index.asp','/portal.php'].includes(path.toLowerCase())&&(!u.searchParams.has('mod')||u.searchParams.get('mod')==='index')){
   const h=m.homes[year];
   if(!h)return html(picker(m,'该年没有可读首页'),404);
   ids=[h.id];preset=[h.rec];
 }
 if(!ids)ids=await lookupIds(u,m,path);
 if(!ids?.length)return html(shell('此页暂无存档','<p>这个历史地址目前没有可用存档。</p><a href="'+homeLink(year)+'">回到 '+year+' 年首页</a>'),404);
 let ctx=u.searchParams.get('__archive_ctx')||'',target=instant(/^\d{14}~/.test(ctx)?ctx.slice(0,14):(year==='2017'?'20171016182614':year+'0701120000'));
 let rows=(preset||await recordsFor(ids,m.chunk)).filter(r=>r&&(!r[7]?.length||r[7].includes(ctx.split('~')[1])));
 if(!rows.length)return html(shell('资源缺失','这个版本的资源尚未恢复。'),404);
 rows.sort((a,b)=>Number(a[5])-Number(b[5])||(a[2]?0:1)-(b[2]?0:1)||Math.abs(instant(a[2])-target)-Math.abs(instant(b[2])-target));
 let row=rows[0],bytes=await fetchGz(BASE+'data/'+row[0]+'.gz'),headers={'Content-Type':row[1],'Content-Security-Policy':CSP,'X-Archive-Capture':row[2],'X-Archive-Provider':encodeURIComponent(row[3])};
 if(!row[1].includes('html'))return new Response(bytes,{headers});
 let text=decode(bytes);text=text.replace('<script src="'+BASE+'client.js">','<script>window.__archiveBrowsingYear='+JSON.stringify(year)+';</script><script src="'+BASE+'client.js">');let date=row[2]?row[2].slice(0,4)+'-'+row[2].slice(4,6)+'-'+row[2].slice(6,8):'无历史抓取日期';let bar='<aside style="padding:9px;background:#f6f3e8;font:14px sans-serif;position:relative;z-index:99999" data-archive-navigation="year">浏览年份 '+escape(year)+' · 本页存档 '+escape(date)+' · '+escape(row[3])+' · <a href="'+homeLink(year)+'">回到 '+escape(year)+' 年首页</a> · <a href="'+BASE+'__archive__/home">选择首页年份</a> · <a href="'+BASE+'search.php?__archive_year='+year+'">搜索</a><details><summary>来源</summary><p>原始地址：'+escape(row[4])+'</p><p>'+escape(row[6]||('https://web.archive.org/web/'+row[2]+'/'+row[4]))+'</p></details></aside>';
 text=/<body\b/i.test(text)?text.replace(/(<body\b[^>]*>)/i,'$1'+bar):bar+text;
 return new Response(text,{headers});
}
self.addEventListener('fetch',e=>{let u=new URL(e.request.url);if(u.origin!==self.location.origin||!u.pathname.startsWith(BASE))return;let p=u.pathname.slice(BASE.length);if(['sw.js','client.js','search.js','index.json.gz','search.json.gz','build-report.json'].includes(p)||/^data\/[0-9a-f]{64}\.gz$/.test(p)||p.startsWith('c/'))return;e.respondWith(replay(e.request).catch(err=>html(shell('加载失败','<p>'+escape(err.message)+'</p><button onclick="location.reload()">重试</button>'),503)));});
