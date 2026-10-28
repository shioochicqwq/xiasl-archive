const BASE='/xiasl-archive/';
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CSP="default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; form-action 'self'; base-uri 'self'";
// 移动网络下 4MB 目录偶发失败,重试并把解压后的字节缓冲下来(数据文件都很小)。
async function fetchGz(path){let lastErr;for(let attempt=0;attempt<3;attempt++){try{let r=await fetch(new URL(path,self.location.origin),{cache:'reload'});if(!r.ok)throw Error('HTTP '+r.status);let buf=await new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();return new Uint8Array(buf);}catch(e){lastErr=e;await new Promise(res=>setTimeout(res,400*(attempt+1)));}}throw Error('数据文件载入失败('+(lastErr&&lastErr.message?lastErr.message:lastErr)+'): '+path);}
let catalog;function catalogGet(){return catalog??=(async()=>JSON.parse(new TextDecoder().decode(await fetchGz(BASE+'index.json.gz'))))().catch(e=>{catalog=null;throw e;});}
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
async function replay(request){if(!['GET','HEAD'].includes(request.method))return html(shell('只读历史存档','登录、发帖等原站后端功能未恢复。'),405);let u=new URL(request.url),c=await catalogGet(),path=u.pathname.slice(BASE.length-1),year=u.searchParams.get('__archive_year')||(request.referrer?new URL(request.referrer).searchParams.get('__archive_year'):null)||'2017';if(!/^\d{4}$/.test(year))year='2017';
 if(path==='/__archive__/home'){let y=u.searchParams.get('year');return y&&c.homes[y]!==undefined?Response.redirect(new URL(homeLink(y),u.origin),302):html(picker(c,y?'该年没有可读首页':''));}
 if(/search\.php$/.test(path)||path.includes('/search.xiasl.net/'))return html(shell('存档搜索','<main id="search" style="max-width:1000px;margin:30px auto;font:16px sans-serif">正在加载搜索索引…</main><script src="'+BASE+'search.js"></script>'));
 let ids=c.routes[key(u)]||c.canonical[canonical(u)];
 if(['/','/index.html','/index.htm','/index.asp','/portal.php'].includes(path.toLowerCase())&&(!u.searchParams.has('mod')||u.searchParams.get('mod')==='index')){if(c.homes[year]===undefined)return html(picker(c,'该年没有可读首页'),404);ids=[c.homes[year]];}
 if(!ids){let bare=key(new URL(u.pathname,u.origin));if(/\.(css|js|gif|png|jpe?g|ico|bmp|woff2?)$/i.test(path)&&[...u.searchParams].filter(([k])=>!['__archive_ctx','__archive_year'].includes(k)).every(([k,v])=>v===''&&/^[a-z0-9]{2,12}$/i.test(k)))ids=c.assets?.[bare]||c.routes[bare];}
 if(!ids?.length)return html(shell('此页暂无存档','<p>这个历史地址目前没有可用存档。</p><a href="'+homeLink(year)+'">回到 '+year+' 年首页</a>'),404);
 let ctx=u.searchParams.get('__archive_ctx')||'',target=instant(/^\d{14}~/.test(ctx)?ctx.slice(0,14):(year==='2017'?'20171016182614':year+'0701120000'));
 let rows=ids.map(i=>c.records[i]).filter(r=>!r[7]?.length||r[7].includes(ctx.split('~')[1]));if(!rows.length)return html(shell('资源缺失','这个版本的资源尚未恢复。'),404);
 rows.sort((a,b)=>Number(a[5])-Number(b[5])||(a[2]?0:1)-(b[2]?0:1)||Math.abs(instant(a[2])-target)-Math.abs(instant(b[2])-target));let row=rows[0],bytes=await fetchGz(BASE+'data/'+row[0]+'.gz'),headers={'Content-Type':row[1],'Content-Security-Policy':CSP,'X-Archive-Capture':row[2],'X-Archive-Provider':encodeURIComponent(row[3])};
 if(!row[1].includes('html'))return new Response(bytes,{headers});
 let text=new TextDecoder().decode(bytes);text=text.replace('<script src="'+BASE+'client.js">','<script>window.__archiveBrowsingYear='+JSON.stringify(year)+';</script><script src="'+BASE+'client.js">');let date=row[2]?row[2].slice(0,4)+'-'+row[2].slice(4,6)+'-'+row[2].slice(6,8):'无历史抓取日期';let bar='<aside style="padding:9px;background:#f6f3e8;font:14px sans-serif;position:relative;z-index:99999" data-archive-navigation="year">浏览年份 '+escape(year)+' · 本页存档 '+escape(date)+' · '+escape(row[3])+' · <a href="'+homeLink(year)+'">回到 '+escape(year)+' 年首页</a> · <a href="'+BASE+'__archive__/home">选择首页年份</a> · <a href="'+BASE+'search.php?__archive_year='+year+'">搜索</a><details><summary>来源</summary><p>原始地址：'+escape(row[4])+'</p><p>'+escape(row[6]||('https://web.archive.org/web/'+row[2]+'/'+row[4]))+'</p></details></aside>';
 text=/<body\b/i.test(text)?text.replace(/(<body\b[^>]*>)/i,'$1'+bar):bar+text;
 return new Response(text,{headers});
}
self.addEventListener('fetch',e=>{let u=new URL(e.request.url);if(u.origin!==self.location.origin||!u.pathname.startsWith(BASE))return;let p=u.pathname.slice(BASE.length);if(['sw.js','client.js','search.js','index.json.gz','search.json.gz','build-report.json'].includes(p)||/^data\/[0-9a-f]{64}\.gz$/.test(p))return;e.respondWith(replay(e.request).catch(err=>html(shell('加载失败','<p>'+escape(err.message)+'</p><button onclick="location.reload()">重试</button>'),503)));});
