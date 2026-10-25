"""Wholesale Telegram shop. Python 3.11+, standard library only.

SQLite transactions persist each update together with its replies and orders.
Network operations run after commit. Retried order messages retain the order ID.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger("marketbot")
TZ = ZoneInfo("Asia/Tashkent")
MIN_ORDER = 500_000
MAX_BLOCKS = 999
PHOTO_BASE = "https://raw.githubusercontent.com/marlen65007100-netizen/naberejnaya/main/p/"
MAIN_MENU = {"keyboard": [["📦 Каталог", "🛒 Корзина"]], "resize_keyboard": True}


def money(value):
    return f"{value:,}".replace(",", " ") + " сум"


def blocks(value):
    a, b = value % 10, value % 100
    word = "блок" if a == 1 and b != 11 else "блока" if 2 <= a <= 4 and not 12 <= b <= 14 else "блоков"
    return f"{value} {word}"


def escape(value):
    return html.escape(str(value), quote=True)


def button(text, data):
    return {"text": text, "callback_data": data}


def keyboard(*rows):
    return {"inline_keyboard": list(rows)}


def phone_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[+0-9 ()-]{9,30}", value):
        raise ValueError("phone")
    digits = re.sub(r"\D", "", value)
    if len(digits) == 9:
        digits = "998" + digits
    if digits.startswith("998") and len(digits) != 12:
        raise ValueError("phone")
    if not 10 <= len(digits) <= 15:
        raise ValueError("phone")
    return "+" + digits


class Catalog:
    def __init__(self, path=None):
        path = path or Path(__file__).with_name("catalog.json")
        self.raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = hashlib.sha256(json.dumps(self.raw, sort_keys=True).encode()).hexdigest()[:16]
        self.products = {}
        self.categories = []
        for source in self.raw["products"]:
            p = dict(source)
            if not re.fullmatch(r"p\d{2,4}", p["id"]) or p["id"] in self.products:
                raise ValueError("Invalid or duplicate product ID")
            for key in ("units_per_block", "unit_price"):
                if type(p[key]) is not int or p[key] <= 0:
                    raise ValueError("Prices and pack sizes must be positive integers")
            p["block_price"] = p["unit_price"] * p["units_per_block"]
            self.products[p["id"]] = p
            if p["category"] not in self.categories:
                self.categories.append(p["category"])

    def lines(self, cart):
        result = []
        for pid, quantity in cart.items():
            if pid not in self.products or type(quantity) is not int or not 1 <= quantity <= MAX_BLOCKS:
                raise ValueError("Invalid cart")
            p = self.products[pid]
            result.append({"id": pid, "name": p["name"], "blocks": quantity,
                           "units": quantity * p["units_per_block"],
                           "units_per_block": p["units_per_block"],
                           "unit_price": p["unit_price"], "block_price": p["block_price"],
                           "amount": quantity * p["block_price"]})
        return result


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS sessions (chat_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS processed (update_id INTEGER PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT UNIQUE NOT NULL,
            chat_id INTEGER NOT NULL, payload TEXT NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'new');
          CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL, params TEXT NOT NULL,
            order_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
            next_try REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
        """)

    def session(self, chat_id):
        row = self.db.execute("SELECT data FROM sessions WHERE chat_id=?", (chat_id,)).fetchone()
        return json.loads(row[0]) if row else {"stage": "shopping", "cart": {}, "draft": {}}

    def save(self, chat_id, data):
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (chat_id, json.dumps(data)))

    def enqueue(self, method, params, order_id=None):
        self.db.execute("INSERT INTO outbox(method,params,order_id) VALUES (?,?,?)",
                        (method, json.dumps(params), order_id))

    def offset(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0


def order_text(payload, order_id=None):
    title = f"ЗАКАЗ №{order_id} — НАБЕРЕЖНАЯ" if order_id else "Проверьте заказ"
    rows = [f"<b>{title}</b>", ""]
    for line in payload["lines"]:
        rows.append(f"{escape(line['name'])}\n{blocks(line['blocks'])} / {line['units']} шт. — {money(line['amount'])}")
    rows.extend(["", f"<b>Итого: {money(payload['total'])}</b>",
                 f"Всего: {blocks(sum(p['blocks'] for p in payload['lines']))}",
                 f"Имя / магазин: {escape(payload['name'])}", f"Телефон: {escape(payload['phone'])}",
                 f"Доставка: {payload['delivery_date']}, 08:00–18:00",
                 "По Ташкенту. Доставка включена. Оплата наличными."])
    if payload.get("address"):
        rows.append("Адрес / ориентир: " + escape(payload["address"]))
    lat, lon = payload["location"]["latitude"], payload["location"]["longitude"]
    rows.append(f'<a href="https://maps.google.com/?q={lat},{lon}">📍 Локация доставки</a>')
    if order_id:
        rows.append(f'<a href="tg://user?id={payload["chat_id"]}">Написать клиенту в Telegram</a>')
    return "\n".join(rows)


class Shop:
    def __init__(self, store, catalog, admin_chat_id=0, clock=None):
        self.store, self.catalog = store, catalog
        self.admin = admin_chat_id
        self.clock = clock or (lambda: datetime.now(TZ))

    def send(self, chat_id, text, markup=None):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "link_preview_options": {"is_disabled": True}}
        if markup is not None:
            p["reply_markup"] = markup
        self.store.enqueue("sendMessage", p)

    def catalog_screen(self, chat_id):
        rows = [[button(title, f"cat:{i}")] for i, title in enumerate(self.catalog.categories)]
        rows.append([button("🛒 Корзина", "cart")])
        self.send(chat_id, "<b>НАБЕРЕЖНАЯ — оптовый каталог</b>\n\n"
                  "Выберите раздел. Количество — целыми блоками. Цены с НДС.\n"
                  f"Минимальный заказ: {money(MIN_ORDER)}.\n"
                  "Доставка по Ташкенту включена. Оплата наличными.", keyboard(*rows))

    def category_screen(self, chat_id, index):
        if not index.isdigit() or int(index) >= len(self.catalog.categories):
            self.catalog_screen(chat_id)
            return
        category = self.catalog.categories[int(index)]
        rows = [[button(f"{p['name']} · {money(p['block_price'])}/блок", "product:" + p["id"])]
                for p in self.catalog.products.values() if p["category"] == category]
        rows.append([button("← Разделы", "catalog"), button("🛒 Корзина", "cart")])
        self.send(chat_id, f"<b>{escape(category)}</b>\nЦена указана за один блок.", keyboard(*rows))

    def product_screen(self, chat_id, state, pid, message=None):
        p = self.catalog.products.get(pid)
        if not p:
            self.send(chat_id, "Этот товар недоступен. Откройте каталог заново.")
            return
        quantity = state["cart"].get(pid, 0)
        text = (f"<b>{escape(p['name'])}</b>\n\n"
                f"1 блок = {p['units_per_block']} шт.\n"
                f"<b>Цена блока: {money(p['block_price'])}</b>\n"
                f"Цена за банку: {money(p['unit_price'])} · с НДС\n\n"
                f"В корзине: {blocks(quantity)} / {quantity*p['units_per_block']} шт.\n"
                f"Сумма: {money(quantity*p['block_price'])}")
        markup = keyboard([button("− 1 блок", f"qty:{pid}:-1"), button("+ 1 блок", f"qty:{pid}:1")],
                          [button("Указать количество блоков", "set:" + pid)],
                          [button("← Каталог", "catalog"), button("🛒 Корзина", "cart")])
        if message:
            params = {"chat_id": chat_id, "message_id": message["message_id"],
                      "parse_mode": "HTML", "reply_markup": markup}
            params["caption" if message.get("photo") else "text"] = text
            self.store.enqueue("editMessageCaption" if message.get("photo") else "editMessageText", params)
        elif p.get("image"):
            self.store.enqueue("sendPhoto", {"chat_id": chat_id, "photo": PHOTO_BASE+p["image"]+".jpg",
                                "caption": text, "parse_mode": "HTML", "reply_markup": markup})
        else:
            self.send(chat_id, text, markup)

    def cart_screen(self, chat_id, state):
        lines = self.catalog.lines(state["cart"])
        if not lines:
            self.send(chat_id, "Корзина пока пустая.", keyboard([button("📦 Каталог", "catalog")]))
            return
        total = sum(p["amount"] for p in lines)
        rows = ["<b>Ваша корзина</b>", ""]
        for line in lines:
            rows.append(f"{escape(line['name'])}\n{blocks(line['blocks'])} / {line['units']} шт. — {money(line['amount'])}")
        rows += ["", f"<b>Итого: {money(total)}</b>", "Доставка по Ташкенту включена."]
        if total < MIN_ORDER:
            rows.append(f"До минимального заказа ещё {money(MIN_ORDER-total)}.")
        markup = [[button("Изменить: " + p["name"], "product:"+p["id"])] for p in lines]
        markup += [[button("Оформить заказ", "checkout")],
                   [button("Продолжить выбор", "catalog"), button("Очистить корзину", "clear")]]
        self.send(chat_id, "\n".join(rows), keyboard(*markup))

    def process(self, update):
        uid = update.get("update_id")
        if type(uid) is not int:
            return
        with self.store.db:
            if self.store.db.execute("SELECT 1 FROM processed WHERE update_id=?", (uid,)).fetchone():
                return
            query = update.get("callback_query")
            message = query.get("message", {}) if query else update.get("message", {})
            chat = message.get("chat", {})
            sender = query.get("from", {}) if query else message.get("from", {})
            if query:
                self.store.enqueue("answerCallbackQuery", {"callback_query_id": query["id"]})
            # Never collect customer data or accept forwarded keyboards in groups.
            if chat.get("type") == "private" and chat.get("id") == sender.get("id"):
                chat_id = chat["id"]
                state = self.store.session(chat_id)
                if query:
                    self.callback(chat_id, state, str(query.get("data", "")), message, uid)
                else:
                    self.message(chat_id, state, message)
                self.store.save(chat_id, state)
            self.store.db.execute("INSERT INTO processed VALUES (?)", (uid,))
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('offset', ?)", (str(uid+1),))

    def callback(self, chat_id, state, data, message, update_id):
        if data.startswith("status:"):
            self.change_status(chat_id, data)
            return
        if data.startswith("submit:"):
            self.submit(chat_id, state, data[7:])
            return
        if data == "checkout":
            self.checkout(chat_id, state, update_id)
            return
        # A cart change invalidates any previously displayed confirmation.
        if data in ("catalog", "cart", "clear", "edit") or data.startswith(("product:", "qty:", "set:", "cat:")):
            state["stage"], state["draft"] = "shopping", {}
        if data == "catalog" or data == "edit":
            self.catalog_screen(chat_id)
        elif data.startswith("cat:"):
            self.category_screen(chat_id, data[4:])
        elif data == "cart":
            self.cart_screen(chat_id, state)
        elif data == "clear":
            self.send(chat_id, "Очистить корзину?", keyboard([button("Да, очистить", "clear_yes"), button("Сохранить", "cart")]))
        elif data == "clear_yes":
            state.update(stage="shopping", cart={}, draft={})
            self.cart_screen(chat_id, state)
        elif data.startswith("product:"):
            self.product_screen(chat_id, state, data[8:])
        elif data.startswith("qty:"):
            parts = data.split(":")
            if len(parts) != 3 or parts[1] not in self.catalog.products or parts[2] not in ("1", "-1"):
                return
            pid, delta = parts[1], int(parts[2])
            quantity = max(0, min(MAX_BLOCKS, state["cart"].get(pid, 0)+delta))
            if quantity:
                state["cart"][pid] = quantity
            else:
                state["cart"].pop(pid, None)
            self.product_screen(chat_id, state, pid, message)
        elif data.startswith("set:") and data[4:] in self.catalog.products:
            state["stage"] = "quantity"
            state["draft"] = {"product": data[4:]}
            self.send(chat_id, f"Введите количество целых блоков от 0 до {MAX_BLOCKS}.\n0 — убрать товар.\n/cancel — назад.")

    def checkout(self, chat_id, state, update_id):
        if not self.admin:
            self.send(chat_id, "Приём заказов ещё подключается. Корзина сохранена.")
            return
        lines = self.catalog.lines(state["cart"])
        total = sum(p["amount"] for p in lines)
        if not lines or total < MIN_ORDER:
            self.cart_screen(chat_id, state)
            return
        state["stage"] = "name"
        state["draft"] = {"nonce": f"{chat_id}-{update_id}", "catalog_version": self.catalog.version}
        self.send(chat_id, "<b>1 из 3. Как к вам обращаться?</b>\nВведите имя или название магазина.\n/cancel — вернуться к выбору.", {"remove_keyboard": True})

    def message(self, chat_id, state, message):
        text = (message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
        if command in ("/id", "/myid"):
            self.send(chat_id, f"Ваш Telegram ID: <code>{chat_id}</code>\nДля получения заказов этот номер указывается как ADMIN_CHAT_ID.")
            return
        if command == "/orders":
            if chat_id == self.admin:
                self.admin_orders(chat_id)
            return
        if command in ("/start", "/catalog", "/cancel") or text == "📦 Каталог":
            state.update(stage="shopping", draft={})
            self.send(chat_id, "Добро пожаловать в НАБЕРЕЖНУЮ. Выбирайте товары и блоки.", MAIN_MENU)
            self.catalog_screen(chat_id)
            return
        if command == "/cart" or text == "🛒 Корзина":
            state.update(stage="shopping", draft={})
            self.cart_screen(chat_id, state)
            return
        stage, draft = state["stage"], state["draft"]
        if stage == "quantity":
            if not re.fullmatch(r"[0-9]{1,3}", text):
                self.send(chat_id, f"Нужно целое число блоков от 0 до {MAX_BLOCKS}, например 3.")
                return
            quantity, pid = int(text), draft["product"]
            if quantity:
                state["cart"][pid] = quantity
            else:
                state["cart"].pop(pid, None)
            state.update(stage="shopping", draft={})
            self.product_screen(chat_id, state, pid)
        elif stage == "name":
            if not 2 <= len(text) <= 100 or text.startswith("/") or any(ord(c) < 32 for c in text):
                self.send(chat_id, "Введите имя или магазин: от 2 до 100 символов.")
                return
            draft["name"], state["stage"] = text, "phone"
            self.send(chat_id, "<b>2 из 3. Ваш телефон</b>\nНажмите кнопку или напишите номер.",
                      {"keyboard": [[{"text": "📞 Отправить мой номер", "request_contact": True}]],
                       "resize_keyboard": True, "one_time_keyboard": True})
        elif stage == "phone":
            contact = message.get("contact")
            if contact and contact.get("user_id") != chat_id:
                self.send(chat_id, "Отправьте свой контакт кнопкой или напишите номер вручную.")
                return
            try:
                draft["phone"] = phone_number(contact["phone_number"] if contact else text)
            except (ValueError, KeyError):
                self.send(chat_id, "Проверьте номер. Например: +998 90 123 45 67.")
                return
            state["stage"] = "location"
            self.send(chat_id, "<b>3 из 3. Куда доставить?</b>\n"
                      "Отправьте точку доставки через скрепку → Геопозиция. "
                      "Если доставка туда, где вы сейчас, нажмите кнопку ниже.\n"
                      "Доставка по Ташкенту.",
                      {"keyboard": [[{"text": "📍 Отправить мою геопозицию", "request_location": True}]],
                       "resize_keyboard": True, "one_time_keyboard": True})
        elif stage == "location":
            location = message.get("location") or message.get("venue", {}).get("location") or {}
            lat, lon = location.get("latitude"), location.get("longitude")
            if (type(lat) not in (int, float) or type(lon) not in (int, float)
                    or not math.isfinite(lat) or not math.isfinite(lon)
                    or not -90 <= lat <= 90 or not -180 <= lon <= 180):
                self.send(chat_id, "Нужна точка на карте. Скрепка → Геопозиция → выберите адрес доставки.")
                return
            draft["location"] = {"latitude": lat, "longitude": lon}
            state["stage"] = "address"
            self.send(chat_id, "Добавьте адрес / ориентир, чтобы водитель нашёл вас. Или нажмите «Пропустить».",
                      {"keyboard": [["Пропустить"]], "resize_keyboard": True, "one_time_keyboard": True})
        elif stage == "address":
            if not text or len(text) > 200:
                self.send(chat_id, "Напишите адрес до 200 символов или нажмите «Пропустить».")
                return
            draft["address"] = "" if text == "Пропустить" else text
            now = self.clock().astimezone(TZ)
            delivery = now.date() if now.hour < 12 else now.date()+timedelta(days=1)
            draft["delivery_date"] = delivery.strftime("%d.%m.%Y")
            state["stage"] = "confirm"
            payload = self.payload(chat_id, state)
            self.send(chat_id, "Данные заполнены. Осталось подтвердить заказ.", {"remove_keyboard": True})
            self.send(chat_id, order_text(payload), keyboard(
                [button("✅ Отправить заказ", "submit:"+draft["nonce"])],
                [button("Изменить заказ", "edit")]))
        elif stage == "confirm":
            self.send(chat_id, "Для отправки нажмите «✅ Отправить заказ» под проверкой заказа. /cancel — вернуться.")
        else:
            self.catalog_screen(chat_id)

    def payload(self, chat_id, state):
        lines = self.catalog.lines(state["cart"])
        return {**state["draft"], "chat_id": chat_id, "lines": lines,
                "total": sum(p["amount"] for p in lines), "created_at": self.clock().isoformat()}

    def submit(self, chat_id, state, nonce):
        existing = self.store.db.execute("SELECT id,chat_id FROM orders WHERE nonce=?", (nonce,)).fetchone()
        if existing:
            if existing["chat_id"] == chat_id:
                self.send(chat_id, f"Заказ №{existing['id']} уже сохранён. Повторно отправлять его не нужно.")
            return
        draft = state["draft"]
        if state["stage"] != "confirm" or draft.get("nonce") != nonce:
            self.send(chat_id, "Это подтверждение устарело. Откройте корзину и оформите заказ заново.")
            return
        if draft.get("catalog_version") != self.catalog.version:
            state.update(stage="shopping", draft={})
            self.send(chat_id, "Прайс обновился. Проверьте новую сумму в корзине и подтвердите заказ ещё раз.")
            self.cart_screen(chat_id, state)
            return
        payload = self.payload(chat_id, state)
        required = ("name", "phone", "location", "delivery_date")
        if not self.admin or not payload["lines"] or payload["total"] < MIN_ORDER or any(not payload.get(k) for k in required):
            self.send(chat_id, "Заказ не отправлен: заполните данные через корзину.")
            return
        # Ask again if confirmation straddles the delivery cutoff.
        now = self.clock().astimezone(TZ)
        delivery = now.date() if now.hour < 12 else now.date()+timedelta(days=1)
        delivery_date = delivery.strftime("%d.%m.%Y")
        if payload["delivery_date"] != delivery_date:
            draft["delivery_date"] = delivery_date
            payload["delivery_date"] = delivery_date
            self.send(chat_id, "Время приёма изменилось. Проверьте новую дату доставки и подтвердите заказ ещё раз.")
            self.send(chat_id, order_text(payload), keyboard([button("✅ Отправить заказ", "submit:"+nonce)]))
            return
        cur = self.store.db.execute("INSERT INTO orders(nonce,chat_id,payload) VALUES (?,?,?)",
                                    (nonce, chat_id, json.dumps(payload)))
        order_id = cur.lastrowid
        self.send(chat_id, f"Заказ №{order_id} сохранён и передаётся менеджеру. Не отправляйте его повторно.", MAIN_MENU)
        self.store.enqueue("sendMessage", {"chat_id": self.admin, "text": order_text(payload, order_id),
                           "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                           "reply_markup": keyboard([button("Принят", f"status:{order_id}:accepted"),
                                                    button("Доставлен", f"status:{order_id}:delivered")])}, order_id)
        state.update(stage="shopping", cart={}, draft={})

    def change_status(self, chat_id, data):
        if chat_id != self.admin:
            return
        parts = data.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("accepted", "delivered"):
            return
        row = self.store.db.execute("SELECT * FROM orders WHERE id=?", (int(parts[1]),)).fetchone()
        if not row or row["status"] == parts[2] or (row["status"] == "delivered" and parts[2] == "accepted"):
            return
        self.store.db.execute("UPDATE orders SET status=? WHERE id=?", (parts[2], row["id"]))
        label = "принят в работу" if parts[2] == "accepted" else "доставлен"
        self.send(chat_id, f"Заказ №{row['id']}: {label}.")
        self.send(row["chat_id"], f"Ваш заказ №{row['id']} {label}.")

    def admin_orders(self, chat_id):
        rows = self.store.db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            self.send(chat_id, "Заказов пока нет.")
        for row in rows:
            p = json.loads(row["payload"])
            self.send(chat_id, order_text(p, row["id"])+f"\nСтатус: {escape(row['status'])}", keyboard(
                [button("Принят", f"status:{row['id']}:accepted"), button("Доставлен", f"status:{row['id']}:delivered")]))


