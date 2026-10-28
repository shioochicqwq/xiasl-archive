(async()=>{
const base='/xiasl-archive/',main=document.getElementById('search'),params=new URL(location.href).searchParams;
const year=params.get('__archive_year')||'2017',query=params.get('q')||params.get('srchtxt')||params.get('keyword')||'',source=params.get('source')||'all';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// "2017年的首页" 这类查询直接跳转
let home=query.match(/^\s*(\d{4})\s*年?\s*的?\s*(?:首页|主页)\s*$/);
if(home){location.replace(base+'?__archive_year='+home[1]);return;}

main.innerHTML='<h1>存档搜索</h1><p><a href="'+base+'?__archive_year='+year+'">回到 '+year+' 年首页</a></p><form><input name="q" value="'+esc(query)+'" aria-label="关键词"><input type="hidden" name="__archive_year" value="'+year+'"><select name="source">'+[['all','全部来源'],['ia','Internet Archive'],['cc','Common Crawl'],['cc_only','仅本地 CC 有记录'],['reprint','第三方转载']].map(([v,t])=>'<option value="'+v+'"'+(v===source?' selected':'')+'>'+t+'</option>').join('')+'</select><button>搜索</button></form><p id="count"></p><section id="results"></section>';
if(!query)return;

const countEl=document.getElementById('count'),resultsEl=document.getElementById('results');
const CAT=base+'s/',CACHE='xiasl-search-v2',PAGE_SIZE=25;
const enc=new TextEncoder();
function fnv1a(s){let h=2166136261;const b=enc.encode(s);for(let i=0;i<b.length;i++){h^=b[i];h=Math.imul(h,16777619);}return h>>>0;}
const CJK=/[\u4e00-\u9fff]+/g,WORD=/[a-z0-9]+/g;
function tokenize(text){const out=new Set();const t=(text||'').toLowerCase();for(const run of t.match(CJK)||[]){if(run.length===1)out.add(run);else for(let i=0;i<run.length-1;i++)out.add(run.slice(i,i+2));}for(const run of t.match(WORD)||[]){if(run.length>=2)out.add(run);}return [...out];}

// 分片走 Cache Storage,重复查询不再联网
async function getJsonGz(path){
  try{const c=await caches.open(CACHE);const hit=await c.match(new URL(path,location.origin));if(hit)return JSON.parse(await new Response(hit.body.pipeThrough(new DecompressionStream('gzip'))).text());}catch(e){}
  const r=await fetch(path);
  if(!r.ok)throw Error('索引加载失败 '+r.status);
  const buf=await r.arrayBuffer();
  try{const c=await caches.open(CACHE);await c.put(new URL(path,location.origin),new Response(buf,{headers:{'Content-Type':'application/gzip'}}));}catch(e){}
  const text=await new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  return JSON.parse(text);
}
function intersect(a,b){const out=[];let i=0,j=0;while(i<a.length&&j<b.length){if(a[i]===b[j]){out.push(a[i]);i++;j++;}else if(a[i]<b[j])i++;else j++;}return out;}

try{
  countEl.textContent='正在查询索引…';
  const man=await getJsonGz(CAT+'manifest.json.gz');
  const toks=[...new Set(tokenize(query))];
  let ids=[];
  if(toks.length){
    const lists=await Promise.all(toks.map(async t=>{
      const bucket=await getJsonGz(CAT+'ix/'+(fnv1a(t)%man.shards)+'.json.gz');
      return bucket[t]||[];
    }));
    if(lists.every(l=>l.length)){
      lists.sort((a,b)=>a.length-b.length);
      ids=lists[0];
      for(let k=1;k<lists.length&&ids.length;k++)ids=intersect(ids,lists[k]);
    }
  }

  // 来源筛选:flags 每位 = provIdx*2 + cc,只有 13KB 左右
  const flags=await getJsonGz(CAT+'flags.json.gz');
  const filtered=[];
  for(const id of ids){
    const f=flags[id]||0,provIdx=f>>1,cc=f&1,prov=(man.provs[provIdx]||'').toLowerCase();
    const ok=source==='all'||(source==='ia'&&prov.includes('internet archive'))||(source==='cc'&&prov.includes('common crawl'))||(source==='cc_only'&&cc)||(source==='reprint'&&prov.includes('reprint'));
    if(ok)filtered.push(id);
  }

  const page=Math.max(1,Number(params.get('page'))||1);
  const slice=filtered.slice((page-1)*PAGE_SIZE,page*PAGE_SIZE);
  countEl.textContent='找到 '+filtered.length+' 项。CC 筛选仅反映本地收录，不能证明 IA 无档。';
  if(!slice.length){resultsEl.innerHTML='<p>没有匹配的存档页面。</p>';return;}

  // 只取当前这一页需要的文档分片
  const shardIds=[...new Set(slice.map(id=>id%man.docShards))];
  const shards=await Promise.all(shardIds.map(s=>getJsonGz(CAT+'docs/'+s+'.json.gz')));
  const byShard={};shardIds.forEach((s,i)=>{const m={};shards[i].forEach((row,idx)=>m[s+':'+idx]=row);byShard[s]=shards[i];});
  const docAt=id=>{const s=id%man.docShards,rows=byShard[s];if(!rows)return null;const idx=Math.floor(id/man.docShards);return rows[idx]||null;};

  resultsEl.innerHTML=slice.map(id=>{
    const d=docAt(id);if(!d)return'';
    const host=man.hosts[d[6]]||'',path=d[2]||'/';
    let href=base+(host==='xiasl.net'||host===''?'':'__hosts__/'+host+'/')+path.replace(/^\//,'');
    href+=(path.includes('?')?'&':'?')+'__archive_year='+year;
    return'<article><h2><a href="'+esc(href)+'">'+esc(d[0])+'</a></h2><p>'+esc((d[1]||'').slice(0,180))+'</p><small>'+esc(man.provs[d[4]]||'')+' · '+esc(d[3])+'</small></article>';
  }).join('')+'<p>'+[page>1?page-1:null,page*PAGE_SIZE<filtered.length?page+1:null].filter(Boolean).map(n=>{const p=new URLSearchParams(params);p.set('page',n);return'<a href="?'+esc(p.toString())+'">第 '+n+' 页</a>';}).join(' · ')+'</p>';
}catch(e){countEl.textContent=e.message;}
})();
