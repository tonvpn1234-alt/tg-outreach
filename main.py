# -*- coding: utf-8 -*-
"""
2ГИС -> Telegram: сбор телефонов компаний по заданным направлениям (нишам)
и рассылка им сообщений в Telegram.

Как работает:
1. Playwright открывает поисковые страницы 2ГИС (по одной на каждую нишу и страницу
   пагинации). Первая страница результатов зашита в HTML (SSR) в блоке
   `var initialState = JSON.parse('...')` — оттуда достаются ID и названия фирм.
2. Для каждой фирмы открывается её страница https://2gis.ru/firm/<id> — в SSR
   этой страницы уже есть полные контакты: телефоны, сайт, соцсети.
3. Фильтры: телефон должен быть российским (по умолчанию только мобильные +79xx,
   телефоны из ссылки Telegram карточки добавляются всегда). Писать можно всем
   компаниям — и с сайтом, и без (REQUIRE_NO_WEBSITE); текст подбирается по нише
   и по наличию сайта.
4. Telethon рассылает сообщения. Если у фирмы в карточке указан Telegram — пишет
   туда в первую очередь (по юзернейму из t.me-ссылки или по номеру из ссылки
   t.me/+7...), иначе — по номерам телефона через импорт контакта. Контакт после
   отправки удаляется. Паузы между сообщениями защищают аккаунт от бана.

Режимы запуска:
    python main.py                     # полный цикл: сбор + рассылка
    python main.py --collect-only      # только сбор лидов (в leads.json)
    python main.py --send-only         # рассылка по уже собранному leads.json
    python main.py --selftest          # тест Telegram: отправка сообщения себе
    python main.py --limit 10          # не более 10 сообщений за запуск
    python main.py --niches "барбершопы, стоматологии" --max-pages 3
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.types import InputPeerUser, InputPhoneContact

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# utf-8-sig: .env сохранён с BOM, обычная загрузка ломает имя первого ключа
load_dotenv(encoding="utf-8-sig")

# Ключи берутся из .env или переменных окружения (в коде их больше нет,
# чтобы репозиторий можно было открыть публично, а секреты хранить в GitHub Secrets)
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "")

# Куда складывать файлы состояния (сессия, лиды, processed/archived).
# В Docker/vps DATA_DIR=/app/data — примонтированный том, файлы не теряются.
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

SESSION_NAME = "session_sender"                     # имя файла сессии Telegram
SESSION_PATH = os.path.join(DATA_DIR, SESSION_NAME)  # без расширения .session
# Строка Telethon-сессии для облака (Render/Railway): задайте переменную
# окружения SESSION_STRING вместо файла сессии. Получить: python main.py --export-session
SESSION_STRING = os.getenv("SESSION_STRING")
LEADS_FILE = os.path.join(DATA_DIR, "leads.json")         # сюда складываются собранные компании
PROCESSED_FILE = os.path.join(DATA_DIR, "processed.txt")  # номера, по которым уже отправляли
ARCHIVED_FILE = os.path.join(DATA_DIR, "archived.txt")    # номера, ссылки на которые уже кинули в «Избранное»
ANALYTICS_FILE = os.path.join(DATA_DIR, "analytics.json")  # ответы: кто согласился/отказал/промолчал

# Город: слаг из адресной строки 2ГИС (2gis.ru/<слаг>/search/...)
SEARCH_CITY = "Санкт-Петербург"

# 🎯 Список направлений (ниш), по которым идёт поиск в 2ГИС:
SEARCH_QUERIES = [
    "стоматологии",
    "автосервисы",
    "салоны красоты",
    "кафе",
    "детские центры",
    "фитнес-клубы",
]

# Лимиты сбора
MAX_PAGES_PER_NICHE = 3   # сколько страниц пагинации 2ГИС проходить по каждой нише
MAX_FIRMS_PER_NICHE = 15  # сколько фирм из ниши максимально открывать (страница фирмы = ~4 сек)

# Фильтры качества лидов
MOBILE_ONLY = True         # True — только мобильные +79xx (у городских номеров почти нет Telegram);
                           # телефоны из ссылки Telegram карточки добавляются всегда
REQUIRE_NO_WEBSITE = False # False — писать всем компаниям (и с сайтом, и без);
                           # True — только компаниям без своего сайта

# Браузер без окна (для сервера/Docker обязательно)
HEADLESS = os.getenv("HEADLESS", "0") == "1"

# Настройки безопасности рассылки
MIN_DELAY = 60    # минимальная пауза между сообщениями (сек) — у каждого своя, случайная
MAX_DELAY = 90    # максимальная пауза между сообщениями (сек)
BATCH_SIZE = 50   # размер пачки: собрали 50 -> сводка в «Избранное» -> пишем каждому
REST_TIME = 900   # перерыв между пачками (сек)
IGNORE_HOURS = 48  # нет ответа дольше этого — получатель считается «проигнорил»

# Конструктор уникальных сообщений: каждому получателю — свой текст.
# Сообщение собирается из трёх случайных частей: приветствие × тело (по нише
# и наличию сайта) × закрывающий вопрос. Комбинаций — сотни на нишу, дубли
# внутри запуска исключаются.
GREETINGS = [
    "Привет!",
    "Здравствуйте!",
    "Добрый день!",
    "Приветствую!",
]

CLOSINGS = [
    "Подскажите, актуален ли сейчас для вас такой вопрос?",
    "Скажите, интересна ли вам эта тема?",
    "Актуально ли это для вас сейчас?",
    "Не думали об этом?",
    "Если коротко — да или нет, буду рад ответу.",
    "Хотелось бы узнать ваше мнение.",
]

# Дополнительная фраза — вклеивается, если случайно выпала уже отправленная
# комбинация (крайне редко), чтобы текст остался уникальным.
POSTSCRIPTS = [
    "Если интересно — скину примеры работ и цены.",
    "Могу за пару минут показать, как это будет выглядеть.",
    "Готов ответить на любые вопросы.",
    "Напишите, если интересно — обсудим детали.",
]

# Тела сообщений по нишам: {name} = название компании.
# Ключи — слова, которые ищутся в тексте направления поиска.
MESSAGE_TEMPLATES = [
    (("стоматолог", "дент", "медиц", "клиник"),
     {"no_site": [
         "Нашёл вашу клинику «{name}» в 2ГИС и обратил внимание, что своего сайта у вас пока нет. "
         "Делаю сайты для клиник: онлайн-запись, перечень услуг, отзывы пациентов.",
         "Увидел «{name}» в справочнике 2ГИС — сайта, судя по всему, у вас нет. "
         "Занимаюсь разработкой сайтов для стоматологий: запись пациентов онлайн, прайс, удобная навигация.",
         "Открыл карточку «{name}» в 2ГИС и заметил, что сайта у клиники нет. "
         "Помогаю стоматологиям запускать сайты, которые приводят пациентов.",
     ],
         "has_site": [
         "Нашёл вашу клинику «{name}» в 2ГИС. Занимаюсь разработкой и доработкой сайтов "
         "для стоматологий: онлайн-запись, каталог услуг, скорость загрузки.",
         "Увидел «{name}» в 2ГИС. Делаю современные сайты для клиник: запись, прайс, отзывы — "
         "всё, чтобы пациенту было удобно выбрать вас.",
         "Открыл «{name}» в 2ГИС. Помогаю клиникам с сайтами: редизайн, доработки, онлайн-запись.",
     ]}),
    (("барбер", "салон", "красот", "парикмахер", "маникюр", "студия", "бьюти"),
     {"no_site": [
         "Нашёл ваш салон «{name}» в 2ГИС — официального сайта, как я понял, у вас нет. "
         "Делаю сайты для бьюти-сферы: онлайн-запись, портфолио мастеров, отзывы клиентов.",
         "Увидел «{name}» в справочнике 2ГИС и заметил, что сайта у вас нет. "
         "Занимаюсь сайтами для салонов и студий — от визитки до лендинга с онлайн-записью.",
         "Открыл карточку «{name}» в 2ГИС: сайта нет, а клиентов из интернета хочется. "
         "Помогаю салонам красоты запускать удобные сайты.",
     ],
         "has_site": [
         "Нашёл «{name}» в 2ГИС. Занимаюсь сайтами для бьюти-сферы: онлайн-запись, "
         "портфолио, быстрый современный дизайн.",
         "Увидел ваш салон «{name}» в 2ГИС. Делаю и обновляю сайты для салонов — "
         "думаю, вашему сайту могла бы пригодиться пара улучшений.",
         "Открыл «{name}» в 2ГИС. Помогаю салонам красоты со сайтами: запись онлайн, "
         "актуальный прайс, отзывы.",
     ]}),
    (("авто", "сервис", "сто", "шиномонтаж", "детейлинг", "кузов"),
     {"no_site": [
         "Искал автосервисы в 2ГИС и обратил внимание на «{name}» — своего сайта у вас, "
         "похоже, нет. Делаю сайты для сервисов: каталог услуг, онлайн-запись, отзывы.",
         "Увидел «{name}» в 2ГИС и заметил, что сайта нет. Занимаюсь сайтами для "
         "автосервисов и СТО — клиентам проще выбрать тех, у кого есть сайт с ценами.",
         "Открыл карточку «{name}» в 2ГИС: сайта нет. Помогаю автосервисам запускать "
         "сайты с каталогом услуг и записью.",
     ],
         "has_site": [
         "Увидел «{name}» в 2ГИС. Делаю сайты для автосервисов: каталог услуг, "
         "онлайн-запись, фото работ.",
         "Нашёл «{name}» в 2ГИС. Занимаюсь разработкой и доработкой сайтов для СТО — "
         "от лендинга под акцию до полноценного каталога.",
         "Открыл «{name}» в 2ГИС. Помогаю автосервисам с сайтами: обновление дизайна, "
         "скорость, приём заявок.",
     ]}),
    (("кафе", "ресторан", "доставка", "суши", "пицца", "кофейн", "бар", "столовая", "пекарн"),
     {"no_site": [
         "Заметил в 2ГИС, что у «{name}» нет сайта с меню и заказом. "
         "Делаю сайты для кафе и ресторанов: меню, бронирование, доставка.",
         "Нашёл «{name}» в 2ГИС — сайта, судя по всему, нет. Занимаюсь сайтами для "
         "общепита: онлайн-меню, заказ столиков, отзывы.",
         "Увидел «{name}» в 2ГИС и подумал, что сайта вам не хватает. "
         "Помогаю заведениям запускать красивые сайты с меню.",
     ],
         "has_site": [
         "Нашёл «{name}» в 2ГИС. Делаю сайты для заведений: онлайн-меню, заказ, "
         "бронирование — пригодилось бы и вашему сайту.",
         "Увидел «{name}» в 2ГИС. Занимаюсь сайтами для общепита — возможно, "
         "вашему сайту не хватает пары фишек.",
         "Открыл «{name}» в 2ГИС. Помогаю кафе и ресторанам с сайтами: меню, заказ, скорость.",
     ]}),
    (("спорт", "фитнес", "тренажер", "йог", "секци", "бассейн"),
     {"no_site": [
         "Увидел «{name}» в 2ГИС — сайта у вас нет. Делаю сайты для фитнес-клубов: "
         "расписание, покупка абонементов, отзывы.",
         "Нашёл «{name}» в 2ГИС и заметил, что сайта нет. Занимаюсь сайтами для "
         "спортсекций и клубов.",
         "Открыл карточку «{name}» в 2ГИС: своего сайта не нашёл. Помогаю спортивным "
         "клубам запускать сайты с расписанием.",
     ],
         "has_site": [
         "Увидел «{name}» в 2ГИС. Делаю сайты для фитнес-клубов: расписание, "
         "абонементы онлайн.",
         "Нашёл «{name}» в 2ГИС. Занимаюсь сайтами для спортивных клубов — обновление "
         "дизайна, запись, оплата.",
         "Открыл «{name}» в 2ГИС. Помогаю клубам с сайтами: расписание, онлайн-оплата, отзывы.",
     ]}),
    (("детск", "развива", "центр"),
     {"no_site": [
         "Нашёл «{name}» в 2ГИС — сайта у вас, похоже, нет. Делаю сайты для детских "
         "центров: расписание, запись онлайн, отзывы родителей.",
         "Увидел «{name}» в 2ГИС и заметил, что сайта нет. Занимаюсь сайтами для "
         "детских студий и центров развития.",
         "Открыл «{name}» в 2ГИС: сайта не нашёл. Помогаю детским центрам запускать "
         "удобные сайты с записью.",
     ],
         "has_site": [
         "Нашёл «{name}» в 2ГИС. Делаю сайты для детских центров: расписание, запись, отзывы.",
         "Увидел «{name}» в 2ГИС. Занимаюсь сайтами для детских студий — обновление и доработка.",
         "Открыл «{name}» в 2ГИС. Помогаю центрам развития с сайтами: запись онлайн, программы.",
     ]}),
]
DEFAULT_TEMPLATES = {
    "no_site": [
        "Нашёл вашу компанию «{name}» в 2ГИС и обратил внимание, что собственного сайта "
        "у вас нет. Занимаюсь веб-разработкой для бизнеса.",
        "Увидел «{name}» в справочнике 2ГИС — сайта, судя по всему, у вас нет. "
        "Делаю сайты: лендинги, визитки, каталоги.",
        "Открыл карточку «{name}» в 2ГИС: сайта не нашёл. Помогаю бизнесу запускать "
        "сайты под задачи.",
    ],
    "has_site": [
        "Нашёл вашу компанию «{name}» в 2ГИС. Занимаюсь разработкой и обновлением "
        "сайтов для бизнеса.",
        "Увидел «{name}» в 2ГИС. Делаю сайты: современный дизайн, скорость, заявки.",
        "Открыл «{name}» в 2ГИС. Помогаю с сайтами: редизайн, доработки, поддержка.",
    ],
}

# Известные слаги городов 2ГИС
CITY_SLUGS = {
    "санкт-петербург": "spb",
    "москва": "moscow",
    "новосибирск": "novosibirsk",
    "екатеринбург": "ekaterinburg",
    "казань": "kazan",
    "нижний новгород": "nnov",
    "краснодар": "krasnodar",
    "самара": "samara",
    "ростов-на-дону": "rostov",
    "уфа": "ufa",
    "красноярск": "krasnoyarsk",
    "воронеж": "voronezh",
    "перми": "perm",
    "перми ": "perm",
    "волгоград": "volgograd",
}

# 🇷🇺 Города для режима --rotate-cities: «распарсить весь РФ» = идём по списку
# по кругу (прогресс хранится в city_cursor.txt, отправленные — в processed.txt)
ROTATE_CITIES = [
    "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", "Казань",
    "Нижний Новгород", "Челябинск", "Самара", "Омск", "Ростов-на-Дону",
    "Уфа", "Красноярск", "Воронеж", "Пермь", "Волгоград", "Краснодар",
    "Саратов", "Тюмень", "Тольятти", "Ижевск", "Барнаул", "Ульяновск",
    "Иркутск", "Хабаровск", "Ярославль", "Владивосток", "Томск",
    "Оренбург", "Кемерово", "Рязань", "Астрахань", "Пенза", "Липецк",
    "Тула", "Киров", "Чебоксары", "Калининград", "Брянск", "Курск",
    "Сочи", "Ставрополь", "Белгород", "Сургут", "Тверь", "Магнитогорск",
    "Иваново", "Владимир", "Архангельск",
]

CITY_CURSOR_FILE = os.path.join(DATA_DIR, "city_cursor.txt")


def next_rotate_city():
    """Берёт следующий город из списка ротации и запоминает позицию."""
    idx = 0
    if os.path.exists(CITY_CURSOR_FILE):
        try:
            with open(CITY_CURSOR_FILE, "r", encoding="utf-8") as f:
                idx = int(f.read().strip() or 0)
        except (ValueError, OSError):
            idx = 0
    city = ROTATE_CITIES[idx % len(ROTATE_CITIES)]
    with open(CITY_CURSOR_FILE, "w", encoding="utf-8") as f:
        f.write(str((idx + 1) % len(ROTATE_CITIES)))
    return city

tg_client = None  # создаётся в main() при необходимости


class NotInTelegram(Exception):
    """Номер не найден в Telegram (не зарегистрирован / скрыт)."""


def make_client():
    """Клиент Telegram: из SESSION_STRING (облако) или из файла сессии."""
    if not API_ID or not API_HASH:
        sys.exit("[!] Не заданы API_ID/API_HASH — добавьте их в .env "
                 "(локально) или в GitHub Secrets / переменные окружения (облако).")
    if SESSION_STRING:
        return TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    return TelegramClient(SESSION_PATH, API_ID, API_HASH)

# ---------------------------------------------------------------------------
# Извлечение данных из HTML 2ГИС (SSR-состояние)
# ---------------------------------------------------------------------------

def _extract_json_parse_blocks(html):
    """Достаёт все объекты из конструкции JSON.parse('...') в HTML 2ГИС."""
    results = []
    pos = 0
    while True:
        m = re.search(r"JSON\.parse\('", html[pos:])
        if not m:
            break
        start = pos + m.end()
        i = start
        buf = []
        while i < len(html):
            ch = html[i]
            if ch == "\\":
                buf.append(html[i:i + 2])
                i += 2
                continue
            if ch == "'":
                break
            buf.append(ch)
            i += 1
        raw = "".join(buf)
        pos = i + 1

        def unesc(mm):
            e = mm.group(0)
            if e.lower().startswith("\\u"):
                return chr(int(e[2:], 16))
            mapping = {"\\'": "'", '\\"': '"', "\\\\": "\\", "\\n": "\n",
                       "\\r": "\r", "\\t": "\t", "\\/": "/"}
            return mapping.get(e, e)

        decoded = re.sub(r"\\u[0-9a-fA-F]{4}|\\.", unesc, raw)
        try:
            results.append(json.loads(decoded))
        except Exception:
            pass
    return results


def extract_profiles(html):
    """Все профили фирм со страницы 2ГИС: {id: {name, address_name, ...}}."""
    found = {}
    for state in _extract_json_parse_blocks(html):
        if not isinstance(state, dict):
            continue
        entity = ((state.get("data") or {}).get("entity")) or {}
        for key in ("profile", "branch", "firm"):
            block = entity.get(key)
            if isinstance(block, dict):
                for pid, prof in block.items():
                    if isinstance(prof, dict) and isinstance(prof.get("data"), dict):
                        found[str(pid)] = prof["data"]
    return found


def normalize_phone(raw):
    """'+7 (812) 210-56-20' / '+78122105620' -> '+78122105620' или None."""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) > 11 and digits.startswith("7"):
        digits = digits[:11]
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits[0] == "7":
        return "+7" + digits[1:]
    return None


def extract_contacts(profile):
    """Из профиля фирмы достаёт (телефоны, сайты, tg-юзернеймы, телефон из tg-ссылки)."""
    phones, sites, tgs = [], [], []
    tg_phone = None
    for group in profile.get("contact_groups") or []:
        for c in group.get("contacts", []):
            ctype = c.get("type")
            value = c.get("value")
            url = value.get("url") if isinstance(value, dict) else value
            url = url or c.get("text") or ""
            if ctype == "phone":
                ph = normalize_phone(url) or normalize_phone(c.get("text", ""))
                if ph:
                    phones.append(ph)
            elif ctype == "website":
                # ссылка обёрнута в редирект 2ГИС: ...?http://real-site.ru
                real = url.split("?", 1)[1] if "?" in url else url
                real = unquote(real).strip()
                if real:
                    sites.append(real)
            elif ctype == "telegram":
                tail = url.rstrip("/").split("t.me/")[-1].lstrip("@")
                if tail.startswith("+"):
                    # t.me/+79117803322 — аккаунт привязан к номеру (не путать
                    # с пригласительными ссылками t.me/+AbCdEf)
                    digits = tail[1:]
                    if digits.isdigit():
                        ph = normalize_phone(digits)
                        if ph:
                            tg_phone = tg_phone or ph
                else:
                    m = re.fullmatch(r"[A-Za-z0-9_]{4,32}", tail)
                    if m and tail.lower() not in ("joinchat", "share", "addstickers", "proxy"):
                        tgs.append(tail)
    return phones, sites, tgs, tg_phone


# ---------------------------------------------------------------------------
# Сбор лидов из 2ГИС
# ---------------------------------------------------------------------------

async def _get_html(page, url, tries=3):
    """Открывает URL и возвращает HTML, попутно обходя заглушки 2ГИС."""
    for attempt in range(1, tries + 1):
        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"  [!] Не удалось открыть страницу (попытка {attempt}/{tries}): {e}")
            await asyncio.sleep(5)
            continue
        await page.wait_for_timeout(2000)

        # Заглушка "2ГИС советует обновить браузер" — жмём кнопку пропуска
        for _ in range(2):
            try:
                btn = page.get_by_text("Пропустить обновление браузера и перейти в 2ГИС")
                await btn.click(timeout=3000)
                await page.wait_for_timeout(3000)
            except Exception:
                break

        html = await page.content()
        low = html.lower()
        if "доступ ограничен" in low or "подтвердите, что запросы" in low:
            print(f"  [!] 2ГИС показывает антибот-проверку. Жду 30 сек (попытка {attempt}/{tries})...")
            await asyncio.sleep(30)
            continue
        return html
    return None


async def collect_leads(city, niches, max_pages, max_firms, mobile_only=True,
                        require_no_website=False, headless=False, archive_cb=None,
                        target_leads=None):
    """Возвращает список лидов: {phone, tg_username, name, niche, address}.

    Новые лиды дописываются к уже сохранённым в LEADS_FILE (без дублей).
    target_leads — остановить сбор, когда за этот запуск наберётся столько новых.
    """
    leads = load_leads()
    seen_phones = {l["phone"] for l in leads}
    seen_firm_ids = {l.get("firm_id") for l in leads if l.get("firm_id")}
    initial_count = len(leads)
    stop_collection = False

    def _target_reached():
        return bool(target_leads and (len(leads) - initial_count) >= target_leads)

    slug = CITY_SLUGS.get(city.lower().strip(), quote(city.lower()))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 850},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        # картинки/шрифты не нужны — только HTML: так быстрее и меньше похоже на парсинг
        async def _block(route):
            if route.request.resource_type in ("image", "font", "media"):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", _block)

        page = await context.new_page()

        # Прогрев: главная города, чтобы пройти заглушку/куки один раз
        await _get_html(page, f"https://2gis.ru/{slug}")

        for niche in niches:
            if stop_collection:
                break
            print(f"\n[*] Ниша: «{niche}» ({city})")

            # 1) Собираем ID фирм из поисковых страниц
            firm_ids = []
            for page_num in range(1, max_pages + 1):
                if stop_collection:
                    break
                search_url = f"https://2gis.ru/{slug}/search/{quote(niche)}"
                if page_num > 1:
                    search_url += f"?page={page_num}"
                html = await _get_html(page, search_url)
                profiles = extract_profiles(html) if html else {}
                new_ids = [pid for pid in profiles
                           if pid not in seen_firm_ids and not profiles[pid].get("is_promoted")]
                if not profiles:
                    print("  [!] Результаты не получены (возможен антибот) — ниша пропущена.")
                    break
                if not new_ids:
                    break  # на этой странице всё уже видели
                for pid in new_ids:
                    seen_firm_ids.add(pid)
                    firm_ids.append(pid)
                print(f"  [*] Страница {page_num}: +{len(new_ids)} фирм (всего в нише {len(firm_ids)})")
                if len(firm_ids) >= max_firms:
                    break
                await asyncio.sleep(random.uniform(1.5, 3.0))

            # 2) Открываем страницы фирм и достаём контакты
            taken = 0
            for pid in firm_ids[:max_firms]:
                if stop_collection:
                    break
                html = await _get_html(page, f"https://2gis.ru/firm/{pid}")
                profiles = extract_profiles(html) if html else {}
                prof = profiles.get(pid) or next(
                    (d for d in profiles.values() if d.get("contact_groups")), None)
                if not prof:
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue

                name = (prof.get("name") or "").strip()
                address = (prof.get("address_name") or "").strip()
                phones, sites, tgs, tg_phone = extract_contacts(prof)
                has_site = bool(sites)

                if require_no_website and has_site:
                    print(f"  [-] {name}: есть сайт ({sites[0]}) — пропущен (фильтр REQUIRE_NO_WEBSITE)")
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue

                # Цели для отправки: телефон из tg-ссылки идёт всегда (он точно в Telegram),
                # остальные — по фильтру MOBILE_ONLY
                candidates = []
                for ph in ([tg_phone] if tg_phone else []) + phones:
                    if ph and ph not in candidates and (not mobile_only or ph.startswith("+79")
                                                        or ph == tg_phone):
                        candidates.append(ph)
                new_candidates = [ph for ph in candidates if ph not in seen_phones]
                for ph in new_candidates:
                    seen_phones.add(ph)

                if not new_candidates:
                    print(f"  [-] {name}: подходящих телефонов нет")
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue

                lead = {
                    "phone": new_candidates[0],
                    "firm_id": pid,
                    "phones": new_candidates,
                    "tg_phone": tg_phone,
                    "tg_username": tgs[0] if tgs else None,
                    "has_site": has_site,
                    "name": name,
                    "niche": niche,
                    "address": address,
                    "found_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                leads.append(lead)
                _save_leads(leads)
                taken += 1
                if archive_cb:
                    try:
                        await archive_cb(lead)
                    except Exception as e:
                        print(f"  [!] Не удалось сохранить ссылку в «Избранное»: {e}")
                site_note = f"сайт: {sites[0]}" if has_site else "без сайта"
                tg_note = f" | tg: @{tgs[0]}" if tgs else (f" | tg: {tg_phone}" if tg_phone else "")
                progress = f" | набрано {len(leads) - initial_count}/{target_leads}" if target_leads else ""
                print(f"  [+] {name} — {address} | {site_note}{tg_note} | тел: {new_candidates}{progress}")

                if _target_reached():
                    print(f"\n[🎯] Собрано {target_leads} новых лидов — прекращаю сбор, перехожу к рассылке.")
                    stop_collection = True
                    break

                await asyncio.sleep(random.uniform(1.5, 3.0))

            if stop_collection:
                break
            print(f"[+] Итог по нише «{niche}»: {taken} компаний-лидов")

        await browser.close()

    return leads


def _save_leads(leads):
    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)


def load_leads():
    if not os.path.exists(LEADS_FILE):
        return []
    with open(LEADS_FILE, "r", encoding="utf-8") as f:
        leads = json.load(f)
    # нормализуем лиды старого формата (без phones/has_site/tg_phone)
    for item in leads:
        item.setdefault("phones", [item["phone"]] if item.get("phone") else [])
        item.setdefault("has_site", False)
        item.setdefault("tg_phone", None)
        item.setdefault("tg_username", None)
        item.setdefault("firm_id", None)
    return leads


# ---------------------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------------------

_used_full_texts = set()  # уже отправленные тексты за запуск — гарантирует уникальность


def generate_message(lead, custom_text=None):
    """Собирает уникальный текст: приветствие + тело под нишу + закрытие.

    Комбинации случайны и не повторяются внутри запуска (проверка по полному
    тексту), так что все 50 получателей пачки получают разные сообщения.
    """
    if custom_text:
        return custom_text.replace("{name}", lead.get("name") or "")

    niche = (lead.get("niche") or "").lower()
    variant = "has_site" if lead.get("has_site") else "no_site"
    bodies = None
    for keys, tmpls in MESSAGE_TEMPLATES:
        if any(k in niche for k in keys):
            bodies = tmpls[variant]
            break
    if bodies is None:
        bodies = DEFAULT_TEMPLATES[variant]

    name = lead.get("name") or ""
    for _ in range(25):
        msg = (f"{random.choice(GREETINGS)} "
               f"{random.choice(bodies).replace('{name}', name)} "
               f"{random.choice(CLOSINGS)}")
        if random.random() < 0.3:
            msg += " " + random.choice(POSTSCRIPTS)
        if msg not in _used_full_texts:
            _used_full_texts.add(msg)
            return msg
    return msg  # комбинации кончились (практически невозможно) — шлём как есть


# ---------------------------------------------------------------------------
# Рассылка Telegram
# ---------------------------------------------------------------------------

def load_processed():
    if not os.path.exists(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
        return set(line.strip().lower() for line in f if line.strip())


def save_processed(phone):
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(phone.lower() + "\n")


def load_archived():
    if not os.path.exists(ARCHIVED_FILE):
        return set()
    with open(ARCHIVED_FILE, "r", encoding="utf-8") as f:
        return set(line.strip().lower() for line in f if line.strip())


def save_archived(key):
    with open(ARCHIVED_FILE, "a", encoding="utf-8") as f:
        f.write(key.lower() + "\n")


# ---------------------------------------------------------------------------
# Аналитика ответов: кто согласился, кто отказал, кто проигнорил
# ---------------------------------------------------------------------------

DECLINE_RE = re.compile(
    r"не\s?актуальн|не\s?нужн|не\s?надо|не\s?интерес|неинтерес|не\s?требу|"
    r"уже есть|у нас есть|отпад|не\s?сейчас|не\s?пишите|не\s?звоните|отстань|"
    r"спам|жалоб[уи]|дорог|нет денег|\bнет\b|\bнеа\b|не, спасибо")
INTEREST_RE = re.compile(
    r"интересн|актуальн|дава[йит]|конечно|хорошо|\bок\b|\bда\b|хочу|"
    r"сколько|цена|расцен|покажи|скинь|пример|портфолио|обсуди|"
    r"звоните|позвоните|набер|напишите|жду|давайте|давно думал")


def classify_reply(text):
    """Категория ответа: 'interested' | 'declined' | 'other' (или None)."""
    if not text or not text.strip():
        return None
    t = text.lower().strip()
    if DECLINE_RE.search(t):
        return "declined"    # «не актуально», «не надо», «уже есть»...
    if INTEREST_RE.search(t):
        return "interested"  # «да, актуально», «сколько стоит»...
    return "other"           # ответ есть, но по смыслу непонятен


def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def parse_utc_str(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def load_analytics():
    if not os.path.exists(ANALYTICS_FILE):
        return {}
    with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_analytics(data):
    with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def record_sent(lead, peer=None):
    """Фиксирует факт отправки — дальше по этому ключу ловим ответ."""
    data = load_analytics()
    key = str(peer.get("user_id")) if peer and peer.get("user_id") else (lead.get("phone") or "")
    if not key:
        return
    data[key] = {
        "phone": lead.get("phone"),
        "user_id": (peer or {}).get("user_id"),
        "access_hash": (peer or {}).get("access_hash"),
        "name": lead.get("name"),
        "niche": lead.get("niche"),
        "sent_at": now_utc_str(),
        "status": "sent",
        "category": None,
        "reply_text": None,
        "replied_at": None,
    }
    save_analytics(data)


async def sweep_replies(client):
    """Проходит по тем, кому писали, читает их последние сообщения и
    классифицирует ответы. Возвращает число новых классифицированных."""
    data = load_analytics()
    changed = 0
    for key, info in data.items():
        if info.get("status") != "sent":
            continue
        uid, ah = info.get("user_id"), info.get("access_hash")
        if not uid or not ah:
            continue
        try:
            peer = InputPeerUser(int(uid), int(ah))
            msgs = await client.get_messages(peer, limit=5)
        except Exception:
            continue  # не достали (личка закрыта и т.п.) — попробуем в следующий раз
        sent_dt = parse_utc_str(info["sent_at"])
        for m in msgs:
            if m.out:
                continue
            incoming_dt = m.date.astimezone(timezone.utc).replace(tzinfo=None)
            if incoming_dt < sent_dt:
                break
            info["status"] = "replied"
            info["category"] = classify_reply(m.text) or "other"
            info["reply_text"] = (m.text or "(без текста)")[:200]
            info["replied_at"] = incoming_dt.strftime("%Y-%m-%d %H:%M")
            changed += 1
            break  # свежий ответ главнее старых
    if changed:
        save_analytics(data)
    return changed


def build_report_text():
    """Сводный отчёт по всей аналитике."""
    data = load_analytics()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    interested, declined, other, waiting, ignored = [], [], [], [], []
    for info in data.values():
        if info.get("status") == "replied":
            {"interested": interested, "declined": declined}.get(
                info.get("category"), other).append(info)
        else:
            age_h = (now - parse_utc_str(info["sent_at"])).total_seconds() / 3600
            (waiting if age_h < IGNORE_HOURS else ignored).append(info)

    total = len(data) or 1
    lines = [
        "📊 Аналитика рассылки (весь прогресс)",
        f"Отправлено всего: {len(data)}",
        f"✅ Заинтересовались: {len(interested)} ({len(interested) * 100 // total}%)",
        f"❌ Отказали («не актуально/не надо»): {len(declined)} ({len(declined) * 100 // total}%)",
        f"❓ Другие ответы: {len(other)} ({len(other) * 100 // total}%)",
        f"⏳ Ждут ответа (<{IGNORE_HOURS} ч): {len(waiting)}",
        f"🔇 Проигнорили (>{IGNORE_HOURS} ч без ответа): {len(ignored)} ({len(ignored) * 100 // total}%)",
    ]

    # разбивка по нишам
    niches = {}
    for info in data.values():
        n = info.get("niche") or "—"
        stat = niches.setdefault(n, {"sent": 0, "interested": 0, "declined": 0})
        stat["sent"] += 1
        if info.get("category") == "interested":
            stat["interested"] += 1
        elif info.get("category") == "declined":
            stat["declined"] += 1
    if niches:
        lines.append("\nПо направлениям:")
        for n, stat in sorted(niches.items(), key=lambda kv: -kv[1]["interested"])[:8]:
            lines.append(f"  {n}: отправлено {stat['sent']}, ✅ {stat['interested']}, ❌ {stat['declined']}")

    if interested:
        lines.append("\n🎯 Последние заинтересованные (напишите им первыми!):")
        for info in sorted(interested, key=lambda i: i.get("replied_at") or "", reverse=True)[:5]:
            lines.append(f"  {info.get('phone')} — {info.get('name')} | «{(info.get('reply_text') or '')[:60]}»")

    return "\n".join(lines)


async def send_analytics_report(client):
    text = build_report_text()
    await client.send_message("me", text)
    print("\n" + text)


async def archive_to_saved(client, lead):
    """Кидает в «Избранное» кликабельную ссылку на лида: https://t.me/+номер.

    Делается сразу, как только получен номер, — чтобы можно было написать
    человеку вручную, даже если автоматическая отправка не удалась.
    Повторно один и тот же лид в «Избранное» не дублируется.
    """
    phone = lead.get("phone") or (lead.get("phones") or [None])[0]
    if not phone:
        return False
    username = lead.get("tg_username")
    key = ("@" + username.lower()) if username else phone.lower()
    if key in load_archived():
        return False

    link = f"https://t.me/{username}" if username else f"https://t.me/{phone}"
    name = lead.get("name") or "Компания"
    site_note = "сайт есть" if lead.get("has_site") else "без сайта"
    text = (f"{link}\n"
            f"▪️ {name} | {lead.get('niche') or ''}\n"
            f"📍 {lead.get('address') or 'адрес не указан'} | {site_note}")

    await client.send_message("me", text)
    save_archived(key)
    await asyncio.sleep(random.uniform(0.5, 1.5))  # не заваливаем Telegram потоком сообщений
    return True


async def send_msg_by_phone(client, phone, message):
    """Импортирует номер как контакт, отправляет сообщение, удаляет контакт.

    Возвращает объект пользователя (для аналитики ответов)."""
    if not phone.startswith("+"):
        phone = "+" + phone
    contact = InputPhoneContact(client_id=0, phone=phone, first_name="Lead", last_name="")
    result = await client(ImportContactsRequest([contact]))
    if not result.users:
        raise NotInTelegram("номер не зарегистрирован в Telegram или скрыт настройками приватности")
    user = result.users[0]
    await client.send_message(user, message)
    await client(DeleteContactsRequest(id=[user]))
    return user


def _lead_targets(item):
    """Порядок попыток отправки: tg-юзернейм -> телефон из tg-ссылки -> остальные номера."""
    targets = []
    if item.get("tg_username"):
        targets.append(("username", item["tg_username"]))
    for key in ("tg_phone", "phone"):
        ph = item.get(key)
        if ph:
            targets.append(("phone", ph))
    for ph in item.get("phones") or []:
        targets.append(("phone", ph))
    ordered, seen = [], set()
    for kind, val in targets:
        if (kind, val.lower()) not in seen:
            seen.add((kind, val.lower()))
            ordered.append((kind, val))
    return ordered


async def _deliver(client, item, message, processed):
    """Пробует доставить сообщение по всем целям лида.

    Возвращает (цель, peer, причина): цель — '@username' или '+7...', peer —
    данные пользователя для аналитики ({user_id, access_hash}) или None,
    причина — 'not_in_tg' | 'error' | None (при успехе None).
    Постоянные ошибки (номер не в Telegram) помечает в processed.txt,
    FloodWaitError пробрасывает наверх.
    """
    last_reason = None
    for kind, target in _lead_targets(item):
        if kind == "phone" and target.lower() in processed:
            continue
        try:
            if kind == "username":
                print(f"  [->] Пишу в Telegram @{target} (из карточки 2ГИС)...")
                await client.send_message(target, message)
                peer = None
                try:
                    entity = await client.get_input_entity(target)
                    if isinstance(entity, InputPeerUser):
                        peer = {"user_id": entity.user_id, "access_hash": entity.access_hash}
                except Exception:
                    pass
                return target, peer, None
            print(f"  [->] Пишу на номер {target} (через импорт контакта)...")
            user = await send_msg_by_phone(client, target, message)
            return target, {"user_id": user.id, "access_hash": user.access_hash}, None
        except errors.FloodWaitError:
            raise
        except (NotInTelegram,
                errors.PhoneNumberUnoccupiedError,
                errors.PhoneNumberBannedError,
                errors.UserDeactivatedBanError) as e:
            print(f"  [-] {target}: {e}")
            processed.add(target.lower())
            if kind == "phone":
                save_processed(target)  # постоянная ошибка — больше не пробуем
            last_reason = "not_in_tg"
            continue
        except ValueError as e:
            # у Telethon ValueError, если username не найден
            print(f"  [-] @{target}: {e}")
            last_reason = "not_in_tg"
            continue
        except Exception as e:
            print(f"  [-] Ошибка при отправке на {target}: {e}")
            last_reason = "error"
            continue
    return None, None, last_reason


async def send_batch_digest(client, batch):
    """Собирает пачку лидов «в кучу»: сообщение в «Избранное» со всеми ссылками."""
    lines = []
    for i, item in enumerate(batch, 1):
        username = item.get("tg_username")
        link = f"https://t.me/{username}" if username else f"https://t.me/{item.get('phone')}"
        name = item.get("name") or "Компания"
        site = "сайт" if item.get("has_site") else "без сайта"
        lines.append(f"{i}) {link} — {name} ({site}, {item.get('niche') or '—'})")

    chunk = f"📦 Новая пачка: {len(batch)} лидов\n\n"
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:  # лимит Telegram — 4096 символов
            await client.send_message("me", chunk)
            await asyncio.sleep(1)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await client.send_message("me", chunk)


async def send_all(leads, limit=None, custom_text=None):
    processed = load_processed()
    sent = 0
    not_in_tg = 0
    failures_in_row = 0

    # Кого реально будем писать в этом запуске
    pending = []
    for item in leads:
        lead_phones = [val for kind, val in _lead_targets(item) if kind == "phone"]
        if lead_phones and all(ph.lower() in processed for ph in lead_phones):
            continue
        pending.append(item)
    # лиды с Telegram из карточки (юзернейм/tg-телефон) доставляются надёжнее —
    # пишем им в первую очередь
    pending.sort(key=lambda l: 0 if (l.get("tg_username") or l.get("tg_phone")) else 1)
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        print("[~] Всех лидов уже обрабатывали — новых сообщений нет.")
        return 0

    # 1) Всю пачку «в кучу» — сводным списком в «Избранное»
    try:
        await send_batch_digest(tg_client, pending)
        print(f"[📥] Сводка пачки ({len(pending)} лидов) отправлена в «Избранное»")
    except Exception as e:
        print(f"[!] Не удалось отправить сводку пачки в «Избранное»: {e}")

    for item in pending:
        lead_phones = [val for kind, val in _lead_targets(item) if kind == "phone"]

        # 2) Ссылка на лида тоже летит в «Избранное» — кликабельный архив
        try:
            await archive_to_saved(tg_client, item)
        except Exception as e:
            print(f"[!] Не удалось сохранить ссылку в «Избранное»: {e}")

        # 3) Пишем человеку: у каждого получателя свой случайный таймер
        message = generate_message(item, custom_text)
        label = f"{item.get('name')} ({item.get('niche')})"
        tg_note = f" | tg: @{item['tg_username']}" if item.get("tg_username") else ""
        print(f"\n[📞] {label} | тел: {item.get('phone')}{tg_note}")
        print(f"[💬] {message[:120]}...")

        target = peer = fail_reason = None
        try:
            target, peer, fail_reason = await _deliver(tg_client, item, message, processed)
        except errors.FloodWaitError as e:
            wait = e.seconds + 5
            print(f"[!] FloodWait от Telegram: жду {wait} сек...")
            await asyncio.sleep(wait)
            try:
                target, peer, fail_reason = await _deliver(tg_client, item, message, processed)
            except Exception as e2:
                print(f"[-] Повторная отправка не удалась: {e2}")

        if target:
            print("[+] Успешно отправлено!")
            record_sent(item, peer)  # запоминаем — теперь ждём от него ответ
            save_processed(target)
            processed.add(target.lower())
            for ph in lead_phones:  # компанию целиком помечаем, чтобы не писать повторно
                save_processed(ph)
                processed.add(ph.lower())
            sent += 1
            failures_in_row = 0

            if limit is not None and sent >= limit:
                break
            if sent % BATCH_SIZE == 0:
                print(f"\n[🛑] Пачка из {BATCH_SIZE} сообщений отправлена. "
                      f"Перерыв {REST_TIME // 60} мин для защиты аккаунта...")
                await asyncio.sleep(REST_TIME)
            else:
                delay = random.randint(MIN_DELAY, MAX_DELAY)  # свой таймер у каждого
                print(f"[*] Пауза перед следующим получателем: {delay} сек...")
                await asyncio.sleep(delay)
        else:
            if fail_reason == "not_in_tg":
                # штатная ситуация (номер не в Telegram) — идём к следующему без пауз
                not_in_tg += 1
                continue
            print("[-] Отправить не удалось — ошибка доставки.")
            failures_in_row += 1
            await asyncio.sleep(15)

        if failures_in_row >= 10:
            print("[🛑] 10 неудач подряд — похоже, проблемы с сетью/аккаунтом. Останавливаюсь.")
            break

    print(f"\n[✅] Готово. Отправлено: {sent} | не найдено в Telegram (ссылки в «Избранном»): {not_in_tg}")
    return sent


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="2ГИС -> Telegram рассылка")
    parser.add_argument("--city", default=SEARCH_CITY, help="Город поиска (по умолчанию из конфига)")
    parser.add_argument("--niches", default=None,
                        help="Направления через запятую (по умолчанию из конфига)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_NICHE)
    parser.add_argument("--max-firms", type=int, default=MAX_FIRMS_PER_NICHE)
    parser.add_argument("--limit", type=int, default=None, help="Максимум сообщений за запуск")
    parser.add_argument("--target-leads", type=int, default=50,
                        help="Остановить сбор, когда за запуск набрано столько новых лидов (50)")
    parser.add_argument("--rotate-cities", action="store_true",
                        help="Брать следующий город из списка ROTATE_CITIES (весь РФ по кругу)")
    parser.add_argument("--collect-only", action="store_true", help="Только сбор лидов, без рассылки")
    parser.add_argument("--send-only", action="store_true", help="Только рассылка по готовому leads.json")
    parser.add_argument("--headless", action="store_true", default=HEADLESS,
                        help="Браузер без окна (на сервере/Docker включается сам по env HEADLESS=1)")
    parser.add_argument("--message-file", default=None,
                        help="Файл с текстом сообщения ({name} заменится на название фирмы); "
                             "по умолчанию — шаблоны под нишу")
    parser.add_argument("--selftest", action="store_true",
                        help="Тест Telegram: отправить тестовое сообщение самому себе")
    parser.add_argument("--export-session", action="store_true",
                        help="Печать строки сессии для переменной окружения SESSION_STRING "
                             "(для Render/Railway)")
    parser.add_argument("--loop", action="store_true",
                        help="Непрерывный режим для сервера: цикл «сбор+рассылка» каждые --every-hours часов")
    parser.add_argument("--every-hours", type=float, default=6.0,
                        help="Раз в сколько часов повторять цикл в режиме --loop (по умолчанию 6)")
    return parser.parse_args()


def ensure_session_or_exit():
    """Без SESSION_STRING и файла сессии в облаке логиниться некому — выходим с подсказкой."""
    if SESSION_STRING:
        return
    if not os.path.exists(SESSION_PATH + ".session") and not sys.stdin.isatty():
        print(f"[!] Нет ни SESSION_STRING, ни файла сессии: {SESSION_PATH}.session")
        print("[!] На компьютере выполните:  python main.py --export-session")
        print("[!] и вставьте выведенную строку в переменную окружения SESSION_STRING")
        sys.exit(1)


async def async_main(args):
    global tg_client

    # Город для этого запуска: ротация по всей РФ или фиксированный из конфига
    if args.rotate_cities:
        city = next_rotate_city()
        print(f"[🌍] Ротация городов: следующий — «{city}» "
              f"(позиция в {CITY_CURSOR_FILE})")
    else:
        city = args.city

    if args.export_session:
        # Выгружает локальную сессию в строку для переменной окружения SESSION_STRING
        if not os.path.exists(SESSION_PATH + ".session"):
            print(f"[!] Локальный файл сессии не найден: {SESSION_PATH}.session")
            print("[!] Сначала запустите скрипт локально и залогиньтесь.")
            sys.exit(1)
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.start(phone=PHONE_NUMBER)
        session_line = StringSession.save(client.session)
        await client.disconnect()
        print("\n[✅] Скопируйте строку ниже целиком в переменную окружения SESSION_STRING:\n")
        print(session_line)
        return

    if args.selftest:
        tg_client = make_client()
        await tg_client.start(phone=PHONE_NUMBER or None)
        me = await tg_client.get_me()
        print(f"[+] Аккаунт: {me.first_name} ({me.phone})")

        await tg_client.send_message("me", "Тест 1/2: прямая отправка в «Избранное» работает ✅")
        print("[+] Тест 1/2: сообщение ушло в «Избранное» напрямую")

        # Проверяем сам механизм рассылки: импорт контакта по номеру + отправка.
        # Своё сообщение попадёт в «Избранное» — безопасно.
        await send_msg_by_phone(tg_client, PHONE_NUMBER,
                                "Тест 2/2: отправка через импорт контакта работает ✅")
        print("[+] Тест 2/2: отправка через импорт контакта работает")

        # Показываем формат архивной записи в «Избранном»
        sample = {"phone": "+79990000000", "name": "Тестовая компания",
                  "niche": "тест", "address": "Тестовый адрес, 1", "has_site": False}
        await archive_to_saved(tg_client, sample)
        print(f"[+] Пример архивной записи (https://t.me/+{sample['phone'][1:]}) отправлен в «Избранное»")

        await tg_client.disconnect()
        return

    if args.collect_only:
        niches = [n.strip() for n in args.niches.split(",")] if args.niches else list(SEARCH_QUERIES)
        leads = await collect_leads(city, niches, args.max_pages, args.max_firms,
                                    MOBILE_ONLY, REQUIRE_NO_WEBSITE, args.headless,
                                    target_leads=args.target_leads)
        print(f"\n[✅] Сбор завершён. Всего лидов: {len(leads)} (сохранены в {LEADS_FILE})")

        # Архивируем ссылки на лидов в «Избранное»
        ensure_session_or_exit()
        tg_client = make_client()
        await tg_client.start(phone=PHONE_NUMBER or None)
        archived = 0
        try:
            for lead in leads:
                if await archive_to_saved(tg_client, lead):
                    archived += 1
            changed = await sweep_replies(tg_client)
            if changed:
                print(f"[📊] Классифицировано новых ответов: {changed}")
            await send_analytics_report(tg_client)
        finally:
            await tg_client.disconnect()
        print(f"[✅] Ссылки на новых лидов ({archived} шт.) добавлены в «Избранное»")
        return

    # Полный цикл или рассылка: запускаем Telegram
    ensure_session_or_exit()
    tg_client = make_client()
    await tg_client.start(phone=PHONE_NUMBER or None)
    me = await tg_client.get_me()
    print(f"[+] Telegram аккаунт: {me.first_name} ({me.phone})")

    # Аналитика: разбираем ответы, пришедшие с прошлого запуска
    changed = await sweep_replies(tg_client)
    if changed:
        print(f"[📊] Классифицировано новых ответов: {changed}")

    # ...и ловим ответы прямо во время рассылки
    async def _on_reply(event):
        key = str(event.sender_id)
        data = load_analytics()
        info = data.get(key)
        if info and info.get("status") == "sent":
            info["status"] = "replied"
            info["category"] = classify_reply(event.text) or "other"
            info["reply_text"] = (event.text or "(без текста)")[:200]
            info["replied_at"] = now_utc_str()
            save_analytics(data)
            print(f"[📊] Ответ от {info.get('name')} ({info.get('phone')}): "
                  f"«{(event.text or '')[:60]}» → {info['category']}")

    tg_client.add_event_handler(_on_reply, events.NewMessage(incoming=True))

    try:
        leads = load_leads()
        if not args.send_only:
            niches = [n.strip() for n in args.niches.split(",")] if args.niches else list(SEARCH_QUERIES)
            await collect_leads(city, niches, args.max_pages, args.max_firms,
                                MOBILE_ONLY, REQUIRE_NO_WEBSITE, args.headless,
                                archive_cb=lambda lead: archive_to_saved(tg_client, lead),
                                target_leads=args.target_leads)
            leads = load_leads()

        if not leads:
            print("[!] Лидов нет — сначала выполните сбор (без --send-only).")
            return

        custom_text = None
        if args.message_file and os.path.exists(args.message_file):
            with open(args.message_file, "r", encoding="utf-8-sig") as f:
                custom_text = f.read().strip() or None
            if custom_text:
                print(f"[*] Использую текст сообщения из {args.message_file}")

        await send_all(leads, limit=args.limit, custom_text=custom_text)

        # Круг аналитики: сводный отчёт в «Избранное» и в консоль
        await send_analytics_report(tg_client)
    finally:
        await tg_client.disconnect()


def main():
    args = parse_args()

    if not args.loop:
        try:
            asyncio.run(async_main(args))
        except KeyboardInterrupt:
            print("\n[!] Остановлено пользователем. Прогресс сохранён (processed.txt / leads.json).")
        return

    # Непрерывный режим для сервера: цикл за циклом, ошибки не убивают контейнер
    print(f"[🔁] Непрерывный режим: цикл «сбор + рассылка» каждые {args.every_hours} ч")
    while True:
        cycle_start = time.time()
        try:
            asyncio.run(async_main(args))
        except KeyboardInterrupt:
            print("\n[!] Остановлено пользователем. Прогресс сохранён.")
            break
        except Exception as e:
            print(f"\n[!] Ошибка цикла: {e}. Следующая попытка через 10 минут.")
            time.sleep(600)
            continue

        elapsed_min = (time.time() - cycle_start) / 60
        print(f"\n[💤] Цикл завершён за {elapsed_min:.0f} мин. "
              f"Следующий цикл через {args.every_hours} ч.")
        try:
            time.sleep(args.every_hours * 3600)
        except KeyboardInterrupt:
            print("\n[!] Остановлено пользователем. Прогресс сохранён.")
            break


if __name__ == "__main__":
    main()
