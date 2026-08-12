/**
 * Telegram-бот трекера квартир. Живёт на Deno Deploy и отвечает мгновенно.
 *
 * Разделение обязанностей:
 *   - этот бот  — общение с людьми: подписки (Deno KV) и команды;
 *   - GitHub Actions — обход сайта раз в N минут; найдя новое, дёргает
 *     POST /broadcast, и бот рассылает карточки подписчикам.
 *
 * Переменные окружения (Deno Deploy → Settings → Environment Variables):
 *   BOT_TOKEN         — токен от @BotFather
 *   WEBHOOK_SECRET    — произвольная строка, ей Telegram подписывает вебхук
 *   BROADCAST_SECRET  — произвольная строка для /broadcast и /setup
 *   OWNER_CHAT_ID     — chat_id владельца, подписан всегда
 *   STATE_URL         — сырой state.json из репозитория (для /list)
 */

const TOKEN = Deno.env.get("BOT_TOKEN") ?? "";
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET") ?? "";
const BROADCAST_SECRET = Deno.env.get("BROADCAST_SECRET") ?? "";
const OWNER_CHAT_ID = Deno.env.get("OWNER_CHAT_ID") ?? "";
const STATE_URL = Deno.env.get("STATE_URL") ??
  "https://raw.githubusercontent.com/kostapchuk/flat-tracker/main/state.json";

const CAPTION_LIMIT = 1024;
const MAX_CARDS = 10;

const kv = await Deno.openKv();

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

// --------------------------------------------------------------------------
// Telegram API
// --------------------------------------------------------------------------

type Card = { id?: string; text: string; photo?: string };
/** delivered=false — чат недоступен, подписчика надо убрать */
type Sent = { delivered: boolean; messageId?: number; photo?: boolean };
type SentMessage = { messageId: number; photo: boolean };

