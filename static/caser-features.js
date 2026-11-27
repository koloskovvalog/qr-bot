/* Caser-inspired creator cases, inventory favorites and weekly rewards. */
let creatorCaseItems=[], creatorCatalogItems=[], creatorInventoryItems=[];
let creatorFilter="all", creatorSearchTimer=null, creatorCatalogPage=0;
const creatorView=$("creatorCasesView"), rewardsView=$("rewardsView");
const baseSwitchTab=switchTab;
switchTab=function(tab){
  if(creatorView)creatorView.hidden=tab!=="creator";
  if(rewardsView)rewardsView.hidden=tab!=="rewards";
  baseSwitchTab(tab);
  if(tab==="creator")loadCreatorCases();
  if(tab==="rewards")loadRewards();
};

function creatorNavButton(tab,icon,label){
  return '<button data-nav="'+tab+'"><span>'+icon+'</span>'+label+'</button>';
}
function creatorCaseCard(c){
 const fav='<button class="creator-fav" data-creator-fav="'+c.id+'" aria-label="Избранное">'+(c.is_favorite?'♥':'♡')+'</button>';
 const art=c.image_url?'<img src="'+esc(c.image_url)+'" alt="" loading="lazy">':'<div class="creator-case-placeholder">CS2</div>';
 const mine=c.is_mine?'<span class="creator-tag">МОЙ КЕЙС</span>':'<span class="creator-tag">АВТОРСКИЙ</span>';
 return '<article class="creator-case-card"><div class="creator-case-art">'+art+fav+'</div><div class="creator-case-info">'+mine+'<strong>'+esc(c.title)+'</strong><small>'+esc(c.description||'Авторская подборка скинов')+'</small><div class="creator-case-meta"><span>'+c.items_count+' предметов</span><b>DEMO · 0 ₽</b></div><button class="goldbtn" data-creator-open="'+c.id+'">Посмотреть и открыть</button></div></article>';
}
async function loadCreatorCases(){
 const host=$("creatorCasesGrid"); if(!host)return;
 host.innerHTML='<div class="empty">Загружаем авторские кейсы…</div>';
 try{
  const params=new URLSearchParams();
  if(creatorFilter==="mine")params.set("mine","1");
  if(creatorFilter==="favorites")params.set("favorites","1");
  const d=await api("/api/creator-cases?"+params.toString());
  let items=d.items||[];
  const q=($("creatorCaseSearch")?.value||"").trim().toLowerCase();
  if(q)items=items.filter(x=>(x.title+" "+x.description).toLowerCase().includes(q));
  host.innerHTML=items.length?items.map(creatorCaseCard).join(""):'<div class="empty">Пока нет кейсов в этом разделе. Создай свой первый авторский кейс.</div>';
  host.querySelectorAll("[data-creator-fav]").forEach(b=>b.onclick=async()=>{
    try{await api("/api/creator-cases/"+b.dataset.creatorFav+"/favorite",{method:"POST",body:"{}"});loadCreatorCases()}catch(e){toast(e.message)}
  });
  host.querySelectorAll("[data-creator-open]").forEach(b=>b.onclick=()=>openCreatorCase(Number(b.dataset.creatorOpen)));
 }catch(e){host.innerHTML='<div class="empty">'+esc(e.message)+'</div>'}
}
async function openCreatorCase(id){
 try{
  const d=await api("/api/creator-cases");
  const c=(d.items||[]).find(x=>Number(x.id)===id);
  if(!c){toast("Кейс не найден");return}
  $("creatorCaseDetail").hidden=false;
  $("creatorCaseDetail").innerHTML='<div class="creator-detail-head"><button class="case-back" id="creatorCaseBack">← К авторским кейсам</button><h3>'+esc(c.title)+'</h3><p>'+esc(c.description||"Авторский кейс QR BOT PRO")+'</p><span class="demo-pill">DEMO · БЕСПЛАТНО</span></div><div class="creator-detail-grid">'+c.items.map(x=>'<article class="case-item">'+(x.image_url?'<img class="item-thumb" src="'+esc(x.image_url)+'" alt="">':'<div class="item-thumb-placeholder">CS2</div>')+'<strong>'+esc(x.name)+'</strong><small>'+esc(x.rarity)+' · '+Number(x.chance_percent).toFixed(2)+'%</small></article>').join("")+'</div><div class="case-open-actions"><button class="goldbtn" id="creatorOpenOne">Открыть 1 раз</button><button class="case-quick-btn" id="creatorOpenFive">Открыть 5 раз</button></div><div id="creatorOpenResults"></div>';
  $("creatorCaseBack").onclick=()=>{$("creatorCaseDetail").hidden=true;};
  $("creatorOpenOne").onclick=()=>runCreatorOpen(id,1);
  $("creatorOpenFive").onclick=()=>runCreatorOpen(id,5);
  window.scrollTo({top:0,behavior:"smooth"});
 }catch(e){toast(e.message)}
}
async function runCreatorOpen(id,count){
 const host=$("creatorOpenResults");
 host.innerHTML='<div class="empty">Сервер выбирает предметы…</div>';
 $("creatorOpenOne").disabled=true;$("creatorOpenFive").disabled=true;
 try{
  const d=await api("/api/creator-cases/"+id+"/open",{method:"POST",body:JSON.stringify({count})});
  host.innerHTML='<div class="creator-open-results"><h3>Результат открытия</h3>'+d.items.map(x=>'<div class="inventory-item">'+(x.image_url?'<img class="item-thumb" src="'+esc(x.image_url)+'" alt="">':'')+'<div><strong>'+esc(x.item_name)+'</strong><small>'+esc(x.rarity)+'</small></div><b>'+Number(x.chance_percent).toFixed(2)+'%</b></div>').join("")+'<div class="demo-pill">ВИРТУАЛЬНЫЕ ПРЕДМЕТЫ · DEMO</div></div>';
  toast(d.message);showInventory();loadCreatorCases();
 }catch(e){host.innerHTML='<div class="empty">'+esc(e.message)+'</div>'}
 finally{$("creatorOpenOne").disabled=false;$("creatorOpenFive").disabled=false}
}

