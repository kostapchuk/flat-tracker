/**
 * Telegram-бот трекера квартир. Живёт на Cloudflare Workers и отвечает мгновенно.
 *
 * Разделение обязанностей:
 *   - этот бот  — общение с людьми: подписки, личные фильтры, команды;
 *   - GitHub Actions — обход сайта по расписанию. Список фильтров он забирает
 *     здесь (GET /filters), а результат отдаёт обратно: POST /broadcast
 *     (появилась квартира) и POST /retire (квартиру сняли).
 *
 * Настройки в панели Cloudflare (Workers → Settings):
 *   Variables:  BOT_TOKEN, WEBHOOK_SECRET, BROADCAST_SECRET, OWNER_CHAT_ID, STATE_URL
 *   KV binding: SUBS
 * При сборке из репозитория привязка и несекретные переменные берутся
 * из wrangler.toml.
 *
 * Ключи в KV:
 *   subs:<chatId>                  — подписчик
 *   filters:<chatId>               — все личные фильтры человека, одним списком
 *   index:filters                  — чаты, у которых фильтры есть
 *   msg:<filterId>:<flatId>:<chat> — каким сообщением прислали эту квартиру
 *
 * Почему такая схема: в KV операция list согласована лишь в конечном счёте —
 * только что записанный ключ может не попадать в перебор ещё около минуты.
 * Поэтому всё, что нужно читать сразу после записи (фильтры человека и список
 * таких людей), лежит в конкретных ключах и читается через get, а не list.
 *
 * На бесплатном тарифе один запрос может сделать не больше 50 подзапросов,
 * поэтому check.py шлёт по одной карточке за запрос.
 */

const CAPTION_LIMIT = 1024;
const MAX_CARDS = 10;
const MAX_FILTERS = 5;
const DEFAULT_FILTER = "default";

const COMMANDS = [
  { command: "list", description: "Что подходит прямо сейчас" },
  { command: "add", description: "Добавить фильтр — прислать ссылку с сайта" },
  { command: "filters", description: "Мои фильтры" },
  { command: "del", description: "Удалить фильтр" },
  { command: "start", description: "Подписаться на новые квартиры" },
  { command: "stop", description: "Отписаться от уведомлений" },
];

const HOW_TO_ADD = [
  "Чтобы добавить фильтр:",
  "",
  "1. Открой <a href=\"https://newbor.by/podbor-kvartiry/\">подбор квартир</a> на сайте",
  "2. Выстави параметры — площадь, дом, цену, отделку, что нужно",
  "3. Скопируй адрес из строки браузера",
  "4. Пришли его мне сюда одним сообщением",
  "",
  "Можно дописать название в том же сообщении — например, «четырёшки у парка». " +
    "Тогда я так фильтр и назову.",
].join("\n");

const INTRO = [
  "Слежу за квартирами в «Новой Боровой» и присылаю новые, как только они появляются в продаже.",
  "",
  "<b>Свой фильтр.</b> Собери подборку на newbor.by (площадь, дом, цена, отделка — что угодно) и пришли мне ссылку из адресной строки. Буду следить именно по ней.",
  "Можно добавить до " + MAX_FILTERS + " фильтров. Если своих нет, работает общий: свободные квартиры от 70 м².",
  "",
  "/add — добавить фильтр",
  "/filters — мои фильтры",
  "/list — что подходит прямо сейчас",
  "/stop — отписаться",
].join("\n");

// ---------------------------------------------------------------------------
// Telegram API
// ---------------------------------------------------------------------------