async function api(method: string, payload: unknown): Promise<Response> {
  return await fetch(`https://api.telegram.org/bot${TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function send(chatId: string, card: Card): Promise<Sent> {
  const canCaption = Boolean(card.photo) && card.text.length <= CAPTION_LIMIT;
  const attempts: Array<[string, boolean, unknown]> = canCaption
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
    const response = await api(method, payload);
    if (response.ok) {
      const body = await response.json().catch(() => null);
      const messageId = body?.result?.message_id as number | undefined;
      // запоминаем сообщение: когда квартиру снимут, отредактируем именно его
      if (messageId && card.id) {
        await kv.set(["msg", card.id, chatId], { messageId, photo: isPhoto });
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

async function text(chatId: string, message: string): Promise<Sent> {
  return await send(chatId, { text: message });
}

// --------------------------------------------------------------------------
// Подписчики
// --------------------------------------------------------------------------

type Subscriber = { name?: string; username?: string; since: string };

async function subscribe(chatId: string, info: Subscriber): Promise<boolean> {
  const existing = await kv.get<Subscriber>(["subs", chatId]);
  if (existing.value) return false;
  await kv.set(["subs", chatId], info);
  return true;
}

async function unsubscribe(chatId: string): Promise<boolean> {
  const existing = await kv.get<Subscriber>(["subs", chatId]);
  await kv.delete(["subs", chatId]);
  return Boolean(existing.value);
}

async function subscribers(): Promise<string[]> {
  const ids = new Set<string>();
  if (OWNER_CHAT_ID) ids.add(OWNER_CHAT_ID); // владелец подписан всегда
  for await (const entry of kv.list<Subscriber>({ prefix: ["subs"] })) {
    ids.add(String(entry.key[1]));
  }
  return [...ids];
}

// --------------------------------------------------------------------------
// Текущая выдача — из state.json, который коммитит GitHub Actions
// --------------------------------------------------------------------------

async function currentCards(): Promise<Card[] | null> {
  try {
    const response = await fetch(STATE_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`state.json -> ${response.status}`);
    const state = await response.json();
    return Object.values(state.flats ?? {}).map((flat) => ({
      id: (flat as { id?: string }).id,
      text: (flat as { text?: string }).text ?? "",
      photo: (flat as { plan_image?: string }).plan_image,
    })).filter((card) => card.text);
  } catch (error) {
    console.error("не смог прочитать state.json:", error);
    return null;
  }
}

// --------------------------------------------------------------------------
// Команды
// --------------------------------------------------------------------------

async function handleUpdate(update: Record<string, any>): Promise<void> {
  const message = update.message;
  const chat = message?.chat;
  if (!chat?.id || typeof message.text !== "string") return;

  const chatId = String(chat.id);
  const command = message.text.trim().split(/\s+/)[0].split("@")[0].toLowerCase();
  const name = [chat.first_name, chat.last_name].filter(Boolean).join(" ");
  console.log(`${command} от ${name || chatId} (${chatId})`);

  if (command === "/start") {
    const isNew = await subscribe(chatId, {
      name,
      username: chat.username,
      since: new Date().toISOString(),
    });
    await text(
      chatId,
      (isNew
        ? "✅ Подписал! Пришлю, как только появится новая подходящая квартира.\n\n"
        : "Ты уже подписан 👌\n\n") + INTRO,
    );
    if (isNew) {
      const cards = await currentCards();
      if (cards?.length) {
        await text(chatId, `Сейчас под фильтр подходит: <b>${cards.length}</b>. Показываю…`);
        for (const card of cards.slice(0, MAX_CARDS)) await send(chatId, card);
      }
    }
    return;
  }

  if (command === "/stop") {
    const was = await unsubscribe(chatId);
    await text(
      chatId,
      was ? "🔕 Отписал. Вернуться — /start" : "Ты и так не подписан. Подписаться — /start",
    );
    return;
  }

  if (command === "/list") {
    const cards = await currentCards();
    if (cards === null) {
      await text(chatId, "Не могу получить список, попробуй через пару минут.");
    } else if (cards.length === 0) {
      await text(chatId, "Сейчас нет ни одной квартиры под фильтр. Как появится — сразу напишу.");
    } else {
      await text(chatId, `Сейчас подходит квартир: <b>${cards.length}</b>`);
      for (const card of cards.slice(0, MAX_CARDS)) await send(chatId, card);
      if (cards.length > MAX_CARDS) {
        await text(chatId, `…и ещё ${cards.length - MAX_CARDS} — смотри на сайте.`);
      }
    }
    return;
  }

  await text(chatId, INTRO);
}

// --------------------------------------------------------------------------
// Рассылка от GitHub Actions
// --------------------------------------------------------------------------

async function handleBroadcast(request: Request): Promise<Response> {
  const payload = await request.json().catch(() => null) as
    | { header?: string; cards?: Card[] }
    | null;
  if (!payload?.cards?.length) {
    return new Response("нужен непустой cards[]", { status: 400 });
  }

  const targets = await subscribers();
  let delivered = 0;
  let dropped = 0;

  for (const chatId of targets) {
    if (payload.header) await text(chatId, payload.header);
    let alive = true;
    for (const card of payload.cards.slice(0, MAX_CARDS)) {
      alive = (await send(chatId, card)).delivered;
      if (!alive) break;
    }
    if (alive) {
      delivered++;
    } else {
      dropped++;
      await kv.delete(["subs", chatId]); // заблокировал бота — отписываем
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
async function handleRetire(request: Request): Promise<Response> {
  const payload = await request.json().catch(() => null) as
    | { flats?: Array<{ id: string; text: string; short: string }> }
    | null;
  if (!payload?.flats?.length) {
    return new Response("нужен непустой flats[]", { status: 400 });
  }

  let edited = 0;
  let sent = 0;

  for (const flat of payload.flats) {
    let known = false;

    for await (const entry of kv.list<SentMessage>({ prefix: ["msg", flat.id] })) {
      known = true;
      const chatId = String(entry.key[2]);
      const { messageId, photo } = entry.value;

      const response = await api(
        photo ? "editMessageCaption" : "editMessageText",
        photo
          ? { chat_id: chatId, message_id: messageId, caption: flat.text, parse_mode: "HTML" }
          : { chat_id: chatId, message_id: messageId, text: flat.text, parse_mode: "HTML" },
      );

      if (response.ok) {
        edited++;
      } else {
        console.error(`правка ${flat.id} в ${chatId}: ${(await response.text()).slice(0, 160)}`);
        if ((await text(chatId, flat.short)).delivered) sent++;
      }
      await kv.delete(entry.key);
    }

    // квартиру никому не отправляли (например, была в выдаче до запуска бота)
    if (!known) {
      for (const chatId of await subscribers()) {
        if ((await text(chatId, flat.short)).delivered) sent++;
      }
    }
  }

  return Response.json({ edited, sent });
}

// --------------------------------------------------------------------------
// HTTP
// --------------------------------------------------------------------------

function authorized(request: Request): boolean {
  const url = new URL(request.url);
  const provided = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ??
    url.searchParams.get("key") ?? "";
  return Boolean(BROADCAST_SECRET) && provided === BROADCAST_SECRET;
}

Deno.serve(async (request: Request) => {
  const url = new URL(request.url);

  if (url.pathname === "/webhook" && request.method === "POST") {
    if (WEBHOOK_SECRET &&
        request.headers.get("x-telegram-bot-api-secret-token") !== WEBHOOK_SECRET) {
      return new Response("нет", { status: 401 });
    }
    const update = await request.json().catch(() => null);
    if (update) await handleUpdate(update);
    return new Response("ok"); // Telegram доволен в любом случае
  }

  if (url.pathname === "/broadcast" && request.method === "POST") {
    if (!authorized(request)) return new Response("нет", { status: 401 });
    return await handleBroadcast(request);
  }

  if (url.pathname === "/retire" && request.method === "POST") {
    if (!authorized(request)) return new Response("нет", { status: 401 });
    return await handleRetire(request);
  }

  // Разовая настройка: привязать вебхук к этому адресу и задать меню команд
  if (url.pathname === "/setup") {
    if (!authorized(request)) return new Response("нет", { status: 401 });
    const webhook = await api("setWebhook", {
      url: `${url.origin}/webhook`,
      secret_token: WEBHOOK_SECRET,
      allowed_updates: ["message"],
      drop_pending_updates: false,
    });
    const menu = await api("setMyCommands", { commands: COMMANDS });
    return Response.json({
      webhook: await webhook.json(),
      commands: await menu.json(),
    });
  }

  if (url.pathname === "/") {
    const count = (await subscribers()).length;
    return new Response(`Трекер квартир жив. Подписчиков: ${count}\n`);
  }

  return new Response("не найдено", { status: 404 });
});