function creatorBuilderRender(){
 const host=$("creatorSelectedItems"), total=creatorCaseItems.reduce((a,x)=>a+Number(x.chance||0),0);
 $("creatorChanceTotal").textContent="Сумма шансов: "+total.toFixed(2)+"%";
 host.innerHTML=creatorCaseItems.map((x,i)=>'<div class="creator-selected-row">'+(x.image_url?'<img src="'+esc(x.image_url)+'" alt="">':'<span>CS2</span>')+'<div><strong>'+esc(x.name)+'</strong><small>'+esc(x.rarity)+'</small></div><label>Шанс %<input type="number" min="0.01" max="99.99" step="0.01" value="'+Number(x.chance).toFixed(2)+'" data-creator-chance="'+i+'"></label><button data-creator-remove="'+i+'" aria-label="Удалить">×</button></div>').join("");
 host.querySelectorAll("[data-creator-chance]").forEach(inp=>inp.onchange=()=>{creatorCaseItems[Number(inp.dataset.creatorChance)].chance=Math.max(.01,Math.min(99.99,Number(inp.value)||.01));creatorBuilderRender()});
 host.querySelectorAll("[data-creator-remove]").forEach(b=>b.onclick=()=>{creatorCaseItems.splice(Number(b.dataset.creatorRemove),1);creatorBuilderRender()});
}
async function searchCreatorCatalog(reset=true){
 const q=$("creatorSkinSearch").value.trim();
 if(q.length<2){$("creatorCatalogResults").innerHTML='<div class="empty">Введи минимум 2 символа для поиска скина.</div>';return}
 if(reset)creatorCatalogPage=0;
 $("creatorCatalogResults").innerHTML='<div class="empty">Ищем в полном каталоге CS2…</div>';
 try{
  const d=await api("/api/cs2/skins?"+new URLSearchParams({q,page:String(creatorCatalogPage),limit:"24"}));
  creatorCatalogItems=d.items||[];
  $("creatorCatalogResults").innerHTML=creatorCatalogItems.map(x=>'<button class="creator-catalog-item" data-creator-add="'+esc(x.market_hash_name)+'">'+(x.image_url?'<img src="'+esc(x.image_url)+'" alt="">':'<span>CS2</span>')+'<strong>'+esc(x.name)+'</strong><small>'+esc(x.rarity)+' · '+esc(x.wear||"")+'</small>'+(x.market_price_minor?'<b>'+money(x.market_price_minor,"₽")+'</b>':'<b>Цена уточняется</b>')+'</button>').join("")||'<div class="empty">Ничего не найдено.</div>';
  $("creatorCatalogResults").querySelectorAll("[data-creator-add]").forEach(b=>b.onclick=()=>{
   const x=creatorCatalogItems.find(y=>y.market_hash_name===b.dataset.creatorAdd);
   if(!x||creatorCaseItems.some(y=>y.market_hash_name===x.market_hash_name)||creatorCaseItems.length>=30)return;
   const n=creatorCaseItems.length+1;
   creatorCaseItems.forEach(y=>y.chance=100/n);
   creatorCaseItems.push({market_hash_name:x.market_hash_name,name:x.name,rarity:x.rarity,image_url:x.image_url,chance:100/n});
   creatorBuilderRender();toast("Скин добавлен в кейс");
  });
 }catch(e){$("creatorCatalogResults").innerHTML='<div class="empty">'+esc(e.message)+'</div>'}
}
async function createCreatorCase(){
 const title=$("creatorCaseTitle").value.trim(),description=$("creatorCaseDescription").value.trim();
 if(title.length<3||creatorCaseItems.length<2){toast("Добавь название и минимум 2 разных скина");return}
 let sum=creatorCaseItems.reduce((a,x)=>a+Number(x.chance||0),0);
 if(Math.abs(sum-100)>.01){toast("Сумма шансов должна быть ровно 100%. Сейчас "+sum.toFixed(2)+"%");return}
 const items=creatorCaseItems.map(x=>({market_hash_name:x.market_hash_name,chance_bp:Math.round(Number(x.chance)*100)}));
 const diff=10000-items.reduce((a,x)=>a+x.chance_bp,0);
 items[items.length-1].chance_bp+=diff;
 try{
  const d=await api("/api/creator-cases",{method:"POST",body:JSON.stringify({title,description,items})});
  creatorCaseItems=[];$("creatorCaseTitle").value="";$("creatorCaseDescription").value="";
  creatorBuilderRender();$("creatorBuilder").hidden=true;$("creatorCaseDetail").hidden=true;
  creatorFilter="mine";document.querySelectorAll("[data-creator-filter]").forEach(b=>b.classList.toggle("active",b.dataset.creatorFilter==="mine"));
  await loadCreatorCases();toast(d.message);
 }catch(e){toast(e.message)}
}
async function showInventory(){
 const host=$("inventory");if(!host)return;
 try{
  const d=await api("/api/inventory");creatorInventoryItems=d.items||[];
  renderEnhancedInventory();
 }catch(e){host.innerHTML='<div class="empty">Откройте Mini App через Telegram, чтобы увидеть инвентарь.</div>'}
}
function renderEnhancedInventory(){
 const host=$("inventory");let items=creatorInventoryItems.slice();
 const q=($("inventorySearch")?.value||"").trim().toLowerCase();
 const filter=$("inventoryFilter")?.value||"all",sort=$("inventorySort")?.value||"price";
 if(q)items=items.filter(x=>x.item_name.toLowerCase().includes(q));
 if(filter==="favorites")items=items.filter(x=>x.is_favorite);
 if(filter==="skins")items=items.filter(x=>x.item_type==="skin");
 if(sort==="price")items.sort((a,b)=>Number(b.value_minor||0)-Number(a.value_minor||0));
 if(sort==="new")items.sort((a,b)=>Number(b.id)-Number(a.id));
 if(!items.length){host.innerHTML='<div class="empty">В этой категории пока нет предметов.</div>';return}
 host.innerHTML=items.map(i=>'<div class="inventory-item '+(i.is_favorite?'inventory-favorite':'')+'">'+(i.image_url?'<img class="item-thumb" src="'+esc(i.image_url)+'" alt="">':'')+'<div style="flex:1;min-width:0"><strong>'+esc(i.item_name)+'</strong><small>'+esc(i.item_type==="case"?"Кейс":"Скин")+' · '+(i.market_price_minor?money(i.market_price_minor,"₽"):"Цена уточняется")+'</small><div class="rarity">'+esc(i.rarity)+'</div>'+skinMetaTags(i)+'</div><div class="inventory-actions"><button class="inventory-fav-btn" data-inv-fav="'+i.id+'">'+(i.is_favorite?'♥ В избранном':'♡ В избранное')+'</button><button class="buy" data-inv-upgrade="'+i.id+'" data-item-type="'+esc(i.item_type)+'">Апгрейд</button>'+(i.sell_value_minor?'<button class="sell-item" data-inv-sell="'+i.id+'">Продать '+money(i.sell_value_minor,"₽")+'</button>':'<button class="sell-item" disabled>Цена уточняется</button>')+'</div></div>').join("");
 host.querySelectorAll("[data-inv-fav]").forEach(b=>b.onclick=async()=>{try{const d=await api("/api/inventory/"+b.dataset.invFav+"/favorite",{method:"POST",body:"{}"});const item=creatorInventoryItems.find(x=>x.id===Number(b.dataset.invFav));if(item)item.is_favorite=d.is_favorite;renderEnhancedInventory()}catch(e){toast(e.message)}});
 host.querySelectorAll("[data-inv-upgrade]").forEach(b=>b.onclick=()=>upgradeInventoryItem(Number(b.dataset.invUpgrade),b.dataset.itemType));
 host.querySelectorAll("[data-inv-sell]").forEach(b=>b.onclick=()=>sellInventoryItem(Number(b.dataset.invSell),false));
}
async function loadRewards(){
 try{
  const d=await api("/api/rewards");
  $("rewardPoints").textContent=Number(d.points).toLocaleString("ru-RU");
  $("rewardStatus").textContent=d.available?"Еженедельная награда доступна":"Следующая награда "+new Date(d.next_claim_at).toLocaleString("ru-RU");
  $("claimWeeklyReward").disabled=!d.available;
  $("claimWeeklyReward").textContent=d.available?"Получить 100 баллов":"Уже получено";
 }catch(e){$("rewardStatus").textContent=e.message}
}
async function claimWeeklyReward(){
 $("claimWeeklyReward").disabled=true;
 try{const d=await api("/api/rewards/weekly/claim",{method:"POST",body:"{}"});toast(d.message);await loadRewards()}
 catch(e){toast(e.message);loadRewards()}
}

document.querySelectorAll("[data-creator-filter]").forEach(b=>b.onclick=()=>{
 creatorFilter=b.dataset.creatorFilter;
 document.querySelectorAll("[data-creator-filter]").forEach(x=>x.classList.toggle("active",x===b));
 loadCreatorCases();
});
$("creatorCaseSearch").oninput=()=>{clearTimeout(creatorSearchTimer);creatorSearchTimer=setTimeout(loadCreatorCases,180)};
$("openCreatorBuilder").onclick=()=>{$("creatorBuilder").hidden=false;$("creatorCaseDetail").hidden=true;creatorCaseItems=[];creatorBuilderRender();};
$("creatorSkinSearch").oninput=()=>{clearTimeout(creatorSearchTimer);creatorSearchTimer=setTimeout(()=>searchCreatorCatalog(true),250)};
$("createCreatorCase").onclick=createCreatorCase;
$("inventorySearch").oninput=renderEnhancedInventory;
$("inventoryFilter").onchange=renderEnhancedInventory;
$("inventorySort").onchange=renderEnhancedInventory;
$("claimWeeklyReward").onclick=claimWeeklyReward;