function api(env, method, payload) {
  return fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/**
 * Отправляет карточку. Возвращает { delivered, messageId }.
 * delivered=false — чат недоступен, подписчика надо убрать.
 */
async function send(env, chatId, card, filterId) {
  const withCaption = Boolean(card.photo) && card.text.length <= CAPTION_LIMIT;
  const attempts = withCaption
    ? [
      ["sendPhoto", true, {
        chat_id: chatId,
        photo: card.photo,
        caption: card.text,
        parse_mode: "HTML",
      }],
      // картинка может не открыться на стороне Telegram — тогда просто текстом
      ["sendMessage", false, { chat_id: chatId, text: card.text, parse_mode: "HTML" }],
    ]
    : [["sendMessage", false, {
      chat_id: chatId,
      text: card.text,
      parse_mode: "HTML",
      disable_web_page_preview: !card.photo,
    }]];

  for (const [method, isPhoto, payload] of attempts) {
    const response = await api(env, method, payload);

    if (response.ok) {
      const body = await response.json().catch(() => null);
      const messageId = body && body.result && body.result.message_id;
      // запоминаем сообщение: когда квартиру снимут, отредактируем именно его
      if (messageId && card.id && filterId) {
        await env.SUBS.put(
          `msg:${filterId}:${card.id}:${chatId}`,
          JSON.stringify({ messageId, photo: isPhoto }),
          { expirationTtl: 60 * 60 * 24 * 120 }, // через 4 месяца уже не актуально
        );
      }
      return { delivered: true, messageId };
    }

    const body = await response.text();
    console.error(`${method} -> ${response.status}: ${body.slice(0, 200)}`);
    // 403 — бот заблокирован, 400 chat not found — чат удалён
    if (response.status === 403 || body.includes("chat not found")) {
      return { delivered: false };
    }
  }
  return { delivered: true }; // не доставили, но подписчик ни при чём — оставляем
}

function text(env, chatId, message) {
  return send(env, chatId, { text: message });
}

// ---------------------------------------------------------------------------
// Подписчики и фильтры
// ---------------------------------------------------------------------------

async function subscribe(env, chatId, info) {
  const existing = await env.SUBS.get(`subs:${chatId}`);
  if (existing) return false;
  await env.SUBS.put(`subs:${chatId}`, JSON.stringify(info));
  return true;
}

async function unsubscribe(env, chatId) {
  const existing = await env.SUBS.get(`subs:${chatId}`);
  await env.SUBS.delete(`subs:${chatId}`);
  return Boolean(existing);
}

async function subscribers(env) {
  const ids = new Set();
  if (env.OWNER_CHAT_ID) ids.add(String(env.OWNER_CHAT_ID)); // владелец подписан всегда
  const listed = await env.SUBS.list({ prefix: "subs:" });
  for (const key of listed.keys) ids.add(key.name.slice("subs:".length));
  return [...ids];
}

/** Фильтры, оставшиеся от прежней схемы «ключ на фильтр» */
async function legacyFilters(env, chatId) {
  const listed = await env.SUBS.list({ prefix: `filter:${chatId}:` });
  const filters = [];
  for (const key of listed.keys) {
    const value = await env.SUBS.get(key.name);
    if (value) filters.push({ id: key.name.split(":")[2], ...JSON.parse(value) });
    await env.SUBS.delete(key.name);
  }
  return filters;
}

/** Фильтры одного человека: [{ id, name, url }] */
async function userFilters(env, chatId) {
  const stored = await env.SUBS.get(`filters:${chatId}`);
  const filters = stored ? JSON.parse(stored) : [];

  const legacy = await legacyFilters(env, chatId);
  if (legacy.length) {
    const known = new Set(filters.map((filter) => filter.url));
    for (const filter of legacy) {
      if (!known.has(filter.url)) filters.push(filter);
    }
    await saveFilters(env, chatId, filters);
  }
  return filters;
}

async function saveFilters(env, chatId, filters) {
  await env.SUBS.put(`filters:${chatId}`, JSON.stringify(filters));

  // указатель, чтобы обходиться без list — он отстаёт от записи
  const stored = await env.SUBS.get("index:filters");
  const chats = new Set(stored ? JSON.parse(stored) : []);
  const before = chats.size;
  filters.length ? chats.add(chatId) : chats.delete(chatId);
  if (chats.size !== before) {
    await env.SUBS.put("index:filters", JSON.stringify([...chats]));
  }
}

/** Все фильтры всех людей — для проверялки */
async function allFilters(env) {
  const stored = await env.SUBS.get("index:filters");
  const chats = stored ? JSON.parse(stored) : [];
  const filters = [];
  for (const chatId of chats) {
    const value = await env.SUBS.get(`filters:${chatId}`);
    for (const filter of value ? JSON.parse(value) : []) {
      filters.push({ id: filter.id, chat_id: chatId, name: filter.name, url: filter.url });
    }
  }
  return filters;
}

function newFilterId() {
  return Math.random().toString(36).slice(2, 8);
}

// ---------------------------------------------------------------------------
// Текущая выдача — из state.json, который коммитит GitHub Actions
// ---------------------------------------------------------------------------

async function loadState(env) {
  try {
    const response = await fetch(env.STATE_URL, {
      cf: { cacheTtl: 0 },
      headers: { "cache-control": "no-cache" },
    });
    if (!response.ok) throw new Error(`state.json -> ${response.status}`);
    return await response.json();
  } catch (error) {
    console.error("не смог прочитать state.json:", error);
    return null;
  }
}

/** Карточки конкретного фильтра. null — состояние недоступно, [] — пусто */
function cardsOf(state, filterId) {
  if (!state) return null;
  const filter = (state.filters || {})[filterId];
  if (!filter) return [];
  const catalog = state.catalog || {};
  return (filter.ids || [])
    .map((id) => catalog[id])
    .filter(Boolean)
    .map((flat) => ({ id: flat.id, text: flat.text || "", photo: flat.plan_image }))
    .filter((card) => card.text);
}

// ---------------------------------------------------------------------------
// Команды
// ---------------------------------------------------------------------------

async function showMatches(env, chatId, state, filterId, title) {
  const cards = cardsOf(state, filterId);

  if (cards === null) {
    await text(env, chatId, "Не могу получить список, попробуй через пару минут.");
    return;
  }
  if (!cards.length) {
    await text(env, chatId, `${title}: сейчас пусто. Как появится — сразу напишу.`);
    return;
  }

  await text(env, chatId, `${title}: <b>${cards.length}</b>`);
  for (const card of cards.slice(0, MAX_CARDS)) await send(env, chatId, card, filterId);
  if (cards.length > MAX_CARDS) {
    await text(env, chatId, `…и ещё ${cards.length - MAX_CARDS} — смотри на сайте.`);
  }
}

async function addFilter(env, chatId, url, name) {
  const filters = await userFilters(env, chatId);
  if (filters.length >= MAX_FILTERS) {
    await text(
      env,
      chatId,
      `У тебя уже ${MAX_FILTERS} фильтров — больше нельзя. Удали лишний: /filters`,
    );
    return;
  }
  if (filters.some((filter) => filter.url === url)) {
    await text(env, chatId, "Такой фильтр уже добавлен. Список: /filters");
    return;
  }

  const id = newFilterId();
  const title = name || `Фильтр ${filters.length + 1}`;
  filters.push({ id, name: title, url, created: new Date().toISOString() });
  await saveFilters(env, chatId, filters);
  await subscribe(env, chatId, { since: new Date().toISOString() });

  await text(
    env,
    chatId,
    `✅ Добавил фильтр «${title}».\n\nПервая проверка по нему пройдёт в ближайшие минуты — ` +
      "тогда и пришлю, что под него подходит. Дальше буду сообщать только о новых.\n\n" +
      "Мои фильтры: /filters",
  );
}

async function handleUpdate(env, update) {
  const message = update.message;
  const chat = message && message.chat;
  if (!chat || !chat.id || typeof message.text !== "string") return;

  const chatId = String(chat.id);
  const body = message.text.trim();
  const command = body.split(/\s+/)[0].split("@")[0].toLowerCase();
  const name = [chat.first_name, chat.last_name].filter(Boolean).join(" ");
  console.log(`${command} от ${name || chatId} (${chatId})`);

  // Ссылка на подборку = новый фильтр. Остальной текст сообщения — его название.
  const link = body.match(/https?:\/\/(?:www\.)?newbor\.by\/\S+/i);
  if (link) {
    const title = body.replace(link[0], "").replace(/^\/\w+/, "").trim().slice(0, 60);
    await addFilter(env, chatId, link[0], title);
    return;
  }

  if (command === "/start") {
    const isNew = await subscribe(env, chatId, {
      name,
      username: chat.username || "",
      since: new Date().toISOString(),
    });
    await text(
      env,
      chatId,
      (isNew
        ? "✅ Подписал! Пришлю, как только появится новая подходящая квартира.\n\n"
        : "Ты уже подписан 👌\n\n") + INTRO,
    );
    if (isNew) {
      const state = await loadState(env);
      await showMatches(env, chatId, state, DEFAULT_FILTER, "Сейчас по общему фильтру подходит");
    }
    return;
  }

  if (command === "/stop") {
    const was = await unsubscribe(env, chatId);
    await text(
      env,
      chatId,
      was
        ? "🔕 Отписал. Фильтры сохранил — вернуться можно командой /start"
        : "Ты и так не подписан. Подписаться — /start",
    );
    return;
  }

  if (command === "/add") {
    await text(env, chatId, HOW_TO_ADD);
    return;
  }

  if (command === "/filters") {
    const filters = await userFilters(env, chatId);
    if (!filters.length) {
      await text(
        env,
        chatId,
        "Своих фильтров нет — работает общий: свободные квартиры от 70 м², любой дом.\n\n" +
          HOW_TO_ADD,
      );
      return;
    }
    const lines = filters.map((filter, index) =>
      `${index + 1}. <b>${filter.name}</b>\n<a href="${filter.url}">ссылка</a> · удалить: /del_${filter.id}`
    );
    await text(env, chatId, "Твои фильтры:\n\n" + lines.join("\n\n") +
      "\n\nДобавить ещё — /add");
    return;
  }

  if (command === "/del" || command.startsWith("/del_")) {
    const filters = await userFilters(env, chatId);
    const id = command.startsWith("/del_")
      ? command.slice("/del_".length)
      : (body.split(/\s+/)[1] || "").replace(/^\//, "");

    if (!id) {
      if (!filters.length) {
        await text(env, chatId, "Удалять нечего — своих фильтров нет.\n\n" + HOW_TO_ADD);
        return;
      }
      const lines = filters.map((filter) => `• <b>${filter.name}</b> — удалить: /del_${filter.id}`);
      await text(env, chatId, "Какой фильтр удалить?\n\n" + lines.join("\n"));
      return;
    }

    const doomed = filters.find((filter) => filter.id === id);
    if (!doomed) {
      await text(env, chatId, "Такого фильтра нет. Список: /filters");
      return;
    }
    await saveFilters(env, chatId, filters.filter((filter) => filter.id !== id));
    await text(env, chatId, `🗑 Удалил «${doomed.name}». Остальные: /filters`);
    return;
  }

  if (command === "/list") {
    const state = await loadState(env);
    const filters = await userFilters(env, chatId);
    if (!filters.length) {
      await showMatches(env, chatId, state, DEFAULT_FILTER, "Сейчас по общему фильтру подходит");
      return;
    }
    for (const filter of filters) {
      await showMatches(env, chatId, state, filter.id, `«${filter.name}»`);
    }
    return;
  }

  await text(env, chatId, INTRO);
}

// ---------------------------------------------------------------------------
// Обмен с проверялкой
// ---------------------------------------------------------------------------

/** Кого обслуживает общий фильтр: подписчики без своих фильтров */
async function handleFilters(env) {
  const filters = await allFilters(env);
  const withOwn = new Set(filters.map((filter) => filter.chat_id));
  const defaultChats = (await subscribers(env)).filter((chatId) => !withOwn.has(chatId));
  return Response.json({ filters, default_chats: defaultChats });
}

async function handleBroadcast(env, request) {
  const payload = await request.json().catch(() => null);
  if (!payload || !payload.cards || !payload.cards.length) {
    return new Response("нужен непустой cards[]", { status: 400 });
  }

  const targets = payload.chats && payload.chats.length
    ? payload.chats.map(String)
    : await subscribers(env);
  const filterId = payload.filter_id || DEFAULT_FILTER;
  let delivered = 0;
  let dropped = 0;

  for (const chatId of targets) {
    if (payload.header) await text(env, chatId, payload.header);

    let alive = true;
    for (const card of payload.cards.slice(0, MAX_CARDS)) {
      alive = (await send(env, chatId, card, filterId)).delivered;
      if (!alive) break;
    }

    if (alive) {
      delivered++;
    } else {
      dropped++;
      await env.SUBS.delete(`subs:${chatId}`); // заблокировал бота — отписываем
      console.log(`отписан ${chatId}: чат недоступен`);
    }
  }

  return Response.json({ targets: targets.length, delivered, dropped });
}

/**
 * Квартиру сняли с продажи. Правим уже отправленные сообщения, чтобы список
 * в переписке оставался честным. Telegram разрешает редактировать свои
 * сообщения только 48 часов — что старше, дублируем коротким новым.
 */
async function handleRetire(env, request) {
  const payload = await request.json().catch(() => null);
  if (!payload || !payload.flats || !payload.flats.length) {
    return new Response("нужен непустой flats[]", { status: 400 });
  }

  const filterId = payload.filter_id || DEFAULT_FILTER;
  let edited = 0;
  let sent = 0;

  for (const flat of payload.flats) {
    const listed = await env.SUBS.list({ prefix: `msg:${filterId}:${flat.id}:` });

    for (const key of listed.keys) {
      const chatId = key.name.split(":")[3];
      const stored = JSON.parse(await env.SUBS.get(key.name) || "{}");
      if (!stored.messageId) continue;

      const response = await api(
        env,
        stored.photo ? "editMessageCaption" : "editMessageText",
        stored.photo
          ? { chat_id: chatId, message_id: stored.messageId, caption: flat.text, parse_mode: "HTML" }
          : { chat_id: chatId, message_id: stored.messageId, text: flat.text, parse_mode: "HTML" },
      );

      if (response.ok) {
        edited++;
      } else {
        console.error(`правка ${flat.id} в ${chatId}: ${(await response.text()).slice(0, 160)}`);
        if ((await text(env, chatId, flat.short)).delivered) sent++;
      }
      await env.SUBS.delete(key.name);
    }

    // квартиру никому не отправляли — сообщаем тем, кого этот фильтр касается
    if (!listed.keys.length) {
      const targets = payload.chats && payload.chats.length
        ? payload.chats.map(String)
        : await subscribers(env);
      for (const chatId of targets) {
        if ((await text(env, chatId, flat.short)).delivered) sent++;
      }
    }
  }

  return Response.json({ edited, sent });
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

function authorized(env, request, url) {
  const header = request.headers.get("authorization") || "";
  const provided = header.replace(/^Bearer\s+/i, "") || url.searchParams.get("key") || "";
  return Boolean(env.BROADCAST_SECRET) && provided === env.BROADCAST_SECRET;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/webhook" && request.method === "POST") {
      if (env.WEBHOOK_SECRET &&
          request.headers.get("x-telegram-bot-api-secret-token") !== env.WEBHOOK_SECRET) {
        return new Response("нет", { status: 401 });
      }
      const update = await request.json().catch(() => null);
      if (update) await handleUpdate(env, update);
      return new Response("ok"); // Telegram доволен в любом случае
    }

    if (url.pathname === "/filters" && authorized(env, request, url)) {
      return await handleFilters(env);
    }

    if (url.pathname === "/broadcast" && request.method === "POST") {
      if (!authorized(env, request, url)) return new Response("нет", { status: 401 });
      return await handleBroadcast(env, request);
    }

    if (url.pathname === "/retire" && request.method === "POST") {
      if (!authorized(env, request, url)) return new Response("нет", { status: 401 });
      return await handleRetire(env, request);
    }

    // Разовая настройка: привязать вебхук к этому адресу и задать меню команд
    if (url.pathname === "/setup") {
      if (!authorized(env, request, url)) return new Response("нет", { status: 401 });
      const webhook = await api(env, "setWebhook", {
        url: `${url.origin}/webhook`,
        secret_token: env.WEBHOOK_SECRET,
        allowed_updates: ["message"],
        drop_pending_updates: false,
      });
      const menu = await api(env, "setMyCommands", { commands: COMMANDS });
      return Response.json({ webhook: await webhook.json(), commands: await menu.json() });
    }

    if (url.pathname === "/keys" && authorized(env, request, url)) {
      const listed = await env.SUBS.list();
      return Response.json({ keys: listed.keys.map((key) => key.name) });
    }

    // Диагностика настройки: только факт наличия, без значений
    if (url.pathname === "/health") {
      return Response.json({
        kv_binding_SUBS: Boolean(env.SUBS),
        BOT_TOKEN: Boolean(env.BOT_TOKEN),
        WEBHOOK_SECRET: Boolean(env.WEBHOOK_SECRET),
        BROADCAST_SECRET: Boolean(env.BROADCAST_SECRET),
        OWNER_CHAT_ID: env.OWNER_CHAT_ID || null,
        STATE_URL: env.STATE_URL || null,
      });
    }

    if (url.pathname === "/") {
      if (!env.SUBS) {
        return new Response(
          "Не привязано хранилище KV: Settings → Bindings → KV namespace, " +
          "Variable name должен быть SUBS. Подробности: /health\n",
          { status: 500 },
        );
      }
      const people = (await subscribers(env)).length;
      const filters = (await allFilters(env)).length;
      return new Response(`Трекер квартир жив. Подписчиков: ${people}, личных фильтров: ${filters}\n`);
    }

    return new Response("не найдено", { status: 404 });
  },
};
