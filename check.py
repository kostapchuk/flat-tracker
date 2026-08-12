#!/usr/bin/env python3
"""
Трекер квартир на newbor.by.

Раз в N минут забирает страницу подбора квартир с сохранённым фильтром,
парсит карточки квартир и шлёт в Telegram сообщение о новых.

Зависимостей нет — только стандартная библиотека.
"""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "tracker.log")

# Фильтр по умолчанию: любой дом, площадь от 70 м², цена/этаж любые,
# статус «Свободна» (забронированные скрыты).
# Верхние границы не передаём намеренно — иначе квартира больше текущего
# максимума площади не попадёт в выдачу.
DEFAULT_FILTER_URL = (
    "https://newbor.by/podbor-kvartiry/"
    "?FILTER_NAME=arrFlatFilter&link=main&IBLOCK_ID=29&IBLOCK_SECTION_ID="
    "&booked=hide"
    "&arrFlatFilter_139_MIN=70"
    "&arrFlatFilter_144_4252452532=Y"
    "&set_filter=%D0%9F%D0%BE%D0%BA%D0%B0%D0%B7%D0%B0%D1%82%D1%8C+%D0%BA%D0%B2%D0%B0%D1%80%D1%82%D0%B8%D1%80%D1%8B"
)

SITE = "https://newbor.by"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_PAGES = 20
MAX_PHOTO_MESSAGES = 10  # больше — уже спам, остальное уйдёт списком
BYN_GLYPH = ""  # символ бел. рубля из шрифта сайта

