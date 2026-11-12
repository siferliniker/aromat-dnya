"use strict";

(() => {
  const webapp = window.Telegram?.WebApp;
  const initData = webapp?.initData || "";
  const main = document.querySelector("#main");
  const dialog = document.querySelector("#details-dialog");
  const catalogDialog = document.querySelector("#catalog-dialog");
  const filtersDialog = document.querySelector("#filters-dialog");
  const undoButton = document.querySelector("#undo-reaction");
  const searchButton = document.querySelector("#open-catalog");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const motionAnimations = new Set();
  const closingDialogs = new WeakMap();
  let viewVersion = 0, cardExit = null;
  let catalogItems = [], catalogCount = 0, catalogSequence = 0,
    catalogTimer, catalogFacetsReady = false, catalogBusy = false, catalogPage = 0, catalogLimit = 5;
  const libraryPages = {};
  let detailView = null, viewportTimer;
  let state = null,
    activeTab = "discover",
    pending = false,
    libraries = {},
    toastTimer;

  const labels = {
    want: "Хочу попробовать",
    known: "Уже знаком",
    not_for_me: "Не моё",
    bought: "В коллекции",
  };
  const { families, concentrations, notes } = window.AROMATIA_RU;
  const icons = {
    want: '<path d="M20.8 4.7a5.5 5.5 0 0 0-7.8 0l-1 1-1-1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.5a5.5 5.5 0 0 0 0-7.8Z"/>',
    known: '<path d="m5 12 4 4L19 6"/>',
    not_for_me: '<path d="m7 7 10 10M17 7 7 17"/>',
    bought: '<path d="M7 8h10v13H7zM9 3h6v5H9zM7 12h10"/>',
  };
  const seasonNames = {
    spring: "Весна",
    summer: "Лето",
    autumn: "Осень",
    winter: "Зима",
  };
  const seasonIcons = {
    spring:
      '<path d="M12 21V10M12 15C5 15 3 11 3 7c6 0 9 4 9 8ZM12 12c6 0 9-4 9-8-6 0-9 4-9 8Z"/>',
    summer:
      '<circle cx="12" cy="12" r="4"/><path d="M12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M20 4l-2 2M6 18l-2 2"/>',
    autumn:
      '<path d="M5 20 18 7M5 18C0 3 12 4 21 3c0 10-2 20-16 15ZM11 14V8m0 6h7"/>',
    winter:
      '<path d="M12 2v20M3.3 7l17.4 10M3.3 17 20.7 7M9 4l3 3 3-3M9 20l3-3 3 3M4 10l4-1-1-4M20 14l-4 1 1 4M7 19l1-4-4-1M17 5l-1 4 4 1"/>',
  };
  const svg = (body) =>
    `<svg viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const translated = (value, dictionary) =>
    dictionary[value] || (/[^\x00-\x7F]/.test(value) ? value : "Не указано");
  const safeList = (value) =>
    Array.isArray(value) ? value.filter((v) => typeof v === "string" && v) : [];

  function playMotion(element, frames, options) {
    if (!element?.animate || reducedMotion.matches) return null;
    let animation;
    try {
      animation = element.animate(frames, options);
      if (!animation.finished?.then) { animation.cancel(); return null; }
    } catch (_) { return null; }
    motionAnimations.add(animation);
    const finished = () => motionAnimations.delete(animation);
    animation.finished.then(finished, finished);
    return animation;
  }

  function resetViewMotion() {
    ++viewVersion;
    cardExit?.cancel();
    cardExit = null;
  }

  async function replaceCard(direction, render) {
    const version = viewVersion;
    const card = main.querySelector(".perfume");
    const current = card ? getComputedStyle(card) : null;
    const animation = playMotion(card, [
      { opacity: current?.opacity || 1, transform: current?.transform || "none" },
      { opacity: 0, transform: `translateX(${direction === "back" ? 24 : -24}px) scale(.985)` },
    ], { duration: 150, easing: "cubic-bezier(.4,0,1,1)", fill: "forwards" });
    cardExit = animation;
    if (animation) await animation.finished.catch(() => {});
    if (version !== viewVersion) { animation?.cancel(); return false; }
    main.dataset.motion = direction;
    render();
    animation?.cancel();
    return true;
  }

  function closeDialog(target) {
    if (!target.open) return Promise.resolve();
    if (closingDialogs.has(target)) return closingDialogs.get(target);
    target.dataset.closing = "true";
    const animation = playMotion(target, [
      { opacity: 1, transform: "translateY(0) scale(1)" },
      { opacity: 0, transform: "translateY(18px) scale(.985)" },
    ], { duration: 160, easing: "cubic-bezier(.4,0,1,1)", fill: "forwards" });
    const task = (animation ? animation.finished.catch(() => {}) : Promise.resolve()).then(() => {
      if (target.open) target.close();
      animation?.cancel();
      delete target.dataset.closing;
      closingDialogs.delete(target);
    });
    closingDialogs.set(target, task);
    return task;
  }

  const respectMotionPreference = () => {
    if (reducedMotion.matches) for (const animation of motionAnimations) animation.cancel();
  };
  if (reducedMotion.addEventListener) reducedMotion.addEventListener("change", respectMotionPreference);
  else reducedMotion.addListener?.(respectMotionPreference);

  function theme() {
    const dark = webapp?.colorScheme === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const background = dark ? "#1d2722" : "#f5f2ea";
    document.querySelector('meta[name="theme-color"]').content = background;
    try {
      webapp?.setHeaderColor(background);
      webapp?.setBackgroundColor(background);
      if (webapp?.isVersionAtLeast("7.10"))
        webapp.setBottomBarColor(background);
    } catch (_) {
      /* Older Telegram clients retain their own chrome. */
    }
  }

  function notify(text) {
    const toast = document.querySelector("#toast");
    toast.textContent = text;
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 2400);
  }

  async function api(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 16000);
    try {
      const response = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: {
          Authorization: `tma ${initData}`,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
        credentials: "omit",
        cache: "no-store",
        signal: controller.signal,
      });
      if (response.status === 401 || response.status === 403) {
        const error = new Error("auth");
        error.auth = true;
        throw error;
      }
      if (!response.ok) throw new Error("unavailable");
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  function updateCounts() {
    undoButton.hidden = !state?.can_undo;
    undoButton.disabled = pending || !state?.can_undo;
    searchButton.disabled = !state;
    for (const key of ["wishlist", "known"])
      document.querySelector(`#${key}-count`).textContent =
        state?.counts?.[key] || "";
  }

  function locked(expired = false) {
    resetViewMotion();
    state = null;
    updateCounts();
    if (dialog.open) dialog.close();
    if (catalogDialog.open) catalogDialog.close();
    if (filtersDialog.open) filtersDialog.close();
    document.querySelector(".bottom-nav").hidden = true;
    document.querySelector("#header-note").textContent =
      "Ваше личное пространство";
    main.innerHTML = `<section class="message-panel"><img class="message-mark" src="/static/mark.svg" alt=""><h1>${expired ? "Откройте Ароматию заново" : "Встречаемся в Telegram"}</h1><p>${expired ? "Сессия завершилась или доступ не подтверждён. Закройте приложение и снова откройте его через меню вашего бота в Telegram." : "Откройте Ароматию из меню вашего бота в Telegram. Там вас ждут подбор ароматов, избранное и личная коллекция."}</p></section>`;
  }

  function errorPanel(retry) {
    resetViewMotion();
    main.innerHTML =
      '<section class="message-panel"><img class="message-mark" src="/static/mark.svg" alt=""><h1>Небольшая пауза</h1><p>Не удалось загрузить ароматы. Проверьте соединение и попробуйте ещё раз.</p><button class="plain-button" id="retry">Попробовать снова</button></section>';
    document.querySelector("#retry").addEventListener("click", retry);
  }

  function safeUrl(value, image = false) {
    if (typeof value !== "string" || !value) return "";
    try {
      const url = new URL(value, window.location.origin);
      if (url.username || url.password) return "";
      if (image && url.origin === window.location.origin && url.pathname.startsWith("/static/"))
        return url.href;
      if (image ? url.protocol !== "https:" : !["https:", "http:"].includes(url.protocol)) return "";
      return url.href;
    } catch (_) {
      return "";
    }
  }

  function imageMarkup(card) {
    const image = card.image;
    const url = safeUrl(image?.url, true);
    if (!url) return "";
    return `<figure class="perfume-visual"><img class="perfume-image" src="${esc(url)}" alt="${esc(image.alt || `${card.house} ${card.name}`)}" decoding="async" referrerpolicy="no-referrer"></figure>`;
  }

  const noteIllustrations = new Map([
    [0, ["Bergamot", "Calabrian Bergamot"]],
    [1, ["Lemon", "Orange", "Mandarin", "Citron", "Grapefruit", "Lime", "Citruses", "Citrus"]],
    [2, ["Rose", "Turkish Rose", "Bulgarian Rose", "Damask Rose"]],
    [3, ["Jasmine", "Jasmine Sambac", "White Flowers"]],
    [4, ["Lavender", "Lavandin"]],
    [5, ["Vanilla", "Bourbon Vanilla", "Madagascar Vanilla"]],
    [6, ["Cardamom", "Cardamome"]],
    [7, ["Black Pepper", "Pink Pepper", "Pepper"]],
    [8, ["Cedar", "Cedarwood", "Cedar Wood", "Virginia Cedar"]],
    [9, ["Sandalwood", "Australian Sandalwood"]],
    [10, ["Patchouli", "Indonesian Patchouli Leaf"]],
    [11, ["Vetiver", "Haitian Vetiver"]],
    [12, ["Amber", "Labdanum", "Ciste", "Benzoin", "Resins"]],
    [13, ["Leather", "Suede"]],
    [14, ["Musk", "White Musk"]],
    [15, ["Oud", "Oud Wood", "Agarwood", "Agarwood (Oud)"]],
    [16, ["Apple", "Green Apple", "Pear"]],
    [17, ["Red Berries", "Blackcurrant", "Black Currant", "Raspberry", "Wild Berries"]],
    [18, ["Iris", "Orris", "Orris Root"]],
    [19, ["Neroli", "Neroli Oil", "Orange Blossom", "Orange Flower"]],
    [20, ["Oakmoss", "Moss"]],
    [21, ["Incense", "Frankincense", "Olibanum"]],
    [22, ["Tonka", "Tonka Bean", "Tonka Beans"]],
    [23, ["Saffron"]],
    [24, ["Nutmeg"]],
    [25, ["Elemi"]],
    [26, ["Pimento", "Allspice"]],
    [27, ["Dragon fruit", "Dragonfruit", "Pitahaya"]],
    [28, ["Cognac"]],
    [29, ["Toffee"]],
    [30, ["Magnolia"]],
    [31, ["Clary Sage"]],
    [32, ["Ginger", "Red Ginger"]],
    [33, ["Cinnamon", "Cinnamon Bark"]],
    [34, ["Clove", "Cloves"]],
    [35, ["Mint", "Peppermint"]],
    [36, ["Basil"]],
    [37, ["Geranium"]],
    [38, ["Orange", "Mandarin", "Blood Orange", "Mandarin Orange"]],
    [39, ["Mango"]],
    [40, ["Peach"]],
    [41, ["Coconut"]],
    [42, ["Honey"]],
    [43, ["Coffee"]],
    [44, ["Tobacco", "Tobacco Leaf"]],
    [45, ["Fig"]],
    [46, ["Ylang Ylang", "Ylang-Ylang"]],
    [47, ["Akigalawood", "Ambrofix", "Mahonial", "Magonial", "Cashmeran", "Ambroxan", "Ambrox", "Hedione", "Ambermax", "Amberketal", "Ambrocenide", "Ambrettolide", "Muscenone", "Muscone", "Cetalox", "Georgywood", "Iso E Super", "Javanol", "Kephalis", "Norlimbanol", "Dihydromyrcenol"]],
  ].flatMap(([index, names]) => names.map(name => [name.toLocaleLowerCase("en"), index])));

  function noteMarkup(note) {
    const name = translated(note, notes);
    const index = noteIllustrations.get(note.toLocaleLowerCase("en"));
    const illustration = index === undefined
      ? `<span class="note-art note-art--abstract" aria-hidden="true"><svg viewBox="0 0 100 100"><path d="M24 66C9 44 26 15 51 20c25-13 47 12 31 35 5 27-31 39-44 20-8 1-12-3-14-9Z"/><path d="M31 69c7-15 13-29 35-36M39 54l-7-13m20 5 13 3M46 64l15 5"/><circle cx="66" cy="33" r="3"/></svg></span>`
      : `<span class="note-art${index >= 24 ? " note-art--second" : ""} note-art--${index}" aria-hidden="true"></span>`;
    const label = index === undefined ? `Абстрактная иллюстрация ноты «${name}»`
      : index === 47 ? `Абстрактный образ синтетической ноты «${name}»; не фото ингредиента`
      : `Художественная иллюстрация ингредиента или группы: ${name}`;
    return `<figure class="note-orbit" tabindex="0" title="${esc(label)}">${illustration}<figcaption>${esc(name)}</figcaption></figure>`;
  }

  function handleImageError(image) {
    if (!(image instanceof HTMLImageElement) || !image.classList.contains("perfume-image") || !image.isConnected)
      return;
    const article = image.closest(".perfume");
    image.closest(".perfume-visual")?.remove();
    article?.classList.remove("has-visual");
    const details = article?.querySelector(".card-details");
    if (details && !details.querySelector(".image-status")) {
      const status = document.createElement("p");
      status.className = "image-status";
      status.textContent = "Фото недоступно.";
      details.append(status);
    }
  }

  function checkImages(container) {
    container.querySelectorAll(".perfume-image").forEach((image) => {
      if (image.complete && image.naturalWidth === 0) handleImageError(image);
    });
  }

  const arrows = { prev: '<path d="m14 6-6 6 6 6"/>', next: '<path d="m10 6 6 6-6 6"/>' };
  function pagerMarkup(page, count, scope) {
    return `<button class="icon-button" data-${scope}-page="${page - 1}" aria-label="Предыдущая страница" ${page <= 0 ? "disabled" : ""}>${svg(arrows.prev)}</button><span class="page-number">${page + 1} / ${Math.max(1, count)}</span><button class="icon-button" data-${scope}-page="${page + 1}" aria-label="Следующая страница" ${page >= count - 1 ? "disabled" : ""}>${svg(arrows.next)}</button>`;
  }
  function cardMarkup(card) {
    const levels = [safeList(card.notes?.top), safeList(card.notes?.heart), safeList(card.notes?.base)];
    const selected = [];
    for (let index = 0; index < Math.max(...levels.map(level => level.length), 0) && selected.length < 6; index++) {
      for (const level of levels) if (level[index] && !selected.includes(level[index]) && selected.length < 6) selected.push(level[index]);
    }
    const visual = imageMarkup(card);
    const meta = [card.family ? translated(card.family, families) : "", card.concentration ? translated(card.concentration, concentrations) : ""].filter(Boolean).join(" · ");
    const performance = card.performance;
    const metrics = performance ? [performance.longevity ? `<span><small>Стойкость</small>${esc(performance.longevity)}</span>` : "", performance.sillage ? `<span><small>${performance.diffusion_type === "projection" ? "Проекция" : "Шлейф"}</small>${esc(performance.sillage)}</span>` : ""].join("") : "";
    return `<article class="perfume compact-card${visual ? " has-visual" : ""}"><header class="perfume-hero"><p class="house">${esc(card.house)}</p><h1 class="perfume-name" title="${esc(card.name)}">${esc(card.name)}</h1><p class="compact-meta">${esc(meta)}</p></header>${visual}<section class="compact-notes note-constellation" aria-label="Ноты аромата">${selected.map(noteMarkup).join("")}</section>${metrics ? `<div class="compact-metrics">${metrics}</div>` : ""}</article>`;
  }
  function renderDiscover() {
    resetViewMotion();
    if (!state?.current) {
      main.innerHTML = `<section class="message-panel"><img class="message-mark" src="/static/mark.svg" alt=""><h1>Вы познакомились со всеми ароматами</h1><p>Загляните в избранное: там сохранились ароматы, которые хочется попробовать.</p><button class="plain-button" id="open-wishlist">Открыть избранное</button></section>`;
      document
        .querySelector("#open-wishlist")
        .addEventListener("click", () => switchTab("wishlist"));
      return;
    }
    main.innerHTML = `${cardMarkup(state.current)}<section class="reaction-area" aria-label="Ваша реакция"><h2 class="reaction-caption">Как вам этот аромат?</h2><div class="reaction-buttons">${["want", "bought", "known", "not_for_me"].map((reaction) => `<button type="button" class="reaction" data-reaction="${reaction}" aria-label="${labels[reaction]}" title="${labels[reaction]}">${svg(icons[reaction])}</button>`).join("")}</div><p class="reaction-help">Выберите реакцию — и откроется следующий аромат</p><div id="reaction-error" class="error-inline" role="alert"></div></section>`;
    checkImages(main);
    main
      .querySelectorAll("[data-reaction]")
      .forEach((button) =>
        button.addEventListener("click", () => react(button.dataset.reaction)),
      );
  }

  async function react(reaction) {
    if (pending || !state?.current) return;
    pending = true;
    updateCounts();
    main.classList.add("saving");
    main.querySelector(`[data-reaction="${reaction}"]`)?.classList.add("is-saving");
    main
      .querySelectorAll("[data-reaction]")
      .forEach((button) => (button.disabled = true));
    const request = {
      perfume_id: state.current.id,
      revision: state.revision,
      reaction,
    };
    try {
      state = await api("/api/react", request);
      libraries = {};
      updateCounts();
      if (activeTab === "discover") {
        if (await replaceCard("forward", renderDiscover)) window.scrollTo(0, 0);
      }
      try {
        webapp?.HapticFeedback?.impactOccurred("light");
      } catch (_) {}
      notify(state.applied === false ? "Карточка уже обновлена" : "Сохранено");
    } catch (error) {
      if (error.auth) locked(true);
      else {
        const target = document.querySelector("#reaction-error");
        if (target) {
          target.textContent =
            "Не удалось сохранить. Повторите нажатие — реакция не потеряется.";
        } else notify("Не удалось сохранить. Попробуйте ещё раз.");
      }
    } finally {
      pending = false;
      updateCounts();
      main.classList.remove("saving");
      main
        .querySelectorAll("[data-reaction]")
        .forEach((button) => { button.disabled = false; button.classList.remove("is-saving"); });
    }
  }

  async function renderLibrary(kind) {
    resetViewMotion();
    const wishlist = kind === "wishlist";
    const heading = `<div class="library-head"><h1 class="library-title">${wishlist ? "Избранное" : "Коллекция"}</h1></div>`;
    main.innerHTML = `${heading}<section class="message-panel"><span class="loading-leaf"></span><p>Собираем ваши ароматы…</p></section>`;
    try {
      if (!libraries[kind])
        libraries[kind] = await api(`/api/library?kind=${kind}`);
      if (activeTab !== kind) return;
      paintLibrary(kind);
    } catch (error) {
      if (activeTab === kind)
        error.auth ? locked(true) : errorPanel(() => renderLibrary(kind));
    }
  }

  function paintLibrary(kind) {
    const items = libraries[kind]?.items || [];
    const limit = Math.max(1, Math.min(7, Math.floor((main.clientHeight - 100) / 78)));
    const count = Math.max(1, Math.ceil(items.length / limit));
    const page = libraryPages[kind] = Math.max(0, Math.min(libraryPages[kind] || 0, count - 1));
    const visible = items.slice(page * limit, (page + 1) * limit);
    main.innerHTML = `<header class="library-head"><h1 class="library-title">${kind === "wishlist" ? "Избранное" : "Коллекция"}</h1><span class="library-count">${items.length}</span></header>` +
      (items.length ? `<div class="library-list">${visible.map((card, i) => `<button class="library-row" data-detail="${i}"><span><span class="row-house">${esc(card.house)}</span><span class="row-name">${esc(card.name)}</span><span class="row-detail">${esc(labels[card.reaction] || "")}</span></span><span class="row-arrow" aria-hidden="true">↗</span></button>`).join("")}</div><div class="page-controls">${pagerMarkup(page, count, "library")}</div>` : '<section class="message-panel"><img class="message-mark" src="/static/mark.svg" alt=""><p>Здесь появятся отмеченные ароматы</p></section>');
    main.querySelectorAll("[data-detail]").forEach(button => button.addEventListener("click", () => showDetails(visible[Number(button.dataset.detail)])));
    main.querySelectorAll("[data-library-page]").forEach(button => button.addEventListener("click", () => { libraryPages[kind] = Number(button.dataset.libraryPage); paintLibrary(kind); }));
  }

  function detailSections() {
    const card = detailView.card;
    if (detailView.tab === "sources") return [
      { title: "Каталог", text: "Каталог ароматов — Scento, лицензия CC BY 4.0. Изменения: выборка, перевод описаний, нот и семейств.", links: [["Scento", "https://www.scento.com/perfume-database"], ["CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"]] },
      { title: "Иллюстрации", text: "Картинки нот — художественные образы ингредиентов. Для синтетических и редких нот используются абстрактные иллюстрации. Расположение не обозначает интенсивность нот." },
      ...(card?.image ? [{ title: "Фото флакона", text: card.image.author || card.image.source_title || card.house, links: [["Открыть источник", card.image.source_url]] }] : []),
    ];
    if (detailView.tab === "notes") return [
      ["Верхние ноты", card.notes?.top], ["Сердце", card.notes?.heart], ["База", card.notes?.base],
    ].filter(([, values]) => safeList(values).length).map(([title, values]) => ({title, notes:safeList(values)}));
    if (detailView.tab === "reviews") return (Array.isArray(card.reviews) ? card.reviews : []).filter(review => review.text && safeUrl(review.source_url)).map(review => ({
      title: review.author || "Обзор", text: review.text, links: [["Открыть обзор", review.source_url]],
    }));
    const metrics = card.performance;
    const passport = [card.house + " · " + card.name, card.family ? translated(card.family, families) : "", card.concentration ? translated(card.concentration, concentrations) : "", card.release_year || "", safeList(card.perfumers).join(", "), safeList(card.seasons).map(value => seasonNames[value] || value).join(", ")].filter(Boolean).join("\n");
    return [
      { title: "Об аромате", text: card.description_ru || passport },
      ...(metrics ? [{title:"Стойкость и шлейф", text:[metrics.longevity ? "Стойкость: " + metrics.longevity : "", metrics.sillage ? (metrics.diffusion_type === "projection" ? "Проекция: " : "Шлейф: ") + metrics.sillage : "", metrics.basis === "reviewer_report" ? "По опыту автора" : "Оценка источника"].filter(Boolean).join("\n"), links:[[metrics.source_name || "Источник",metrics.source_url]]}] : []),
      ...(card.description_ru ? [{title:"Паспорт аромата",text:passport}] : []),
      ...(card.recommendation_reason_ru ? [{title:"Почему этот аромат",text:card.recommendation_reason_ru}] : []),
    ];
  }

  function detailPiece(section, text) {
    const links = (section.links || []).filter(([, url]) => safeUrl(url));
    return `<h2 class="section-label">${esc(section.title)}</h2><p class="detail-copy">${esc(text)}</p>${links.length ? `<footer class="detail-sources">${links.map(([label, url]) => `<a href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`).join("")}</footer>` : ""}`;
  }

  function renderDetailPages() {
    const content = document.querySelector("#details-content");
    const card = detailView.card;
    const tabs = card ? [["overview","Об аромате"],["notes","Ноты"],["reviews","Отзывы"],["sources","Источники"]] : [];
    content.innerHTML = `<header class="detail-heading"><p class="house">${esc(card?.house || "АРОМАТИЯ")}</p><h1 class="detail-title">${esc(card?.name || "Источники")}</h1></header>${tabs.length ? `<nav class="detail-tabs" aria-label="Подробности">${tabs.map(([key,label]) => `<button data-detail-tab="${key}" aria-pressed="${detailView.tab === key}">${label}</button>`).join("")}</nav>` : ""}<section class="detail-pane"></section><div class="page-controls detail-pager"></div>${detailView.fromCatalog && card?.can_select ? `<button class="icon-button select-perfume" aria-label="Выбрать этот аромат" title="Выбрать этот аромат" ${pending ? "disabled" : ""}>${svg('<path d="M4 12h16m-6-6 6 6-6 6"/>')}</button>` : ""}`;
    const pane = content.querySelector(".detail-pane");
    const pages = [];
    for (const section of detailSections()) {
      if (section.notes) {
        const count = pane.clientHeight < 140 ? 1 : Math.max(3, Math.min(9, Math.floor((pane.clientHeight - 32) / 88) * 3));
        for (let offset = 0; offset < section.notes.length; offset += count) pages.push(`<h2 class="section-label">${esc(section.title)}</h2><div class="detail-note-grid" data-count="${count}">${section.notes.slice(offset, offset + count).map(noteMarkup).join("")}</div>`);
        continue;
      }
      let tokens = (String(section.text || "").match(/\S+\s*/gu) || []).flatMap(token => {
        const chars = [...token]; return chars.length > 48 ? Array.from({length:Math.ceil(chars.length/48)}, (_, i) => chars.slice(i*48,(i+1)*48).join("")) : [token];
      });
      while (tokens.length) {
        let low = 1, high = tokens.length, fit = 1;
        while (low <= high) {
          const middle = Math.floor((low + high) / 2);
          pane.innerHTML = detailPiece(section, tokens.slice(0,middle).join(""));
          const copy = pane.querySelector(".detail-copy");
          if (copy.scrollHeight <= copy.clientHeight + 1) { fit = middle; low = middle + 1; } else high = middle - 1;
        }
        pages.push(detailPiece(section, tokens.slice(0,fit).join(""))); tokens = tokens.slice(fit);
      }
    }
    if (!pages.length) pages.push('<div class="detail-empty">Пока нет данных для этого раздела</div>');
    detailView.page = Math.max(0, Math.min(detailView.page, pages.length - 1));
    pane.innerHTML = pages[detailView.page];
    content.querySelector(".detail-pager").innerHTML = pagerMarkup(detailView.page, pages.length, "detail");
    content.querySelectorAll("[data-detail-tab]").forEach(button => button.addEventListener("click", () => { detailView.tab = button.dataset.detailTab; detailView.page = 0; renderDetailPages(); }));
    content.querySelectorAll("[data-detail-page]").forEach(button => button.addEventListener("click", () => { detailView.page = Number(button.dataset.detailPage); renderDetailPages(); }));
    content.querySelector(".select-perfume")?.addEventListener("click", () => selectPerfume(card));
  }

  function showDetails(card, fromCatalog = false) {
    const content = document.querySelector("#details-content");
    content.dataset.perfumeId = String(card.id);
    detailView = {card, fromCatalog, tab:"overview", page:0};
    dialog.showModal();
    renderDetailPages();
    if (!card.performance) {
      api(`/api/performance/${card.id}`).then(data => {
        if (!data.performance) return;
        card.performance = data.performance;
        if (!dialog.open || content.dataset.perfumeId !== String(card.id)) return;
        if (detailView?.card?.id === card.id) renderDetailPages();
      }).catch(error => {
        if (error.auth) { dialog.close(); catalogDialog.close(); locked(true); }
      });
    }
    try { webapp?.BackButton?.show(); } catch (_) {}
  }

  async function selectPerfume(card) {
    if (pending || !state) return;
    pending = true;
    updateCounts();
    const button = dialog.querySelector(".select-perfume");
    if (button) button.disabled = true;
    try {
      const response = await api("/api/select", { perfume_id: card.id, revision: state.revision });
      state = response.applied === false ? await api("/api/state") : response;
      libraries = {};
      await Promise.all([closeDialog(dialog), closeDialog(catalogDialog)]);
      switchTab("discover");
      notify(response.applied === false ? "Подбор обновился. Выберите аромат заново." : "Аромат выбран");
    } catch (error) {
      if (error.auth) locked(true);
      else {
        let message = document.querySelector("#selection-error");
        if (!message) {
          message = document.createElement("p");
          message.id = "selection-error";
          message.className = "error-inline";
          message.setAttribute("role", "alert");
          document.querySelector("#details-content").append(message);
        }
        message.textContent = "Не удалось выбрать аромат. Попробуйте ещё раз.";
      }
    } finally {
      pending = false;
      updateCounts();
      if (button?.isConnected) button.disabled = false;
    }
  }

  async function undoReaction() {
    if (pending || !state?.can_undo) return;
    pending = true;
    updateCounts();
    try {
      const response = await api("/api/undo", { revision: state.revision });
      state = response.applied === false ? await api("/api/state") : response;
      libraries = {};
      updateCounts();
      if (activeTab === "discover") {
        if (await replaceCard("back", renderDiscover)) window.scrollTo(0, 0);
      } else switchTab("discover", "back");
      notify(response.applied === false ? "Эту реакцию уже нельзя отменить" : "Последняя реакция отменена");
    } catch (error) {
      error.auth ? locked(true) : notify("Не удалось отменить реакцию. Попробуйте ещё раз.");
    } finally {
      pending = false;
      updateCounts();
    }
  }

  function populateFacets(facets) {
    const options = [
      ["family", facets?.families, families],
      ["season", facets?.seasons, seasonNames],
      ["time", facets?.times, {day:"День",evening:"Вечер",night:"Ночь",morning:"Утро"}],
      ["note", facets?.notes, notes],
    ];
    for (const [key, values, dictionary] of options) {
      const select = document.querySelector(`#catalog-${key}`);
      for (const value of safeList(values).sort((a, b) => translated(a, dictionary).localeCompare(translated(b, dictionary), "ru"))) {
        select.add(new Option(translated(value, dictionary), value));
      }
    }
    catalogFacetsReady = true;
  }

  function renderCatalogResults() {
    const results = document.querySelector("#catalog-results");
    results.innerHTML = catalogItems.length
      ? `<div class="library-list">${catalogItems.map((card, index) => `<button type="button" class="library-row" data-catalog-detail="${index}"><span><span class="row-house">${esc(card.house)}</span><span class="row-name">${esc(card.name)}</span><span class="row-detail">${esc([card.family ? translated(card.family, families) : "", labels[card.reaction] || ""].filter(Boolean).join(" · "))}</span></span><span class="row-arrow" aria-hidden="true">↗</span></button>`).join("")}</div>`
      : '<p class="catalog-empty">Ничего не найдено. Попробуйте другую ноту или уберите часть фильтров.</p>';
    results.querySelectorAll("[data-catalog-detail]").forEach(button => {
      button.addEventListener("click", () => showDetails(catalogItems[Number(button.dataset.catalogDetail)], true));
    });
    document.querySelector("#catalog-status").textContent = `${catalogCount} ароматов`;
    const count = Math.max(1, Math.ceil(catalogCount / catalogLimit));
    document.querySelector("#catalog-pager").innerHTML = pagerMarkup(catalogPage, count, "catalog");
    document.querySelectorAll("[data-catalog-page]").forEach(button => button.addEventListener("click", () => { if (!catalogBusy) loadCatalog(Number(button.dataset.catalogPage)); }));
  }

  async function loadCatalog(page = 0, sequence = ++catalogSequence) {
    const status = document.querySelector("#catalog-status");
    catalogLimit = Math.max(1, Math.min(7, Math.floor((catalogDialog.clientHeight - 180) / 78)));
    const params = new URLSearchParams({ offset: String(page * catalogLimit), limit: String(catalogLimit) });
    for (const key of ["q", "family", "season", "time", "note"]) {
      const value = document.querySelector(key === "q" ? "#catalog-query" : `#catalog-${key}`).value.trim();
      if (value) params.set(key, value);
    }
    catalogBusy = true;
    document.querySelectorAll("[data-catalog-page]").forEach(button => { button.disabled = true; });
    status.textContent = "Ищем ароматы…";
    try {
      const data = await api(`/api/catalog?${params}`);
      if (sequence !== catalogSequence || !catalogDialog.open) return;
      if (!catalogFacetsReady) populateFacets(data.facets);
      catalogPage = page;
      catalogItems = data.items;
      catalogCount = data.count;
      renderCatalogResults();
    } catch (error) {
      if (sequence !== catalogSequence || !catalogDialog.open) return;
      if (error.auth) locked(true);
      else {
        status.textContent = "Не удалось загрузить каталог.";
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "text-button";
        retry.textContent = "Попробовать снова";
        retry.addEventListener("click", () => loadCatalog(page));
        document.querySelector("#catalog-results").append(retry);
      }
    } finally {
      if (sequence === catalogSequence) {
        catalogBusy = false;
      }
    }
  }

  function queueCatalogSearch(immediate = false) {
    clearTimeout(catalogTimer);
    const sequence = ++catalogSequence;
    catalogTimer = setTimeout(() => loadCatalog(0, sequence), immediate ? 0 : 250);
  }

  function openCatalog() {
    if (!state) return;
    catalogDialog.showModal();
    try { webapp?.BackButton?.show(); } catch (_) {}
    loadCatalog();
    document.querySelector("#catalog-query").focus();
  }

  function switchTab(tab, direction = "forward") {
    resetViewMotion();
    main.dataset.motion = direction;
    const previousTab = activeTab;
    activeTab = tab;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle("active", active);
      active
        ? button.setAttribute("aria-current", "page")
        : button.removeAttribute("aria-current");
      if (active && previousTab !== tab) playMotion(button.querySelector("svg"), [
        { transform: "translateY(0) scale(.9)" },
        { transform: "translateY(-5px) scale(1.12)", offset: .58 },
        { transform: "translateY(-2px) scale(1.04)" },
      ], { duration: 420, easing: "cubic-bezier(.2,.8,.2,1)" });
    });
    if (tab === "discover") renderDiscover();
    else renderLibrary(tab);
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  async function load() {
    try {
      state = await api("/api/state");
      updateCounts();
      renderDiscover();
    } catch (error) {
      error.auth ? locked(true) : errorPanel(load);
    }
  }

  searchButton.addEventListener("click", openCatalog);
  document.querySelector("#open-details").addEventListener("click", () => {
    if (state?.current) showDetails(state.current);
    else {
      detailView = {card:null, fromCatalog:false, tab:"sources", page:0};
      delete document.querySelector("#details-content").dataset.perfumeId;
      dialog.showModal(); renderDetailPages();
    }
  });
  undoButton.addEventListener("click", undoReaction);
  document.querySelector("#catalog-query").addEventListener("input", () => queueCatalogSearch());
  for (const key of ["family", "season", "time", "note"])
    document.querySelector(`#catalog-${key}`).addEventListener("change", () => queueCatalogSearch(true));
  document.querySelector("#catalog-form").addEventListener("submit", event => {
    event.preventDefault();
    queueCatalogSearch(true);
  });
  document.querySelector("#catalog-reset").addEventListener("click", () => {
    document.querySelector("#catalog-form").reset();
    for (const key of ["family", "season", "time", "note"]) document.querySelector(`#catalog-${key}`).value = "";
    queueCatalogSearch(true);
  });
  document.querySelector("#catalog-filter-toggle").addEventListener("click", () => {
    filtersDialog.showModal();
    try { webapp?.BackButton?.show(); } catch (_) {}
  });
  filtersDialog.querySelector(".close-dialog").addEventListener("click", () => closeDialog(filtersDialog));
  filtersDialog.addEventListener("click", event => { if (event.target === filtersDialog) closeDialog(filtersDialog); });
  catalogDialog.querySelector(".close-dialog").addEventListener("click", () => closeDialog(catalogDialog));
  catalogDialog.addEventListener("click", event => {
    if (event.target === catalogDialog) closeDialog(catalogDialog);
  });
  catalogDialog.addEventListener("close", () => {
    clearTimeout(catalogTimer);
    ++catalogSequence;
    catalogBusy = false;
    try { if (!dialog.open) webapp?.BackButton?.hide(); } catch (_) {}
  });

  document.querySelectorAll("[data-tab]").forEach((button) =>
    button.addEventListener("click", () => {
      if (state) switchTab(button.dataset.tab);
    }),
  );
  const closeDetails = () => {
    if (dialog.open) closeDialog(dialog);
    else if (filtersDialog.open) closeDialog(filtersDialog);
    else if (catalogDialog.open) closeDialog(catalogDialog);
  };
  for (const target of [dialog, catalogDialog, filtersDialog]) target.addEventListener("cancel", event => {
    event.preventDefault();
    closeDialog(target);
  });
  dialog.querySelector(".close-dialog").addEventListener("click", closeDetails);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeDetails();
  });
  dialog.addEventListener("close", () => {
    try {
      if (catalogDialog.open) webapp?.BackButton?.show();
      else webapp?.BackButton?.hide();
    } catch (_) {}
  });
  document.addEventListener("error", (event) => handleImageError(event.target), true);
  function fitViewport() {
    const height = Math.round(window.visualViewport?.height || window.innerHeight);
    document.documentElement.style.setProperty("--app-height", `${height}px`);
    clearTimeout(viewportTimer);
    viewportTimer = setTimeout(() => {
      if (dialog.open && detailView) renderDetailPages();
      if (catalogDialog.open) loadCatalog(0);
      if (["wishlist", "known"].includes(activeTab) && libraries[activeTab]) paintLibrary(activeTab);
    }, 180);
  }
  window.visualViewport?.addEventListener("resize", fitViewport);
  window.addEventListener("resize", fitViewport);
  fitViewport();
  theme();
  try {
    webapp?.ready();
    webapp?.expand();
    webapp?.onEvent("themeChanged", theme);
    webapp?.BackButton?.onClick(closeDetails);
  } catch (_) {}
  if (!initData) locked();
  else load();
})();
