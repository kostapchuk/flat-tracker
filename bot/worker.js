/**
 * Telegram-бот трекера квартир. Живёт на Cloudflare Workers и отвечает мгновенно.
 *
 * Разделение обязанностей:
 *   - этот бот  — общение с людьми: подписки (KV) и команды;
 *   - GitHub Actions — обход сайта по расписанию; найдя изменения, дёргает
 *     POST /broadcast (появилась квартира) и POST /retire (квартиру сняли).
 *
 * Настройки в панели Cloudflare (Workers → Settings):
 *   Variables:  BOT_TOKEN, WEBHOOK_SECRET, BROADCAST_SECRET, OWNER_CHAT_ID, STATE_URL
 *   KV binding: SUBS
 *
 * Ключи в KV:
 *   subs:<chatId>            — подписчик
 *   msg:<flatId>:<chatId>    — какое сообщение прислало эту квартиру
 *
 * На бесплатном тарифе один запрос может сделать не больше 50 подзапросов,
 * поэтому check.py шлёт по одной карточке за запрос — так мы в лимит не упрёмся.
 */

const CAPTION_LIMIT = 1024;
const MAX_CARDS = 10;

const COMMANDS = [
  { command: "start", description: "Подписаться на новые квартиры" },
  { command: "stop", description: "Отписаться от уведомлений" },
  { command: "list", description: "Что подходит прямо сейчас" },
];

const INTRO = [
  "Слежу за квартирами в «Новой Боровой» и присылаю новые, как только они появляются в продаже.",
  "",
  "Что отслеживаю: свободные квартиры от 70 м², любой дом, любые цена и этаж.",
  "",
  "/start — подписаться",
  "/stop — отписаться",
  "/list — что подходит прямо сейчас",
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
 * Отправляет карточку. Возвращает { delivered, messageId, photo }.
 * delivered=false — чат недоступен, подписчика надо убрать.
 */
async function send(env, chatId, card) {
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
      if (messageId && card.id) {
        await env.SUBS.put(
          `msg:${card.id}:${chatId}`,
          JSON.stringify({ messageId, photo: isPhoto }),
          { expirationTtl: 60 * 60 * 24 * 120 }, // через 4 месяца уже не актуально
        );
      }
      return { delivered: true, messageId, photo: isPhoto };
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
// Подписчики
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

// ---------------------------------------------------------------------------
// Текущая выдача — из state.json, который коммитит GitHub Actions
// ---------------------------------------------------------------------------

async function currentCards(env) {
  try {
    const response = await fetch(env.STATE_URL, {
      cf: { cacheTtl: 0 },
      headers: { "cache-control": "no-cache" },
    });
    if (!response.ok) throw new Error(`state.json -> ${response.status}`);
    const state = await response.json();
    return Object.values(state.flats || {})
      .map((flat) => ({ id: flat.id, text: flat.text || "", photo: flat.plan_image }))
      .filter((card) => card.text);
  } catch (error) {
    console.error("не смог прочитать state.json:", error);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Команды
// ---------------------------------------------------------------------------

async function handleUpdate(env, update) {
  const message = update.message;
  const chat = message && message.chat;
  if (!chat || !chat.id || typeof message.text !== "string") return;

  const chatId = String(chat.id);
  const command = message.text.trim().split(/\s+/)[0].split("@")[0].toLowerCase();
  const name = [chat.first_name, chat.last_name].filter(Boolean).join(" ");
  console.log(`${command} от ${name || chatId} (${chatId})`);

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
      const cards = await currentCards(env);
      if (cards && cards.length) {
        await text(env, chatId, `Сейчас под фильтр подходит: <b>${cards.length}</b>. Показываю…`);
        for (const card of cards.slice(0, MAX_CARDS)) await send(env, chatId, card);
      }
    }
    return;
  }

  if (command === "/stop") {
    const was = await unsubscribe(env, chatId);
    await text(
      env,
      chatId,
      was ? "🔕 Отписал. Вернуться — /start" : "Ты и так не подписан. Подписаться — /start",
    );
    return;
  }

  if (command === "/list") {
    const cards = await currentCards(env);
    if (cards === null) {
      await text(env, chatId, "Не могу получить список, попробуй через пару минут.");
    } else if (cards.length === 0) {
      await text(env, chatId, "Сейчас нет ни одной квартиры под фильтр. Как появится — сразу напишу.");
    } else {
      await text(env, chatId, `Сейчас подходит квартир: <b>${cards.length}</b>`);
      for (const card of cards.slice(0, MAX_CARDS)) await send(env, chatId, card);
      if (cards.length > MAX_CARDS) {
        await text(env, chatId, `…и ещё ${cards.length - MAX_CARDS} — смотри на сайте.`);
      }
    }
    return;
  }

  await text(env, chatId, INTRO);
}

// ---------------------------------------------------------------------------
// Рассылка от GitHub Actions
// ---------------------------------------------------------------------------

async function handleBroadcast(env, request) {
  const payload = await request.json().catch(() => null);
  if (!payload || !payload.cards || !payload.cards.length) {
    return new Response("нужен непустой cards[]", { status: 400 });
  }

  const targets = await subscribers(env);
  let delivered = 0;
  let dropped = 0;

  for (const chatId of targets) {
    if (payload.header) await text(env, chatId, payload.header);

    let alive = true;
    for (const card of payload.cards.slice(0, MAX_CARDS)) {
      alive = (await send(env, chatId, card)).delivered;
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

  return Response.json({ subscribers: targets.length, delivered, dropped });
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

  let edited = 0;
  let sent = 0;

  for (const flat of payload.flats) {
    const listed = await env.SUBS.list({ prefix: `msg:${flat.id}:` });

    for (const key of listed.keys) {
      const chatId = key.name.split(":")[2];
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

    // квартиру никому не отправляли (например, была в выдаче до запуска бота)
    if (!listed.keys.length) {
      for (const chatId of await subscribers(env)) {
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

    // Диагностика настройки: только факт наличия, без значений.
    // Помогает понять, доехали ли до воркера переменные и привязка KV.
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
      const count = (await subscribers(env)).length;
      return new Response(`Трекер квартир жив. Подписчиков: ${count}\n`);
    }

    return new Response("не найдено", { status: 404 });
  },
};
