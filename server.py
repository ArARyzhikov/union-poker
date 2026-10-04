#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UNION POKER — сервер клуба.

Один файл, только стандартная библиотека Python. Ничего устанавливать не нужно.
Запуск:  python server.py

Внутри четыре части, работающие одновременно:
  1) База данных SQLite (файл club.db рядом со скриптом)
  2) Telegram-бот: регистрация игроков и запись на турниры
  3) Веб-сервер: отдаёт приложение, кассу и таймер + API для них
  4) Часовой: достраивает афишу на две недели вперёд и открывает турнир
     в кассе за 10 минут до старта

Настройки лежат в config.json — он создаётся автоматически при первом запуске.
"""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "club.db")
CFG_PATH = os.path.join(BASE, "config.json")
PUBLIC = os.path.join(BASE, "public")

# ----------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ----------------------------------------------------------------------------

DEFAULT_CFG = {
    # Версия формата настроек. Когда она меняется, сервер сам обновляет
    # config.json под новый формат, сохранив ваши личные строки: токен,
    # админов, ключ кассы, адрес приложения и афишу.
    "cfg_version": 8,

    "bot_token": "",                       # токен от @BotFather
    "proxy": "",                           # если Telegram недоступен: "http://127.0.0.1:2080"
    "admins": [],                          # ваш Telegram ID (узнать: напишите боту /id)
    "admin_key": "union-admin-key",        # пароль для кассы, поменяйте на свой
    "app_url": "",                         # адрес Mini App, например https://club.ru/app.html
    "club": "Union Poker",
    "port": 8080,
    "dev": True,                           # True — можно открывать приложение в браузере без Telegram

    # Каким по умолчанию получается турнир в афише
    "tournament": {
        "title": "Турнир клуба",
        "time": "20:00",
        "buyin": 1500,
        "reentry": 1500,                   # ребай — та же цена, что и вход
        "addon": 1500,                     # 0 — аддона нет, кнопка в кассе скрыта
        "addon_stack": 50000,              # сколько фишек даёт аддон и поздний вход
        "seats": 36,
        "stack": 25000,
        "meta": "Hold'em · 4 стола · вход 1500 ₽",
        "theme": ""
    },

    # Столы клуба: сколько их и сколько мест за каждым.
    # Касса сама сажает пришедшего за самый свободный стол.
    "tables": 4,

    # Расписание клуба: по этим дням сервер сам достраивает афишу вперёд.
    # Поставьте "on": true, когда определитесь с постоянными днями игры.
    "schedule": {
        "on": False,
        "days": ["пт", "сб", "вс"],
        "time": "20:00",
        "weeks_ahead": 2
    },

    # Разовые турниры, которых нет в расписании. Дата — в формате ГГГГ-ММ-ДД
    "events": [
        {"date": "2026-10-01", "time": "20:00", "title": "Открытие клуба",
         "seats": 36,
         "meta": "Первый турнир сезона · стек 25 000"}
    ],

    # очки за место: сколько получает 1-е, 2-е, 3-е и так далее
    "points": [100, 85, 72, 61, 52, 44, 38, 32, 27],
    "points_rest": 10,                     # всем остальным, кто играл
    "seats_per_table": 9,
    "final_at": 9,                         # при скольких игроках финальный стол

    # Структура турнира: [малый блайнд, большой блайнд, анте] или "перерыв 10".
    # Первые пятнадцать уровней — ребай-период: открыт вход и ребаи.
    # После них перерыв 15 минут — это аддон-тайм. Дальше финальная стадия.
    "structure": [
        [100, 200, 200], [200, 400, 400], [300, 600, 600], [400, 800, 800],
        [500, 1000, 1000],
        "перерыв 10",
        [600, 1200, 1200], [1000, 2000, 2000], [1500, 3000, 3000], [2000, 4000, 4000],
        [2500, 5000, 5000],
        "аддон 15",
        [7500, 15000, 15000], [10000, 20000, 20000], [15000, 30000, 30000],
        [25000, 50000, 50000], [50000, 100000, 100000]
    ],
    "level_minutes": 10,
    "late_levels": 10,                     # до конца какого уровня идут ребаи и поздняя запись
    "cancel_before_min": 10,               # за сколько минут до старта закрывается отмена записи
    "open_before_min": 10,                 # за сколько минут до старта турнир открывается в кассе

    # Размен стартового стека: [сколько фишек, номинал]
    "chips": [[25, 100], [5, 500], [10, 1000], [2, 5000]],

    "rules": [
        "Играть можно только после регистрации в боте клуба",
        "Стартовый стек 25 000, уровни по 10 минут",
        "Формат анте — большой блайнд (BB ante)",
        "Вход 1500 ₽ открыт до конца 10 уровня — заходить можно в любой момент",
        "Ребай 1500 ₽ — когда кончился стек, без ограничения по количеству",
        "После 10 уровня перерыв 15 минут: поздняя регистрация и аддон по 1500 ₽",
        "В этот перерыв дают 50 000 фишек вместо 25 000",
        "С окончанием перерыва входов, ребаев и аддонов больше нет",
        "Перерыв 10 минут после 5 уровня",
        "Финальный стол собирается сам, когда остаётся 9 игроков",
        "Игра один на один идёт без анте",
        "Призы клуба — очки рейтинга сезона",
        "Телефоны за столом на беззвучном режиме"
    ],
}


CFG_UPGRADED = False


def load_cfg():
    if not os.path.exists(CFG_PATH):
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
        print("Создан config.json — впишите туда токен бота и запустите снова.")
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    # Переход на новый формат настроек. Личные строки сохраняем, всё остальное
    # (цены, структура, правила) берём из новой версии — иначе сервер будет
    # работать по свежему коду, но по старым настройкам.
    global CFG_UPGRADED
    if cfg.get("cfg_version") != DEFAULT_CFG["cfg_version"]:
        keep = ("bot_token", "proxy", "admins", "admin_key", "app_url", "club", "port", "dev",
                "schedule",
                # цены, время старта и число мест — это настройки клуба, а не
                # код: при обновлении их больше не сбрасываем
                "tournament")
        fresh = json.loads(json.dumps(DEFAULT_CFG))
        for k in keep:
            if k in cfg:
                fresh[k] = cfg[k]
        # афишу не теряем: старые события оставляем, новые из этой версии
        # добавляем, если такого дня и времени ещё нет
        old_events = list(cfg.get("events") or [])
        seen = {(e.get("date"), e.get("time")) for e in old_events}
        for e in DEFAULT_CFG.get("events") or []:
            if (e.get("date"), e.get("time")) not in seen:
                old_events.append(e)
        fresh["events"] = old_events
        try:
            # Старый конфиг прячем в backup: он содержит токен, а папка backup
            # не попадает в git. Раньше он ложился рядом с кодом и уехал в GitHub.
            old_dir = os.path.join(BASE, "backup")
            os.makedirs(old_dir, exist_ok=True)
            os.replace(CFG_PATH, os.path.join(
                old_dir, datetime.now().strftime("config-%Y-%m-%d-%H%M.json")))
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(fresh, f, ensure_ascii=False, indent=2)
            print("config.json обновлён под новый формат. Старый лежит в папке backup.")
            CFG_UPGRADED = True
        except Exception as e:
            print("! Не получилось обновить config.json:", e)
        cfg = fresh

    merged = dict(DEFAULT_CFG)
    merged.update(cfg)
    # вложенные словари тоже дополняем значениями по умолчанию
    for key in ("tournament", "schedule"):
        d = dict(DEFAULT_CFG[key])
        d.update(merged.get(key) or {})
        merged[key] = d
    return merged


CFG = load_cfg()

# ----------------------------------------------------------------------------
# ДАТЫ И ВРЕМЯ
# ----------------------------------------------------------------------------

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WD_FULL = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
FMT = "%Y-%m-%d %H:%M"


def now():
    return datetime.now().replace(second=0, microsecond=0)


def parse_dt(s):
    """Строка '2026-10-04 18:00' → дата и время. Кривое значение не роняет сервер."""
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.strptime(s[:16], FMT)
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return None


def date_text(dt):
    return f"{dt.day} {MONTHS[dt.month - 1]}"


def weekday_text(dt):
    return WD_SHORT[dt.weekday()]


def tag_text(dt):
    """Короткая метка для афиши: '4 окт · 18:00'."""
    return f"{dt.day} {MONTHS[dt.month - 1][:3]} · {dt.strftime('%H:%M')}"


def when_text(dt):
    """Человеческое: 'сегодня в 18:00', 'завтра в 18:00', 'в субботу, 4 октября'."""
    d = (dt.date() - now().date()).days
    if d == 0:
        return f"сегодня в {dt.strftime('%H:%M')}"
    if d == 1:
        return f"завтра в {dt.strftime('%H:%M')}"
    if d < 0:
        return f"{date_text(dt)} в {dt.strftime('%H:%M')}"
    return f"в {WD_FULL[dt.weekday()]}, {date_text(dt)}, в {dt.strftime('%H:%M')}"


def minutes_left(dt):
    return int((dt - now()).total_seconds() // 60)


def late_minutes():
    """Сколько минут от старта идут ре-энтри и поздняя регистрация.

    Считается по структуре: уровни до late_levels плюс перерывы внутри
    этого отрезка. Для структуры клуба получается 110 минут.
    """
    limit = int(CFG.get("late_levels", 10))
    per = int(CFG.get("level_minutes", 10))
    total = levels = 0
    for item in CFG.get("structure", []):
        if isinstance(item, str):
            m = re.search(r"\d+", item)
            total += int(m.group()) if m else 0
        else:
            levels += 1
            total += per
            if levels >= limit:
                break
    return total


# ----------------------------------------------------------------------------
# БАЗА ДАННЫХ
# ----------------------------------------------------------------------------

_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

# Своя lower(): встроенная в SQLite работает только с латиницей, поэтому
# «Савелий» и «савелий» считались разными именами, а ники должны совпадать.
db.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s)


def q(sql, args=(), one=False):
    """Запрос на чтение."""
    with _lock:
        cur = db.execute(sql, args)
        rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


BACKUP_DIR = os.path.join(BASE, "backup")


def backup_db():
    """Копия базы в папку backup. Делается сама: при запуске и раз в три часа.

    Храним последние 60 копий — это примерно неделя. Копия снимается средствами
    SQLite, поэтому её можно делать на ходу, не останавливая турнир.
    """
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dst = os.path.join(BACKUP_DIR, datetime.now().strftime("club-%Y-%m-%d-%H%M.db"))
        with _lock:
            out = sqlite3.connect(dst)
            try:
                db.backup(out)
            finally:
                out.close()
        old = sorted(f for f in os.listdir(BACKUP_DIR)
                     if f.startswith("club-") and f.endswith(".db"))
        for f in old[:-60]:
            try:
                os.remove(os.path.join(BACKUP_DIR, f))
            except OSError:
                pass
        return dst
    except Exception as e:
        print("Не удалось сделать копию базы:", e)
        return ""


def x(sql, args=()):
    """Запрос на запись. Возвращает id вставленной строки."""
    with _lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


def init_db():
    with _lock:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS players(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id     INTEGER UNIQUE,
            name      TEXT NOT NULL,
            username  TEXT,
            phone     TEXT UNIQUE,
            number    INTEGER,
            created   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tournaments(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            title     TEXT,
            date      TEXT,
            status    TEXT DEFAULT 'open',   -- open | live | finished
            created   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS entries(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tid         INTEGER NOT NULL,
            player_id   INTEGER NOT NULL,
            registered  TEXT DEFAULT (datetime('now')),
            arrived     INTEGER DEFAULT 0,      -- оплатил вход
            busted      INTEGER DEFAULT 0,      -- выбыл
            place       INTEGER DEFAULT 0,
            points      INTEGER DEFAULT 0,
            UNIQUE(tid, player_id)
        );

        CREATE TABLE IF NOT EXISTS purchases(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            tid       INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            kind      TEXT NOT NULL,            -- buyin | reentry | addon
            amount    INTEGER NOT NULL,
            ts        TEXT DEFAULT (datetime('now')),
            by_admin  INTEGER
        );

        CREATE TABLE IF NOT EXISTS settings(
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS log(
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT DEFAULT (datetime('now')),
            who    TEXT,
            action TEXT
        );

        -- Подписи под документами клуба. Строки отсюда не удаляются и не
        -- переписываются: это доказательство того, что человек согласился, и
        -- с какой именно редакцией текста. Подписал заново — новая строка.
        CREATE TABLE IF NOT EXISTS consents(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            tg_id     INTEGER,
            code      TEXT NOT NULL,     -- oferta | pravila | pdn
            version   TEXT NOT NULL,     -- редакция документа
            doc_hash  TEXT,              -- отпечаток текста на момент подписи
            fio       TEXT,
            born      TEXT,
            phone     TEXT,
            ip        TEXT,
            ts        TEXT DEFAULT (datetime('now'))
        );
        """)
        db.commit()

    # Мягкие миграции: добавляем недостающие колонки в уже существующие базы,
    # чтобы обновление сервера не требовало удалять club.db
    add_column("entries", "wait", "INTEGER DEFAULT 0")
    # Управляющий, которого посадили добить стол. Сидит и играет, но в счёт
    # не идёт: ни в рейтинг, ни в места, ни в сбор финального стола.
    add_column("entries", "house", "INTEGER DEFAULT 0")
    add_column("entries", "table_no", "INTEGER DEFAULT 0")
    add_column("entries", "seat_no", "INTEGER DEFAULT 0")
    # Для согласия на обработку данных нужно настоящее имя: ник «lamer» под
    # документом ничего не значит. Ник остаётся тем, под которым объявляют.
    add_column("players", "fio", "TEXT")
    add_column("players", "born", "TEXT")
    # Отметка администратора: документ на входе предъявлен, человеку 18+.
    add_column("players", "id_ok", "TEXT")
    for col, decl in (("start", "TEXT"), ("time", "TEXT"), ("weekday", "TEXT"),
                      ("buyin", "INTEGER DEFAULT 0"), ("reentry", "INTEGER DEFAULT 0"),
                      ("addon", "INTEGER DEFAULT 0"), ("stack", "INTEGER DEFAULT 0"),
                      ("seats", "INTEGER DEFAULT 36"), ("meta", "TEXT"), ("theme", "TEXT"),
                      ("auto", "INTEGER DEFAULT 0"),
                      # тестовый турнир: сыгран, но в рейтинг и в историю не идёт
                      ("test", "INTEGER DEFAULT 0"),
                      ("stage", "TEXT DEFAULT 'rebuy'")):
        add_column("tournaments", col, decl)

    fix_old_tournaments()
    if CFG_UPGRADED:
        refresh_money()
    ensure_events()


