/* License: GNU AGPL-3.0-or-later */
"use strict";
const $ = (s, root=document) => root.querySelector(s);
const esc = value => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let overview, currentDeck, review, sessionInfo, revealed=false, busy=false, browseVersion=0;
let telegramLoginAttempted=false, activeView="decks", routeVersion=0;
let telegram=window.Telegram?.WebApp;
const main=$("#main"), dialog=$("#dialog");
let toastTimer;
function toast(message) { const el=$("#toast"); el.textContent=message; el.hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.hidden=true,5000); }
async function api(path, data) {
  const options = data === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json", "X-Anki-Token":overview?.csrf || sessionInfo?.csrf || ""}, body:JSON.stringify(data)};
  const response = await fetch(path, options);
  let result;
  try { result=await response.json(); } catch { throw Error("Сервер вернул неожиданный ответ."); }
  if (!response.ok) { const error=Error(result.error || "Не удалось выполнить запрос."); error.status=response.status; throw error; }
  return result;
}
async function refresh() {
  overview=await api("/api/overview");
  $("#deck-count").textContent=overview.decks.length;
  $("#undo-button").disabled=!overview.undo;
  $("#undo-button").title=overview.undo ? `Отменить: ${overview.undo}` : "Нечего отменять";
}
function setNavigation(view) {
  activeView=view;
  if(telegram?.initData) { if(view==="decks" && !dialog.open) telegram.BackButton.hide(); else telegram.BackButton.show(); }
  document.querySelectorAll("[data-nav]").forEach(a=>a.classList.toggle("active",a.dataset.nav===view));
  $("#breadcrumb").innerHTML=`Библиотека <span>/</span> ${view==="browse"?"Все карточки":view==="study"?"Повторение":view==="stats"?"Статистика":"Колоды"}`;
}
function errorView(error) {
  if(error.status===401) { overview=null; renderLogin(); return; }
  main.innerHTML=`<div class="error-state"><h2>Не удалось открыть коллекцию</h2><p>${esc(error.message)}</p><button class="button" id="retry">Попробовать снова</button></div>`;
  $("#retry").onclick=route;
}
async function route() {
  if (busy) return;
  browseVersion++;
  const version=++routeVersion;
  review=null; revealed=false;
  try {
    if(!await ensureAccess()) return;
    await refresh();
    if(version!==routeVersion) return;
    if(location.hash.startsWith("#browse")) renderBrowse();
    else if(location.hash.startsWith("#stats")) await renderStats();
    else renderDecks();
  } catch(error) { errorView(error); }
}
function renderDecks() {
  setNavigation("decks");
  const due=overview.decks.filter(d=>!d.name.includes("::")).reduce((n,d)=>n+d.new+d.learning+d.review,0);
  const date=new Intl.DateTimeFormat("ru",{day:"numeric",month:"long",weekday:"long"}).format(new Date());
  main.innerHTML=`<div class="page-heading"><div><p class="eyebrow">ВАШЕ ПРОСТРАНСТВО ДЛЯ ЗНАНИЙ</p><h1>Хороший день, чтобы вспомнить.</h1><p>Один маленький шаг к тому, что останется с вами.</p></div><span class="date">${esc(date)}</span></div>
  <section class="summary" aria-label="Статистика коллекции"><div class="stat accent"><div class="stat-label">К повторению сегодня <span>↗</span></div><div class="stat-value">${due}<small>карточек в очереди</small></div></div><div class="stat"><div class="stat-label">Повторено сегодня <span>✓</span></div><div class="stat-value">${overview.reviewed}<small>ответов</small></div></div><div class="stat"><div class="stat-label">В вашей коллекции <span>▤</span></div><div class="stat-value">${overview.total}<small>карточек</small></div></div></section>
  <div class="section-top"><div class="section-title"><h2>Мои колоды</h2><span class="count-pill">${overview.decks.length}</span></div><button class="button" id="new-deck">＋ Новая колода</button></div>
  <section class="deck-table" aria-label="Колоды"><div class="table-head"><span>Название колоды</span><span>Новые</span><span>Учить</span><span>Повторить</span><span></span></div>${overview.decks.map(d=>`<div class="deck-row"><div class="deck-name"><span class="deck-icon" aria-hidden="true">▤</span><div><strong>${esc(d.name.replaceAll("::"," / "))}</strong><small>${d.total} карточек${d.name.includes("::")?" · вложенная колода":""}</small><button class="deck-settings" data-settings="${d.id}" aria-label="Настроить колоду ${esc(d.name)}">Настроить ⚙</button></div></div><span class="deck-number blue">${d.new}</span><span class="deck-number orange">${d.learning}</span><span class="deck-number green">${d.review}</span><button class="button ${d.new+d.learning+d.review?"primary":""}" data-study="${d.id}">${d.total?"Повторять →":"Добавить ＋"}</button></div>`).join("")}</section>
  ${!overview.total?`<div class="empty"><div class="empty-symbol">✧</div><h2>Начните с одной карточки</h2><p>Добавьте вопрос и ответ или импортируйте готовую колоду Anki в формате .apkg.</p><button class="button dark" id="first-card">＋ Создать первую карточку</button></div>`:""}
  <div class="tip"><span aria-hidden="true">☼</span><div><strong>Знания любят регулярность</strong><p>Отвечайте честно: «Снова», если забыли, и «Хорошо», если вспомнили. Anki сам выберет время следующего повторения.</p></div></div>`;
  main.querySelectorAll("[data-settings]").forEach(b=>b.onclick=()=>deckSettings(b.dataset.settings));
  $("#new-deck").onclick=newDeck;
  $("#first-card")?.addEventListener("click",()=>addNote());
  main.querySelectorAll("[data-study]").forEach(button=>button.onclick=()=> {
    const deck=overview.decks.find(d=>d.id===button.dataset.study);
    deck.total ? startStudy(deck) : addNote(deck.id);
  });
}
function openForm(title, content, submit) {
  $("#dialog-title").textContent=title;
  const form=$("#dialog-form");
  form.innerHTML=content+`<p class="form-error" role="alert" hidden></p><div class="form-actions"><button type="button" class="button" data-cancel>Отмена</button><button type="submit" class="button dark">Сохранить</button></div>`;
  $("[data-cancel]",form).onclick=()=>dialog.close();
  form.onsubmit=async event=> {
    event.preventDefault();
    const button=$("[type=submit]",form), error=$(".form-error",form);
    button.disabled=true; error.hidden=true;
    try { await submit(new FormData(form)); dialog.close(); await route(); }
    catch(err) { error.textContent=err.message; error.hidden=false; }
    finally { button.disabled=false; }
  };
  dialog.showModal();
  if(telegram?.initData) telegram.BackButton.show();
}
function newDeck() {
  openForm("Новая колода",`<label for="deck-name">Название</label><input id="deck-name" name="name" placeholder="Например, английский" maxlength="200" required><p class="form-help">Для вложенной колоды используйте ::, например «Языки::Английский».</p>`,async form=> {await api("/api/decks",{name:form.get("name")});toast("Колода создана");});
}
function deckOptions(selected) { return overview.decks.map(d=>`<option value="${d.id}" ${d.id===selected?"selected":""}>${esc(d.name)}</option>`).join(""); }
function addNote(selected) {
  if (!overview) return toast("Дождитесь загрузки коллекции");
  if (!overview.decks.length) return newDeck();
  openEditor(null, selected);
}
function openEditor(note, selected) {
  const fields=note?.fields || [{name:"Вопрос",value:""},{name:"Ответ",value:""}];
  const types=`<label for="note-kind">Тип карточки</label><select id="note-kind" name="kind"><option value="basic">Вопрос — ответ</option><option value="reversed">Прямая и обратная</option><option value="cloze">Пропуски (cloze)</option></select>`;
  openForm(note?"Редактировать заметку":"Добавить карточку", `${note?"":`<div class="editor-grid"><div><label for="note-deck">Колода</label><select id="note-deck" name="deck_id">${deckOptions(selected)}</select></div><div>${types}</div></div>`}
    <div class="editor-toolbar" role="toolbar" aria-label="Форматирование"><button type="button" data-wrap="b" aria-label="Жирный"><b>B</b></button><button type="button" data-wrap="i" aria-label="Курсив"><i>I</i></button><button type="button" id="insert-cloze" title="Создать пропуск из выделенного текста">[ … ]</button><button type="button" id="attach-media">＋ Медиа</button><label for="note-format" class="format-label">Формат</label><select id="note-format" name="format"><option value="text" ${note?"":"selected"}>Текст</option><option value="html" ${note?"selected":""}>HTML</option></select></div>
    <p id="editor-help" class="form-help">Одна мысль — одна карточка. Можно прикрепить изображение, аудио или видео.</p>
    ${fields.map((f,i)=>`<label for="field-${i}" data-field-label="${i}">${esc(f.name)}</label><textarea id="field-${i}" name="field-${i}" maxlength="${note?100000:20000}" ${i===0||(!note&&i===1)?"required":""}>${esc(f.value)}</textarea>`).join("")}
    <label for="note-tags">Метки через пробел</label><input id="note-tags" name="tags" maxlength="1000" value="${esc(note?.tags.join(" ") || "")}" placeholder="семестр1 матанализ">
    <input type="file" id="media-input" accept=".png,.jpg,.jpeg,.gif,.webp,.mp3,.m4a,.ogg,.wav,.mp4,.webm" hidden>`, async form=> {
      const values=fields.map((_,i)=>form.get(`field-${i}`));
      const format=form.get("format");
      if(note) {
        await api("/api/notes/edit", {id:note.id, fields:format==="html"?values:values.map(v=>esc(v).replaceAll("\n","<br>")), tags:form.get("tags")});
      } else {
        const result=await api("/api/notes", {deck_id:form.get("deck_id"),kind:form.get("kind"),front:values[0],back:values[1],tags:form.get("tags"),format});
        toast(`Создано карточек: ${result.cards}`);
      }
      if(note) toast("Заметка сохранена");
    });
  const form=$("#dialog-form"), format=$("#note-format"), areas=[...form.querySelectorAll("textarea")];
  let target=areas[0];
  areas.forEach(area=>area.addEventListener("focus",()=>target=area));
  function makeHTML() {
    if(format.value==="html") return;
    const start=target.selectionStart, end=target.selectionEnd, value=target.value;
    areas.forEach(area=>area.value=esc(area.value).replaceAll("\n","<br>"));
    format.value="html";
    target.setSelectionRange(esc(value.slice(0,start)).replaceAll("\n","<br>").length,esc(value.slice(0,end)).replaceAll("\n","<br>").length);
  }
  function insert(value) { target.setRangeText(value,target.selectionStart,target.selectionEnd,"end");target.focus(); }
  form.querySelectorAll("[data-wrap]").forEach(button=>button.onclick=()=> {
    makeHTML(); const tag=button.dataset.wrap, selected=target.value.slice(target.selectionStart,target.selectionEnd);
    insert(`<${tag}>${selected || "текст"}</${tag}>`);
  });
  $("#insert-cloze").onclick=()=> {
    if(target!==areas[0]) {toast("Выделите пропуск в первом поле карточки");return;}
    if(!note) { $("#note-kind").value="cloze"; $("#note-kind").dispatchEvent(new Event("change")); }
    const next=Math.max(0,...areas.flatMap(a=>[...a.value.matchAll(/\{\{c(\d+)::/g)].map(m=>Number(m[1]))))+1;
    insert(`{{c${next}::${target.value.slice(target.selectionStart,target.selectionEnd)||"пропуск"}}}`);
  };
  $("#note-kind")?.addEventListener("change",event=> {
    const cloze=event.target.value==="cloze";
    $("[data-field-label='0']",form).textContent=cloze?"Текст с пропусками":"Вопрос";
    $("[data-field-label='1']",form).textContent=cloze?"Пояснение (необязательно)":"Ответ";
    areas[1].required=!cloze;
    $("#editor-help").textContent=cloze?"Выделите текст и нажмите [ … ]. Разные номера создают отдельные карточки: {{c1::ответ}}.":"Обратный тип создаёт две карточки. Медиа и форматирование сохраняются в Anki.";
  });
  $("#attach-media").onclick=()=>$("#media-input").click();
  $("#media-input").onchange=async event=> {
    const file=event.target.files[0]; if(!file) return;
    if(file.size>10*1024*1024) { toast("Максимальный размер вложения — 10 МБ");event.target.value="";return; }
    const submit=$("[type=submit]",form); submit.disabled=true; $("#attach-media").disabled=true;
    try {
      const response=await fetch("/api/media",{method:"POST",headers:{"X-Anki-Token":overview.csrf,"X-Filename":encodeURIComponent(file.name)},body:file});
      const result=await response.json(); if(!response.ok) throw Error(result.error);
      if(!dialog.open) return;
      makeHTML(); insert(result.image?`<img src="${esc(result.filename)}">`:`[sound:${result.filename}]`); toast("Вложение добавлено");
    } catch(error) {toast(error.message);}
    finally {submit.disabled=false;$("#attach-media").disabled=false;event.target.value="";}
  };
}
async function startStudy(deck) {
  if (busy) return;
  busy=true;
  try {
    currentDeck=deck;
    review=await api("/api/study",{deck_id:deck.id});
    revealed=false;
    setNavigation("study"); renderStudy();
  } catch(error) { toast(error.message); }
  finally {busy=false;}
}
function cardDocument(content, media) {
  // Card templates are isolated from the application, including imported templates.
  const base=location.origin+"/media/";
  const players=media.map(file=> {
    const video=/\.(mp4|webm|ogv|mov)$/i.test(file), tag=video?"video":"audio";
    return `<${tag} controls preload="none" src="${esc(base+encodeURIComponent(file))}"></${tag}>`;
  }).join("");
  return `<!doctype html><html><head><meta charset="utf-8"><base href="${esc(base)}"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src ${esc(location.origin)} data:; media-src ${esc(location.origin)}; script-src 'none'; form-action 'none'"><style>html{height:100%}body{min-height:100%;margin:0;padding:34px;box-sizing:border-box;display:flex;align-items:center;justify-content:center;flex-direction:column;color:#253a35;background:white;font-family:system-ui,sans-serif;font-size:24px;line-height:1.6;text-align:center;overflow-wrap:anywhere}.card{font-family:system-ui,sans-serif}img{max-width:100%;height:auto}audio{max-width:100%}hr{width:80%;border:0;border-top:1px solid #e4e9dd;margin:25px auto}a{color:#567b40}</style></head><body class="card">${content}${players}</body></html>`;
}
function renderStudy() {
  const card=review.card;
  main.innerHTML=`<div class="review-top"><button class="button" id="leave-study">← К колодам</button><button class="button quiet" id="undo-review" ${review.can_undo?"":"disabled"}>↶ Отменить ответ</button><div class="review-counters"><span class="blue">${review.counts.new} новых</span><span class="orange">${review.counts.learning} учить</span><span class="green">${review.counts.review} повторить</span></div></div>
  <div class="page-heading"><div><p class="eyebrow">ВРЕМЯ ВСПОМНИТЬ</p><h1>${esc(currentDeck.name.replaceAll("::"," / "))}</h1></div></div>
  ${card?`<section class="review-card"><div class="card-meta">${revealed?"ОТВЕТ":"ВОПРОС"}<details class="card-menu"><summary aria-label="Действия с карточкой">•••</summary><button type="button" data-card-action="bury">Отложить до завтра</button><button type="button" data-card-action="suspend">Приостановить</button></details></div><iframe class="card-frame" id="card-frame" sandbox="allow-same-origin" title="${revealed?"Ответ карточки":"Вопрос карточки"}"></iframe></section><div class="review-actions">${revealed?`<div class="ratings">${["Снова","Трудно","Хорошо","Легко"].map((label,i)=>`<button class="rating" data-rating="${i+1}">${label}<span>${esc(card.intervals[i]||"")}</span></button>`).join("")}</div><p class="shortcut-hint">Оцените, насколько легко вы вспомнили · клавиши 1–4</p>`:`<button class="button dark large" id="reveal">Показать ответ</button><p class="shortcut-hint">Сначала попробуйте вспомнить · пробел для ответа</p>`}</div>`:`<section class="review-card empty"><div class="empty-symbol">✧</div><h2>На сейчас всё!</h2><p>В доступной очереди больше нет карточек. Anki покажет следующие, когда придёт время повторения.</p><button class="button dark" id="check-again">Проверить очередь</button></section>`}`;
  $("#leave-study").onclick=route;
  $("#undo-review").onclick=async()=> {
    if(busy) return; busy=true;
    try {await api("/api/undo",{review_only:true});busy=false;await startStudy(currentDeck);await refresh();}
    catch(error) {toast(error.message);} finally {busy=false;}
  };
  main.querySelectorAll("[data-card-action]").forEach(b=>b.onclick=async()=> {
    if(busy) return; busy=true;
    try {await api("/api/cards/action",{card_id:card.id,action:b.dataset.cardAction});busy=false;await startStudy(currentDeck);await refresh();}
    catch(error) {toast(error.message);} finally {busy=false;}
  });
  $("#reveal")?.addEventListener("click",()=>{revealed=true;renderStudy();});
  if(card) $("#card-frame").srcdoc=cardDocument(revealed?card.answer:card.question, revealed?card.answer_media:card.question_media);
  $("#check-again")?.addEventListener("click",()=>startStudy(currentDeck));
  main.querySelectorAll("[data-rating]").forEach(b=>b.onclick=()=>answer(Number(b.dataset.rating)));
}
async function answer(rating) {
  if (busy || !review?.card || !revealed) return;
  busy=true;
  main.querySelectorAll("[data-rating]").forEach(b=>b.disabled=true);
  try { review=await api("/api/answer",{token:review.card.token,rating}); revealed=false; renderStudy(); await refresh(); }
  catch(error) { toast(error.message); main.querySelectorAll("[data-rating]").forEach(b=>b.disabled=false); }
  finally { busy=false; }
}
function plain(html) { const doc=new DOMParser().parseFromString(html,"text/html"); return doc.body.textContent || "[Медиа]"; }
function renderBrowse() {
  setNavigation("browse");
  main.innerHTML=`<div class="page-heading"><div><p class="eyebrow">ВСЁ, ЧТО ВЫ УЧИТЕ</p><h1>Все карточки</h1><p>Найдите нужное и освежите детали.</p></div></div><form class="search-bar" id="search-form"><input name="search" aria-label="Поиск карточек" placeholder="Поиск по коллекции…"><select name="deck_id" aria-label="Колода"><option value="">Все колоды</option>${deckOptions()}</select><button class="button dark">Найти</button></form><div id="notes-results" aria-live="polite"></div>`;
  $("#search-form").onsubmit=event=>{event.preventDefault();loadNotes();};
  $("#search-form select").onchange=loadNotes;
  loadNotes();
}
async function loadNotes() {
  const container=$("#notes-results");
  const version=++browseVersion;
  container.innerHTML=`<div class="loading">Ищем карточки…</div>`;
  try {
    const query=new URLSearchParams(new FormData($("#search-form")));
    const result=await api("/api/notes?"+query);
    if(version!==browseVersion || !container.isConnected) return;
    container.innerHTML=`<p class="notes-meta">Найдено записей: ${result.total}${result.total>200?" · показаны первые 200, уточните поиск":""}</p><div class="deck-table">${result.notes.length?result.notes.map(note=>`<article class="note-item">${note.fields.slice(0,2).map(f=>`<div class="note-field"><small>${esc(f.name)}</small>${esc(plain(f.value))}</div>`).join("")}<div class="note-controls"><span class="tags">${note.tags.map(esc).join(" · ")}</span><button class="button quiet" data-edit="${note.id}" aria-label="Редактировать карточку">Изменить ↗</button><button class="button quiet" data-toggle="${note.id}">${note.cards.every(c=>c.suspended)?"Продолжить":"Приостановить"}</button><button class="button quiet danger" data-remove="${note.id}">Удалить</button></div></article>`).join(""):`<div class="empty"><h2>Здесь пока пусто</h2><p>Измените запрос или добавьте свою первую карточку.</p></div>`}</div>`;
    container.querySelectorAll("[data-edit]").forEach(button=>button.onclick=()=>editNote(result.notes.find(n=>n.id===button.dataset.edit)));
    container.querySelectorAll("[data-toggle]").forEach(button=>button.onclick=async()=> {
      button.disabled=true;
      const note=result.notes.find(n=>n.id===button.dataset.toggle), action=note.cards.every(c=>c.suspended)?"resume":"suspend";
      try {await api("/api/cards/action",{note_id:note.id,action});await refresh();await loadNotes();}
      catch(error) {toast(error.message);button.disabled=false;}
    });
    container.querySelectorAll("[data-remove]").forEach(button=>button.onclick=()=> {
      const note=result.notes.find(n=>n.id===button.dataset.remove);
      openForm("Удалить заметку?",`<p>Будет удалена заметка и все её карточки (${note.cards.length}). Действие можно отменить кнопкой ↶.</p><p class="form-help">${esc(plain(note.fields[0].value).slice(0,200))}</p>`,async()=>{await api("/api/notes/remove",{id:note.id});toast("Заметка удалена. Доступна отмена ↶.");});
      $("#dialog-form [type=submit]").textContent="Удалить";
    });
  } catch(error) { if(version===browseVersion && container.isConnected) container.innerHTML=`<div class="error-state">${esc(error.message)}</div>`; }
}
function editNote(note) { openEditor(note); }

async function deckSettings(id) {
  try {
    const settings=await api("/api/decks/settings?id="+id);
    openForm("Настройки колоды",`<p>${esc(settings.name)}</p><div class="editor-grid"><div><label for="daily-new">Новых в день</label><input id="daily-new" name="new" type="number" value="${settings.new}" min="0" max="9999" required></div><div><label for="daily-review">Повторений в день</label><input id="daily-review" name="review" type="number" value="${settings.review}" min="0" max="9999" required></div></div><p class="form-help">Лимиты применяются к этой колоде. Также действуют ограничения родительской колоды. Текущий алгоритм: ${settings.fsrs?"FSRS":"SM-2"}.</p><label for="rename-deck">Название</label><input id="rename-deck" name="name" value="${esc(settings.name)}" maxlength="200" required>`,async form=> {
      await api("/api/decks/settings",{id,new:form.get("new"),review:form.get("review")});
      if(form.get("name")!==settings.name) await api("/api/decks/rename",{id,name:form.get("name")});
      toast("Настройки сохранены");
    });
  } catch(error) {toast(error.message);}
}
async function renderStats() {
  setNavigation("stats");
  const version=routeVersion;
  const data=await api("/api/statistics"), max=Math.max(1,...data.days.map(d=>d.reviews));
  if(version!==routeVersion) return;
  main.innerHTML=`<div class="page-heading"><div><p class="eyebrow">КАК ИДЁТ УЧЁБА</p><h1>Ваш прогресс</h1><p>Последние 30 дней по истории повторений Anki.</p></div></div><section class="summary"><div class="stat accent"><div class="stat-label">Ответов за 30 дней</div><div class="stat-value">${data.reviews}</div></div><div class="stat"><div class="stat-label">Время повторений</div><div class="stat-value">${data.minutes}<small>минут</small></div></div><div class="stat"><div class="stat-label">Ответов «Снова»</div><div class="stat-value">${data.again}</div></div></section><section class="stats-panel"><h2>Ритм повторений</h2><div class="activity-chart" role="img" aria-label="Ответы за 30 дней: ${data.reviews}">${data.days.map(d=>`<div class="activity-day" title="${d.date}: ${d.reviews} ответов"><span style="height:${Math.max(2,d.reviews/max*100)}%"></span><small>${d.date}</small><b>${d.reviews}</b></div>`).join("")}</div><p class="form-help">Каждый столбец — один день Anki. Ответы импортированных карточек также учитываются.</p></section><section class="stats-panel"><h2>Состав коллекции</h2><div class="distribution">${[["0","Новые"],["1","В изучении"],["2","На повторении"],["3","Переучивание"],["-1","Приостановлены"]].map(([key,label])=>`<div><span>${label}</span><strong>${data.distribution[key]||0}</strong></div>`).join("")}</div></section>`;
}
async function ensureAccess() {
  sessionInfo=await api("/api/session");
  if(!sessionInfo.authenticated && sessionInfo.telegram && telegram?.initData && !telegramLoginAttempted) {
    telegramLoginAttempted=true;
    try {await login({method:"telegram",init_data:telegram.initData});sessionInfo=await api("/api/session");}
    catch(error) {renderLogin(error.message);return false;}
  }
  document.body.classList.toggle("app-locked",!sessionInfo.authenticated);
  $("#logout-button").hidden=!(sessionInfo.password||sessionInfo.telegram);
  $(".workspace small").textContent=sessionInfo.remote?"Личное пространство":"На этом компьютере";
  $(".storage-label").innerHTML=`<i></i> ${sessionInfo.remote?"Сохраняется на вашем сервере":"Сохраняется на компьютере"}`;
  if(!sessionInfo.authenticated) {renderLogin();return false;}
  return true;
}
async function login(data) {
  const response=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json","X-Anki-Client":"web"},body:JSON.stringify(data)});
  const result=await response.json(); if(!response.ok) throw Error(result.error);
}
function renderLogin(message="") {
  overview=null; review=null; document.body.classList.add("app-locked");
  main.innerHTML=`<section class="login-panel"><div class="empty-symbol">✦</div><h1>Ваша коллекция Anki</h1><p class="muted">Войдите, чтобы продолжить учиться.</p>${sessionInfo?.password?`<form id="login-form"><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" maxlength="1024" required><button class="button dark large" type="submit">Открыть коллекцию</button></form>`:""}${sessionInfo?.telegram?`<p class="form-help">Откройте Mini App через своего Telegram-бота. Доступ разрешён только владельцу коллекции.</p>`:""}<p class="form-error" id="login-error" role="alert">${esc(message)}</p></section>`;
  $("#login-form")?.addEventListener("submit",async event=> {
    event.preventDefault(); const button=$("#login-form button");button.disabled=true;
    try {await login({method:"password",password:$("#password").value});await route();}
    catch(error) {$("#login-error").textContent=error.message;}
    finally {button.disabled=false;}
  });
}
$("#add-button").onclick=()=>addNote();
$("#close-dialog").onclick=()=>dialog.close();
dialog.addEventListener("close",()=>{if(telegram?.initData && activeView==="decks") telegram.BackButton.hide();});
$("#import-button").onclick=()=> { if(!busy && overview) $("#import-file").click(); };
$("#import-file").onchange=async event=> {
  const file=event.target.files[0];
  if(!file) return;
  if(!file.name.toLowerCase().endsWith(".apkg")) {toast("Выберите файл .apkg");event.target.value="";return;}
  if(file.size>100*1024*1024) {toast("Максимальный размер файла — 100 МБ");event.target.value="";return;}
  busy=true; $("#import-button").disabled=true; toast("Импортируем колоду…");
  try {
    const response=await fetch("/api/import",{method:"POST",headers:{"X-Anki-Token":overview?.csrf || sessionInfo?.csrf || "","Content-Type":"application/octet-stream"},body:file});
    const result=await response.json();
    if(!response.ok) throw Error(result.error);
    toast(`Импорт завершён. Добавлено: ${result.added}, обновлено: ${result.updated}`);
    busy=false; await route();
  } catch(error) {toast(error.message);}
  finally {busy=false;$("#import-button").disabled=false;event.target.value="";}
};
window.addEventListener("hashchange",route);
document.addEventListener("keydown",event=> {
  if(dialog.open || busy || ["INPUT","TEXTAREA","SELECT","BUTTON","A"].includes(document.activeElement?.tagName)) return;
  if(!review?.card) return;
  if(event.code==="Space" && !revealed) {event.preventDefault();revealed=true;renderStudy();}
  else if(revealed && /^[1-4]$/.test(event.key)) {event.preventDefault();answer(Number(event.key));}
});
$("#undo-button").onclick=async()=> {
  if(busy) return; busy=true;
  try {await api("/api/undo",{});busy=false;await route();toast("Последнее действие отменено");}
  catch(error) {toast(error.message);} finally {busy=false;}
};
$("#logout-button").onclick=async()=> {
  try {await api("/api/logout",{});telegramLoginAttempted=true;await route();}
  catch(error) {toast(error.message);}
};
function applyTheme(theme) { document.documentElement.dataset.theme=theme; }
$("#theme-toggle").onclick=()=> {
  const theme=document.documentElement.dataset.theme==="dark"?"light":"dark";
  applyTheme(theme);try {localStorage.setItem("anki-theme",theme);} catch {}
};
try {applyTheme(localStorage.getItem("anki-theme") || (telegram?.initData?telegram.colorScheme:"light"));} catch {applyTheme("light");}
function initializeTelegram() {
  if(!telegram?.initData) return;
  telegram.ready();telegram.expand();document.body.classList.add("telegram-app");
  function safeArea() {
    const safe=telegram.contentSafeAreaInset;
    document.documentElement.style.setProperty("--tg-safe-top",`${Math.max(safe?.top||0,telegram.safeAreaInset?.top||0)}px`);
    document.documentElement.style.setProperty("--tg-safe-bottom",`${Math.max(safe?.bottom||0,telegram.safeAreaInset?.bottom||0)}px`);
  }
  safeArea();telegram.onEvent("safeAreaChanged",safeArea);telegram.onEvent("contentSafeAreaChanged",safeArea);
  telegram.onEvent("themeChanged",()=>applyTheme(telegram.colorScheme));
  telegram.BackButton.onClick(()=>{if(dialog.open) dialog.close();else {if(location.hash==="#decks") route();else location.hash="#decks";}});
  telegram.BackButton.show();
}
async function boot() {
  try {
    const config=await api("/api/session");
    if(config.telegram && !telegram) {
      try {
        await new Promise((resolve,reject)=> {
          const script=document.createElement("script");
          const timer=setTimeout(()=>reject(Error("Не удалось загрузить Telegram SDK")),10000);
          script.src="https://telegram.org/js/telegram-web-app.js?64";
          script.onload=()=>{clearTimeout(timer);resolve();};
          script.onerror=()=>{clearTimeout(timer);reject(Error("Telegram SDK недоступен"));};
          document.head.append(script);
        });
        telegram=window.Telegram?.WebApp;
      } catch(error) { toast(error.message); }
    }
    initializeTelegram();
    await route();
  } catch(error) {errorView(error);}
}
boot();