class TelegramError(Exception):
    def __init__(self, code, retry_after=0, description=""):
        super().__init__(f"Telegram API error {code}")
        self.code, self.retry_after, self.description = code, retry_after, description


class Telegram:
    def __init__(self, token):
        self._base = "https://api.telegram.org/bot"+token+"/"

    def call(self, method, params):
        request = urllib.request.Request(self._base+method, data=json.dumps(params).encode(),
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError):
                raise TelegramError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Never include request URLs in logs: Telegram URLs contain the token.
            raise TelegramError(0) from None
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 0),
                                result.get("description", ""))
        return result.get("result")


def flush_outbox(shop, api):
    store = shop.store
    rows = store.db.execute("SELECT * FROM outbox WHERE done=0 AND next_try<=? ORDER BY id LIMIT 100", (time.time(),)).fetchall()
    for row in rows:
        params = json.loads(row["params"])
        try:
            api.call(row["method"], params)
        except TelegramError as error:
            with store.db:
                if row["method"] == "sendPhoto" and error.code in (400, 404):
                    # A photo host outage must not prevent ordering.
                    fallback = {k:v for k,v in params.items() if k not in ("photo", "caption")}
                    fallback["text"] = params["caption"]
                    store.db.execute("UPDATE outbox SET method='sendMessage',params=? WHERE id=?", (json.dumps(fallback), row["id"]))
                elif error.code == 400 and row["method"] in ("editMessageText", "editMessageCaption"):
                    if "message is not modified" not in error.description.lower():
                        fallback = {k:v for k,v in params.items() if k not in ("message_id", "caption")}
                        fallback["text"] = params.get("text", params.get("caption", ""))
                        store.enqueue("sendMessage", fallback)
                    store.db.execute("UPDATE outbox SET done=1 WHERE id=?", (row["id"],))
                elif row["method"] == "answerCallbackQuery" or (error.code in (400, 403, 404) and not row["order_id"]):
                    store.db.execute("UPDATE outbox SET done=1 WHERE id=?", (row["id"],))
                else:
                    delay = max(error.retry_after, min(300, 2**min(row["attempts"]+1, 8)))
                    store.db.execute("UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?", (time.time()+delay, row["id"]))
                    LOG.warning("Delivery retry: method=%s code=%s", row["method"], error.code)
            continue
        with store.db:
            store.db.execute("UPDATE outbox SET done=1 WHERE id=?", (row["id"],))
            if row["order_id"]:
                order = store.db.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],)).fetchone()
                if not order["notified"]:
                    store.db.execute("UPDATE orders SET notified=1 WHERE id=?", (order["id"],))
                    payload = json.loads(order["payload"])
                    shop.send(order["chat_id"], f"✅ Заказ №{order['id']} передан менеджеру.\n"
                              f"Сумма: {money(payload['total'])}.\n"
                              f"Доставка: {payload['delivery_date']}, 08:00–18:00. Спасибо!")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise SystemExit("Set BOT_TOKEN in the hosting environment before starting.")
    admin = os.environ.get("ADMIN_CHAT_ID", "0").strip()
    if not admin.isdigit():
        raise SystemExit("ADMIN_CHAT_ID must be a positive private Telegram chat ID.")
    on_render = os.environ.get("RENDER") == "true"
    default_db = "/var/data/marketbot.sqlite3" if on_render else str(Path(__file__).with_name("data") / "marketbot.sqlite3")
    db_path = os.environ.get("BOT_DB_PATH", default_db)
    if on_render:
        if not os.path.ismount("/var/data"):
            raise SystemExit("Attach a persistent disk at /var/data before accepting orders on Render.")
        if not Path(db_path).resolve().is_relative_to(Path("/var/data")):
            raise SystemExit("BOT_DB_PATH must be under the persistent /var/data mount.")
    os.umask(0o077)
    shop = Shop(Store(db_path), Catalog(), int(admin))
    api, stop = Telegram(token), threading.Event()
    health = {"verified": False, "poll_at": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/health"):
                self.send_error(404)
                return
            ok = health["verified"] and time.monotonic()-health["poll_at"] < 120
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"running": ok, "orders_configured": bool(shop.admin)}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        me = api.call("getMe", {})
        if str(me.get("username", "")).lower() != "naberejnayamarketbot":
            raise SystemExit("BOT_TOKEN belongs to a different bot. Expected @naberejnayamarketbot.")
        webhook = api.call("getWebhookInfo", {})
        if webhook.get("url"):
            raise SystemExit("This bot already has a webhook. Confirm migration before changing its delivery mode.")
        if shop.admin:
            recipient = api.call("getChat", {"chat_id": shop.admin})
            if recipient.get("type") != "private":
                raise SystemExit("Order recipient must be the owner's private Telegram chat.")
        api.call("setMyCommands", {"commands": [
            {"command": "catalog", "description": "Оптовый каталог"},
            {"command": "cart", "description": "Моя корзина"},
            {"command": "cancel", "description": "Вернуться к выбору"},
            {"command": "id", "description": "Мой Telegram ID"}]})
        health["verified"], health["poll_at"] = True, time.monotonic()
        LOG.info("Bot verified. Products=%s, order recipient configured=%s", len(shop.catalog.products), bool(shop.admin))
        while not stop.is_set():
            try:
                flush_outbox(shop, api)
                pending = shop.store.db.execute("SELECT MIN(next_try) FROM outbox WHERE done=0").fetchone()[0]
                timeout = 20 if pending is None else max(1, min(20, int(pending-time.time())))
                updates = api.call("getUpdates", {"offset": shop.store.offset(), "timeout": timeout,
                                   "allowed_updates": ["message", "callback_query"]})
                health["poll_at"] = time.monotonic()
                for update in updates:
                    shop.process(update)
                flush_outbox(shop, api)
            except TelegramError as error:
                LOG.warning("Telegram connection: code=%s", error.code)
                if error.code in (401, 409):
                    raise SystemExit("Check bot credentials and ensure only one polling instance runs.") from None
                stop.wait(max(2, min(error.retry_after or 5, 30)))
    finally:
        health["verified"] = False
        server.shutdown()
        shop.store.db.close()


if __name__ == "__main__":
    main()
