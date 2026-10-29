"""The Control Center page, its service worker and icon (kept in Python so the .exe needs no data files).

Plain HTML/CSS/JS, no outside scripts or fonts: it works on the home network without
internet and nothing is loaded from other sites. All text from the bot is inserted
with textContent, never as HTML.
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#0f1419"/>
<path d="M112 352 L208 240 L280 296 L400 160" fill="none" stroke="#3ecf8e" stroke-width="40"
 stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="400" cy="160" r="28" fill="#3ecf8e"/>
</svg>"""

# Caches only the page shell so the app opens offline and shows "bağlantı yok".
SERVICE_WORKER = """
const CACHE = 'kb-shell-v1';
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/panel/api')) return;
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request)));
});
"""

PAGE_HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1419">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Kripto Botu">
<title>Kripto Botu</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<style>
:root{--bg:#0f1419;--card:#171d24;--line:#26303a;--text:#e6edf3;--mute:#8b98a5;
--good:#3ecf8e;--warn:#f0b429;--bad:#f25f5c;--accent:#4c9aff;--r:14px}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
body{padding-bottom:calc(72px + env(safe-area-inset-bottom))}
header{position:sticky;top:0;z-index:5;background:rgba(15,20,25,.96);border-bottom:1px solid var(--line);
padding:calc(10px + env(safe-area-inset-top)) 16px 10px;display:flex;align-items:center;gap:10px}
header h1{font-size:17px;margin:0;flex:1}
.pill{font-size:12px;font-weight:600;padding:4px 10px;border-radius:99px;background:var(--line);white-space:nowrap}
.pill.good{background:rgba(62,207,142,.15);color:var(--good)}
.pill.warn{background:rgba(240,180,41,.15);color:var(--warn)}
.pill.bad{background:rgba(242,95,92,.15);color:var(--bad)}
main{max-width:1100px;margin:0 auto;padding:14px 16px}
.grid{display:grid;gap:14px;grid-template-columns:1fr}
@media(min-width:820px){.grid.two{grid-template-columns:1fr 1fr}.grid.three{grid-template-columns:1fr 1fr 1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;min-width:0}
.card h2{font-size:13px;font-weight:600;color:var(--mute);margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em}
.big{font-size:30px;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--mute);font-size:13px}
.row{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.row span:last-child{text-align:right;overflow-wrap:anywhere}
@media(max-width:480px){.wide{display:none}}
.pos{color:var(--good)}.neg{color:var(--bad)}
.btns{display:flex;gap:10px;margin-top:12px}
button{font:inherit;font-weight:600;border:0;border-radius:10px;padding:12px 14px;cursor:pointer;flex:1;min-height:44px}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.b-stop{background:var(--bad);color:#fff}.b-go{background:var(--line);color:var(--text)}
.b-main{background:var(--accent);color:#fff}
.bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin:6px 0 2px}
.bar i{display:block;height:100%;border-radius:99px;background:var(--good)}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{color:var(--mute);font-weight:500;text-align:left;padding:6px 4px;font-size:12px}
.table td{padding:7px 4px;border-top:1px solid var(--line);vertical-align:top}
.table td.n,.table th.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
.ev{padding:9px 0;border-top:1px solid var(--line);white-space:pre-wrap;word-break:break-word;font-size:14px}
.ev:first-child{border-top:0}
.ev time{display:block;color:var(--mute);font-size:12px}
pre{white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,Consolas,monospace;margin:0;color:var(--text)}
.seg{display:flex;background:var(--line);border-radius:10px;padding:3px;margin-bottom:10px}
.seg button{background:none;color:var(--mute);padding:8px;min-height:36px}
.seg button.on{background:var(--card);color:var(--text)}
.score{display:flex;align-items:center;gap:16px}
.ring{width:84px;height:84px;flex:none}
nav{position:fixed;bottom:0;left:0;right:0;z-index:5;display:flex;background:rgba(15,20,25,.97);
border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
nav button{background:none;color:var(--mute);border-radius:0;padding:10px 2px;font-size:12px;font-weight:500;min-height:56px}
nav button.on{color:var(--text)}
nav svg{display:block;margin:0 auto 3px;width:22px;height:22px}
@media(min-width:820px){nav{position:sticky;top:57px;bottom:auto;max-width:1100px;margin:0 auto;border:0;
background:var(--bg);padding:0 16px}nav button{flex:none;padding:10px 16px;min-height:44px;font-size:14px}
nav svg{display:none}body{padding-bottom:20px}}
.hide{display:none!important}
.login{max-width:420px;margin:12vh auto;padding:0 16px}
.login input{width:100%;font:inherit;padding:12px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--text);margin:10px 0}
.notice{padding:10px 12px;border-radius:10px;background:rgba(240,180,41,.12);color:var(--warn);font-size:14px;margin-bottom:14px}
.guide h3{font-size:15px;margin:16px 0 6px}.guide p,.guide li{color:#c9d3dc}.guide ul{padding-left:20px;margin:6px 0}
code{font:13px ui-monospace,Consolas,monospace;background:var(--line);padding:1px 5px;border-radius:5px}
</style>
</head>
<body>
<div id="login" class="login hide">
  <h1>Kripto Botu Kontrol Merkezi</h1>
  <p class="sub">Bilgisayardaki siyah pencerede yazan bağlantıyı açın veya Telegram'da <code>/panel</code> yazın.
  Anahtarı elle girmek isterseniz: <code>.env</code> dosyasındaki <code>PANEL_KEY</code>.</p>
  <label for="key">Panel anahtarı</label>
  <input id="key" type="password" autocomplete="current-password">
  <button class="b-main" id="save">Giriş yap</button>
  <p id="loginmsg" class="sub"></p>
</div>

<div id="app" class="hide">
<header><h1>Kripto Botu</h1><span id="mode" class="pill">…</span><span id="pill" class="pill">…</span></header>
<nav id="nav"></nav>
<main>
  <div id="offline" class="notice hide">Bota ulaşılamıyor. Bilgisayar açık ve bot çalışıyor mu? Son bilgiler gösteriliyor.</div>

  <section data-tab="ozet" class="grid">
    <div class="grid two">
      <div class="card"><h2>Deneme hesabı</h2>
        <div class="big" id="equity">–</div><div class="sub" id="equityusd"></div>
        <div id="acct" style="margin-top:10px"></div>
      </div>
      <div class="card"><h2>Sistem sağlık puanı</h2>
        <div class="score"><svg class="ring" viewBox="0 0 36 36" aria-hidden="true">
          <circle cx="18" cy="18" r="15.9" fill="none" stroke="#26303a" stroke-width="3.2"/>
          <circle id="ring" cx="18" cy="18" r="15.9" fill="none" stroke="#3ecf8e" stroke-width="3.2"
            stroke-dasharray="0 100" stroke-linecap="round" transform="rotate(-90 18 18)"/></svg>
          <div><div class="big" id="score">–</div><div class="sub" id="scoresum"></div></div></div>
        <div class="btns"><button class="b-stop" id="stop">Acil durdur</button><button class="b-go" id="resume">Devam ettir</button></div>
        <p class="sub" id="ctlmsg"></p>
      </div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Açık pozisyonlar</h2><div id="positions"></div></div>
      <div class="card"><h2>Bugünkü özet</h2><div id="today"></div></div>
    </div>
    <div class="card"><h2>Fiyatlar</h2><div class="scroll"><table class="table" id="prices"></table></div></div>
  </section>

  <section data-tab="islem" class="grid hide">
    <div class="card"><h2>Kazanç raporu</h2>
      <div class="seg" id="seg"><button data-p="gün" class="on">Bugün</button><button data-p="hafta">Bu hafta</button><button data-p="ay">Bu ay</button></div>
      <div id="report"></div></div>
    <div class="card"><h2>Son gün sonu özeti</h2><pre id="daily" class="sub"></pre></div>
    <div class="card"><h2>Son olaylar (alış, satış, uyarı)</h2><div id="events"></div></div>
  </section>

  <section data-tab="risk" class="grid hide">
    <div class="grid two">
      <div class="card"><h2>Zarar sınırları</h2><div id="limits"></div></div>
      <div class="card"><h2>Durdurmalar</h2><div id="halts"></div></div>
    </div>
    <div class="card"><h2>Strateji kararları</h2><div id="strategy"></div></div>
    <div class="card"><h2>Çiftlerin işleme uygunluğu</h2><div id="tradable"></div></div>
    <div class="card"><h2>Son geçmiş veri testi</h2><pre id="backtest" class="sub"></pre></div>
  </section>

  <section data-tab="saglik" class="grid hide">
    <div class="card"><h2>Parça parça sağlık puanı (hedef 10 üzerinden 8+)</h2><div id="parts"></div>
      <p class="sub" id="healthnote"></p></div>
    <div class="card"><h2>Jarvis bağlantısı</h2><div id="jarvis"></div></div>
  </section>

  <section data-tab="rehber" class="grid hide">
    <div class="card guide" id="guide"></div>
  </section>
</main>
</div>

<script>
"use strict";
const TABS=[["ozet","Özet","M3 12l9-9 9 9M5 10v10h14V10"],["islem","İşlemler","M4 6h16M4 12h16M4 18h10"],
["risk","Risk","M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"],["saglik","Sağlık","M3 12h4l3-8 4 16 3-8h4"],
["rehber","Rehber","M5 4h10l4 4v12H5zM9 12h6M9 16h6"]];
const $=id=>document.getElementById(id);
let KEY=null,STATE=null,PERIOD="gün",TAB="ozet",timer=null;
try{KEY=localStorage.getItem("kb_key")}catch(e){}
const frag=new URLSearchParams(location.hash.slice(1));
if(frag.get("k")){KEY=frag.get("k");try{localStorage.setItem("kb_key",KEY)}catch(e){}history.replaceState(null,"","/")}

function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function clear(n){while(n.firstChild)n.removeChild(n.firstChild);return n}
function num(v,d=2){if(v==null||v==="")return"–";const x=Number(v);if(!isFinite(x))return String(v);
return x.toLocaleString("tr-TR",{minimumFractionDigits:d,maximumFractionDigits:d})}
function tl(v){return num(v)+" TL"}
function signed(v,unit){const x=Number(v||0);return(x>0?"+":"")+num(x)+(unit?" "+unit:"")}
function cls(v){const x=Number(v||0);return x>0?"pos":x<0?"neg":""}
function row(parent,a,b,bcls){const r=el("div","row");r.append(el("span",null,a));r.append(el("span",bcls||null,b));parent.append(r);return r}
function when(iso){try{return new Date(iso).toLocaleString("tr-TR",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"})}catch(e){return iso}}

function buildNav(){const nav=clear($("nav"));for(const[id,name,d]of TABS){const b=el("button",id===TAB?"on":"");
b.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="'+d+'"/></svg>';
b.append(document.createTextNode(name));b.onclick=()=>{TAB=id;buildNav();for(const s of document.querySelectorAll("section"))s.classList.toggle("hide",s.dataset.tab!==id);
try{localStorage.setItem("kb_tab",id)}catch(e){}};nav.append(b)}}

async function api(path,opts={}){const r=await fetch("/panel/api/"+path,{...opts,headers:{"Authorization":"Bearer "+KEY,"Content-Type":"application/json"}});
if(r.status===401||r.status===429){const j=await r.json().catch(()=>({}));showLogin(j.message||"Anahtar yanlış.");throw new Error("auth")}
if(!r.ok)throw new Error("http "+r.status);return r.json()}

function showLogin(msg){$("app").classList.add("hide");$("login").classList.remove("hide");$("loginmsg").textContent=msg||"";if(timer){clearInterval(timer);timer=null}}
function showApp(){$("login").classList.add("hide");$("app").classList.remove("hide");buildNav();
for(const s of document.querySelectorAll("section"))s.classList.toggle("hide",s.dataset.tab!==TAB);refresh();timer=setInterval(refresh,5000)}

async function refresh(){try{STATE=await api("state");$("offline").classList.add("hide");render()}
catch(e){if(e.message!=="auth")$("offline").classList.remove("hide")}}

function render(){const s=STATE,risk=s.risk||{},pf=s.portfolio||null,h=s.health||null;
$("mode").textContent=s.mode==="PAPER_TRADING"?"Deneme parası":s.mode==="READ_ONLY"?"Sadece izleme":s.mode;
const p=$("pill");if(risk.halts&&risk.halts.length){p.textContent="Durduruldu";p.className="pill bad"}
else if(s.new_positions_allowed){p.textContent="Normal";p.className="pill good"}else{p.textContent="Bekliyor";p.className="pill warn"}
// account
if(pf){$("equity").textContent=tl(pf.equity_try);$("equityusd").textContent="≈ "+num(pf.equity_usd)+" USD · başlangıç "+tl(pf.starting_capital);
const a=clear($("acct"));row(a,"Başlangıca göre",signed(pf.total_change_try,"TL"),cls(pf.total_change_try));
row(a,"Kapanan işlemlerden kâr",signed(pf.realized_try,"TL")+" ("+signed(pf.realized_usd,"USD")+")",cls(pf.realized_try));
row(a,"Kapanan işlem",String(pf.trades_closed));row(a,"Ödenen komisyon",tl(pf.fees_try));
const g=risk.capital_guard;if(g)row(a,"Ana para tabanı",tl(g.floor)+" (tabana "+tl(g.headroom)+" var)");
for(const[k,v]of Object.entries(pf.balances||{}))if(Number(v))row(a,"Boştaki para ("+(k==="TRY"?"TL":k)+")",num(v));
const ps=clear($("positions"));if(!pf.positions.length)ps.append(el("p","sub","Açık pozisyon yok. Strateji net bir yükseliş bekliyor; günlerce alış olmaması normaldir."));
for(const x of pf.positions){const c=el("div");c.style.marginTop="6px";c.append(el("b",null,x.pair));ps.append(c);
row(ps,"Alış → şimdi",num(x.entry_price)+" → "+num(x.price));row(ps,"Kâr/zarar",signed(x.unrealized_try,"TL")+" (%"+num(x.unrealized_pct)+")",cls(x.unrealized_try));
row(ps,"Zarar-kes (stop)",num(x.stop_price));row(ps,"Açılış",when(x.opened_at))}}
// health
if(h){$("score").textContent=num(h.score,1)+" / 10";$("scoresum").textContent="%"+h.pct+" · "+h.summary;
const ring=$("ring");ring.setAttribute("stroke-dasharray",h.pct+" 100");ring.setAttribute("stroke",h.score>=h.target?"#3ecf8e":h.score>=6?"#f0b429":"#f25f5c");
const parts=clear($("parts"));for(const x of h.parts){const d=el("div");d.style.padding="8px 0";const top=el("div","row");top.style.borderTop="0";top.style.padding="0";
top.append(el("span",null,x.name));top.append(el("span",x.ok?"pos":"neg",num(x.score,1)+" / 10"));d.append(top);
const bar=el("div","bar");const i=el("i");i.style.width=(x.score*10)+"%";i.style.background=x.ok?"var(--good)":x.score>=6?"var(--warn)":"var(--bad)";bar.append(i);d.append(bar);
d.append(el("div","sub",x.note));parts.append(d)}$("healthnote").textContent=h.note}
// today + reports
renderReport(clear($("today")),(s.reports||{})["gün"]);renderReport(clear($("report")),(s.reports||{})[PERIOD]);
// prices
const t=clear($("prices"));const hd=el("tr");for(const[x,c]of[["Çift",""],["Son fiyat","n"],["USD","n wide"],["24s %","n"],["Durum",""]])hd.append(el("th",c,x));t.append(hd);
for(const x of s.pairs||[]){const tr=el("tr");tr.append(el("td",null,x.pair));tr.append(el("td","n",x.last?num(x.last,x.pair.startsWith("USDT")?4:2):"…"));
tr.append(el("td","n wide",x.last_usd?num(x.last_usd):"–"));tr.append(el("td","n "+cls(x.change_24h_pct),x.change_24h_pct!=null?signed(x.change_24h_pct):"–"));
tr.append(el("td",x.stale?"neg":"pos",x.stale?"eski":"canlı"));t.append(tr)}
// events
const ev=clear($("events"));if(!(s.events||[]).length)ev.append(el("p","sub","Henüz olay yok."));
for(const e of s.events||[]){const d=el("div","ev");d.append(el("time",null,when(e.time)+" · "+e.level));d.append(document.createTextNode(e.message));ev.append(d)}
// limits
const L=clear($("limits")),lim=risk.limits||{};
const guardRows=risk.capital_guard?[["Ana para tabanına kalan paydan kullanılan",risk.capital_guard.used_pct,100]]:[];
for(const[name,used,max]of[...guardRows,["Bugünkü zarar",risk.daily_loss_pct,lim.max_daily_loss_pct],["Bu haftaki zarar",risk.weekly_loss_pct,lim.max_weekly_loss_pct],["Zirveden düşüş",risk.drawdown_pct,lim.max_total_drawdown_pct]]){
const u=Math.max(0,Number(used||0)),m=Number(max||1),share=Math.min(100,u/m*100);const d=el("div");d.style.padding="6px 0";
const top=el("div","row");top.style.border="0";top.style.padding="0";top.append(el("span",null,name));top.append(el("span",null,"%"+num(u)+" / %"+num(m)));d.append(top);
const bar=el("div","bar");const i=el("i");i.style.width=share+"%";i.style.background=share<50?"var(--good)":share<70?"var(--warn)":"var(--bad)";bar.append(i);d.append(bar);L.append(d)}
if(risk.capital_guard)row(L,"Ana para tabanı",tl(risk.capital_guard.floor)+" (başlangıcın %"+num(risk.capital_guard.max_loss_from_start_pct,0)+" altı, kârın %"+num(risk.capital_guard.lock_profit_pct,0)+"'si kilitli)");
if(risk.capital_guard&&risk.capital_guard.pause_after_losses>0)row(L,"Üst üste zararlı işlem",risk.capital_guard.loss_streak+" / "+risk.capital_guard.pause_after_losses+" (dolarsa "+risk.capital_guard.pause_hours+" saat mola)");
row(L,"İşlem başına en fazla risk","%"+num(lim.max_risk_per_trade_pct));row(L,"Aynı anda en fazla pozisyon",(risk.open_positions??"–")+" / "+(lim.max_concurrent_positions||"–"));
const H=clear($("halts"));if(!(risk.halts||[]).length)H.append(el("p","pos","Durdurma yok, bot normal çalışıyor."));
for(const x of risk.halts||[])H.append(el("p","neg",x.message));
// strategy
const S=clear($("strategy"));const st=s.strategy;if(!st)S.append(el("p","sub","Strateji kapalı."));else{
S.append(el("p","sub","Kural: "+(st.bar_minutes>=1440?"günlük":st.bar_minutes/60+" saatlik")+" mumlar, "+(st.trend_bar_minutes>=1440?"günlük":st.trend_bar_minutes/60+" saatlik")+" trend filtresi."));
for(const[pair,x]of Object.entries(st.pairs||{})){const d=el("div","ev");d.append(el("time",null,pair+(x.last_candle?" · son mum "+when(x.last_candle):"")));
d.append(document.createTextNode(x.why||(x.ready?"Karar bekleniyor.":"Geçmiş mumlar yükleniyor…")));S.append(d)}}
const T=clear($("tradable"));for(const x of s.pairs||[]){if(x.tradable===undefined)continue;
row(T,x.pair,x.tradable?"uygun":(x.problems||[]).join("; "),x.tradable?"pos":"neg")}
$("daily").textContent=s.daily_summary?s.daily_summary.text:"Henüz gün sonu özeti yok. Her akşam 22:00'de yazılır.";
$("backtest").textContent=s.backtest||"Henüz test yapılmadı. GeriyeTest.bat ile çalıştırın.";
// jarvis
const J=clear($("jarvis"));const j=s.jarvis||{};row(J,"Durum",j.enabled?(j.running?"açık":"açılamadı"):"kapalı",j.running?"pos":"neg");
if(j.url){row(J,"Adres",j.url);J.append(el("p","sub","Jarvis tek bir istekle her şeyi alır: GET "+j.url+"/overview (Authorization: Bearer JARVIS_READ_KEY). "+
"Anahtarlar .env dosyasında; burada gösterilmez. Jarvis sadece okuyabilir ve acil durdurabilir, emir veremez."))}
row(J,"Olay bildirimi (webhook)",j.webhook?"açık":"kapalı")}

function renderReport(node,r){if(!r){node.append(el("p","sub","–"));return}const c=r.categories;
row(node,"Kazanç",signed(c["Kazanç"].pnl_try,"TL")+" ("+signed(c["Kazanç"].pnl_usd,"USD")+")",cls(c["Kazanç"].pnl_try));
const a=c["Al-sat işlemleri"];row(node,"İşlemler",a.total+" (kârlı "+a.wins+", zararlı "+a.losses+")");
for(const[coin,v]of Object.entries(c["Kripto birimleri"]))row(node,coin,signed(v,"TL"),cls(v))}

$("seg").onclick=e=>{const b=e.target.closest("button");if(!b)return;PERIOD=b.dataset.p;
for(const x of $("seg").children)x.classList.toggle("on",x===b);if(STATE)render()};
$("stop").onclick=async()=>{if(!confirm("Yeni işlem açmayı durdurayım mı? Açık pozisyonların stop kuralları çalışmaya devam eder."))return;
try{const r=await api("stop",{method:"POST",body:JSON.stringify({reason:"Kontrol Merkezi'nden durduruldu"})});$("ctlmsg").textContent=r.message;refresh()}catch(e){}};
$("resume").onclick=async()=>{let confirmBig=false;const dd=(STATE&&STATE.risk&&STATE.risk.halts||[]).some(h=>h.kind==="drawdown");
if(dd){const t=prompt("Acil durdurma (zirveden düşüş) açık. Kaldırmak büyük bir karar. Emin iseniz ONAY yazın:");if(t===null)return;confirmBig=t.trim().toUpperCase()==="ONAY"}
try{const r=await api("resume",{method:"POST",body:JSON.stringify({confirm:confirmBig})});$("ctlmsg").textContent=r.message;refresh()}catch(e){}};
$("save").onclick=()=>{KEY=$("key").value.trim();try{localStorage.setItem("kb_key",KEY)}catch(e){}showApp()};

const G=[
["Bu sistem ne yapar?","Kripto Botu, OKX borsasındaki BTC ve ETH fiyatlarını canlı izler, bir trend kuralına göre deneme parasıyla (sahte para) alım-satım yapar ve her şeyi size bildirir. Gerçek emir gönderilmez."],
["Nereden takip edilir?",["Bu ekran (Kontrol Merkezi): bilgisayar, telefon ve tablette, uygulama gibi.","Telegram (Achilles): her alış, satış, stop ve hata anında mesaj; her akşam gün sonu özeti.","Jarvis: her akşam gün sonu özetini alır ve size anlatır.","Bot arka planda kendi başına çalışır; Jarvis, telefon veya bu ekran kapalı olsa da durmaz."]],
["Nasıl alım yapar?",["4 saatlik mum kapanınca bakar (Türkiye saatiyle 03, 07, 11, 15, 19, 23).","Günlük trend yukarı, fiyat ortalamanın üstünde ve son 20 mumun en yükseğini geçmiş olmalı.","Emir defterinde satıcılar baskınsa almaz.","Sonra risk motoru son kararı verir. Günlerce alış olmaması normaldir."]],
["Nasıl satar?",["Zarar-kes (stop): alıştan sonra fiyat belirli bir seviyeye inerse.","Başa-baş ve iz süren stop: fiyat yükseldikçe stop yukarı gelir, kâr korunur.","3 günde kâra geçmezse veya 15 gün dolunca satar; günlük trend bozulursa satar."]],
["Para neden dolarda duruyor?","Geçmiş veri testinde TL dolara karşı değer kaybettiği için boştaki parayı dolarda (USDT) tutmak daha iyi çıktı. Bakiye yine TL ve USD olarak gösterilir; asıl başarı ölçüsü USD sonucudur."],
["Ana para koruması",["Bot, başlangıç parasının hemen altında bir taban korur (başlangıç %2 eksi; komisyon yüzünden tam sıfır mümkün değil).","İşlem büyüklükleri, açık işlemlerin hepsi aynı anda zarar-kese takılsa ve fiyat stop'un %0,5 altından satılsa bile bakiye tabanın üstünde kalacak şekilde küçültülür.","Kâr geldikçe taban yükselir: kesinleşen kârın (satılmış ya da yükselen stop ile güvenceye alınmış) yarısı kilitlenir. Henüz kâğıt üstündeki kâr tabanı yükseltmez; böylece normal bir geri çekilme botu boş yere durdurmaz.","Bakiye yine de tabana inerse (ani fiyat boşluğu gibi) bot her şeyi satar ve durur; sadece siz 'ONAY' ile açabilirsiniz.","Hiçbir sistem zararı tamamen imkânsız yapamaz; bu koruma zararı küçük ve sınırlı tutar."]],
["Diğer koruma kuralları",["Günlük zarar %3 → gece yarısına kadar yeni işlem yok.","Haftalık zarar %6 → pazartesiye kadar yeni işlem yok.","Zirveden %10 düşüş → bot tüm pozisyonları kapatır ve durur; sadece siz 'ONAY' ile açabilirsiniz.","Üst üste 3 işlem zararla kapanırsa 24 saat mola: piyasa stratejiye uymuyorsa bot zorlamaz.","İşlem başına en fazla %1 risk, aynı anda en fazla 2 pozisyon, tek pozisyon bakiyenin en fazla %30'u.","Sadece spot, kaldıraç yok. Dolar kuru 5 dakikadan eskiyse yeni işlem açılmaz."]],
["Düğmeler",["Acil durdur: yeni işlem açılmaz; bot izlemeye ve açık pozisyonları korumaya devam eder.","Devam ettir: sizin veya Jarvis'in durdurmasını kaldırır. Günlük/haftalık zarar durdurması kendiliğinden kalkar."]],
["Telegram komutları",["/durum – anlık durum","/limitler – zarar sınırları","/rapor (hafta, ay) – kazanç özeti","/durdur ve /devam – acil durdurma","/panel – bu ekranın bağlantısı"]],
["Jarvis neler yapabilir?",["Her akşam 22:00'de gün sonu özetini alır (kapalıysa açılınca gelir; /api/v1/daily-summary ile de okur).","Okur: durum, limitler, olaylar, kazanç raporu, sağlık puanı (tek istekte: /api/v1/overview).","Acil durdurabilir ve kendi durdurmasını kaldırabilir.","Emir veremez, limit değiştiremez, sizin durdurmanızı kaldıramaz. Her Jarvis komutu Telegram'a bildirilir."]],
["Sağlık puanı ne demek?","Her parça (korumalar, zarar sınırlarına uzaklık, fiyat verisi, dolar kuru, strateji verisi, bildirimler, Jarvis) 10 üzerinden puanlanır. Hedef 8 ve üstü. Bu puan sistemin güvenli ve eksiksiz çalıştığını gösterir; kâr garantisi değildir."],
["Arka planda çalışma",["ArkaPlanda-Calistir.bat: bot penceresiz çalışır ve Windows her açıldığında kendiliğinden başlar.","Çökerse veya internet giderse 1 dakika sonra kendini yeniden başlatır.","Bot çalışırken bilgisayar uykuya geçmez (ekran kapanabilir). Bilgisayar kapalıysa bot da çalışmaz.","Durdurmak için: ArkaPlan-Durdur.bat"]],
["Yol haritası",["Bitti: altyapı, risk motoru, Telegram, deneme hesabı, trend stratejisi, geçmiş veri testi, Kontrol Merkezi, ana para koruması, emir defteri kontrolü, gün sonu özeti, arka planda çalışma.","Sonra: haber ve blokzincir verisi.","Bilerek yapılmayanlar: vadeli işlem ve kaldıraç (riski çok artırır).","Gerçek para: sadece deneme hesabı haftalarca dolar bazında kâr ederse, küçük tutarla ve sizin onayınızla."]],
["Önemli","Geçmiş veri testleri ve deneme sonuçları gelecekteki kazancı garanti etmez. Bu bir yatırım tavsiyesi değildir."]];
(function(){const g=$("guide");for(const[t,b]of G){g.append(el("h3",null,t));if(Array.isArray(b)){const u=el("ul");for(const x of b)u.append(el("li",null,x));g.append(u)}else g.append(el("p",null,b))}})();

try{TAB=localStorage.getItem("kb_tab")||"ozet"}catch(e){}
if(!TABS.some(t=>t[0]===TAB))TAB="ozet";
if("serviceWorker"in navigator&&window.isSecureContext)navigator.serviceWorker.register("/sw.js").catch(()=>{});
if(KEY)showApp();else showLogin("");
</script>
</body>
</html>
"""
