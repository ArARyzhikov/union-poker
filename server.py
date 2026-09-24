#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UNION POKER — сервер клуба.

Один файл, только стандартная библиотека Python. Ничего устанавливать не нужно.
Запуск:  python server.py

Внутри три части, работающие одновременно:
  1) База данных SQLite (файл club.db рядом со скриптом)
  2) Telegram-бот: регистрация игроков и запись на турнир
  3) Веб-сервер: отдаёт приложение, кассу и таймер + API для них

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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "club.db")
CFG_PATH = os.path.join(BASE, "config.json")
PUBLIC = os.path.join(BASE, "public")

# ----------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ----------------------------------------------------------------------------

DEFAULT_CFG = {
    "bot_token": "",                       # токен от @BotFather
    "admins": [],                          # ваш Telegram ID (узнать: напишите боту /id)
    "admin_key": "union-admin-key",        # пароль для кассы, поменяйте на свой
    "app_url": "",                         # адрес Mini App, например https://club.ru/app.html
    "club": "Union Poker",
    "port": 8080,
    "dev": True,                           # True — можно открывать приложение в браузере без Telegram
    "tournament": {
        "id": 1,
        "title": "Демо-день клуба",
        "date": "27 сентября",
        "weekday": "вс",
        "time": "18:00",
        "buyin": 2000,
        "reentry": 2000,
        "addon": 3000,
        "seats": 36,
        "stack": 25000,
        "meta": "Hold'em · стек 25 000 · вход 2000 ₽"
    },
    # очки за место: сколько получает 1-е, 2-е, 3-е и так далее
    "points": [100, 85, 72, 61, 52, 44, 38, 32, 27],
    "points_rest": 10,                     # всем остальным, кто играл
}


def load_cfg():
    if not os.path.exists(CFG_PATH):
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
        print("Создан config.json — впишите туда токен бота и запустите снова.")
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CFG)
    merged.update(cfg)
    return merged


CFG = load_cfg()

# ----------------------------------------------------------------------------
# БАЗА ДАННЫХ
# ----------------------------------------------------------------------------

