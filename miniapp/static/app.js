"use strict";

(() => {
  const webapp = window.Telegram?.WebApp;
  const initData = webapp?.initData || "";
  const main = document.querySelector("#main");
  const dialog = document.querySelector("#details-dialog");
  const catalogDialog = document.querySelector("#catalog-dialog");
  const undoButton = document.querySelector("#undo-reaction");
  const searchButton = document.querySelector("#open-catalog");
  let catalogItems = [], catalogCount = 0, catalogSequence = 0,
    catalogTimer, catalogFacetsReady = false, catalogBusy = false;
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
    state = null;
    updateCounts();
    if (dialog.open) dialog.close();
    if (catalogDialog.open) catalogDialog.close();
    document.querySelector(".bottom-nav").hidden = true;
    document.querySelector("#header-note").textContent =
      "Ваше личное пространство";
    main.innerHTML = `<section class="message-panel"><img class="message-mark" src="/static/mark.svg" alt=""><h1>${expired ? "Откройте Ароматию заново" : "Встречаемся в Telegram"}</h1><p>${expired ? "Сессия завершилась или доступ не подтверждён. Закройте приложение и снова откройте его через меню вашего бота в Telegram." : "Откройте Ароматию из меню вашего бота в Telegram. Там вас ждут подбор ароматов, избранное и личная коллекция."}</p></section>`;
  }

  function errorPanel(retry) {
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

  function reviewsMarkup(card) {
    const reviews = Array.isArray(card.reviews)
      ? card.reviews.filter((review) => review && typeof review.text === "string" && review.text.trim() && safeUrl(review.source_url))
      : [];
    if (!reviews.length) return "";
    return `<section class="reviews" aria-label="Отзывы"><h2 class="section-label">Отзывы</h2>${reviews.map((review) => {
      const videoOnly = /без текстового|без извлеч|не расшифровано/.test(review.source_title || "");
      return `<article class="review">${videoOnly ? "" : `<p class="review-text">${esc(review.text)}</p>`}<a class="review-source" href="${esc(safeUrl(review.source_url))}" target="_blank" rel="noopener noreferrer">${esc(review.author || "Источник")} · ${videoOnly ? "Видеообзор" : "Читать обзор"} ↗</a></article>`;
    }).join("")}</section>`;
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

  function cardMarkup(card) {
    const chips = [
      card.family
        ? `<span class="chip family">${esc(translated(card.family, families))}</span>`
        : "",
      card.concentration
        ? `<span class="chip">${esc(translated(card.concentration, concentrations))}</span>`
        : "",
    ].join("");
    const pyramid = [
      ["top", "Верхние", card.notes?.top || card.top_notes],
      ["heart", "Сердце", card.notes?.heart || card.heart_notes],
      ["base", "База", card.notes?.base || card.base_notes],
    ]
      .filter(([, , values]) => safeList(values).length)
      .map(([level, label, values], index) =>
        `<section class="note-constellation note-constellation--${level}" aria-label="${label} ноты"><header class="note-stage"><span class="note-stage-number" aria-hidden="true">0${index + 1}</span><h2>${label}</h2><span class="note-stage-line" aria-hidden="true"></span></header><div class="note-cloud">${safeList(values).map(noteMarkup).join("")}</div></section>`,
      )
      .join("");
    const seasons = safeList(card.seasons)
      .filter((season) => seasonNames[season])
      .map(
        (season) =>
          `<span class="season">${svg(seasonIcons[season])}${seasonNames[season]}</span>`,
      )
      .join("");
    const meta = [
      card.release_year ? String(card.release_year) : "",
      safeList(card.perfumers).join(", "),
    ]
      .filter(Boolean)
      .join(" · ");
    const description = typeof card.description_ru === "string" ? card.description_ru.trim() : "";
    const visual = imageMarkup(card);
    const imageSource = safeUrl(card.image?.source_url);
    const more = [
      !visual ? '<p class="image-status">Фото пока не добавлено.</p>' : "",
      card.recommendation_reason_ru ? `<p>${esc(card.recommendation_reason_ru)}</p>` : "",
      meta ? `<p>${esc(meta)}</p>` : "",
      imageSource ? `<p>Фото: <a href="${esc(imageSource)}" target="_blank" rel="noopener noreferrer">${esc(card.image.author || card.image.source_title || "Источник")} ↗</a></p>` : "",
      '<p>Изображения нот — художественные иллюстрации ингредиентов и их групп; для редких нот используется абстрактный образ.</p>',
    ].filter(Boolean).join("");
    return `<article class="perfume${visual ? " has-visual" : ""}">${visual}<div class="perfume-content"><header class="perfume-hero"><p class="house">${esc(card.house)}</p><h1 class="perfume-name">${esc(card.name)}</h1>${chips ? `<div class="chips">${chips}</div>` : ""}</header>${description ? `<p class="description">${esc(description)}</p>` : ""}${pyramid ? `<section class="pyramid" aria-label="Пирамида аромата">${pyramid}</section>` : ""}${seasons ? `<div class="seasons">${seasons}</div>` : ""}${reviewsMarkup(card)}${more ? `<details class="card-details"><summary>Подробнее</summary>${more}</details>` : ""}</div></article>`;
  }

  function renderDiscover() {
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
        main.dataset.motion = "forward";
        renderDiscover();
        window.scrollTo(0, 0);
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
        .forEach((button) => (button.disabled = false));
    }
  }

  async function renderLibrary(kind) {
    const wishlist = kind === "wishlist";
    const heading = `<div class="library-head"><h1 class="library-title">${wishlist ? "Избранное" : "Коллекция"}</h1></div>`;
    main.innerHTML = `${heading}<section class="message-panel"><span class="loading-leaf"></span><p>Собираем ваши ароматы…</p></section>`;
    try {
      if (!libraries[kind])
        libraries[kind] = await api(`/api/library?kind=${kind}`);
      if (activeTab !== kind) return;
      const items = libraries[kind].items;
      main.innerHTML =
        heading +
        (items.length
          ? `<div class="library-list">${items.map((card, i) => `<button type="button" class="library-row" data-detail="${i}"><span><span class="row-house">${esc(card.house)}</span><span class="row-name">${esc(card.name)}</span><span class="row-detail">${card.reaction === "bought" ? "В коллекции" : card.reaction === "known" ? "Уже знаком" : "Хочу попробовать"}${card.family ? ` · ${esc(translated(card.family, families))}` : ""}</span></span><span class="row-arrow" aria-hidden="true">↗</span></button>`).join("")}</div>`
          : `<section class="message-panel"><img class="message-mark" src="/static/mark.svg" alt=""><h2>${wishlist ? "Первое желание — впереди" : "Здесь будет ваша история"}</h2><p>${wishlist ? "Нажмите на сердце в подборе, и аромат появится здесь." : "Отмечайте знакомые и купленные ароматы в подборе — мы сохраним их для вас."}</p><button type="button" class="plain-button" id="back-discover">К подбору</button></section>`);
      document
        .querySelector("#back-discover")
        ?.addEventListener("click", () => switchTab("discover"));
      main.querySelectorAll("[data-detail]").forEach((button) =>
        button.addEventListener("click", () => {
          const card = items[Number(button.dataset.detail)];
          showDetails(card);
        }),
      );
    } catch (error) {
      if (activeTab === kind)
        error.auth ? locked(true) : errorPanel(() => renderLibrary(kind));
    }
  }

  function showDetails(card, fromCatalog = false) {
    const content = document.querySelector("#details-content");
    content.innerHTML = cardMarkup(card) + (fromCatalog && card.can_select
      ? '<button type="button" class="plain-button select-perfume">Выбрать для знакомства</button>'
      : `<p class="dialog-reaction">${esc(labels[card.reaction] || (fromCatalog ? "Аромат уже в вашей истории" : ""))}</p>`);
    checkImages(content);
    content.querySelector(".select-perfume")?.addEventListener("click", () => selectPerfume(card));
    dialog.showModal();
    dialog.scrollTop = 0;
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
      dialog.close();
      catalogDialog.close();
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
      switchTab("discover");
      main.dataset.motion = "back";
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
    document.querySelector("#catalog-more").hidden = catalogItems.length >= catalogCount;
  }

  async function loadCatalog(reset = true, sequence = ++catalogSequence) {
    const more = document.querySelector("#catalog-more");
    const status = document.querySelector("#catalog-status");
    const params = new URLSearchParams({ offset: String(reset ? 0 : catalogItems.length), limit: "30" });
    for (const key of ["q", "family", "season", "time", "note"]) {
      const value = document.querySelector(key === "q" ? "#catalog-query" : `#catalog-${key}`).value.trim();
      if (value) params.set(key, value);
    }
    catalogBusy = true;
    more.disabled = true;
    if (reset) {
      catalogItems = [];
      document.querySelector("#catalog-results").innerHTML = "";
      more.hidden = true;
    }
    status.textContent = "Ищем ароматы…";
    try {
      const data = await api(`/api/catalog?${params}`);
      if (sequence !== catalogSequence || !catalogDialog.open) return;
      if (!catalogFacetsReady) populateFacets(data.facets);
      catalogItems = reset ? data.items : catalogItems.concat(data.items);
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
        retry.addEventListener("click", () => loadCatalog(reset));
        document.querySelector("#catalog-results").append(retry);
      }
    } finally {
      if (sequence === catalogSequence) {
        catalogBusy = false;
        more.disabled = false;
      }
    }
  }

  function queueCatalogSearch(immediate = false) {
    clearTimeout(catalogTimer);
    const sequence = ++catalogSequence;
    catalogTimer = setTimeout(() => loadCatalog(true, sequence), immediate ? 0 : 250);
  }

  function openCatalog() {
    if (!state) return;
    catalogDialog.showModal();
    try { webapp?.BackButton?.show(); } catch (_) {}
    loadCatalog();
    document.querySelector("#catalog-query").focus();
  }

  function switchTab(tab) {
    main.dataset.motion = "forward";
    activeTab = tab;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle("active", active);
      active
        ? button.setAttribute("aria-current", "page")
        : button.removeAttribute("aria-current");
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
    queueCatalogSearch(true);
  });
  document.querySelector("#catalog-more").addEventListener("click", () => {
    if (!catalogBusy) loadCatalog(false);
  });
  catalogDialog.querySelector(".close-dialog").addEventListener("click", () => catalogDialog.close());
  catalogDialog.addEventListener("click", event => {
    if (event.target === catalogDialog) catalogDialog.close();
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
    if (dialog.open) dialog.close();
    else if (catalogDialog.open) catalogDialog.close();
  };
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
