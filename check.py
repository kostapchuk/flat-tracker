#!/usr/bin/env python3
"""
Трекер квартир на newbor.by.

Обходит подборки квартир: общий фильтр плюс личные фильтры подписчиков
(их список забирает у бота), и сообщает о появившихся и об ушедших.

Зависимостей нет — только стандартная библиотека.
"""

import argparse
import calendar
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
BOT_USER_AGENT = "flat-tracker/1.0 (+https://github.com/kostapchuk/flat-tracker)"
DEFAULT_FILTER_ID = "default"  # общий фильтр для тех, кто не завёл свой
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
                # готовый текст кладём в state.json: бот берёт его оттуда для
                # /list и не дублирует у себя вёрстку сообщения
                flat["text"] = format_flat(flat)
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


def split_title(title):
    """«Дом № 7.84, кв № 21» → ('7.84', '21')"""
    house = re.search(r"Дом\s*№?\s*([\d.]+)", title or "")
    number = re.search(r"кв\.?\s*№?\s*(\d+)", title or "")
    return (house.group(1) if house else (title or "")), (number.group(1) if number else "")


def format_flat(flat, retired=False):
    """
    Главное — по строке на факт, в порядке, который важен при выборе:
    дом, метраж, цена метра, номер квартиры, этаж, подъезд, отделка, сдача.
    Всё прочее уходит в «Дополнительно», чтобы не мешало смотреть.
    """
    props = dict(flat.get("props", {}))
    house, number = split_title(flat.get("title", ""))
    lines = []

    if retired:
        lines.append(f"❌ <s>{esc(flat.get('title', ''))}</s>")
        lines.append("<b>Забронирована или продана</b>\n")

    lines.append(f"🏠 <b>Дом {esc(house)}</b>")
    if flat.get("area"):
        lines.append(f"📐 <b>{esc(flat['area'])}</b>")
    per_m2 = " · ".join(x for x in (flat.get("per_m2_usd"), flat.get("per_m2_byn")) if x)
    if per_m2:
        lines.append(f"💵 {esc(per_m2)}")
    if number:
        lines.append(f"🔑 Квартира № {esc(number)}")
    if flat.get("floor"):
        lines.append(f"🏢 {esc(flat['floor'])}")
    if props.get("Подъезд"):
        lines.append(f"🚪 Подъезд {esc(props['Подъезд'])}")
    finish = props.get("Отделка", "")
    if finish:
        # в главном блоке важен сам факт, а какая именно отделка — ниже
        lines.append("🧱 Без отделки" if "без отделки" in finish.lower() else "🧱 С отделкой")
    if props.get("Срок сдачи"):
        lines.append(f"📅 Сдача: {esc(props['Срок сдачи'])}")

    extra = []
    price = " / ".join(x for x in (money(flat.get("price_usd"), "$"),
                                   money(flat.get("price_byn"), "BYN")) if x)
    if price:
        extra.append(f"Стоимость: <b>{esc(price)}</b>")
    if props.get("Комнаты") or flat.get("rooms"):
        extra.append(f"Комнат: {esc(props.get('Комнаты') or flat.get('rooms'))}")
    if finish and "без отделки" not in finish.lower():
        extra.append(f"Отделка: {esc(re.sub(r'^Отделка ', '', finish))}")
    if props.get("Расположение"):
        extra.append(f"Квартал: {esc(props['Расположение'])}")
    # про отделку уже сказано выше — в особенностях и плашке её не повторяем
    if flat.get("badge") and "отделк" not in flat["badge"].lower():
        extra.append(esc(flat["badge"]))
    features = [f for f in flat.get("features", []) if "отделк" not in f.lower()]
    if features:
        extra.append("Особенности: " + esc(", ".join(features)))
    for key, value in props.items():  # всё, что не разобрали выше
        if value and key not in ("Комнаты", "Площадь", "Этаж", "Подъезд",
                                 "Расположение", "Отделка", "Срок сдачи"):
            extra.append(f"{esc(key)}: {esc(value)}")

    if extra:
        lines.append("\n<b>Дополнительно</b>")
        lines.extend(extra)

    lines.append(f'\n<a href="{esc(flat.get("url", ""))}">Открыть на сайте</a>')
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# рассылка
# --------------------------------------------------------------------------- #