log = logging.getLogger("flat-tracker")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def load_dotenv(path):
    """Простой парсер .env — чтобы не тащить python-dotenv."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def setup_logging(verbose):
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if verbose or sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# --------------------------------------------------------------------------- #
# fetch + parse
# --------------------------------------------------------------------------- #

def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def with_page(url, page):
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "PAGEN_1"]
    if page > 1:
        query.append(("PAGEN_1", str(page)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def results_block(page_html):
    """Кусок HTML со списком карточек. None — если разметка не опознана."""
    start = page_html.find('id="getHtml"')
    if start < 0:
        return None
    end = page_html.find('<div class="div_form">', start)
    return page_html[start:end if end > 0 else len(page_html)]


def clean(text):
    text = re.sub(r"<sup>(.*?)</sup>", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_card(card_html):
    m = re.search(r'href="(/genplan/[^"]*?kvartira_(\d+)/)"', card_html)
    if not m:
        return None
    url, flat_id = m.group(1), m.group(2)

    def grab(pattern, default=""):
        found = re.search(pattern, card_html, re.S)
        return clean(found.group(1)) if found else default

    flat = {
        "id": flat_id,
        "url": SITE + url,
        "title": grab(r'flat-card__title js-flat-link"[^>]*>(.*?)</a>'),
        "rooms": grab(r'flat-card__main_min-rooms">(.*?)</span>'),
        "floor": grab(r'flat-card__main_min-floor">(.*?)</span>'),
        "area": grab(r'flat-card__main_min-size">(.*?)</span>'),
        "badge": grab(r'flat-card__hash_text">(.*?)</span>'),
    }

    props, features = {}, []
    for name, value in re.findall(
        r'flat-card__main_prop-name">(.*?)</span>\s*<(?:span|a)[^>]*>(.*?)</(?:span|a)>',
        card_html, re.S,
    ):
        name, value = clean(name), clean(value)
        if name == "Особенность":
            if value not in features:
                features.append(value)
        else:
            props.setdefault(name, value)
    flat["props"] = props
    flat["features"] = features

    usd_block = re.search(r'_cur usd.*?(?=_cur byn|$)', card_html, re.S)
    byn_block = re.search(r'_cur byn.*', card_html, re.S)

    def price(block, pattern):
        if not block:
            return ""
        found = re.search(pattern, block.group(0), re.S)
        return clean(found.group(1)).replace(BYN_GLYPH, "BYN").strip() if found else ""

    flat["price_usd"] = re.sub(r"[^\d]", "", price(usd_block, r'flat-card__prices_main">(.*?)</span>'))
    flat["price_byn"] = re.sub(r"[^\d]", "", price(byn_block, r'flat-card__prices_main">(.*?)</span>'))
    flat["per_m2_usd"] = price(usd_block, r'flat-card__prices_m2">(.*?)</span>')
    flat["per_m2_byn"] = price(byn_block, r'flat-card__prices_m2">(.*?)</span>')

    # Первая картинка карточки — планировка (файл вида 3K_78.23.png),
    # дальше идут рендеры дома и двора.
    images, seen_img = [], set()
    for src in re.findall(r'<img[^>]*src="(/upload/[^"]+)"', card_html):
        if src not in seen_img:
            seen_img.add(src)
            images.append(SITE + src)
    plan = next((i for i in images if re.search(r"/\d+K[_-][\d.,]+\.\w+$", i)), None)
    flat["plan_image"] = plan or (images[0] if images else "")
    return flat


def scrape(filter_url):
    """Все квартиры по фильтру. Бросает RuntimeError, если разметка не распозналась."""
    flats, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        page_html = fetch(with_page(filter_url, page))
        block = results_block(page_html)
        if block is None:
            raise RuntimeError("не найден контейнер #getHtml — вероятно, вёрстка сайта изменилась")

        if "Квартир не найдено" in block:
            log.info("страница %d: квартир не найдено", page)
            break

        cards = re.split(r'(?=<div class="flat js-flat-wrap")', block)[1:]
        if not cards:
            if page == 1:
                raise RuntimeError("нет ни карточек, ни блока «Квартир не найдено» — проверь селекторы")
            break

        added = 0
        for card in cards:
            flat = parse_card(card)
            if flat and flat["id"] not in seen:
                seen.add(flat["id"])
                flats.append(flat)
                added += 1
        log.info("страница %d: карточек %d, новых %d", page, len(cards), added)
        if added == 0:
            break
        time.sleep(1)
    return flats


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #

CAPTION_LIMIT = 1024


class TelegramError(RuntimeError):
    def __init__(self, code, description):
        super().__init__(f"Telegram API {code}: {description}")
        self.code = code
        self.description = description


def telegram_api(method, params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("не задан TELEGRAM_BOT_TOKEN (см. .env)")

    payload = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=payload)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:300]
        try:
            body = json.loads(body).get("description", body)
        except ValueError:
            pass
        raise TelegramError(exc.code, body) from exc


def telegram_send(text, chat_id, disable_preview=True):
    return telegram_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false",
    })


def telegram_send_photo(photo_url, caption, chat_id):
    return telegram_api("sendPhoto", {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    })


def send_flat(flat, chat_id, header=""):
    """Планировка фото + вся инфа подписью. Если фото не ушло — просто текстом."""
    text = (header + "\n\n" if header else "") + format_flat(flat)
    photo = flat.get("plan_image")

    if photo and len(text) <= CAPTION_LIMIT:
        # оригинал крупнее, но если его нет — берём превью из карточки
        candidates = [re.sub(r"/resize_cache(/iblock/[^/]+/[^/]+)/\d+_\d+_\d+/", r"\1/", photo)]
        if photo not in candidates:
            candidates.append(photo)
        for url in candidates:
            try:
                telegram_send_photo(url, text, chat_id)
                return
            except TelegramError as exc:
                if exc.code == 403:  # пользователь заблокировал бота — выше разберутся
                    raise
                log.warning("фото не ушло (%s): %s", url, exc)

    telegram_send(text, chat_id, disable_preview=False)


def esc(value):
    return html.escape(str(value or ""))


def money(value, suffix):
    if not value:
        return ""
    return f"{int(value):,}".replace(",", " ") + f" {suffix}"


def format_flat(flat):
    props = dict(flat.get("props", {}))
    lines = [f"🏠 <b>{esc(flat['title'])}</b>"]

    facts = " · ".join(x for x in (flat.get("rooms"), flat.get("area"), flat.get("floor")) if x)
    if facts:
        lines.append(esc(facts))
    if flat.get("badge"):
        lines.append(f"✨ {esc(flat['badge'])}")

    price = " / ".join(x for x in (money(flat.get("price_usd"), "$"),
                                   money(flat.get("price_byn"), "BYN")) if x)
    if price:
        lines.append(f"💰 <b>{esc(price)}</b>")
    per_m2 = " / ".join(x for x in (flat.get("per_m2_usd"), flat.get("per_m2_byn")) if x)
    if per_m2:
        lines.append(f"📐 {esc(per_m2)}")

    # сначала знакомые поля в понятном порядке, потом всё остальное из карточки
    lines.append("")
    for key in ("Комнаты", "Площадь", "Этаж", "Подъезд", "Расположение", "Отделка", "Срок сдачи"):
        value = props.pop(key, "")
        if value:
            lines.append(f"{esc(key)}: <b>{esc(value)}</b>")
    for key, value in props.items():
        if value:
            lines.append(f"{esc(key)}: <b>{esc(value)}</b>")
    if flat.get("features"):
        lines.append("Особенности: " + esc(", ".join(flat["features"])))

    lines.append(f'\n<a href="{esc(flat["url"])}">Открыть на сайте</a>')
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# подписчики и команды бота
# --------------------------------------------------------------------------- #

BOT_COMMANDS = [
    {"command": "start", "description": "Подписаться на новые квартиры"},
    {"command": "stop", "description": "Отписаться от уведомлений"},
    {"command": "list", "description": "Что подходит прямо сейчас"},
]

# Постоянно работающего процесса нет: бот отвечает только в момент очередной
# проверки сайта. Пишем об этом прямо, чтобы никто не ждал ответа за секунду.
DELAY_NOTE = (
    "⏳ Бот не сидит онлайн постоянно: он просыпается раз в несколько минут, "
    "чтобы проверить сайт, и тогда же отвечает на команды. "
    "Так что пауза до ~5 минут — это нормально, а не поломка."
)

INTRO = (
    "Слежу за квартирами в «Новой Боровой» и присылаю новые, как только они "
    "появляются в продаже.\n\n"
    "Что отслеживаю: свободные квартиры от 70 м², любой дом, любые цена и этаж.\n\n"
    "/start — подписаться\n"
    "/stop — отписаться\n"
    "/list — что подходит прямо сейчас\n\n" + DELAY_NOTE
)


def subscribers(state):
    """Список chat_id получателей. Владелец из .env подписан всегда."""
    subs = state.setdefault("subscribers", {})
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if owner and owner not in subs:
        subs[owner] = {"since": time.strftime("%Y-%m-%d %H:%M:%S"), "note": "владелец (из настроек)"}
    return subs


def broadcast(state, send, dry_run=False):
    """send(chat_id) для каждого подписчика; кто заблокировал бота — отписывается."""
    dropped = []
    for chat_id in list(subscribers(state)):
        if dry_run:
            send(chat_id)
            continue
        try:
            send(chat_id)
        except TelegramError as exc:
            # 403 — бот заблокирован, 400 chat not found — чат удалён
            if exc.code in (400, 403):
                dropped.append(chat_id)
                log.warning("отписываем %s: %s", chat_id, exc.description)
            else:
                log.error("не доставлено %s: %s", chat_id, exc)
        time.sleep(0.5)
    for chat_id in dropped:
        state["subscribers"].pop(chat_id, None)


def handle_commands(state, flats):
    """Разбирает новые сообщения боту. flats=None, если сайт не ответил."""
    try:
        updates = telegram_api("getUpdates", {
            "offset": state.get("tg_offset", 0),
            "timeout": 0,
            "allowed_updates": '["message"]',
        }).get("result", [])
    except (TelegramError, urllib.error.URLError) as exc:
        log.error("не смог забрать сообщения бота: %s", exc)
        return

    # меню перерегистрируем, когда список команд поменялся
    commands_json = json.dumps(BOT_COMMANDS, ensure_ascii=False, sort_keys=True)
    if state.get("commands_registered") != commands_json:
        try:
            telegram_api("setMyCommands", {"commands": commands_json})
            state["commands_registered"] = commands_json
            log.info("меню команд обновлено")
        except TelegramError as exc:
            log.warning("не удалось задать меню команд: %s", exc)

    subs = subscribers(state)
    for update in updates:
        state["tg_offset"] = update["update_id"] + 1
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (message.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            continue

        command = text.split()[0].split("@")[0].lower()
        who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
        log.info("команда %s от %s (%s)", command, who or chat_id, chat_id)

        try:
            if command == "/start":
                already = chat_id in subs
                subs[chat_id] = {
                    "since": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "name": who,
                    "username": chat.get("username", ""),
                }
                if already:
                    telegram_send("Ты уже подписан 👌\n\n" + INTRO, chat_id)
                else:
                    telegram_send("✅ Подписал! Пришлю, как только появится новая "
                                  "подходящая квартира.\n\n" + INTRO, chat_id)
                    if flats:
                        telegram_send(f"Сейчас под фильтр подходит: {len(flats)}. Показываю…", chat_id)
                        for flat in flats[:MAX_PHOTO_MESSAGES]:
                            send_flat(flat, chat_id)
                            time.sleep(0.5)

            elif command == "/stop":
                if subs.pop(chat_id, None):
                    telegram_send("🔕 Отписал. Вернуться — /start", chat_id)
                else:
                    telegram_send("Ты и так не подписан. Подписаться — /start", chat_id)

            elif command == "/list":
                if flats is None:
                    telegram_send("Сайт сейчас не отвечает, попробуй позже.", chat_id)
                elif not flats:
                    telegram_send("Сейчас нет ни одной квартиры под фильтр. "
                                  "Как появится — сразу напишу.", chat_id)
                else:
                    telegram_send(f"Сейчас подходит квартир: <b>{len(flats)}</b>", chat_id)
                    for flat in flats[:MAX_PHOTO_MESSAGES]:
                        send_flat(flat, chat_id)
                        time.sleep(0.5)
                    if len(flats) > MAX_PHOTO_MESSAGES:
                        telegram_send(f"…и ещё {len(flats) - MAX_PHOTO_MESSAGES} — "
                                      "смотри на сайте.", chat_id)

            else:
                telegram_send(INTRO, chat_id)

        except TelegramError as exc:
            log.error("ответ на %s не доставлен: %s", command, exc)


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"flats": {}, "fails": 0, "last_ok": None}
    with open(STATE_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def run(args):
    filter_url = os.environ.get("FILTER_URL") or DEFAULT_FILTER_URL
    state = load_state()
    known = state.get("flats", {})

    error = None
    try:
        flats = scrape(filter_url)
    except Exception as exc:  # сеть упала / вёрстка поехала — состояние не трогаем
        flats, error = None, exc

    # команды бота разбираем в любом случае — даже если сайт лежит
    if not args.dry_run:
        handle_commands(state, flats)

    if error is not None:
        state["fails"] = state.get("fails", 0) + 1
        save_state(state)
        log.error("ошибка проверки (%d подряд): %s", state["fails"], error)
        # молчим о разовых сбоях, но о стабильной поломке сообщаем
        if state["fails"] in (3, 30) and not args.dry_run:
            try:
                broadcast(state, lambda chat_id: telegram_send(
                    f"⚠️ Трекер квартир не может проверить страницу "
                    f"({state['fails']} раза подряд):\n<code>{esc(error)}</code>", chat_id))
                save_state(state)
            except Exception as send_exc:
                log.error("и в Telegram не ушло: %s", send_exc)
        return 1

    current = {f["id"]: f for f in flats}
    new_ids = [i for i in current if i not in known]
    gone_ids = [i for i in known if i not in current]
    log.info("всего по фильтру: %d, новых: %d, пропало: %d",
             len(current), len(new_ids), len(gone_ids))

    state.update({"flats": current, "fails": 0, "last_ok": time.strftime("%Y-%m-%d %H:%M:%S")})

    if args.init:
        save_state(state)
        log.info("состояние проинициализировано без уведомлений")
        return 0

    if new_ids:
        # каждая квартира — отдельным сообщением с планировкой
        for number, flat_id in enumerate(new_ids[:MAX_PHOTO_MESSAGES], start=1):
            flat = current[flat_id]
            header = ("🔔 <b>Новая квартира по твоему фильтру!</b>" if len(new_ids) == 1
                      else f"🔔 <b>Новая квартира {number} из {len(new_ids)}</b>")
            if args.dry_run:
                print(f"[фото: {flat.get('plan_image') or '—'}]")
                print(header + "\n\n" + format_flat(flat) + "\n")
            else:
                broadcast(state, lambda chat_id, f=flat: send_flat(f, chat_id, header))
                log.info("разослана квартира %s (%s)", flat_id, flat.get("title"))
                time.sleep(1)  # не упираемся в лимиты Telegram

        overflow = new_ids[MAX_PHOTO_MESSAGES:]
        if overflow:
            text = (f"…и ещё {len(overflow)} новых:\n" +
                    "\n".join(f'• <a href="{esc(current[i]["url"])}">{esc(current[i]["title"])}</a>'
                              f' — {esc(current[i].get("area"))}, {esc(money(current[i].get("price_usd"), "$"))}'
                              for i in overflow))
            if args.dry_run:
                print(text)
            else:
                broadcast(state, lambda chat_id: telegram_send(text, chat_id))

    if gone_ids and os.environ.get("NOTIFY_REMOVED", "0") == "1":
        text = ("➖ <b>Пропали из выдачи (забронированы/проданы):</b>\n" +
                "\n".join(f"• {esc(known[i].get('title', i))}" for i in gone_ids))
        if args.dry_run:
            print(text)
        else:
            broadcast(state, lambda chat_id: telegram_send(text, chat_id))

    if not args.dry_run:  # прогон «вхолостую» не должен помечать квартиры как виденные
        save_state(state)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Трекер квартир newbor.by → Telegram")
    parser.add_argument("--init", action="store_true",
                        help="запомнить текущую выдачу, ничего не отправляя")
    parser.add_argument("--dry-run", action="store_true",
                        help="печатать сообщения в консоль вместо Telegram")
    parser.add_argument("--test-telegram", action="store_true",
                        help="отправить тестовое сообщение и выйти")
    parser.add_argument("--list", action="store_true",
                        help="показать текущую выдачу и выйти")
    parser.add_argument("--subscribers", action="store_true",
                        help="показать подписчиков и выйти")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv(os.path.join(BASE_DIR, ".env"))
    setup_logging(args.verbose)

    if args.test_telegram:
        telegram_send("✅ Трекер квартир на связи.", os.environ["TELEGRAM_CHAT_ID"])
        print("Отправлено.")
        return 0

    if args.subscribers:
        for chat_id, info in load_state().get("subscribers", {}).items():
            print(f"{chat_id}  {info.get('name', '')} @{info.get('username', '')}"
                  f"  с {info.get('since', '?')}  {info.get('note', '')}")
        return 0

    if args.list:
        for flat in scrape(os.environ.get("FILTER_URL") or DEFAULT_FILTER_URL):
            print(f"{flat['id']}  {flat['title']}  {flat['area']}  "
                  f"{money(flat['price_usd'], '$')}  {flat['url']}")
        return 0

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