_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def q(sql, args=(), one=False):
    """Запрос на чтение."""
    with _lock:
        cur = db.execute(sql, args)
        rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


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
            id        INTEGER PRIMARY KEY,
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

        CREATE TABLE IF NOT EXISTS log(
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT DEFAULT (datetime('now')),
            who    TEXT,
            action TEXT
        );
        """)
        db.commit()

    t = CFG["tournament"]
    if not q("SELECT 1 FROM tournaments WHERE id=?", (t["id"],), one=True):
        x("INSERT INTO tournaments(id, title, date) VALUES(?,?,?)",
          (t["id"], t["title"], t["date"]))


def log(who, action):
    x("INSERT INTO log(who, action) VALUES(?,?)", (str(who), action))


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


def save_player(tg_id, name, username, phone):
    """Регистрация игрока. Один человек — один профиль."""
    phone = norm_phone(phone)
    row = q("SELECT * FROM players WHERE tg_id=? OR (phone IS NOT NULL AND phone=?)",
            (tg_id, phone), one=True)
    if row:
        x("UPDATE players SET tg_id=?, name=?, username=?, phone=COALESCE(phone,?) WHERE id=?",
          (tg_id, name, username, phone, row["id"]))
        return q("SELECT * FROM players WHERE id=?", (row["id"],), one=True)
    num = next_number()
    pid = x("INSERT INTO players(tg_id, name, username, phone, number) VALUES(?,?,?,?,?)",
            (tg_id, name, username, phone, num))
    log(tg_id, f"регистрация: {name}")
    return q("SELECT * FROM players WHERE id=?", (pid,), one=True)


# ----------------------------------------------------------------------------
# ЛОГИКА ТУРНИРА
# ----------------------------------------------------------------------------

def tid():
    return CFG["tournament"]["id"]


def t_status():
    row = q("SELECT status FROM tournaments WHERE id=?", (tid(),), one=True)
    return row["status"] if row else "open"


def taken():
    return q("SELECT COUNT(*) AS c FROM entries WHERE tid=?", (tid(),), one=True)["c"]


def alive_count():
    return q("SELECT COUNT(*) AS c FROM entries WHERE tid=? AND arrived=1 AND busted=0",
             (tid(),), one=True)["c"]


def register_player(player_id):
    if t_status() != "open":
        return False, "Регистрация закрыта"
    if q("SELECT 1 FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True):
        return False, "Вы уже записаны"
    if taken() >= CFG["tournament"]["seats"]:
        return False, "Мест нет"
    x("INSERT INTO entries(tid, player_id) VALUES(?,?)", (tid(), player_id))
    log(player_id, "запись на турнир")
    return True, "Вы записаны"


def unregister_player(player_id):
    row = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    if not row:
        return False, "Вы не записаны"
    if row["arrived"]:
        return False, "Вход уже оплачен, отмена только у администратора"
    x("DELETE FROM entries WHERE id=?", (row["id"],))
    log(player_id, "отмена записи")
    return True, "Запись отменена"


def purchase(player_id, kind, admin=None):
    """Вход, ре-энтри или аддон. Возвращает (успех, сообщение)."""
    t = CFG["tournament"]
    price = {"buyin": t["buyin"], "reentry": t["reentry"], "addon": t["addon"]}.get(kind)
    if price is None:
        return False, "Неизвестная операция"

    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    if not e:
        x("INSERT INTO entries(tid, player_id) VALUES(?,?)", (tid(), player_id))
        e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)

    if kind == "buyin":
        if e["arrived"]:
            return False, "Вход уже оплачен"
        x("UPDATE entries SET arrived=1, busted=0, place=0 WHERE id=?", (e["id"],))
    elif kind == "reentry":
        if not e["busted"]:
            return False, "Игрок ещё в игре"
        x("UPDATE entries SET busted=0, place=0 WHERE id=?", (e["id"],))
    elif kind == "addon":
        if not e["arrived"] or e["busted"]:
            return False, "Игрок не за столом"

    x("INSERT INTO purchases(tid, player_id, kind, amount, by_admin) VALUES(?,?,?,?,?)",
      (tid(), player_id, kind, price, admin))
    log(admin, f"{kind} игроку {player_id}")
    return True, "Готово"


def bust(player_id, admin=None):
    e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    if not e or not e["arrived"]:
        return False, "Игрок не за столом"
    if e["busted"]:
        return False, "Уже отмечен"
    place = alive_count()          # сколько осталось вместе с ним — это его место
    x("UPDATE entries SET busted=1, place=? WHERE id=?", (place, e["id"]))
    log(admin, f"выбыл игрок {player_id}, место {place}")
    return True, f"{place} место"


def finish_tournament(admin=None):
    """Закрывает турнир и начисляет очки рейтинга."""
    rest = q("SELECT * FROM entries WHERE tid=? AND arrived=1 AND busted=0", (tid(),))
    if len(rest) == 1:
        x("UPDATE entries SET busted=1, place=1 WHERE id=?", (rest[0]["id"],))
    pts = CFG["points"]
    for e in q("SELECT * FROM entries WHERE tid=? AND arrived=1", (tid(),)):
        place = e["place"] or 0
        p = pts[place - 1] if 0 < place <= len(pts) else CFG["points_rest"]
        x("UPDATE entries SET points=? WHERE id=?", (p, e["id"]))
    x("UPDATE tournaments SET status='finished' WHERE id=?", (tid(),))
    log(admin, "турнир завершён, очки начислены")
    return True, "Турнир завершён"


def player_stats(player_id):
    row = q("""SELECT COUNT(*) AS games,
                      COALESCE(SUM(points),0) AS points,
                      COALESCE(MIN(NULLIF(place,0)), 0) AS best,
                      SUM(CASE WHEN place BETWEEN 1 AND 9 THEN 1 ELSE 0 END) AS finals
               FROM entries WHERE player_id=? AND arrived=1""", (player_id,), one=True)
    return dict(row)


def rating():
    rows = q("""SELECT p.id, p.name, COUNT(e.id) AS games, COALESCE(SUM(e.points),0) AS points
                FROM players p JOIN entries e ON e.player_id = p.id AND e.arrived = 1
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
                WHERE e.player_id=? AND e.arrived=1
                ORDER BY e.id DESC LIMIT 20""", (player_id,))
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------

TG_API = "https://api.telegram.org/bot{}/{}"


def tg(method, **params):
    if not CFG["bot_token"]:
        return None
    url = TG_API.format(CFG["bot_token"], method)
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("Ошибка Telegram:", e)
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


MENU = [[{"text": "🗓 Афиша"}, {"text": "👤 Мои данные"}],
        [{"text": "ℹ️ О клубе"}, {"text": "📋 Структура"}]]

CONTACT_KB = [[{"text": "📱 Поделиться номером", "request_contact": True}]]


def afisha_text():
    t = CFG["tournament"]
    free = t["seats"] - taken()
    return (f"<b>{t['title']}</b>\n"
            f"{t['weekday']}, {t['date']} · {t['time']}\n"
            f"{t['meta']}\n\n"
            f"Свободно мест: <b>{free}</b> из {t['seats']}")


def afisha_buttons(player_id):
    signed = q("SELECT 1 FROM entries WHERE tid=? AND player_id=?", (tid(), player_id), one=True)
    rows = []
    if signed:
        rows.append([{"text": "❌ Отменить запись", "callback_data": "unreg"}])
    else:
        rows.append([{"text": "✅ Записаться", "callback_data": "reg"}])
    if CFG.get("app_url"):
        rows.append([{"text": "📱 Открыть приложение", "web_app": {"url": CFG["app_url"]}}])
    return rows


def handle_update(u):
    """Обработка одного сообщения от Telegram."""
    if "callback_query" in u:
        cq = u["callback_query"]
        frm = cq["from"]
        p = q("SELECT * FROM players WHERE tg_id=?", (frm["id"],), one=True)
        if not p:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Сначала регистрация: /start")
            return
        if cq["data"] == "reg":
            ok, msg = register_player(p["id"])
        else:
            ok, msg = unregister_player(p["id"])
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=msg)
        try:
            tg("editMessageText", chat_id=cq["message"]["chat"]["id"],
               message_id=cq["message"]["message_id"], text=afisha_text(),
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

    # --- контакт: завершение регистрации ---
    if "contact" in m:
        c = m["contact"]
        if c.get("user_id") != frm.get("id"):
            send(chat, "Пришлите, пожалуйста, свой номер — кнопкой ниже.", keyboard=CONTACT_KB)
            return
        name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) or "Игрок"
        p = save_player(frm["id"], name, frm.get("username"), c.get("phone_number"))
        send(chat, f"Готово, {name}. Вы участник клуба под номером <b>{p['number']}</b>.\n"
                   f"Теперь можно записаться на турнир.", keyboard=MENU)
        send(chat, afisha_text(), inline=afisha_buttons(p["id"]))
        return

    # --- команды ---
    if text.startswith("/start"):
        if player:
            send(chat, f"С возвращением, {player['name']}.", keyboard=MENU)
            send(chat, afisha_text(), inline=afisha_buttons(player["id"]))
        else:
            send(chat, f"Добро пожаловать в <b>{CFG['club']}</b>.\n\n"
                       "Играть в клубе можно после регистрации — так ведётся ваша статистика "
                       "и рейтинг сезона.\n\nНажмите кнопку ниже, чтобы зарегистрироваться.",
                 keyboard=CONTACT_KB)
        return

    if text == "/id":
        send(chat, f"Ваш Telegram ID: <code>{frm.get('id')}</code>")
        return

    if not player:
        send(chat, "Сначала регистрация — нажмите кнопку.", keyboard=CONTACT_KB)
        return

    if text.startswith("🗓") or text == "/afisha":
        send(chat, afisha_text(), inline=afisha_buttons(player["id"]))
        return

    if text.startswith("👤") or text == "/me":
        s = player_stats(player["id"])
        rank = my_rank(player["id"])
        send(chat, f"<b>{player['name']}</b>\n"
                   f"Номер участника: {player['number']}\n"
                   f"Турниров сыграно: {s['games']}\n"
                   f"Очков рейтинга: {s['points']}\n"
                   f"Место в рейтинге: {rank or '—'}")
        return

    if text.startswith("ℹ️") or text == "/about":
        send(chat, f"<b>{CFG['club']}</b>\nКлуб спортивного покера.\n"
                   "Good players · Better people.\n\n"
                   "Играем по правилам спортивного покера, призы — очки рейтинга сезона.")
        return

    if text.startswith("📋") or text == "/structure":
        t = CFG["tournament"]
        send(chat, f"<b>Структура турнира</b>\n"
                   f"Стартовый стек: {t['stack']}\n"
                   f"Уровни по 20 минут\n"
                   f"Ре-энтри и регистрация — первые 100 минут\n"
                   f"Аддон {t['addon']} ₽ в перерыве после 5 уровня\n"
                   f"Вход {t['buyin']} ₽ · ре-энтри {t['reentry']} ₽")
        return

    # --- админ ---
    if is_admin(frm.get("id")):
        if text == "/players":
            rows = q("SELECT name, number, phone FROM players ORDER BY id DESC LIMIT 20")
            total = q("SELECT COUNT(*) AS c FROM players", one=True)["c"]
            lst = "\n".join(f"{r['number']}. {r['name']} {r['phone'] or ''}" for r in rows)
            send(chat, f"Игроков в базе: <b>{total}</b>\n\n{lst}")
            return
        if text == "/list":
            rows = q("""SELECT p.name, p.number, e.arrived FROM entries e
                        JOIN players p ON p.id = e.player_id WHERE e.tid=? ORDER BY e.id""", (tid(),))
            lst = "\n".join(f"{i+1}. {r['name']}" + (" ✅" if r["arrived"] else "") for i, r in enumerate(rows))
            send(chat, f"Записано: <b>{len(rows)}</b>\n\n{lst or '— пока никого'}")
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

    send(chat, "Выберите пункт меню.", keyboard=MENU)


def bot_loop():
    """Постоянно спрашивает у Telegram новые сообщения."""
    if not CFG["bot_token"]:
        print("! Токен бота не задан в config.json — бот не запущен, сайт работает.")
        return
    me = tg("getMe")
    if not me or not me.get("ok"):
        print("! Не удалось подключиться к Telegram. Проверьте токен.")
        return
    print(f"Бот запущен: @{me['result']['username']}")
    tg("setMyCommands", commands=[
        {"command": "start", "description": "Регистрация и меню"},
        {"command": "afisha", "description": "Ближайший турнир"},
        {"command": "me", "description": "Мои данные"},
    ])
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
        user = json.loads(data.get("user", "{}"))
        return user.get("id")
    except Exception:
        return None


# ----------------------------------------------------------------------------
# ВЕБ-СЕРВЕР
# ----------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".png": "image/png",
        ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "UnionPoker"

    def log_message(self, fmt, *args):
        pass  # не засорять терминал

    # --- вспомогательное ---
    def json_out(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
        """Определяет игрока, открывшего приложение."""
        tg_id = check_init_data(self.headers.get("X-Init-Data"))
        if tg_id is None and CFG.get("dev"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "dev_id" in qs:
                tg_id = int(qs["dev_id"][0])
        if tg_id is None:
            return None
        return q("SELECT * FROM players WHERE tg_id=?", (tg_id,), one=True)

    def admin_ok(self):
        key = self.headers.get("X-Admin-Key")
        if not key:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
        return key and key == CFG["admin_key"]

    def do_OPTIONS(self):
        self.json_out({"ok": True})

    # --- маршруты ---
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/health":
            return self.json_out({"ok": True, "time": datetime.now(timezone.utc).isoformat()})

        if path == "/api/app-data":
            p = self.who()
            if not p:
                return self.json_out({"error": "Откройте приложение из бота"}, 401)
            t = CFG["tournament"]
            e = q("SELECT * FROM entries WHERE tid=? AND player_id=?", (tid(), p["id"]), one=True)
            s = player_stats(p["id"])
            return self.json_out({
                "me": {"name": p["name"], "username": p["username"] or "", "number": p["number"],
                       "since": (p["created"] or "")[:4],
                       "tournaments": s["games"], "best": s["best"], "finals": s["finals"] or 0,
                       "points": s["points"], "rank": my_rank(p["id"]) or "—"},
                "tournaments": [{
                    "id": t["id"], "title": t["title"], "date": t["date"], "weekday": t["weekday"],
                    "time": t["time"], "buyin": t["buyin"], "seats": t["seats"], "taken": taken(),
                    "theme": "", "tag": f"{t['date'].split()[0]} {t['date'].split()[1][:3]} · {t['time']}",
                    "meta": t["meta"], "registered": bool(e), "status": t_status()
                }],
                "rating": [{"place": r["place"], "name": r["name"], "games": r["games"],
                            "points": r["points"], "me": r["id"] == p["id"]} for r in rating()],
                "history": [{"date": h["date"], "title": h["title"], "place": h["place"],
                             "of": h["total"], "points": h["points"]} for h in history(p["id"])]
            })

        if path == "/api/admin/state":
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            rows = q("""SELECT p.id, p.name, p.number, e.arrived, e.busted, e.place,
                               (SELECT COUNT(*) FROM purchases s WHERE s.tid=e.tid AND s.player_id=p.id AND s.kind='reentry') AS reentry,
                               (SELECT COUNT(*) FROM purchases s WHERE s.tid=e.tid AND s.player_id=p.id AND s.kind='addon') AS addon
                        FROM entries e JOIN players p ON p.id=e.player_id
                        WHERE e.tid=? ORDER BY p.name""", (tid(),))
            money = q("""SELECT kind, COUNT(*) AS n, COALESCE(SUM(amount),0) AS sum
                         FROM purchases WHERE tid=? GROUP BY kind""", (tid(),))
            return self.json_out({
                "tournament": CFG["tournament"] | {"status": t_status()},
                "players": [dict(r) for r in rows],
                "alive": alive_count(),
                "money": {r["kind"]: {"n": r["n"], "sum": r["sum"]} for r in money},
                "total": sum(r["sum"] for r in money)
            })

        if path == "/api/admin/players.json":
            if not self.admin_ok():
                return self.json_out({"error": "Нет доступа"}, 403)
            rows = q("""SELECT p.name, p.number FROM entries e JOIN players p ON p.id=e.player_id
                        WHERE e.tid=? ORDER BY e.id""", (tid(),))
            return self.json_out([{"name": r["name"], "number": r["number"]} for r in rows])

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

        if path == "/api/register":
            p = self.who()
            if not p:
                return self.json_out({"error": "Откройте приложение из бота"}, 401)
            ok, msg = (register_player(p["id"]) if body.get("action") != "cancel"
                       else unregister_player(p["id"]))
            return self.json_out({"ok": ok, "message": msg, "taken": taken()})

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
                name = (body.get("name") or "").strip()
                if not name:
                    return self.json_out({"ok": False, "message": "Пустое имя"})
                pid = x("INSERT INTO players(name, number) VALUES(?,?)", (name, next_number()))
                x("INSERT OR IGNORE INTO entries(tid, player_id) VALUES(?,?)", (tid(), pid))
                return self.json_out({"ok": True, "message": "Добавлен", "player_id": pid})

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
    os.makedirs(PUBLIC, exist_ok=True)
    threading.Thread(target=bot_loop, daemon=True).start()
    port = int(CFG.get("port", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Сервер работает: http://localhost:{port}/app.html")
    print(f"Касса:           http://localhost:{port}/kassa.html")
    print("Остановить: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