def bot_call(path, payload):
    """POST боту на Cloudflare Workers. None — если бот не настроен."""
    base = os.environ.get("BOT_URL", "").strip().rstrip("/")
    secret = os.environ.get("BROADCAST_SECRET", "").strip()
    if not base or not secret:
        return None

    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {secret}",
            # без своего User-Agent Cloudflare режет запрос по сигнатуре
            # клиента (ошибка 1010) — до воркера он даже не доходит
            "user-agent": BOT_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"бот ответил {exc.code} на {path}: {body}") from None


def heartbeat(summary):
    """
    Отчёт владельцу о прошедшей проверке.
    HEARTBEAT: off — молчим, edit — переписываем одно сообщение, every — новое.
    """
    mode = os.environ.get("HEARTBEAT", "edit").strip().lower()
    if mode == "off":
        return

    # в отчёте человеку — местное время; в state.json остаётся UTC,
    # чтобы машинам было однозначно
    offset = int(os.environ.get("REPORT_UTC_OFFSET", "3"))
    stamp = time.strftime("%H:%M", time.gmtime(time.time() + offset * 3600))
    text = f"✅ <b>Проверка пройдена</b> · {stamp} МСК\n{summary}"
    try:
        if bot_configured():
            bot_call("/heartbeat", {"text": text, "mode": mode})
        else:
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if chat_id:
                telegram_send(text, chat_id)
    except Exception as exc:  # отчёт не должен ронять проверку
        log.warning("не удалось отправить отчёт о проверке: %s", exc)


def as_card(flat):
    """Квартира в виде, в котором её отправляет бот."""
    return {
        "id": flat.get("id", ""),
        "text": flat.get("text") or format_flat(flat),
        "photo": flat.get("plan_image", ""),
    }


def bot_configured():
    return bool(os.environ.get("BOT_URL", "").strip()
                and os.environ.get("BROADCAST_SECRET", "").strip())