def refresh_money():
    """После смены настроек подтягивает новые цены в ещё не сыгранные турниры."""
    b = CFG["tournament"]
    x("""UPDATE tournaments SET buyin=?, reentry=?, addon=?, stack=?, seats=?, meta=?
         WHERE status!='finished'""",
      (int(b["buyin"]), int(b["reentry"]), int(b["addon"]),
       int(b["stack"]), int(b["seats"]), b["meta"]))
    print("Цены и параметры подтянуты в турниры афиши")


def add_column(table, column, decl):
    """Добавляет колонку, если её ещё нет."""
    cols = [r["name"] for r in q(f"PRAGMA table_info({table})")]
    if column not in cols:
        x(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def setting(key, value=None):
    """Прочитать или записать настройку, которая живёт в базе."""
    if value is None:
        row = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
        return row["value"] if row else None
    x("INSERT INTO settings(key, value) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    return str(value)


def fix_old_tournaments():
    """Турнирам из старой версии, где дата была текстом, проставляет настоящую дату."""
    for r in q("SELECT * FROM tournaments WHERE start IS NULL OR start=''"):
        dt = None
        txt = (r["date"] or "").strip().lower()
        m = re.match(r"(\d{1,2})\s+([а-яё]+)", txt)
        if m:
            stem = m.group(2)[:4]
            for i, name in enumerate(MONTHS, 1):
                if name.startswith(stem):
                    hh, mm = ((r["time"] or "18:00").split(":") + ["00"])[:2]
                    try:
                        dt = datetime(now().year, i, int(m.group(1)), int(hh), int(mm))
                    except ValueError:
                        dt = None
                    break
        if dt is None:
            dt = now().replace(hour=18, minute=0)
        x("UPDATE tournaments SET start=? WHERE id=?", (dt.strftime(FMT), r["id"]))
    refresh_display()


def refresh_display():
    """Пересчитывает текстовые дату и день недели из настоящей даты."""
    for r in q("SELECT id, start FROM tournaments"):
        dt = parse_dt(r["start"])
        if dt:
            x("UPDATE tournaments SET date=?, time=?, weekday=? WHERE id=?",
              (date_text(dt), dt.strftime("%H:%M"), weekday_text(dt), r["id"]))


def log(who, action):
    x("INSERT INTO log(who, action) VALUES(?,?)", (str(who), action))


def clean_name(v):
    """Ник без лишних пробелов. Длину ограничиваем, иначе ломается карта столов."""
    v = re.sub(r"\s+", " ", str(v or "").strip())
    return v[:24]


def real_name(v):
    """Ник годится любой, лишь бы он был: две буквы и больше."""
    v = clean_name(v)
    return len(v) >= 2 and any(c.isalpha() for c in v)


def name_owner(name, not_id=None):
    """Кто уже носит этот ник. Ник в клубе один на человека: за столом
    объявляют по нику, и два одинаковых — это путаница и двойные профили."""
    name = clean_name(name)
    if not name:
        return None
    row = q("SELECT * FROM players WHERE lower(name)=lower(?) ORDER BY id LIMIT 1", (name,),
            one=True)
    if row and not_id and row["id"] == not_id:
        return None
    return row


def norm_phone(p):
    """Приводит номер к виду +79991234567, чтобы один человек не стал двумя."""
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return "+" + d if d else None


def next_number():
    row = q("SELECT COALESCE(MAX(number),0) AS m FROM players", one=True)
    return (row["m"] or 0) + 1


def find_player(tg_id, phone=None, name=None):
    """Ищет человека: по Telegram, по телефону и — только если попросили — по имени.

    По имени при регистрации не ищем намеренно: двух Александров склеивать
    нельзя, чужой турнир и чужие деньги попадут не тому. Поиск по имени нужен
    кассе, чтобы предложить объединить профили вручную.
    """
    if tg_id:
        row = q("SELECT * FROM players WHERE tg_id=?", (tg_id,), one=True)
        if row:
            return row
    phone = norm_phone(phone)
    if phone:
        row = q("SELECT * FROM players WHERE phone=?", (phone,), one=True)
        if row:
            return row
    if name and str(name).strip():
        row = q("SELECT * FROM players WHERE tg_id IS NULL AND lower(name)=lower(?) "
                "ORDER BY id LIMIT 1", (str(name).strip(),), one=True)
        if row:
            return row
    return None


def dupe_groups():
    """Профили, которые похожи на один и тот же человек: совпало имя или телефон.

    Решает всегда человек: касса только показывает находки.
    """
    rows = q("""SELECT p.id, p.number, p.name, p.phone, p.tg_id, p.created,
                       (SELECT COUNT(*) FROM entries e WHERE e.player_id=p.id) AS games
                FROM players p ORDER BY p.number""")
    by_key = {}
    for r in rows:
        keys = [("имя", (r["name"] or "").strip().lower())]
        if r["phone"]:
            keys.append(("телефон", r["phone"]))
        for kind, key in keys:
            if key:
                by_key.setdefault((kind, key), []).append(dict(r))
    out, seen = [], set()
    for (kind, key), items in by_key.items():
        if len(items) < 2:
            continue
        ids = tuple(sorted(i["id"] for i in items))
        if ids in seen:
            continue
        seen.add(ids)
        out.append({"why": kind, "players": sorted(items, key=lambda i: i["number"])})
    return out


def merge_players(keep_id, drop_id, admin=None):
    """Склеивает два профиля одного человека. История и деньги переезжают."""
    keep_id, drop_id = int(keep_id or 0), int(drop_id or 0)
    if not keep_id or not drop_id or keep_id == drop_id:
        return False, "Нужны два разных профиля"
    keep = q("SELECT * FROM players WHERE id=?", (keep_id,), one=True)
    drop = q("SELECT * FROM players WHERE id=?", (drop_id,), one=True)
    if not keep or not drop:
        return False, "Профиль не найден"

    x("UPDATE purchases SET player_id=? WHERE player_id=?", (keep_id, drop_id))
    x("UPDATE consents  SET player_id=? WHERE player_id=?", (keep_id, drop_id))
    for e in q("SELECT * FROM entries WHERE player_id=?", (drop_id,)):
        mine = q("SELECT * FROM entries WHERE tid=? AND player_id=?",
                 (e["tid"], keep_id), one=True)
        if not mine:
            x("UPDATE entries SET player_id=? WHERE id=?", (keep_id, e["id"]))
            continue
        # в одном турнире оба профиля — оставляем ту запись, где больше правды
        x("""UPDATE entries SET arrived=MAX(arrived,?), busted=MAX(busted,?),
                                place=CASE WHEN place=0 THEN ? ELSE place END,
                                points=MAX(points,?),
                                table_no=CASE WHEN table_no=0 THEN ? ELSE table_no END,
                                seat_no=CASE WHEN seat_no=0 THEN ? ELSE seat_no END
             WHERE id=?""",
          (e["arrived"], e["busted"], e["place"], e["points"],
           e["table_no"] or 0, e["seat_no"] or 0, mine["id"]))
        x("DELETE FROM entries WHERE id=?", (e["id"],))

    # сначала убираем лишний профиль, иначе телефон и Telegram не дадут скопировать
    number = min(keep["number"] or 0, drop["number"] or 0) or keep["number"]
    x("DELETE FROM players WHERE id=?", (drop_id,))
    x("""UPDATE players SET tg_id=COALESCE(tg_id, ?), phone=COALESCE(phone, ?),
                            username=COALESCE(username, ?), fio=COALESCE(fio, ?),
                            born=COALESCE(born, ?), id_ok=COALESCE(id_ok, ?),
                            number=?
         WHERE id=?""",
      (drop["tg_id"], drop["phone"], drop["username"], drop["fio"], drop["born"],
       drop["id_ok"], number, keep_id))
    log(admin or "admin",
        f"профили объединены: №{drop['number']} ({drop['name']}) → №{number} ({keep['name']})")
    return True, f"Объединено в профиль №{number} · {keep['name']}"


def save_player(tg_id, name, username, phone):
    """Регистрация игрока. Один человек — один профиль, номер не меняется."""
    phone = norm_phone(phone)
    row = find_player(tg_id, phone)
    if row:
        # номер клуба и дату вступления не трогаем никогда
        x("UPDATE players SET tg_id=COALESCE(?, tg_id), name=?, username=?, "
          "phone=COALESCE(phone, ?) WHERE id=?",
          (tg_id, name, username, phone, row["id"]))
        log(tg_id, f"вход в клуб: {name} — профиль №{row['number']} уже был")
        return q("SELECT * FROM players WHERE id=?", (row["id"],), one=True)
    num = next_number()
    try:
        pid = x("INSERT INTO players(tg_id, name, username, phone, number) VALUES(?,?,?,?,?)",
                (tg_id, name, username, phone, num))
    except sqlite3.IntegrityError:
        # кто-то успел зарегистрироваться тем же номером телефона или
        # аккаунтом — отдаём уже существующий профиль, а не создаём второй
        row = find_player(tg_id, phone)
        if row:
            return row
        raise
    log(tg_id, f"НОВЫЙ участник клуба №{num}: {name}")
    return q("SELECT * FROM players WHERE id=?", (pid,), one=True)


# ----------------------------------------------------------------------------
# ДОКУМЕНТЫ КЛУБА И ПОДПИСИ ПОД НИМИ
#
# Тексты лежат в public/docs/*.txt — обычные текстовые файлы, их правит клуб
# без программиста. В начале файла шапка: version, title, required. Версия —
# это редакция документа. Поменяли текст и подняли версию — у всех игроков
# снова попросят подпись, старые подписи при этом остаются в базе.
# ----------------------------------------------------------------------------

DOCS_DIR = os.path.join(PUBLIC, "docs")
DOC_ORDER = ("oferta", "pravila", "pdn")
_DOCS = {"stamp": None, "items": []}


def _yes(v):
    return str(v or "").strip().lower() in ("yes", "y", "да", "1", "true")


def load_docs():
    """Читает документы с диска. Файл изменился — перечитываем сами."""
    stamp = []
    for code in DOC_ORDER:
        p = os.path.join(DOCS_DIR, code + ".txt")
        stamp.append(os.path.getmtime(p) if os.path.isfile(p) else 0)
    if _DOCS["stamp"] == stamp:
        return _DOCS["items"]

    items = []
    for code in DOC_ORDER:
        p = os.path.join(DOCS_DIR, code + ".txt")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        head, _, body = raw.replace("\r\n", "\n").partition("\n\n")
        meta = {}
        for line in head.splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip().lower()] = v.strip()
        body = body.strip()
        items.append({
            "code": code,
            "title": meta.get("title") or code,
            "subtitle": meta.get("subtitle", ""),
            "version": meta.get("version") or "1",
            "required": _yes(meta.get("required", "yes")),
            "needs_fio": _yes(meta.get("needs_fio")),
            "check": meta.get("check") or "Я прочитал документ и согласен",
            "body": body,
            "hash": hashlib.sha256(body.encode()).hexdigest()[:16],
        })
    _DOCS.update(stamp=stamp, items=items)
    return items


def doc_by_code(code):
    for d in load_docs():
        if d["code"] == code:
            return d
    return None


def docs_signed(tg_id):
    """Что человек уже подписал: код документа → редакция и дата."""
    if not tg_id:
        return {}
    out = {}
    for r in q("SELECT code, version, ts FROM consents WHERE tg_id=? ORDER BY id", (tg_id,)):
        out[r["code"]] = {"version": r["version"], "ts": r["ts"]}
    return out


def docs_pending(tg_id):
    """Какие обязательные документы человек ещё не подписал (или подписал старую редакцию)."""
    signed = docs_signed(tg_id)
    out = []
    for d in load_docs():
        if not d["required"]:
            continue
        s = signed.get(d["code"])
        if not s or s["version"] != d["version"]:
            out.append(d["code"])
    return out


def needs_fio(tg_id):
    """Нужно ли спрашивать ФИО и дату рождения: только если такой документ не подписан."""
    pend = set(docs_pending(tg_id))
    return any(d["needs_fio"] and d["code"] in pend for d in load_docs())


def fio_ok(v):
    """Фамилия и имя как минимум. Отчество по желанию."""
    parts = [p for p in re.split(r"[\s]+", str(v or "").strip()) if p]
    if not 2 <= len(parts) <= 4:
        return False
    return all(re.fullmatch(r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z'\-]+", p) for p in parts)


def age_of(v):
    """Возраст по дате рождения в виде 1990-05-17. Непонятная дата — ноль."""
    try:
        d = datetime.strptime(str(v or "").strip()[:10], "%Y-%m-%d")
    except ValueError:
        return 0
    n = datetime.now()
    age = n.year - d.year - ((n.month, n.day) < (d.month, d.day))
    return age if 0 < age < 120 else 0


def born_text(v):
    """1990-05-17 → 17.05.1990, чтобы в документе стояла привычная дата."""
    try:
        return datetime.strptime(str(v or "").strip()[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return ""


def sign_docs(tg_id, codes, player=None, fio=None, born=None, ip=""):
    """Записывает подписи. Строки только добавляются — ничего не перезаписываем."""
    phone = (player["phone"] if player else "") or ""
    pid = player["id"] if player else None
    done = []
    for code in codes:
        d = doc_by_code(code)
        if not d:
            continue
        # Время ставим сами: datetime('now') в SQLite пишет UTC, а в документе
        # должно стоять местное время, как и в отметках администратора.
        x("""INSERT INTO consents(player_id, tg_id, code, version, doc_hash,
                                  fio, born, phone, ip, ts)
             VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (pid, tg_id, code, d["version"], d["hash"], fio or None, born or None, phone, ip,
           datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        done.append(d["title"])
    if pid and (fio or born):
        x("UPDATE players SET fio=COALESCE(?, fio), born=COALESCE(?, born) WHERE id=?",
          (fio or None, born or None, pid))
    if done:
        log(tg_id, "подписаны документы: " + "; ".join(done))
    return done


def docs_for(tg_id, player=None):
    """Документы с отметкой, что из них подписано — для экрана в приложении."""
    signed = docs_signed(tg_id)
    out = []
    for d in load_docs():
        s = signed.get(d["code"])
        # Текст отдаём как он есть, с метками {{ФИО}} и прочими: приложение
        # подставляет их на ходу, пока человек печатает.
        body = d["body"]
        out.append({
            "code": d["code"], "title": d["title"], "subtitle": d["subtitle"],
            "version": d["version"], "required": d["required"],
            "needs_fio": d["needs_fio"], "check": d["check"], "body": body,
            "signed": bool(s and s["version"] == d["version"]),
            "signed_at": (s["ts"] if s else ""),
            "signed_version": (s["version"] if s else ""),
        })
    return out


# ----------------------------------------------------------------------------
# АФИША: ОТКУДА БЕРУТСЯ ТУРНИРЫ
# ----------------------------------------------------------------------------

def create_tournament(dt, title=None, seats=None, buyin=None, meta=None, auto=0, admin=None):
    """Создаёт турнир на дату dt. Если турнир на это время уже есть — возвращает его."""
    base = CFG["tournament"]
    key = dt.strftime(FMT)
    exist = q("SELECT * FROM tournaments WHERE start=?", (key,), one=True)
    if exist:
        return exist["id"]
    nid = x("""INSERT INTO tournaments(title, start, date, time, weekday, buyin, reentry, addon,
                                       stack, seats, meta, theme, status, auto)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
            (title or base["title"], key, date_text(dt), dt.strftime("%H:%M"), weekday_text(dt),
             int(buyin if buyin is not None else base.get("buyin", 0)),
             int(base.get("reentry", 0)), int(base.get("addon", 0)),
             int(base.get("stack", 0)), int(seats or base.get("seats", 36)),
             meta or base.get("meta"), base.get("theme", ""), int(auto)))
    log(admin, f"создан турнир #{nid} на {key}")
    return nid


def ensure_events(quiet=True):
    """Достраивает афишу: разовые события из config.json плюс расписание клуба."""
    made = []

    for ev in CFG.get("events") or []:
        dt = parse_dt(f"{ev.get('date', '')} {ev.get('time') or CFG['tournament']['time']}")
        if not dt or dt < now() - timedelta(hours=12):
            continue
        known = q("SELECT 1 FROM tournaments WHERE start=?", (dt.strftime(FMT),), one=True)
        nid = create_tournament(dt, ev.get("title"), ev.get("seats"), ev.get("buyin"),
                                ev.get("meta"))
        if not known:
            made.append(nid)

    sch = CFG.get("schedule") or {}
    if sch.get("on"):
        days = [str(d).lower()[:2] for d in sch.get("days", [])]
        hh, mm = ((sch.get("time") or "18:00").split(":") + ["00"])[:2]
        for i in range(int(sch.get("weeks_ahead", 2)) * 7 + 1):
            day = (now() + timedelta(days=i)).replace(hour=int(hh), minute=int(mm))
            if WD_SHORT[day.weekday()] not in days or day < now():
                continue
            known = q("SELECT 1 FROM tournaments WHERE start=?", (day.strftime(FMT),), one=True)
            nid = create_tournament(day, auto=1)
            if not known:
                made.append(nid)

    if made and not quiet:
        print(f"Афиша достроена: новых турниров {len(made)}")
    return made


def auto_live():
    """За 10 минут до старта турнир открывается — касса переключается на него сама."""
    before = int(CFG.get("open_before_min", 10))
    for r in q("SELECT * FROM tournaments WHERE status='open'"):
        dt = parse_dt(r["start"])
        if dt and now() >= dt - timedelta(minutes=before):
            x("UPDATE tournaments SET status='live' WHERE id=?", (r["id"],))
            log("сервер", f"турнир #{r['id']} открыт в кассе")
            notify_start(r["id"])


def notify_start(t_id):
    """Сообщение записавшимся, что турнир начинается."""
    row = t_row(t_id)
    if not row or not CFG.get("bot_token"):
        return
    dt = parse_dt(row["start"])
    for r in q("""SELECT p.tg_id FROM entries e JOIN players p ON p.id=e.player_id
                  WHERE e.tid=? AND e.wait=0 AND p.tg_id IS NOT NULL""", (t_id,)):
        send(r["tg_id"], f"<b>{row['title']}</b> начинается в "
                         f"{dt.strftime('%H:%M') if dt else 'ближайшее время'}. "
                         "Подойдите к администратору, чтобы оплатить вход.")
        time.sleep(0.05)


def ticker():
    """Раз в минуту: открыть турнир, если пора, и время от времени достроить афишу."""
    last_backup = 0
    while True:
        try:
            auto_live()
            if now().minute % 30 == 0:
                ensure_events()
            # копия базы раз в три часа: единственная защита от «всё пропало»
            if time.time() - last_backup > 3 * 3600:
                last_backup = time.time()
                backup_db()
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ----------------------------------------------------------------------------
# ТЕКУЩИЙ ТУРНИР
# ----------------------------------------------------------------------------

def t_row(t_id):
    return q("SELECT * FROM tournaments WHERE id=?", (t_id,), one=True)


def tid():
    """Турнир, с которым сейчас работает касса.

    Выбирается сам: идущий турнир, иначе ближайший из афиши. Администратор
    может закрепить любой другой турнир кнопкой в кассе.
    """
    pin = setting("pin_tid")
    if pin and pin.isdigit() and t_row(int(pin)):
        return int(pin)
    # среди открытых в кассе берём тот, что начался последним
    r = q("SELECT id FROM tournaments WHERE status='live' ORDER BY start DESC LIMIT 1", one=True)
    if r:
        return r["id"]
    edge = (now() - timedelta(hours=12)).strftime(FMT)
    r = q("SELECT id FROM tournaments WHERE status!='finished' AND start>=? ORDER BY start LIMIT 1",
          (edge,), one=True)
    if r:
        return r["id"]
    r = q("SELECT id FROM tournaments ORDER BY start DESC LIMIT 1", one=True)
    return r["id"] if r else 0


# ----------------------------------------------------------------------------
# СТАДИИ ТУРНИРА
#
#   rebuy — ребай-период: открыт вход за 1500 и ребай за 1500
#   addon — перерыв после последнего ребай-уровня: только аддон, один раз
#   final — финальная стадия: покупок нет, играем до победителя
#
# Стадию переключает касса кнопкой, а не часы сервера: таймер можно ставить
# на паузу, и время на сервере с ним разъедется.
# ----------------------------------------------------------------------------

STAGES = ("rebuy", "addon", "play", "final")
STAGE_TEXT = {"rebuy": "Ребай-период", "addon": "Перерыв: аддон и поздняя регистрация",
              "play": "Основная игра", "final": "Финальный стол"}
STAGE_HINT = {
    "rebuy": "Открыт вход и ребаи",
    "addon": "Вход, ребай и аддон по 1500 ₽ — дают 50 000 фишек",
    "play": "Покупок нет. Финальный стол соберётся сам, когда останется 9 игроков",
    "final": "Девять за одним столом, играем до победителя",
}
NEXT_STAGE = {"rebuy": "addon", "addon": "play", "play": "", "final": ""}
NEXT_LABEL = {"rebuy": "Закрыть ребаи → перерыв с аддоном", "addon": "Закончить перерыв",
              "play": "", "final": ""}


def final_at():
    return max(2, min(10, int(CFG.get("final_at", 9))))


def check_final(admin=None):
    """Как только в игре остаётся девять — собираем финальный стол сам."""
    row = t_row(tid())
    if not row or row["status"] == "finished":
        return False
    # В перерыв ещё заходят новые игроки, поэтому финал там не собираем
    if stage_of(row) != "play":
        return False
    if alive_count(row["id"]) > final_at():
        return False
    x("UPDATE tournaments SET stage='final' WHERE id=?", (row["id"],))
    # доборы встают из-за стола: финал играют только участники
    x("DELETE FROM entries WHERE tid=? AND COALESCE(house,0)=1", (row["id"],))
    rebalance(admin)   # финальный стол — всегда один, даже при ручной рассадке
    log("сервер", f"турнир #{row['id']}: собран финальный стол")
    notify_players(row["id"], "Собран финальный стол. Удачи!")
    return True


def timer_state():
    """Состояние главного таймера, если он его присылал."""
    raw = setting("timer_state")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def final_table():
    """Боксы финального стола: место → игрок. Пустые места тоже возвращаем."""
    row = t_row(tid())
    if not row or stage_of(row) != "final":
        return []
    rows = q("""SELECT p.name, p.username, p.number, e.seat_no FROM entries e
                JOIN players p ON p.id = e.player_id
                WHERE e.tid=? AND e.arrived=1 AND e.busted=0 AND COALESCE(e.house,0)=0
                ORDER BY e.seat_no""", (row["id"],))
    by_seat = {r["seat_no"]: r for r in rows if r["seat_no"]}
    out = []
    for seat in range(1, final_at() + 1):
        r = by_seat.get(seat)
        out.append({"seat": seat,
                    "name": r["name"] if r else "",
                    "nick": ("@" + r["username"]) if r and r["username"] else "",
                    "number": r["number"] if r else 0})
    return out


def stage_of(row):
    if not row:
        return "rebuy"
    keys = row.keys() if hasattr(row, "keys") else []
    s = row["stage"] if "stage" in keys else "rebuy"
    return s if s in STAGES else "rebuy"


def set_stage(stage, admin=None):
    """Переводит текущий турнир на следующую стадию."""
    if stage not in STAGES:
        return False, "Неизвестная стадия"
    row = t_row(tid())
    if not row:
        return False, "Турнир не найден"
    if row["status"] == "finished":
        return False, "Турнир уже завершён"
    if stage_of(row) == stage:
        return False, "Эта стадия уже идёт"
    x("UPDATE tournaments SET stage=?, status='live' WHERE id=?", (stage, row["id"]))
    log(admin, f"турнир #{row['id']}: стадия {stage}")
    if stage == "addon":
        price = row["addon"] or CFG["tournament"]["addon"]
        notify_players(row["id"],
                       "Ребай-период закончен. Перерыв — аддон "
                       f"{price} ₽, один раз каждому. Дальше финальная стадия.")
    if stage == "play":
        # В ручном режиме рассадка ваша — не трогаем. В автоматическом
        # собираем столы поровну: новых входов дальше не будет.
        if auto_seat_on():
            rebalance(admin)
        notify_players(row["id"], "Аддон-тайм закончен, покупок больше нет. "
                                  f"Финальный стол соберётся при {final_at()} игроках.")
        if check_final(admin):
            return True, "Стадия: " + STAGE_TEXT["final"]
    return True, "Стадия: " + STAGE_TEXT[stage]


def notify_players(t_id, text):
    """Сообщение всем, кто сейчас за столом."""
    if not CFG.get("bot_token"):
        return
    for r in q("""SELECT p.tg_id FROM entries e JOIN players p ON p.id=e.player_id
                  WHERE e.tid=? AND e.arrived=1 AND e.busted=0 AND p.tg_id IS NOT NULL""",
               (t_id,)):
        send(r["tg_id"], text)
        time.sleep(0.05)


def reg_open(row):
    """Можно ли записаться: до старта всегда, после — пока идёт ребай-период."""
    if not row or row["status"] == "finished":
        return False
    if row["status"] == "live":
        return stage_of(row) == "rebuy"
    return True


def can_cancel(row):
    """Отписаться самому можно, пока до старта больше 10 минут."""
    if not row or row["status"] == "finished":
        return False
    dt = parse_dt(row["start"])
    return bool(dt) and now() < dt - timedelta(minutes=int(CFG.get("cancel_before_min", 10)))


def t_info(row):
    """Строка турнира → словарь для приложения и кассы."""
    base = CFG["tournament"]
    if not row:
        dt = now().replace(hour=18, minute=0)
        return {"id": 0, "title": base["title"], "start": dt.strftime(FMT),
                "date": date_text(dt), "time": base["time"], "weekday": weekday_text(dt),
                "tag": tag_text(dt), "when": when_text(dt), "status": "open",
                "buyin": base["buyin"], "reentry": base["reentry"], "addon": base["addon"],
                "stack": base["stack"], "seats": base["seats"], "meta": base["meta"],
                "theme": "", "taken": 0, "free": base["seats"], "waiting": 0,
                "starts_in": 0, "reg_open": False, "can_cancel": False, "late": False,
                "stage": "rebuy", "stage_text": STAGE_TEXT["rebuy"],
                "stage_hint": STAGE_HINT["rebuy"], "next_stage": "addon",
                "next_label": NEXT_LABEL["rebuy"],
                "empty": True}
    dt = parse_dt(row["start"]) or now()
    seats = row["seats"] or base["seats"]
    tk = seated_count(row["id"])
    st = stage_of(row)
    return {
        "id": row["id"],
        "title": row["title"] or base["title"],
        "start": row["start"],
        "date": row["date"] or date_text(dt),
        "time": row["time"] or dt.strftime("%H:%M"),
        "weekday": row["weekday"] or weekday_text(dt),
        "tag": tag_text(dt),
        "when": when_text(dt),
        "status": row["status"],
        "buyin": row["buyin"] or base["buyin"],
        "reentry": row["reentry"] if row["reentry"] is not None else base["reentry"],
        "addon": row["addon"] if row["addon"] is not None else base["addon"],
        "stack": row["stack"] or base["stack"],
        "seats": seats,
        "meta": row["meta"] or base["meta"],
        "theme": row["theme"] or "",
        "taken": tk,
        "free": max(0, seats - tk),
        "waiting": q("SELECT COUNT(*) AS c FROM entries WHERE tid=? AND wait=1",
                     (row["id"],), one=True)["c"],
        "starts_in": minutes_left(dt),
        "reg_open": reg_open(row),
        "can_cancel": can_cancel(row),
        "late": row["status"] != "finished" and now() > dt,
        "stage": st,
        "stage_text": STAGE_TEXT[st],
        "stage_hint": STAGE_HINT[st],
        "next_stage": NEXT_STAGE[st],
        "next_label": NEXT_LABEL[st],
        "tables": tables_count(),
        "seats_per_table": per_table(),
        "empty": False,
    }


def current_tournament():
    """Данные текущего турнира одним словарём."""
    return t_info(t_row(tid()))


def feed(limit=8):
    """Лента афиши: идущие и будущие турниры по порядку."""
    edge = (now() - timedelta(hours=12)).strftime(FMT)
    rows = q("""SELECT * FROM tournaments WHERE start>=? AND status!='finished'
                ORDER BY start LIMIT ?""", (edge, limit))
    if not rows:
        rows = q("SELECT * FROM tournaments ORDER BY start DESC LIMIT 1")
    return [t_info(r) for r in rows]


def t_status(t_id=None):
    row = t_row(t_id or tid())
    return row["status"] if row else "open"


def taken(t_id=None):
    return q("SELECT COUNT(*) AS c FROM entries WHERE tid=?", (t_id or tid(),), one=True)["c"]


def alive_count(t_id=None):
    """Сколько игроков в игре. Управляющие на доборе не считаются."""
    return q("""SELECT COUNT(*) AS c FROM entries
                WHERE tid=? AND arrived=1 AND busted=0 AND COALESCE(house,0)=0""",
             (t_id or tid(),), one=True)["c"]


def seated_count(t_id=None):
    """Сколько человек записано на места (без листа ожидания)."""
    return q("SELECT COUNT(*) AS c FROM entries WHERE tid=? AND wait=0",
             (t_id or tid(),), one=True)["c"]


# ----------------------------------------------------------------------------
# ЗАПИСЬ НА ТУРНИР
# ----------------------------------------------------------------------------

def register_player(player_id, t_id=None):
    t_id = int(t_id or tid())
    row = t_row(t_id)
    if not row:
        return False, "Турнир не найден"
    if row["status"] == "finished":
        return False, "Турнир уже завершён"
    if not reg_open(row):
        return False, "Регистрация на этот турнир закрыта"
    if q("SELECT 1 FROM entries WHERE tid=? AND player_id=?", (t_id, player_id), one=True):
        return False, "Вы уже записаны"
    seats = row["seats"] or CFG["tournament"]["seats"]
    wait = 1 if seated_count(t_id) >= seats else 0
    x("INSERT INTO entries(tid, player_id, wait) VALUES(?,?,?)", (t_id, player_id, wait))
    log(player_id, f"запись на турнир #{t_id}" + (" (лист ожидания)" if wait else ""))
    if wait:
        return True, f"Мест нет, вы в листе ожидания под номером {waiting_no(player_id, t_id)}"
    dt = parse_dt(row["start"])
    return True, ("Вы записаны — ждём " + when_text(dt)) if dt else "Вы записаны"


def waiting_no(player_id, t_id=None):
    """Какой по счёту игрок в листе ожидания."""
    rows = q("SELECT player_id FROM entries WHERE tid=? AND wait=1 ORDER BY id", (t_id or tid(),))
    for i, r in enumerate(rows, 1):
        if r["player_id"] == player_id:
            return i
    return 0


def promote_from_waitlist(t_id=None):
    """Освободилось место — первый из листа ожидания занимает его."""
    t_id = int(t_id or tid())
    row = t_row(t_id)
    seats = (row["seats"] if row else 0) or CFG["tournament"]["seats"]
    if seated_count(t_id) >= seats:
        return None
    e = q("SELECT * FROM entries WHERE tid=? AND wait=1 ORDER BY id LIMIT 1", (t_id,), one=True)
    if not e:
        return None
    x("UPDATE entries SET wait=0 WHERE id=?", (e["id"],))
    p = q("SELECT * FROM players WHERE id=?", (e["player_id"],), one=True)
    if p and p["tg_id"] and row:
        dt = parse_dt(row["start"])
        send(p["tg_id"], f"Освободилось место на турнире «{row['title']}» "
                         f"{when_text(dt) if dt else ''} — вы в основном списке. Ждём вас!")
    log(e["player_id"], f"переведён из листа ожидания, турнир #{t_id}")
    return e["player_id"]


def unregister_player(player_id, t_id=None):
    t_id = int(t_id or tid())
    row = t_row(t_id)
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (t_id, player_id), one=True)
    if not e:
        return False, "Вы не записаны"
    if e["arrived"]:
        return False, "Вход уже оплачен, отмена только у администратора"
    if not can_cancel(row):
        return False, ("До начала меньше 10 минут — список уже закрыт. "
                       "Если не получается прийти, скажите администратору")
    x("DELETE FROM entries WHERE id=?", (e["id"],))
    log(player_id, f"отмена записи, турнир #{t_id}")
    promote_from_waitlist(t_id)
    return True, "Запись отменена"


# ----------------------------------------------------------------------------
# КАССА
# ----------------------------------------------------------------------------

def tables_count():
    return max(1, int(CFG.get("tables", 4)))


def per_table():
    return max(2, min(10, int(CFG.get("seats_per_table", 9))))


MIN_TABLE = 6      # меньше шести за столом — игра не идёт, нужен добор


def tables_needed(n):
    """Сколько столов нужно на n человек: минимум, но не больше, чем есть в клубе."""
    per, tc = per_table(), tables_count()
    return max(1, min(tc, -(-max(0, n) // per))) if n else 1


def free_seat(t_id):
    """Куда посадить пришедшего: первое свободное место по порядку столов.

    Столы заполняются один за другим, а не поровну. Так недобор бывает только
    за тем столом, который сейчас набирается, и сотрудников приходится сажать
    в одно место, а не за три стола сразу.
    """
    per, tc = per_table(), tables_count()
    used = {}
    for r in q("""SELECT table_no, seat_no FROM entries
                  WHERE tid=? AND arrived=1 AND busted=0 AND table_no>0""", (t_id,)):
        used.setdefault(r["table_no"], set()).add(r["seat_no"])
    for t in range(1, tc + 1):
        busy = used.get(t, set())
        if len(busy) >= per:
            continue
        for seat in range(1, per + 1):
            if seat not in busy:
                return t, seat
    return 0, 0


def house_at(t_id, table=None):
    """Сотрудники на доборе: за каким столом и на каком месте сидят."""
    sql = """SELECT e.id, e.player_id, e.table_no, e.seat_no FROM entries e
             WHERE e.tid=? AND e.arrived=1 AND e.busted=0 AND COALESCE(e.house,0)=1
             AND e.table_no>0"""
    args = [t_id]
    if table:
        sql += " AND e.table_no=?"
        args.append(table)
    return q(sql + " ORDER BY e.table_no, e.seat_no DESC", tuple(args))


def real_at(t_id, table):
    return q("""SELECT COUNT(*) AS c FROM entries WHERE tid=? AND arrived=1 AND busted=0
                AND COALESCE(house,0)=0 AND table_no=?""", (t_id, table), one=True)["c"]


def free_house_seats(t_id, table, admin=None):
    """Сотрудники встают, как только за столом хватает живых игроков."""
    freed = []
    for h in house_at(t_id, table):
        if real_at(t_id, table) < MIN_TABLE:
            break
        nm = q("SELECT name FROM players WHERE id=?", (h["player_id"],), one=True)
        x("DELETE FROM entries WHERE id=?", (h["id"],))
        freed.append(nm["name"] if nm else "управляющий")
        log(admin, f"добор снят: {freed[-1]} со стола {table}")
    return freed


def seat_player(entry_id, t_id, admin=None):
    """Сажает пришедшего на первое свободное место. Живых игроков не двигает.

    Если мест нет только потому, что за столами сидят сотрудники на доборе,
    один из них встаёт и отдаёт место клиенту.
    """
    t, s = free_seat(t_id)
    if not t:
        h = house_at(t_id)
        if h:
            nm = q("SELECT name FROM players WHERE id=?", (h[0]["player_id"],), one=True)
            t, s = h[0]["table_no"], h[0]["seat_no"]
            x("DELETE FROM entries WHERE id=?", (h[0]["id"],))
            log(admin, f"добор снят: {nm['name'] if nm else 'управляющий'} отдал место")
    if not t:
        return 0, 0
    x("UPDATE entries SET table_no=?, seat_no=? WHERE id=?", (t, s, entry_id))
    return t, s


def auto_seat_on():
    """Сажает ли касса сама. Можно переключить прямо в кассе."""
    v = setting("seat_mode")
    if v in ("auto", "manual"):
        return v == "auto"
    return bool(CFG.get("auto_seat", True))


def seat_set(player_id, table, seat, admin=None):
    """Сажает игрока на конкретное место. Если место занято — меняет местами."""
    per, tc = per_table(), tables_count()
    try:
        table, seat = int(table), int(seat)
    except (TypeError, ValueError):
        return False, "Непонятное место"
    if not (1 <= table <= tc and 1 <= seat <= per):
        return False, f"Стол 1–{tc}, место 1–{per}"
    t_id = tid()
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (t_id, player_id), one=True)
    if not e:
        return False, "Игрока нет в турнире"
    if not e["arrived"]:
        return False, "Сначала отметьте вход"
    if e["busted"]:
        return False, "Игрок выбыл — сначала верните его в игру"

    busy = q("""SELECT * FROM entries WHERE tid=? AND arrived=1 AND busted=0
                AND table_no=? AND seat_no=?""", (t_id, table, seat), one=True)
    if busy and busy["id"] != e["id"]:
        # меняем двоих местами — так проще всего пересадить, не освобождая место
        x("UPDATE entries SET table_no=?, seat_no=? WHERE id=?",
          (e["table_no"], e["seat_no"], busy["id"]))
    x("UPDATE entries SET table_no=?, seat_no=? WHERE id=?", (table, seat, e["id"]))
    log(admin, f"игрок {player_id} посажен за стол {table}, место {seat}")
    who = q("SELECT name FROM players WHERE id=?", (player_id,), one=True)
    nm = who["name"] if who else "Игрок"
    if busy and busy["id"] != e["id"]:
        other = q("SELECT p.name FROM entries e JOIN players p ON p.id=e.player_id "
                  "WHERE e.id=?", (busy["id"],), one=True)
        return True, f"{nm} и {other['name'] if other else 'игрок'} поменялись местами"
    return True, f"{nm} · стол {table}, место {seat}"


def seat_free(player_id, admin=None):
    """Поднимает игрока с места, не трогая его участие в турнире."""
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    if not e:
        return False, "Игрока нет в турнире"
    x("UPDATE entries SET table_no=0, seat_no=0 WHERE id=?", (e["id"],))
    log(admin, f"игрок {player_id} поднят с места")
    return True, "Игрок без места"


def house_add(name, table, seat, admin=None):
    """Сажает управляющего, чтобы стол играл. В турнире он не участвует."""
    name = (name or "").strip() or "Управляющий"
    t_id = tid()
    row = t_row(t_id)
    if not row or row["status"] == "finished":
        return False, "Турнир завершён"
    p = q("SELECT * FROM players WHERE lower(name)=lower(?)", (name,), one=True)
    pid = p["id"] if p else x("INSERT INTO players(name, number) VALUES(?,?)",
                              (name, next_number()))
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (t_id, pid), one=True)
    if e and not e["house"]:
        return False, f"{name} уже играет в этом турнире"
    if not e:
        x("INSERT INTO entries(tid, player_id, arrived, house) VALUES(?,?,1,1)", (t_id, pid))
    else:
        x("UPDATE entries SET arrived=1, busted=0, house=1 WHERE id=?", (e["id"],))
    ok, msg = seat_set(pid, table, seat, admin)
    if not ok:
        return False, msg
    log(admin, f"добор: {name} за стол {table}, место {seat}")
    return True, f"{name} сел добить стол {table}"


def house_remove(player_id, admin=None):
    """Убирает управляющего со стола. Денег за ним нет, поэтому просто стираем."""
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    if not e:
        return False, "Его нет за столом"
    if not e["house"]:
        return False, "Это обычный игрок, а не добор"
    x("DELETE FROM entries WHERE id=?", (e["id"],))
    log(admin, f"добор снят: игрок {player_id}")
    return True, "Управляющий встал из-за стола"


def house_names():
    return {r["player_id"] for r in q(
        "SELECT player_id FROM entries WHERE tid=? AND COALESCE(house,0)=1", (tid(),))}


def seat_map():
    """Полная карта столов: каждое место с игроком или пустое."""
    per, tc = per_table(), tables_count()
    rows = q("""SELECT p.id, p.name, p.number, e.table_no, e.seat_no,
                       COALESCE(e.house,0) AS house FROM entries e
                JOIN players p ON p.id=e.player_id
                WHERE e.tid=? AND e.arrived=1 AND e.busted=0""", (tid(),))
    at = {(r["table_no"], r["seat_no"]): r for r in rows if r["table_no"]}
    out = []
    for t in range(1, tc + 1):
        seats = []
        for s_ in range(1, per + 1):
            r = at.get((t, s_))
            seats.append({"seat": s_, "player_id": r["id"] if r else 0,
                          "name": r["name"] if r else "", "number": r["number"] if r else 0,
                          "house": bool(r["house"]) if r else False})
        taken = sum(1 for x_ in seats if x_["player_id"])
        out.append({"table": t, "seats": seats, "taken": taken,
                    "players": sum(1 for x_ in seats if x_["player_id"] and not x_["house"]),
                    "small": 0 < taken < MIN_TABLE})
    return {"tables": out, "per": per, "min_table": MIN_TABLE,
            "noseat": [{"player_id": r["id"], "name": r["name"], "number": r["number"]}
                       for r in rows if not r["table_no"] and not r["house"]],
            "auto": auto_seat_on()}


def clear_seat(entry_id):
    x("UPDATE entries SET table_no=0, seat_no=0 WHERE id=?", (entry_id,))


def rebalance(admin=None):
    """Пересобирает столы: игроков поровну на минимально нужное число столов."""
    per = per_table()
    rows = q("""SELECT id FROM entries WHERE tid=? AND arrived=1 AND busted=0
                AND COALESCE(house,0)=0 ORDER BY table_no, seat_no""", (tid(),))
    ids = [r["id"] for r in rows]
    if not ids:
        return 0, 0
    need = tables_needed(len(ids))
    for i, eid in enumerate(ids):
        x("UPDATE entries SET table_no=?, seat_no=? WHERE id=?",
          (i % need + 1, i // need + 1, eid))
    log(admin, f"столы пересобраны: {len(ids)} игроков на {need}")
    return len(ids), need


def purchase(player_id, kind, admin=None):
    """Вход, ребай или аддон. Возвращает (успех, сообщение)."""
    row = t_row(tid())
    if not row:
        return False, "Турнир не найден"
    if row["status"] == "finished":
        return False, "Турнир уже завершён"
    stage = stage_of(row)
    t = t_info(row)
    price = {"buyin": t["buyin"], "reentry": t["reentry"], "addon": t["addon"]}.get(kind)
    if price is None:
        return False, "Неизвестная операция"

    if kind in ("buyin", "reentry") and stage not in ("rebuy", "addon"):
        return False, "Перерыв закончен — входов, ребаев и аддонов больше нет"
    if kind == "addon":
        if not price:
            return False, "Аддон в этом турнире не продаётся"
        if stage == "rebuy":
            return False, ("Аддон продаётся в перерыв после 10 уровня. "
                           "Нажмите «Закрыть ребаи», когда перерыв начнётся")
        if stage != "addon":
            return False, "Перерыв закончился, покупок больше нет"
        if q("SELECT 1 FROM purchases WHERE tid=? AND player_id=? AND kind='addon'",
             (row["id"], player_id), one=True):
            return False, "Аддон уже взят"

    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (row["id"], player_id), one=True)
    if not e:
        x("INSERT INTO entries(tid, player_id) VALUES(?,?)", (row["id"], player_id))
        e = q("SELECT * FROM entries WHERE tid=? AND player_id=?",
              (row["id"], player_id), one=True)

    big = (stage == "addon")
    chips = int(CFG["tournament"].get("addon_stack", 50000)) if big \
        else int(CFG["tournament"].get("stack", 25000))
    seat_msg = ""
    if kind == "buyin":
        if e["arrived"]:
            return False, "Вход уже оплачен"
        x("UPDATE entries SET arrived=1, busted=0, place=0, wait=0 WHERE id=?", (e["id"],))
        if auto_seat_on():
            tb, st = seat_player(e["id"], row["id"], admin)
            seat_msg = f" · стол {tb}, место {st}" if tb else " · свободных мест нет"
            if tb:
                gone = free_house_seats(row["id"], tb, admin)
                if gone:
                    seat_msg += " · " + ", ".join(gone) + (" встал" if len(gone) == 1 else " встали")
        else:
            seat_msg = " · посадите за стол"
    elif kind == "reentry":
        if not e["arrived"]:
            return False, "Сначала оплатите вход"
        if not e["busted"]:
            return False, "Игрок ещё в игре — ребай берут, когда кончился стек"
        x("UPDATE entries SET busted=0, place=0 WHERE id=?", (e["id"],))
        if auto_seat_on():
            tb, st = seat_player(e["id"], row["id"], admin)
            seat_msg = f" · стол {tb}, место {st}" if tb else " · свободных мест нет"
            if tb:
                gone = free_house_seats(row["id"], tb, admin)
                if gone:
                    seat_msg += " · " + ", ".join(gone) + (" встал" if len(gone) == 1 else " встали")
        else:
            seat_msg = " · посадите за стол"
    elif kind == "addon":
        if not e["arrived"] or e["busted"]:
            return False, "Игрок не за столом"

    x("INSERT INTO purchases(tid, player_id, kind, amount, by_admin) VALUES(?,?,?,?,?)",
      (row["id"], player_id, kind, price, admin))
    log(admin, f"{kind} игроку {player_id}")
    what = {"buyin": "Вход оплачен", "reentry": "Ребай", "addon": "Аддон"}[kind]
    return True, f"{what} · {chips:,} фишек".replace(",", " ") + seat_msg


def bust(player_id, admin=None):
    """Стек кончился. В ребай-период это ещё не место, а повод взять ребай."""
    row = t_row(tid())
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?",
          (row["id"] if row else 0, player_id), one=True)
    if not e or not e["arrived"]:
        return False, "Игрок не за столом"
    if e["busted"]:
        return False, "Уже отмечен"
    if stage_of(row) == "rebuy":
        x("UPDATE entries SET busted=1, place=0, table_no=0, seat_no=0 WHERE id=?", (e["id"],))
        log(admin, f"кончился стек у игрока {player_id}")
        return True, "Стек кончился — можно взять ребай"
    place = alive_count()          # сколько осталось вместе с ним — это его место
    x("UPDATE entries SET busted=1, place=?, table_no=0, seat_no=0 WHERE id=?",
      (place, e["id"]))
    log(admin, f"выбыл игрок {player_id}, место {place}")
    msg = f"{place} место"
    if check_final(admin):
        return True, msg + " · собран финальный стол"
    # Столы сами не пересобираем: рассадка остаётся той, что сделали вы.
    # Если стол стал маленьким — касса подскажет, а собирать вам.
    return True, msg


def make_seating(admin=None):
    """Заново раскидывает всех, кто за столом, случайным образом."""
    import random
    per, tc = per_table(), tables_count()
    rows = q("SELECT e.id FROM entries e WHERE e.tid=? AND e.arrived=1 AND e.busted=0", (tid(),))
    ids = [r["id"] for r in rows]
    random.shuffle(ids)
    need = max(1, min(tc, -(-len(ids) // per))) if ids else 1
    for i, eid in enumerate(ids):
        x("UPDATE entries SET table_no=?, seat_no=? WHERE id=?",
          (i % need + 1, i // need + 1, eid))
    log(admin, f"рассадка: {len(ids)} игроков")
    return len(ids)


def seating():
    """Кто за каким столом сидит — все столы клуба, включая пустые."""
    rows = q("""SELECT p.name, p.number, e.table_no, e.seat_no FROM entries e
                JOIN players p ON p.id = e.player_id
                WHERE e.tid=? AND e.arrived=1 AND e.busted=0 AND e.table_no > 0
                ORDER BY e.table_no, e.seat_no""", (tid(),))
    tables = {}
    for r in rows:
        tables.setdefault(r["table_no"], []).append(
            {"seat": r["seat_no"], "name": r["name"], "number": r["number"]})
    out = []
    for t in range(1, tables_count() + 1):
        pl = tables.get(t, [])
        if pl or t <= tables_count():
            out.append({"table": t, "players": pl, "free": max(0, per_table() - len(pl))})
    for t in sorted(k for k in tables if k > tables_count()):
        out.append({"table": t, "players": tables[t], "free": 0})
    return out


def entry_money(player_id, t_id=None):
    """Сколько этот игрок уже оплатил в текущем турнире."""
    r = q("""SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS sum FROM purchases
             WHERE tid=? AND player_id=?""", (t_id or tid(), player_id), one=True)
    return r["n"], r["sum"]


def remove_entry(player_id, admin=None, force=False):
    """Стирает запись игрока вместе с оплатами. Только для ошибок кассира.

    Это НЕ выбывание: для выбывания есть bust(). Если у человека есть оплаты,
    по умолчанию отказываем — иначе одним нажатием уходит выручка вечера.
    """
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    if not e:
        return False, "Игрока нет в турнире"
    n, total = entry_money(player_id)
    if n and not force:
        return False, (f"У игрока {n} {plural(n, 'оплата', 'оплаты', 'оплат')} "
                       f"на {total} ₽. Если он выбыл — нажмите «Выбыл»")
    x("DELETE FROM purchases WHERE tid=? AND player_id=?", (tid(), player_id))
    x("DELETE FROM entries WHERE id=?", (e["id"],))
    log(admin, f"стёрта запись игрока {player_id}" + (f" с оплатами на {total} ₽" if n else ""))
    promote_from_waitlist()
    return True, "Запись стёрта" + (f", выручка уменьшилась на {total} ₽" if n else "")


def unbust(player_id, admin=None):
    """Вернуть игрока в игру, если выбывание отметили по ошибке."""
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    if not e or not e["busted"]:
        return False, "Игрок и так в игре"
    x("UPDATE entries SET busted=0, place=0 WHERE id=?", (e["id"],))
    log(admin, f"игрок {player_id} возвращён в игру")
    return True, "Игрок снова в игре"


def plural(n, one, few, many):
    """Правильное окончание: 1 игрок, 2 игрока, 5 игроков."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def award_points(t_id):
    """Начисляет очки сезона по занятым местам."""
    pts = CFG["points"]
    n = 0
    for e in q("SELECT * FROM entries WHERE tid=? AND arrived=1 AND COALESCE(house,0)=0",
               (t_id,)):
        place = e["place"] or 0
        p = pts[place - 1] if 0 < place <= len(pts) else CFG["points_rest"]
        x("UPDATE entries SET points=? WHERE id=?", (p, e["id"]))
        n += 1
    return n


def set_test(t_id, on, admin=None):
    """Тестовый турнир: сыгран по-настоящему, но в рейтинг не идёт.

    Деньги и список игроков остаются на месте — убираются только очки,
    места в профилях и запись в истории. Можно вернуть обратно.
    """
    t_id = int(t_id or 0)
    row = t_row(t_id)
    if not row:
        return False, "Турнир не найден"
    if on:
        x("UPDATE tournaments SET test=1 WHERE id=?", (t_id,))
        x("UPDATE entries SET points=0 WHERE tid=?", (t_id,))
        log(admin or "admin", f"турнир #{t_id} отмечен тестовым, очки сняты")
        return True, f"«{row['title']}» больше не идёт в рейтинг"
    x("UPDATE tournaments SET test=0 WHERE id=?", (t_id,))
    if row["status"] == "finished":
        award_points(t_id)
    log(admin or "admin", f"турнир #{t_id} снова в зачёте")
    return True, f"«{row['title']}» снова идёт в рейтинг"


def finish_tournament(admin=None):
    """Закрывает турнир и начисляет очки рейтинга."""
    if t_status() == "finished":
        return False, "Турнир уже завершён"
    rest = q("""SELECT * FROM entries WHERE tid=? AND arrived=1 AND busted=0
                AND COALESCE(house,0)=0""", (tid(),))
    if len(rest) > 1:
        return False, (f"В игре ещё {len(rest)} {plural(len(rest), 'игрок', 'игрока', 'игроков')}. "
                       "Отметьте выбывших — тот, кто останется последним, получит первое место")
    if len(rest) == 1:
        x("UPDATE entries SET busted=1, place=1 WHERE id=?", (rest[0]["id"],))
    award_points(tid())
    # записался и не пришёл — незачем хранить в сыгранном турнире
    x("DELETE FROM entries WHERE tid=? AND (arrived=0 OR COALESCE(house,0)=1)", (tid(),))
    x("UPDATE tournaments SET status='finished' WHERE id=?", (tid(),))
    log(admin, f"турнир #{tid()} завершён, очки начислены")
    setting("pin_tid", "")        # касса сама перейдёт к следующему турниру афиши
    return True, "Турнир завершён"


def achievements(player_id):
    """Простые достижения — по тому, что уже есть в базе."""
    s = player_stats(player_id)
    got = []
    if s["games"] >= 1:
        got.append({"icon": "♠", "title": "Первый турнир", "done": True})
    if s["finals"]:
        got.append({"icon": "★", "title": "Финальный стол", "done": True})
    if s["best"] and s["best"] <= 3:
        got.append({"icon": "▲", "title": "Призовая тройка", "done": True})
    if s["best"] == 1:
        got.append({"icon": "♛", "title": "Победа в турнире", "done": True})
    if s["games"] >= 5:
        got.append({"icon": "≡", "title": "Пять турниров", "done": True})
    # ближайшая невыполненная цель — показываем серой
    if s["games"] < 5:
        got.append({"icon": "≡", "title": f"Пять турниров ({s['games']}/5)", "done": False})
    elif s["best"] != 1:
        got.append({"icon": "♛", "title": "Победа в турнире", "done": False})
    return got[:6]


def player_stats(player_id):
    # тестовые турниры в личных показателях не учитываем
    row = q("""SELECT COUNT(*) AS games,
                      COALESCE(SUM(e.points),0) AS points,
                      COALESCE(MIN(NULLIF(e.place,0)), 0) AS best,
                      SUM(CASE WHEN e.place BETWEEN 1 AND 9 THEN 1 ELSE 0 END) AS finals
               FROM entries e JOIN tournaments t ON t.id = e.tid
               WHERE e.player_id=? AND e.arrived=1 AND COALESCE(t.test,0)=0""",
            (player_id,), one=True)
    return dict(row)


def rating():
    rows = q("""SELECT p.id, p.name, COUNT(e.id) AS games, COALESCE(SUM(e.points),0) AS points
                FROM players p
                JOIN entries e ON e.player_id = p.id AND e.arrived = 1
                JOIN tournaments t ON t.id = e.tid AND COALESCE(t.test,0)=0
                GROUP BY p.id HAVING points > 0
                ORDER BY points DESC, games ASC LIMIT 30""")
    return [{"place": i + 1, "id": r["id"], "name": r["name"],
             "games": r["games"], "points": r["points"]} for i, r in enumerate(rows)]


def my_rank(player_id):
    for r in rating():
        if r["id"] == player_id:
            return r["place"]
    return None


def history(player_id):
    rows = q("""SELECT t.title, t.date, e.place, e.points,
                       (SELECT COUNT(*) FROM entries e2 WHERE e2.tid = e.tid AND e2.arrived=1) AS total
                FROM entries e JOIN tournaments t ON t.id = e.tid
                WHERE e.player_id=? AND e.arrived=1 AND t.status='finished'
                  AND COALESCE(t.test,0)=0
                ORDER BY t.start DESC LIMIT 20""", (player_id,))
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------

TG_API = "https://api.telegram.org/bot{}/{}"

# Если Telegram недоступен напрямую (частая ситуация у российских провайдеров),
# в config.json можно указать прокси: "proxy": "http://127.0.0.1:2080"
_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        proxy = CFG.get("proxy") or os.environ.get("HTTPS_PROXY") or ""
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            _opener = urllib.request.build_opener(handler)
        else:
            _opener = urllib.request.build_opener()
    return _opener


def tg(method, **params):
    """Запрос к Telegram. Возвращает ответ или None, если связи нет."""
    if not CFG["bot_token"]:
        return None
    url = TG_API.format(CFG["bot_token"], method)
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with _get_opener().open(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Telegram ответил, но отказал — чаще всего неверный токен
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"description": str(e)}
        if e.code == 401:
            print("! Telegram отклонил токен. Проверьте bot_token в config.json.")
        else:
            print("! Telegram вернул ошибку:", body.get("description", e))
        return None
    except Exception as e:
        # сюда попадают обрывы связи и таймауты
        if method != "getUpdates":
            print("! Нет связи с Telegram:", e)
        return None


def send(chat, text, keyboard=None, inline=None):
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if keyboard:
        p["reply_markup"] = {"keyboard": keyboard, "resize_keyboard": True}
    if inline:
        p["reply_markup"] = {"inline_keyboard": inline}
    return tg("sendMessage", **p)


def is_admin(tg_id):
    return tg_id in CFG.get("admins", [])


def app_button():
    """Кнопка, открывающая приложение клуба прямо в Telegram."""
    if CFG.get("app_url"):
        return [[{"text": "♠ Открыть приложение клуба", "web_app": {"url": CFG["app_url"]}}]]
    return []


def welcome(p):
    """Сообщение новому участнику клуба."""
    if not p or not p["tg_id"]:
        return
    send(p["tg_id"], f"Добро пожаловать в <b>{CFG['club']}</b>, {p['name']}.\n"
                     f"Вы участник клуба под номером <b>{p['number']}</b>.\n\n"
                     "Запись на турниры — в приложении клуба, кнопка «Клуб» внизу.")


def num(n):
    """12345 → 12 345, чтобы в сообщении читалось как на афише."""
    return f"{int(n or 0):,}".replace(",", "\u2009")


def mail_on(tg_id):
    """Человек не отписался от рассылки клуба."""
    return setting(f"nomail:{tg_id}") != "1"


def announce_text():
    """Текст рассылки про ближайший турнир — тот же, что видно на афише."""
    t = current_tournament()
    if not t or t["status"] == "finished":
        return ""
    when = f"{t['weekday']}, {t['date']} · {t['time']}"
    dt = parse_dt(t.get("start"))
    today = bool(dt and dt.date() == now().date())
    head = "<b>Сегодня в клубе</b>" if today else "<b>Ближайший турнир</b>"
    lines = [head, "", f"<b>{t['title']}</b>", when, ""]
    money = f"Вход {num(t['buyin'])} ₽"
    if t.get("reentry"):
        money += f" · ребай {num(t['reentry'])} ₽"
    if t.get("addon"):
        money += f" · аддон {num(t['addon'])} ₽"
    lines.append(money)
    lines.append(f"Стартовый стек {num(t['stack'])} · уровни по "
                 f"{CFG.get('level_minutes', 10)} минут")
    if t["status"] == "live":
        lines.append("")
        lines.append("Турнир уже идёт — ещё можно зайти, пока открыта регистрация.")
    elif t.get("free", 0) > 0:
        lines.append(f"Свободно {t['free']} из {t['seats']} мест")
    else:
        lines.append("Мест нет — можно записаться в лист ожидания")
    lines += ["", "Записаться — кнопкой ниже или в приложении клуба.",
              "", "<i>Не хотите такие сообщения — отправьте боту /stop</i>"]
    return "\n".join(lines)


def broadcast(text, admin_chat=None):
    """Рассылка всем участникам клуба. Идёт в отдельном потоке: игроков может
    быть много, а касса не должна ждать."""
    rows = q("SELECT id, tg_id FROM players WHERE tg_id IS NOT NULL ORDER BY id")
    sent = failed = off = 0
    for r in rows:
        if not mail_on(r["tg_id"]):
            off += 1
            continue
        if send(r["tg_id"], text, inline=afisha_buttons(r["id"])):
            sent += 1
        else:
            failed += 1
        time.sleep(0.06)        # Telegram не любит больше 20-30 сообщений в секунду
    log("admin", f"рассылка: доставлено {sent}, не дошло {failed}, отписаны {off}")
    if admin_chat:
        send(admin_chat, f"Рассылка закончена.\nДоставлено: <b>{sent}</b>\n"
                         f"Не дошло: {failed}\nОтписались раньше: {off}")
    return sent, failed, off


def afisha_text(player_id=None):
    """Вся лента афиши одним сообщением."""
    items = feed()
    if not items:
        return "Афиша пока пустая. Ближайшие турниры появятся здесь."
    out = ["<b>Афиша клуба</b>", ""]
    for t in items:
        mark = ""
        if player_id:
            e = q("SELECT wait FROM entries WHERE tid=? AND player_id=?",
                  (t["id"], player_id), one=True)
            if e:
                mark = " · <b>вы в листе ожидания</b>" if e["wait"] else " · <b>вы записаны</b>"
        state = "идёт" if t["status"] == "live" else f"свободно {t['free']} из {t['seats']}"
        out.append(f"<b>{t['title']}</b>\n{t['weekday']}, {t['date']} · {t['time']}\n"
                   f"{state}{mark}")
        out.append("")
    return "\n".join(out).strip()


def afisha_buttons(player_id=None):
    """Кнопка приложения плюс быстрая запись прямо из чата — на случай,
    если у игрока почему-то не открылось приложение."""
    rows = app_button()
    for t in feed(5):
        e = (q("SELECT 1 FROM entries WHERE tid=? AND player_id=?", (t["id"], player_id), one=True)
             if player_id else None)
        if e:
            rows.append([{"text": f"❌ Отменить · {t['date']}", "callback_data": f"un:{t['id']}"}])
        elif t["reg_open"]:
            rows.append([{"text": f"✅ Записаться · {t['date']}, {t['time']}",
                          "callback_data": f"re:{t['id']}"}])
    return rows


def structure_text():
    per = CFG.get("level_minutes", 10)
    t = CFG["tournament"]
    lines = ["<b>Структура турнира</b>",
             f"Стартовый стек: {t['stack']}",
             f"Уровни по {per} минут · анте по формату большого блайнда",
             f"Вход {t['buyin']} ₽ и ребай {t['reentry']} ₽ — до конца "
             f"{CFG.get('late_levels', 15)} уровня",
             f"Дальше перерыв и аддон {t['addon']} ₽, один раз каждому",
             ""]
    lvl = 0
    for item in CFG.get("structure", []):
        if isinstance(item, str):
            lines.append(f"— {item} —")
        else:
            lvl += 1
            lines.append(f"{lvl}. {item[0]} / {item[1]} · анте {item[2]}")
    return "\n".join(lines)


def handle_update(u):
    """Обработка одного сообщения от Telegram."""
    if "callback_query" in u:
        cq = u["callback_query"]
        frm = cq["from"]
        p = q("SELECT * FROM players WHERE tg_id=?", (frm["id"],), one=True)
        if not p:
            tg("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True,
               text="Сначала зарегистрируйтесь в приложении клуба — кнопка «Клуб» внизу")
            return
        act, _, raw = (cq.get("data") or "").partition(":")
        t_id = int(raw) if raw.isdigit() else tid()
        if act == "re":
            ok, msg = register_player(p["id"], t_id)
        elif act == "un":
            ok, msg = unregister_player(p["id"], t_id)
        else:
            ok, msg = False, "Не понял кнопку"
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=msg, show_alert=not ok)
        try:
            tg("editMessageText", chat_id=cq["message"]["chat"]["id"],
               message_id=cq["message"]["message_id"], text=afisha_text(p["id"]),
               parse_mode="HTML", reply_markup={"inline_keyboard": afisha_buttons(p["id"])})
        except Exception:
            pass
        return

    m = u.get("message")
    if not m:
        return
    chat = m["chat"]["id"]
    frm = m.get("from", {})
    text = (m.get("text") or "").strip()
    player = q("SELECT * FROM players WHERE tg_id=?", (frm.get("id"),), one=True)

    # Если игрок прислал контакт — сохраняем номер. Отдельно просить его не нужно,
    # регистрация происходит сама при первом открытии приложения.
    if "contact" in m:
        c = m["contact"]
        if c.get("user_id") == frm.get("id"):
            phone = norm_phone(c.get("phone_number"))
            if player:
                # уже в клубе — просто обновим номер, имя не трогаем
                x("UPDATE players SET phone=? WHERE id=?", (phone, player["id"]))
                send(chat, "Номер обновлён.", inline=app_button())
            else:
                # профиль заведёт приложение — с тем ником, который человек впишет сам
                setting(f"phone:{frm['id']}", phone)
                send(chat, "Номер получен. Вернитесь в приложение и нажмите "
                           "«Вступить в клуб».", inline=app_button())
        return

    if text == "/id":
        send(chat, f"Ваш Telegram ID: <code>{frm.get('id')}</code>")
        return

    if text == "/stop":
        setting(f"nomail:{frm.get('id')}", "1")
        send(chat, "Больше не будем присылать сообщения об афише. "
                   "Записаться на турнир по-прежнему можно в приложении клуба.\n\n"
                   "Вернуть рассылку — команда /start", inline=app_button())
        return

    if text == "/start" and setting(f"nomail:{frm.get('id')}") == "1":
        setting(f"nomail:{frm.get('id')}", "0")
        send(chat, "Рассылка клуба включена обратно.")

    # --- админ ---
    if is_admin(frm.get("id")):
        if text == "/players":
            rows = q("SELECT name, number, phone FROM players ORDER BY id DESC LIMIT 20")
            total = q("SELECT COUNT(*) AS c FROM players", one=True)["c"]
            lst = "\n".join(f"{r['number']}. {r['name']} {r['phone'] or ''}" for r in rows)
            send(chat, f"Игроков в базе: <b>{total}</b>\n\n{lst}")
            return
        if text == "/list":
            t = current_tournament()
            rows = q("""SELECT p.name, e.arrived, e.wait FROM entries e
                        JOIN players p ON p.id = e.player_id WHERE e.tid=? ORDER BY e.id""",
                     (t["id"],))
            lst = "\n".join(f"{i+1}. {r['name']}" + (" ✅" if r["arrived"] else "") +
                            (" ⏳" if r["wait"] else "") for i, r in enumerate(rows))
            send(chat, f"<b>{t['title']}</b> · {t['date']} {t['time']}\n"
                       f"Записано: <b>{len(rows)}</b>\n\n{lst or '— пока никого'}")
            return
        if text.startswith("/say "):
            msg = text[5:]
            n = 0
            for r in q("SELECT tg_id FROM players WHERE tg_id IS NOT NULL"):
                if send(r["tg_id"], msg):
                    n += 1
                time.sleep(0.05)
            send(chat, f"Отправлено: {n}")
            return
        if text == "/afisha_all":
            # рассылка афиши всем участникам клуба
            n = 0
            for r in q("SELECT id, tg_id FROM players WHERE tg_id IS NOT NULL"):
                if send(r["tg_id"], afisha_text(r["id"]), inline=afisha_buttons(r["id"])):
                    n += 1
                time.sleep(0.05)
            send(chat, f"Афиша отправлена: {n}")
            return

    # --- всем остальным: приветствие, кнопка приложения и афиша ---
    if not player:
        send(chat, f"<b>{CFG['club']}</b> — клуб спортивного покера. Москва.\n"
                   "Good players · Better people.\n\n"
                   "Чтобы записываться на турниры, откройте приложение клуба и "
                   "пройдите короткую регистрацию — это займёт десять секунд.",
             inline=app_button())
    else:
        send(chat, f"С возвращением, {player['name']}.", inline=app_button())

    pid = player["id"] if player else None
    send(chat, afisha_text(pid), inline=afisha_buttons(pid))


def bot_loop():
    """Постоянно спрашивает у Telegram новые сообщения."""
    if not CFG["bot_token"]:
        print("! Токен бота не задан в config.json — бот не запущен, сайт работает.")
        return
    me = tg("getMe")
    if not me or not me.get("ok"):
        print()
        print("! Бот не запустился. Сайт и касса при этом работают.")
        print("  Две возможные причины:")
        print("  1) Неверный токен — проверьте bot_token в config.json.")
        print("  2) Нет доступа к api.telegram.org (частая ситуация у российских провайдеров).")
        print("     Проверка:  curl -I https://api.telegram.org")
        print("     Решение:   включите VPN, либо укажите прокси в config.json:")
        print('                "proxy": "http://127.0.0.1:2080"')
        print("     Либо перенесите сервер на хостинг, у которого доступ есть.")
        print()
        return
    print(f"Бот запущен: @{me['result']['username']}")
    tg("setMyCommands", commands=[
        {"command": "start", "description": "Клуб и афиша"},
    ])
    if CFG.get("app_url"):
        tg("setChatMenuButton", menu_button={
            "type": "web_app", "text": "Клуб",
            "web_app": {"url": CFG["app_url"]}
        })
    offset = 0
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=30)
            if not r or not r.get("ok"):
                time.sleep(3)
                continue
            for u in r["result"]:
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception:
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()
            time.sleep(3)


# ----------------------------------------------------------------------------
# ПРОВЕРКА ПОДПИСИ TELEGRAM (кто открыл приложение)
# ----------------------------------------------------------------------------

def check_init_data(init_data):
    """Проверяет, что данные действительно пришли от Telegram, а не подделаны."""
    if not init_data or not CFG["bot_token"]:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data = dict(pairs)
        got_hash = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", CFG["bot_token"].encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        return json.loads(data.get("user", "{}")) or None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# ВЕБ-СЕРВЕР
# ----------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".png": "image/png",
        ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8",
        ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}


def my_state(t, player_id):
    """Добавляет к карточке турнира то, что касается лично этого игрока."""
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (t["id"], player_id), one=True)
    t = dict(t)
    t.update({
        "registered": bool(e),
        "wait": bool(e and e["wait"]),
        "wait_no": waiting_no(player_id, t["id"]) if (e and e["wait"]) else 0,
        "arrived": bool(e and e["arrived"]),
        "busted": bool(e and e["busted"]),
        "place": (e["place"] if e else 0),
        "table": (e["table_no"] if e else 0),
        "seat": (e["seat_no"] if e else 0),
    })
    return t


def names_of(t_id, wait):
    rows = q("""SELECT p.name FROM entries e JOIN players p ON p.id = e.player_id
                WHERE e.tid=? AND e.wait=? ORDER BY e.id""", (t_id, 1 if wait else 0))
    return [r["name"] for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "UnionPoker"
    protocol_version = "HTTP/1.1"   # держим соединение открытым: ответы приходят быстрее

    def log_message(self, fmt, *args):
        pass  # не засорять терминал

    # --- вспомогательное ---
    def json_out(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Init-Data, X-Admin-Key")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def who(self):
        """Определяет игрока, открывшего приложение. Незнакомого — сразу регистрирует."""
        u = check_init_data(self.headers.get("X-Init-Data"))
        tg_id = u.get("id") if u else None
        if tg_id is None and CFG.get("dev"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "dev_id" in qs:
                try:
                    tg_id = int(qs["dev_id"][0])
                except ValueError:
                    tg_id = None
        if tg_id is None:
            return None
        return q("SELECT * FROM players WHERE tg_id=?", (tg_id,), one=True)

    def tg_user(self):
        """Данные Telegram того, кто открыл приложение (даже если он ещё не участник)."""
        return check_init_data(self.headers.get("X-Init-Data"))

    def tg_id(self):
        """Телеграм-номер открывшего приложение — даже если профиля в клубе ещё нет."""
        u = self.tg_user()
        if u:
            return u.get("id")
        if CFG.get("dev"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "dev_id" in qs:
                try:
                    return int(qs["dev_id"][0])
                except ValueError:
                    return None
        return None

    def client_ip(self):
        """Адрес, с которого пришла подпись. За Cloudflare настоящий — в заголовке."""
        return (self.headers.get("CF-Connecting-IP")
                or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def admin_ok(self):
        key = self.headers.get("X-Admin-Key")
        if not key:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
        return key and key == CFG["admin_key"]

    def do_OPTIONS(self):
        self.json_out({"ok": True})

    def do_HEAD(self):
        """Некоторые проверяльщики шлют HEAD — отвечаем как на обычный запрос, без тела."""
        self.do_GET()

    # --- маршруты ---
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/health":
            return self.json_out({"ok": True, "time": datetime.now(timezone.utc).isoformat()})

        if path == "/api/app-data":
            p = self.who()
            if not p:
                return self.json_out({"error": "Откройте приложение из бота"}, 401)
            s = player_stats(p["id"])
            items = []
            for t in feed():
                t = my_state(t, p["id"])
                t["players"] = names_of(t["id"], False)
                t["waitlist"] = names_of(t["id"], True)
                items.append(t)
            live = current_tournament()
            return self.json_out({
                "club": CFG.get("club", "Union Poker"),
                "me": {"name": p["name"], "username": p["username"] or "", "number": p["number"],
                       "since": (p["created"] or "")[:4],
                       "tournaments": s["games"], "best": s["best"], "finals": s["finals"] or 0,
                       "points": s["points"], "rank": my_rank(p["id"]) or "—",
                       "fio": p["fio"] or "", "born": p["born"] or "",
                       "achievements": achievements(p["id"])},
                "docs": {"pending": docs_pending(p["tg_id"]),
                         "signed": [{"title": d["title"], "version": d["version"],
                                     "at": d["signed_at"]}
                                    for d in docs_for(p["tg_id"], p) if d["signed"]]},
                "tournaments": items,
                "live": {"status": live["status"], "id": live["id"], "title": live["title"],
                         "alive": alive_count(),
                         "stage": live["stage"], "stage_text": live["stage_text"],
                         "stage_hint": live["stage_hint"], "final": final_table(),
                         "final_at": final_at(),
                         "seating": seating() if live["status"] == "live" else []},
                "structure": {"levels": CFG.get("structure", []),
                              "minutes": CFG.get("level_minutes", 10),
                              "stack": CFG["tournament"].get("stack", 0),
                              "chips": CFG.get("chips", []),
                              "late_levels": CFG.get("late_levels", 10),
                              "late_minutes": late_minutes(),
                              "final_at": CFG.get("final_at", 9)},
                "rules": CFG.get("rules", []),
                "rating": [{"place": r["place"], "name": r["name"], "games": r["games"],
                            "points": r["points"], "me": r["id"] == p["id"]} for r in rating()],
                "history": [{"date": h["date"], "title": h["title"], "place": h["place"],
                             "of": h["total"], "points": h["points"]} for h in history(p["id"])]
            })

        if path == "/api/docs":
            # Документы клуба и отметка, что из них человек уже подписал.
            tg_id = self.tg_id()
            if tg_id is None:
                return self.json_out({"error": "Откройте приложение из бота"}, 401)
            p = q("SELECT * FROM players WHERE tg_id=?", (tg_id,), one=True)
            return self.json_out({
                "docs": docs_for(tg_id, p),
                "pending": docs_pending(tg_id),
                "need_fio": needs_fio(tg_id),
                "fio": (p["fio"] if p else "") or "",
                "born": (p["born"] if p else "") or "",
                "phone": (p["phone"] if p else "") or "",
            })

        if path == "/api/phone":
            # Номер, которым человек поделился с ботом. Профиль по нему не
            # создаётся: ник человек выбирает сам в форме регистрации.
            u = self.tg_user()
            if not u:
                return self.json_out({"phone": ""}, 401)
            return self.json_out({"phone": setting(f"phone:{u['id']}") or ""})

        if path == "/api/afisha":
            # Афиша для гостя: её видно всем, кто открыл приложение, ещё до
            # регистрации. Личных данных здесь нет — только то, что и так
            # висит на афише клуба.
            items = []
            for t in feed():
                t = dict(t)
                t.update({"players": [], "waitlist": [], "registered": False, "wait": False,
                          "wait_no": 0, "arrived": False, "busted": False, "place": 0,
                          "table": 0, "seat": 0})
                items.append(t)
            live = current_tournament()
            return self.json_out({
                "club": CFG.get("club", "Union Poker"),
                "guest": True,
                "me": {"name": "Гость", "username": "", "number": 0, "since": "",
                       "tournaments": 0, "best": 0, "finals": 0, "points": 0,
                       "rank": "—", "achievements": []},
                "tournaments": items,
                "live": {"status": live["status"], "id": live["id"], "title": live["title"],
                         "alive": alive_count(), "stage": live["stage"],
                         "stage_text": live["stage_text"], "stage_hint": live["stage_hint"],
                         "final": final_table(), "final_at": final_at(),
                         "seating": seating() if live["status"] == "live" else []},
                "structure": {"levels": CFG.get("structure", []),
                              "minutes": CFG.get("level_minutes", 10),
                              "stack": CFG["tournament"].get("stack", 0),
                              "chips": CFG.get("chips", []),
                              "late_levels": CFG.get("late_levels", 10),
                              "late_minutes": late_minutes(),
                              "final_at": final_at()},
                "rules": CFG.get("rules", []),
                "rating": [{"place": r["place"], "name": r["name"], "games": r["games"],
                            "points": r["points"], "me": False} for r in rating()],
                "history": []
            })

        if path == "/api/live":
            # Открытые данные турнира для таймера на телевизоре. Ключ не нужен:
            # здесь нет ни денег, ни телефонов — только то, что и так висит на экране.
            t = current_tournament()
            ent = q("""SELECT kind, COUNT(*) AS n FROM purchases WHERE tid=? GROUP BY kind""",
                    (tid(),))
            n = {r["kind"]: r["n"] for r in ent}
            return self.json_out({
                "club": CFG.get("club", "Union Poker"),
                "title": t["title"], "date": t["date"], "time": t["time"],
                "status": t["status"], "stage": t["stage"], "stage_text": t["stage_text"],
                "stage_hint": t["stage_hint"],
                "seats": t["seats"], "taken": t["taken"],
                "alive": alive_count(),
                "buyins": n.get("buyin", 0), "reentries": n.get("reentry", 0),
                "addons": n.get("addon", 0),
                "final_at": final_at(),
                "tables": seating() if t["status"] == "live" else [],
                "final": final_table(),
                "level_minutes": CFG.get("level_minutes", 10),
                "late_levels": CFG.get("late_levels", 10),
                "structure": CFG.get("structure", []),
                "stack": CFG["tournament"].get("stack", 0),
                "buyin": t["buyin"], "reentry": t["reentry"], "addon": t["addon"],
                "timer": timer_state(),
                "now": int(time.time() * 1000),
            })

        if path == "/api/admin/state":
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            rows = q("""SELECT p.id, p.name, p.number, p.tg_id, p.fio, p.born, p.id_ok,
                               e.arrived, e.busted, e.place,
                               e.wait, e.table_no, e.seat_no, COALESCE(e.house,0) AS house,
                               (SELECT COUNT(*) FROM purchases s
                                 WHERE s.tid=e.tid AND s.player_id=p.id AND s.kind='reentry') AS reentry,
                               (SELECT COUNT(*) FROM purchases s
                                 WHERE s.tid=e.tid AND s.player_id=p.id AND s.kind='addon') AS addon
                        FROM entries e JOIN players p ON p.id=e.player_id
                        WHERE e.tid=? ORDER BY e.wait, p.name""", (tid(),))
            money = q("""SELECT kind, COUNT(*) AS n, COALESCE(SUM(amount),0) AS sum
                         FROM purchases WHERE tid=? GROUP BY kind""", (tid(),))
            plist = []
            for r in rows:
                d = dict(r)
                tg = d.pop("tg_id", None)
                d["docs"] = 0 if docs_pending(tg) else 1
                d["id_ok"] = d.get("id_ok") or ""
                d["fio"] = d.get("fio") or ""
                plist.append(d)
            return self.json_out({
                "tournament": current_tournament(),
                "pinned": bool(setting("pin_tid")),
                "players": plist,
                "alive": alive_count(),
                "seating": seating(),
                "seatmap": seat_map(),
                "final": final_table(),
                "final_at": final_at(),
                "money": {r["kind"]: {"n": r["n"], "sum": r["sum"]} for r in money},
                "total": sum(r["sum"] for r in money),
                "late_minutes": late_minutes()
            })

        if path == "/api/admin/afisha":
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            past = q("""SELECT id, title, date, status, COALESCE(test,0) AS test,
                               (SELECT COUNT(*) FROM entries e WHERE e.tid=t.id AND e.arrived=1)
                                 AS players
                        FROM tournaments t WHERE status='finished'
                        ORDER BY start DESC LIMIT 10""")
            return self.json_out({"current": tid(), "pinned": bool(setting("pin_tid")),
                                  "items": feed(12), "past": [dict(r) for r in past]})

        if path == "/api/admin/players.json":
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            rows = q("""SELECT p.name, p.number FROM entries e JOIN players p ON p.id=e.player_id
                        WHERE e.tid=? ORDER BY e.id""", (tid(),))
            return self.json_out([{"name": r["name"], "number": r["number"]} for r in rows])

        if path == "/api/admin/people":
            # Все игроки клуба — то, что обычно смотрят прямо в базе.
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            rows = q("""SELECT p.*,
                          (SELECT COUNT(*) FROM entries e WHERE e.player_id=p.id) AS games,
                          (SELECT COUNT(*) FROM purchases s WHERE s.player_id=p.id) AS pays,
                          (SELECT COALESCE(SUM(amount),0) FROM purchases s
                            WHERE s.player_id=p.id) AS money
                        FROM players p ORDER BY p.number""")
            out = []
            for r in rows:
                out.append({
                    "id": r["id"], "number": r["number"], "name": r["name"],
                    "phone": r["phone"] or "", "telegram": bool(r["tg_id"]),
                    "fio": r["fio"] or "", "id_ok": r["id_ok"] or "",
                    "created": (r["created"] or "")[:10],
                    "games": r["games"], "pays": r["pays"], "money": r["money"],
                    "docs": 0 if docs_pending(r["tg_id"]) else 1,
                })
            return self.json_out({"players": out, "total": len(out),
                                  "groups": len(dupe_groups())})

        if path == "/api/admin/dupes":
            # Похожие профили: один человек записан дважды.
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            total = q("SELECT COUNT(*) AS n FROM players", one=True)["n"]
            recent = q("""SELECT id, number, name, tg_id, phone, created
                          FROM players ORDER BY id DESC LIMIT 15""")
            signups = q("""SELECT ts, who, action FROM log
                           WHERE action LIKE '%клуб%' OR action LIKE '%егистрация%'
                           ORDER BY id DESC LIMIT 20""")
            return self.json_out({
                "groups": dupe_groups(), "total": total,
                "recent": [{"number": r["number"], "name": r["name"],
                            "telegram": bool(r["tg_id"]), "phone": r["phone"] or "",
                            "created": r["created"]} for r in recent],
                "signups": [dict(r) for r in signups]})

        if path == "/api/admin/consents":
            # Кто подписал документы клуба, когда и какую редакцию.
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            docs = load_docs()
            rows = q("""SELECT id, tg_id, name, number, phone, fio, born, id_ok
                        FROM players ORDER BY number""")
            out = []
            for r in rows:
                signed = docs_signed(r["tg_id"])
                out.append({
                    "id": r["id"], "name": r["name"], "number": r["number"],
                    "phone": r["phone"] or "", "fio": r["fio"] or "",
                    "born": born_text(r["born"]), "age": age_of(r["born"]),
                    "id_ok": r["id_ok"] or "",
                    "ok": not docs_pending(r["tg_id"]),
                    "docs": [{"title": d["title"], "code": d["code"],
                              "version": d["version"],
                              "signed": bool(signed.get(d["code"])
                                             and signed[d["code"]]["version"] == d["version"]),
                              "at": (signed.get(d["code"]) or {}).get("ts", ""),
                              "old": (signed.get(d["code"]) or {}).get("version", "")}
                             for d in docs],
                })
            return self.json_out({"players": out,
                                  "docs": [{"code": d["code"], "title": d["title"],
                                            "version": d["version"]} for d in docs]})

        # Короткие адреса: их реально набрать пультом телевизора.
        #   /tv    — второй экран таймера, повторяет главный
        #   /timer — главный экран
        #   /kassa, /app — касса и приложение
        SHORT = {"/tv": "/timer.html?screen=2", "/tv2": "/timer.html?screen=2",
                 "/timer": "/timer.html", "/kassa": "/kassa.html", "/app": "/app.html"}
        if path in SHORT:
            self.send_response(302)
            self.send_header("Location", SHORT[path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # --- статика ---
        rel = path.lstrip("/") or "app.html"
        full = os.path.normpath(os.path.join(PUBLIC, rel))
        if not full.startswith(PUBLIC) or not os.path.isfile(full):
            return self.json_out({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self.body_json()

        if path == "/api/signup":
            u = self.tg_user() or {}
            tgid = self.tg_id()
            if tgid is None:
                return self.json_out({"ok": False, "message": "Откройте приложение из бота"}, 401)
            # Уже в клубе — ничего не создаём и не спрашиваем заново.
            have = find_player(tgid)
            if have:
                return self.json_out({"ok": True, "message": "Вы уже в клубе",
                                      "number": have["number"]})
            phone = norm_phone(body.get("phone")) or setting(f"phone:{tgid}")
            if not phone or len(phone) < 12:
                return self.json_out({"ok": False, "message": "Неверный номер телефона"})
            name = clean_name(body.get("name"))
            if not real_name(name):
                # подставлять ник из Telegram нельзя: в списке участников и в
                # кассе должно стоять имя, по которому человека объявляют
                name = clean_name(" ".join(filter(None, [u.get("first_name"),
                                                         u.get("last_name")])))
                if not real_name(name):
                    return self.json_out({"ok": False,
                                          "message": "Впишите ник"})
            busy = name_owner(name)
            if busy:
                # Одинаковые ники — это путаница за столом и двойные профили.
                return self.json_out({"ok": False, "name_taken": True,
                                      "message": f"Ник «{name}» уже занят. Придумайте другой — "
                                                 "например, добавьте первую букву фамилии. "
                                                 "А если вас уже записал администратор, "
                                                 "скажите ему на входе."})
            was = find_player(tgid, phone)
            p = save_player(tgid, name, u.get("username"), phone)
            if not was:
                welcome(p)      # приветствие шлём только настоящему новичку
            return self.json_out({"ok": True, "message": "Добро пожаловать в клуб",
                                  "number": p["number"]})

        if path == "/api/consent":
            # Подпись под документами клуба. Пишем всё, чем потом можно
            # подтвердить согласие: редакцию, отпечаток текста, время, адрес.
            tg_id = self.tg_id()
            if tg_id is None:
                return self.json_out({"ok": False, "message": "Откройте приложение из бота"}, 401)
            p = q("SELECT * FROM players WHERE tg_id=?", (tg_id,), one=True)
            if not p:
                return self.json_out({"ok": False, "message": "Сначала вступите в клуб"})

            accept = [str(c) for c in (body.get("accept") or [])]
            need = docs_pending(tg_id)
            missing = [c for c in need if c not in accept]
            if missing:
                titles = ", ".join((doc_by_code(c) or {}).get("title", c) for c in missing)
                return self.json_out({"ok": False,
                                      "message": f"Не отмечено: {titles}"})

            fio = re.sub(r"\s+", " ", str(body.get("fio") or "").strip())
            born = str(body.get("born") or "").strip()[:10]
            if needs_fio(tg_id):
                if not fio_ok(fio):
                    return self.json_out({"ok": False,
                                          "message": "Впишите фамилию и имя как в документе"})
                age = age_of(born)
                if not age:
                    return self.json_out({"ok": False, "message": "Проверьте дату рождения"})
                if age < 18:
                    return self.json_out({"ok": False,
                                          "message": "В клуб допускаются только с 18 лет"})

            done = sign_docs(tg_id, accept, player=p, fio=fio, born=born, ip=self.client_ip())
            if not done:
                return self.json_out({"ok": False, "message": "Нечего подписывать"})
            return self.json_out({"ok": True, "message": "Документы подписаны",
                                  "pending": docs_pending(tg_id)})

        if path == "/api/register":
            p = self.who()
            if not p:
                return self.json_out({"error": "Откройте приложение из бота"}, 401)
            if body.get("action") != "cancel" and docs_pending(p["tg_id"]):
                # Играть без подписанных документов нельзя — это требование оферты
                # и согласия на обработку данных, а не прихоть приложения.
                return self.json_out({"ok": False, "need_docs": True,
                                      "message": "Сначала подпишите документы клуба"})
            t_id = int(body.get("tid") or tid())
            ok, msg = (register_player(p["id"], t_id) if body.get("action") != "cancel"
                       else unregister_player(p["id"], t_id))
            return self.json_out({"ok": ok, "message": msg, "taken": seated_count(t_id)})

        if path.startswith("/api/admin/"):
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)

            if path == "/api/admin/op":
                pid, op = body.get("player_id"), body.get("op")
                if op == "bust":
                    ok, msg = bust(pid, "admin")
                else:
                    ok, msg = purchase(pid, op, "admin")
                return self.json_out({"ok": ok, "message": msg})

            if path == "/api/admin/add-player":
                name = clean_name(body.get("name"))
                use_id = int(body.get("use_id") or 0)
                if use_id:
                    # кассир подтвердил: это тот самый человек из клуба
                    p = q("SELECT * FROM players WHERE id=?", (use_id,), one=True)
                    if not p:
                        return self.json_out({"ok": False, "message": "Игрок не найден"})
                    x("INSERT OR IGNORE INTO entries(tid, player_id) VALUES(?,?)", (tid(), use_id))
                    return self.json_out({"ok": True, "player_id": use_id,
                                          "message": f"{p['name']} добавлен в турнир"})
                if not real_name(name):
                    return self.json_out({"ok": False, "message": "Впишите ник"})
                busy = name_owner(name)
                if busy:
                    # Молча брать чужой профиль нельзя: деньги и история уедут не тому.
                    games = q("SELECT COUNT(*) AS n FROM entries WHERE player_id=?",
                              (busy["id"],), one=True)["n"]
                    return self.json_out({
                        "ok": False, "exists": {
                            "id": busy["id"], "number": busy["number"], "name": busy["name"],
                            "telegram": bool(busy["tg_id"]), "games": games},
                        "message": f"В клубе уже есть №{busy['number']} {busy['name']}"})
                pid = x("INSERT INTO players(name, number) VALUES(?,?)", (name, next_number()))
                x("INSERT OR IGNORE INTO entries(tid, player_id) VALUES(?,?)", (tid(), pid))
                log("admin", f"касса завела профиль: {name}")
                return self.json_out({"ok": True, "message": "Добавлен", "player_id": pid})

            if path == "/api/admin/seat":
                n = make_seating("admin")
                return self.json_out({"ok": True, "message": f"Рассажено игроков: {n}",
                                      "seating": seating()})

            if path == "/api/admin/timer":
                # Главный таймер присылает сюда своё состояние, второй экран
                # его забирает. Время считаем по часам сервера, чтобы разные
                # часы на ноутбуках не разводили экраны.
                st = body.get("state")
                if not isinstance(st, dict):
                    return self.json_out({"ok": False, "message": "Нет состояния"})
                st["recv"] = int(time.time() * 1000)
                setting("timer_state", json.dumps(st, ensure_ascii=False))
                return self.json_out({"ok": True})

            if path == "/api/admin/delete-player":
                # Удаление профиля целиком. С оплатами не удаляем никогда:
                # деньги вечера считаются по ним, и турнир сойдётся неверно.
                pid = int(body.get("player_id") or 0)
                p = q("SELECT * FROM players WHERE id=?", (pid,), one=True)
                if not p:
                    return self.json_out({"ok": False, "message": "Игрок не найден"})
                pays = q("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s "
                         "FROM purchases WHERE player_id=?", (pid,), one=True)
                if pays["n"] and not body.get("force"):
                    return self.json_out({"ok": False, "has_money": True, "message":
                        f"У игрока {pays['n']} оплат на {pays['s']} ₽. Обычно такой профиль "
                        "не удаляют, а объединяют с настоящим"})
                x("DELETE FROM purchases WHERE player_id=?", (pid,))
                x("DELETE FROM entries WHERE player_id=?", (pid,))
                x("DELETE FROM consents WHERE player_id=?", (pid,))
                x("DELETE FROM players WHERE id=?", (pid,))
                log("admin", f"удалён профиль №{p['number']} {p['name']}")
                return self.json_out({"ok": True,
                                      "message": f"Профиль №{p['number']} {p['name']} удалён"})

            if path == "/api/admin/broadcast":
                # Рассылка участникам клуба. Сначала касса просит текст
                # (preview), показывает его кассиру, и только потом отправляет.
                text = (body.get("text") or "").strip() or announce_text()
                if not text:
                    return self.json_out({"ok": False,
                                          "message": "Нечего рассылать: турнира в афише нет"})
                total = q("SELECT COUNT(*) AS n FROM players WHERE tg_id IS NOT NULL",
                          one=True)["n"]
                off = sum(1 for r in q("SELECT tg_id FROM players WHERE tg_id IS NOT NULL")
                          if not mail_on(r["tg_id"]))
                if body.get("preview"):
                    return self.json_out({"ok": True, "text": text,
                                          "total": total, "off": off})
                who = (CFG.get("admins") or [None])[0]
                threading.Thread(target=broadcast, args=(text, who), daemon=True).start()
                return self.json_out({"ok": True,
                                      "message": f"Отправляю {total - off} участникам"})

            if path == "/api/admin/tournament-test":
                ok, msg = set_test(body.get("tid"), bool(body.get("on")), "admin")
                return self.json_out({"ok": ok, "message": msg})

            if path == "/api/admin/merge":
                ok, msg = merge_players(body.get("keep_id"), body.get("drop_id"), "admin")
                return self.json_out({"ok": ok, "message": msg})

            if path == "/api/admin/backup":
                p = backup_db()
                return self.json_out({"ok": bool(p),
                                      "message": ("Копия базы сделана: " + os.path.basename(p))
                                                 if p else "Не получилось сделать копию"})

            if path == "/api/admin/id-check":
                # Администратор посмотрел документ на входе: человеку 18+ и это он.
                pid = int(body.get("player_id") or 0)
                row = q("SELECT name, id_ok FROM players WHERE id=?", (pid,), one=True)
                if not row:
                    return self.json_out({"ok": False, "message": "Игрок не найден"})
                if row["id_ok"]:
                    x("UPDATE players SET id_ok=NULL WHERE id=?", (pid,))
                    log("admin", f"снята отметка о документе: {row['name']}")
                    return self.json_out({"ok": True, "message": "Отметка снята", "id_ok": ""})
                stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
                x("UPDATE players SET id_ok=? WHERE id=?", (stamp, pid))
                log("admin", f"документ проверен: {row['name']}")
                return self.json_out({"ok": True, "message": "Документ проверен", "id_ok": stamp})

            if path == "/api/admin/stage":
                ok, msg = set_stage(body.get("stage"), "admin")
                return self.json_out({"ok": ok, "message": msg,
                                      "tournament": current_tournament()})

            if path == "/api/admin/seat-set":
                ok, msg = seat_set(body.get("player_id"), body.get("table"),
                                   body.get("seat"), "admin")
                return self.json_out({"ok": ok, "message": msg, "seatmap": seat_map()})

            if path == "/api/admin/seat-free":
                ok, msg = seat_free(body.get("player_id"), "admin")
                return self.json_out({"ok": ok, "message": msg, "seatmap": seat_map()})

            if path == "/api/admin/house-add":
                ok, msg = house_add(body.get("name"), body.get("table"),
                                    body.get("seat"), "admin")
                return self.json_out({"ok": ok, "message": msg, "seatmap": seat_map()})

            if path == "/api/admin/house-remove":
                ok, msg = house_remove(body.get("player_id"), "admin")
                return self.json_out({"ok": ok, "message": msg, "seatmap": seat_map()})

            if path == "/api/admin/seat-mode":
                mode = body.get("mode")
                if mode not in ("auto", "manual"):
                    return self.json_out({"ok": False, "message": "Неизвестный режим"})
                setting("seat_mode", mode)
                log("admin", f"рассадка: {mode}")
                return self.json_out({"ok": True, "auto": mode == "auto",
                                      "message": "Касса сажает сама" if mode == "auto"
                                                 else "Сажаете вручную"})

            if path == "/api/admin/rebalance":
                n, tabs = rebalance("admin")
                if not n:
                    return self.json_out({"ok": False, "message": "За столами пока никого"})
                return self.json_out({"ok": True, "seating": seating(),
                                      "message": f"{n} {plural(n, 'игрок', 'игрока', 'игроков')} "
                                                 f"на {tabs} "
                                                 f"{plural(tabs, 'столе', 'столах', 'столах')}"})

            if path == "/api/admin/remove":
                ok, msg = remove_entry(body.get("player_id"), "admin",
                                       force=bool(body.get("force")))
                return self.json_out({"ok": ok, "message": msg})

            if path == "/api/admin/unbust":
                ok, msg = unbust(body.get("player_id"), "admin")
                return self.json_out({"ok": ok, "message": msg})

            if path == "/api/admin/new-tournament":
                when = f"{body.get('date', '')} {body.get('time') or CFG['tournament']['time']}"
                dt = parse_dt(when)
                if not dt:
                    return self.json_out({"ok": False,
                                          "message": "Не понял дату. Формат: 2026-10-04"})
                nid = create_tournament(dt, body.get("title"), body.get("seats"),
                                        body.get("buyin"), body.get("meta"), admin="admin")
                if body.get("pin"):
                    setting("pin_tid", nid)
                return self.json_out({"ok": True, "id": nid,
                                      "message": f"Турнир создан — {when_text(dt)}"})

            if path == "/api/admin/pin":
                t_id = body.get("tid")
                if not t_id:
                    setting("pin_tid", "")
                    return self.json_out({"ok": True,
                                          "message": "Касса снова выбирает турнир сама"})
                if not t_row(int(t_id)):
                    return self.json_out({"ok": False, "message": "Турнир не найден"})
                setting("pin_tid", int(t_id))
                return self.json_out({"ok": True, "message": "Переключено",
                                      "tournament": current_tournament()})

            if path == "/api/admin/delete-tournament":
                t_id = int(body.get("tid") or 0)
                if not t_row(t_id):
                    return self.json_out({"ok": False, "message": "Турнир не найден"})
                if q("SELECT 1 FROM purchases WHERE tid=? LIMIT 1", (t_id,), one=True):
                    return self.json_out({"ok": False,
                                          "message": "В турнире уже есть оплаты — удалять нельзя"})
                x("DELETE FROM entries WHERE tid=?", (t_id,))
                x("DELETE FROM tournaments WHERE id=?", (t_id,))
                if setting("pin_tid") == str(t_id):
                    setting("pin_tid", "")
                log("admin", f"турнир #{t_id} убран из афиши")
                return self.json_out({"ok": True, "message": "Турнир убран из афиши"})

            if path == "/api/admin/finish":
                ok, msg = finish_tournament("admin")
                return self.json_out({"ok": ok, "message": msg})

            if path == "/api/admin/status":
                st = body.get("status")
                if st in ("open", "live", "finished"):
                    x("UPDATE tournaments SET status=? WHERE id=?", (st, tid()))
                    return self.json_out({"ok": True, "message": f"Статус: {st}"})
                return self.json_out({"ok": False, "message": "Неверный статус"})

        return self.json_out({"error": "not found"}, 404)


def main():
    init_db()
    backup_db()          # копия при каждом запуске — до того, как что-то пойдёт не так
    os.makedirs(PUBLIC, exist_ok=True)
    ensure_events(quiet=False)
    auto_live()
    t = current_tournament()
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=ticker, daemon=True).start()
    port = int(CFG.get("port", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print()
    print(f"Ближайший турнир: {t['title']} — {t['weekday']}, {t['date']} в {t['time']}")
    print(f"Записано: {t['taken']} из {t['seats']}")
    print()
    print(f"Приложение: http://localhost:{port}/app.html")
    print(f"Касса:      http://localhost:{port}/kassa.html")
    print(f"Таймер:     http://localhost:{port}/timer.html")
    print("Остановить: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