def bot_get(path):
    """GET у бота (список фильтров). None — если бот не настроен."""
    base = os.environ.get("BOT_URL", "").strip().rstrip("/")
    secret = os.environ.get("BROADCAST_SECRET", "").strip()
    if not base or not secret:
        return None
    req = urllib.request.Request(f"{base}{path}?key={urllib.parse.quote(secret)}",
                                 headers={"user-agent": BOT_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        raise RuntimeError(f"бот ответил {exc.code} на {path}: {body}") from None


def notify(cards, header="", chats=None, filter_id=DEFAULT_FILTER_ID):
    """
    Отдаём карточки боту — он разошлёт их адресатам этого фильтра.
    Если бот не настроен (локальный прогон), шлём напрямую владельцу.
    """
    if not bot_configured():
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            raise RuntimeError("не настроен ни BOT_URL, ни TELEGRAM_CHAT_ID")
        log.info("бот не настроен — шлю напрямую в чат %s", chat_id)
        if header:
            telegram_send(header, chat_id)
        for card in cards:
            send_flat({"text": card["text"], "plan_image": card.get("photo")}, chat_id)
        return

    # по одной карточке за запрос: у Cloudflare на бесплатном тарифе
    # не больше 50 подзапросов на один входящий запрос
    for number, card in enumerate(cards):
        result = bot_call("/broadcast", {
            "header": header if number == 0 else "",
            "cards": [card],
            "chats": chats or [],
            "filter_id": filter_id,
        })
        log.info("  карточка %d/%d: адресатов %s, доставлено %s, отписано %s",
                 number + 1, len(cards), result.get("targets"),
                 result.get("delivered"), result.get("dropped"))


def notify_retired(flats, chats=None, filter_id=DEFAULT_FILTER_ID):
    """Квартиры ушли из выдачи: правим уже отправленные сообщения."""
    items = [{
        "id": flat.get("id", ""),
        "text": format_flat(flat, retired=True),
        "short": (f"❌ <s>{esc(flat.get('title', 'Квартира'))}</s> — "
                  f'забронирована или продана\n<a href="{esc(flat.get("url", ""))}">карточка</a>'),
    } for flat in flats]

    if not bot_configured():
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            return
        for item in items:
            telegram_send(item["short"], chat_id)
        return

    for item in items:
        result = bot_call("/retire", {
            "flats": [item],
            "chats": chats or [],
            "filter_id": filter_id,
        })
        log.info("  %s: отредактировано %s, отправлено заново %s",
                 item["id"], result.get("edited"), result.get("sent"))


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def load_state():
    """
    Состояние: общий каталог квартир + по фильтру список подходящих id.
    Каталог общий, потому что одна квартира обычно попадает в несколько
    фильтров, а карточка у неё одна.
    """
    if not os.path.exists(STATE_FILE):
        return {"catalog": {}, "filters": {}, "fails": 0, "last_ok_utc": None}
    with open(STATE_FILE, encoding="utf-8") as fh:
        state = json.load(fh)

    # подписчики и очередь сообщений переехали в бота на Cloudflare
    for obsolete in ("subscribers", "tg_offset", "commands_registered", "last_ok"):
        state.pop(obsolete, None)

    # переезд со старой схемы (один фильтр, ключ "flats")
    if "flats" in state:
        flats = state.pop("flats")
        state.setdefault("catalog", {}).update(flats)
        state.setdefault("filters", {})[DEFAULT_FILTER_ID] = {
            "url": DEFAULT_FILTER_URL,
            "ids": list(flats),
        }
    state.setdefault("catalog", {})
    state.setdefault("filters", {})
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def collect_jobs():
    """
    Что обходить: общий фильтр + личные фильтры подписчиков.
    Возвращает список (filter_id, url, chats, name).
    """
    info = bot_get("/filters") if bot_configured() else None
    if info is None:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        return [(DEFAULT_FILTER_ID, DEFAULT_FILTER_URL,
                 [chat_id] if chat_id else [], "общий")]

    jobs = [(DEFAULT_FILTER_ID, DEFAULT_FILTER_URL, info.get("default_chats", []), "общий")]
    for flt in info.get("filters", []):
        jobs.append((flt["id"], flt["url"], [flt["chat_id"]], flt.get("name") or flt["id"]))
    return jobs


def run(args):
    state = load_state()
    catalog = state["catalog"]
    known_filters = state["filters"]

    try:
        jobs = collect_jobs()
    except Exception as exc:
        log.error("не смог забрать список фильтров у бота: %s", exc)
        return 1

    # свой фильтр из .env перебивает общий — удобно для локальных прогонов
    override = os.environ.get("FILTER_URL", "").strip()
    if override:
        jobs = [(fid, override if fid == DEFAULT_FILTER_ID else url, chats, name)
                for fid, url, chats, name in jobs]

    log.info("фильтров к обходу: %d", len(jobs))

    fresh_catalog, fresh_filters = {}, {}
    scraped, failures = {}, []
    total_new = total_gone = 0

    for filter_id, url, chats, name in jobs:
        if url not in scraped:  # одинаковые ссылки обходим один раз
            try:
                scraped[url] = scrape(url)
            except Exception as exc:
                scraped[url] = exc
        flats = scraped[url]

        if isinstance(flats, Exception):
            log.error("фильтр «%s»: %s", name, flats)
            failures.append(name)
            # выдачу не трогаем, иначе после сбоя всё покажется новым
            if filter_id in known_filters:
                fresh_filters[filter_id] = known_filters[filter_id]
                for flat_id in known_filters[filter_id].get("ids", []):
                    if flat_id in catalog:
                        fresh_catalog[flat_id] = catalog[flat_id]
            continue

        current = {flat["id"]: flat for flat in flats}
        fresh_catalog.update(current)
        fresh_filters[filter_id] = {"url": url, "ids": list(current)}

        previous = set(known_filters.get(filter_id, {}).get("ids", []))
        first_run = filter_id not in known_filters
        new_ids = [i for i in current if i not in previous]
        gone_ids = [i for i in previous if i not in current]
        log.info("фильтр «%s»: всего %d, новых %d, ушло %d%s",
                 name, len(current), len(new_ids), len(gone_ids),
                 " (первый обход)" if first_run else "")
        total_new += len(new_ids)
        total_gone += len(gone_ids)

        if args.init or not chats:
            continue

        if new_ids:
            header = ("🔔 <b>Новая квартира</b>" if len(new_ids) == 1
                      else f"🔔 <b>Новых квартир: {len(new_ids)}</b>")
            if filter_id != DEFAULT_FILTER_ID:
                header += f" · фильтр «{esc(name)}»"
            if first_run:
                header = f"📋 <b>Фильтр «{esc(name)}»</b>: подходит {len(new_ids)}"

            cards = [as_card(current[i]) for i in new_ids[:MAX_PHOTO_MESSAGES]]
            overflow = new_ids[MAX_PHOTO_MESSAGES:]
            if overflow:
                cards.append({"text": f"…и ещё {len(overflow)}:\n" + "\n".join(
                    f'• <a href="{esc(current[i]["url"])}">{esc(current[i]["title"])}</a>'
                    f' — {esc(current[i].get("area"))}, {esc(money(current[i].get("price_usd"), "$"))}'
                    for i in overflow)})

            if args.dry_run:
                print(f"\n=== {name}: {header}")
                for card in cards:
                    print(f"[фото: {card.get('photo') or '—'}]\n{card['text']}\n")
            else:
                notify(cards, header, chats=chats, filter_id=filter_id)

        if gone_ids:
            gone = [catalog[i] for i in gone_ids if i in catalog]
            if gone and not args.dry_run:
                notify_retired(gone, chats=chats, filter_id=filter_id)
            elif gone:
                print(f"\n=== {name}: ушли из выдачи")
                for flat in gone:
                    print(format_flat(flat, retired=True))

    if failures and len(failures) == len(jobs):
        state["fails"] = state.get("fails", 0) + 1
        save_state(state)
        log.error("ни один фильтр не обошёлся (%d раз подряд)", state["fails"])
        if state["fails"] in (3, 30) and not args.dry_run:
            try:
                notify([{"text": "⚠️ Трекер квартир не может проверить сайт "
                                 f"({state['fails']} раза подряд). Посмотри логи."}])
            except Exception as exc:
                log.error("и в Telegram не ушло: %s", exc)
        return 1

    # фильтры, которых в этом обходе не было (бот их не отдал), сохраняем как
    # есть — иначе при возвращении фильтр посчитается новым и пришлёт всё заново
    for filter_id, stored in known_filters.items():
        if filter_id not in fresh_filters:
            fresh_filters[filter_id] = stored
            for flat_id in stored.get("ids", []):
                if flat_id in catalog:
                    fresh_catalog.setdefault(flat_id, catalog[flat_id])

    state["catalog"] = fresh_catalog
    state["filters"] = fresh_filters
    state["fails"] = 0
    if not args.dry_run and not args.init:
        heartbeat(f"Фильтров: {len(jobs)} · квартир в выдаче: {len(fresh_catalog)} · "
                  f"новых: {total_new} · ушло: {total_gone}" +
                  (f"\n⚠️ не обошлись: {', '.join(failures)}" if failures else ""))
    state["last_ok_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not args.dry_run:
        save_state(state)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Трекер квартир newbor.by → Telegram")
    parser.add_argument("--init", action="store_true",
                        help="запомнить текущую выдачу, ничего не отправляя")
    parser.add_argument("--dry-run", action="store_true",
                        help="печатать сообщения в консоль вместо Telegram")
    parser.add_argument("--test-telegram", action="store_true",
                        help="отправить тестовое сообщение напрямую и выйти")
    parser.add_argument("--ping", action="store_true",
                        help="проверить связку с ботом: тестовая рассылка и выход")
    parser.add_argument("--list", action="store_true",
                        help="показать выдачу общего фильтра и выйти")
    parser.add_argument("--filters", action="store_true",
                        help="показать фильтры, которые придут из бота, и выйти")
    parser.add_argument("--if-stale", type=int, metavar="МИНУТ", default=0,
                        help="работать, только если последняя удачная проверка "
                             "старше этого срока (страхующий запуск)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv(os.path.join(BASE_DIR, ".env"))
    setup_logging(args.verbose)

    if args.test_telegram:
        telegram_send("✅ Трекер квартир на связи.", os.environ["TELEGRAM_CHAT_ID"])
        print("Отправлено.")
        return 0

    if args.ping:
        where = "через бота" if bot_configured() else "напрямую (BOT_URL не задан!)"
        notify([{"text": "🧪 Проверка связи: GitHub Actions → бот → Telegram.\n"
                         "Если это сообщение пришло, рассылка настроена верно."}],
               header="")
        print(f"Отправлено {where}.")
        return 0

    if args.if_stale:
        last = load_state().get("last_ok_utc")
        if last:
            age = time.time() - calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
            if age < args.if_stale * 60:
                log.info("проверка была %d мин назад — уступаю основному расписанию",
                         age // 60)
                return 0
            log.info("последняя проверка %d мин назад — работаю сам", age // 60)

    if args.filters:
        for filter_id, url, chats, name in collect_jobs():
            print(f"{filter_id:10} {name:24} чатов: {len(chats):2}  {url[:90]}")
        return 0

    if args.list:
        for flat in scrape(os.environ.get("FILTER_URL") or DEFAULT_FILTER_URL):
            print(f"{flat['id']}  {flat['title']}  {flat['area']}  "
                  f"{money(flat['price_usd'], '$')}  {flat['url']}")
        return 0

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
