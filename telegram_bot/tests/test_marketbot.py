import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from telegram_bot.bot import Catalog, Shop, Store, TelegramError, TZ, flush_outbox, order_text, phone_number


class FakeTelegram:
    def __init__(self, fail_chat=None, fail_code=500):
        self.calls = []
        self.fail_chat = fail_chat
        self.fail_code = fail_code

    def call(self, method, params):
        if method == "sendMessage" and params.get("chat_id") == self.fail_chat:
            raise TelegramError(self.fail_code)
        self.calls.append((method, copy.deepcopy(params)))
        return {"message_id": len(self.calls)}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.store = Store(self.path)
        self.catalog = Catalog()
        self.now = datetime(2026, 10, 24, 11, 45, tzinfo=TZ)
        self.shop = Shop(self.store, self.catalog, 900, clock=lambda: self.now)
        self.seq = 0

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def message(self, text=None, who=100, **extra):
        self.seq += 1
        m = {"message_id": self.seq, "chat": {"id": who, "type": "private"},
             "from": {"id": who}, **extra}
        if text is not None:
            m["text"] = text
        update = {"update_id": self.seq, "message": m}
        self.shop.process(update)
        return update

    def callback(self, data, who=100, **extra):
        self.seq += 1
        update = {"update_id": self.seq, "callback_query": {"id": str(self.seq), "data": data,
                  "from": {"id": who}, "message": {"message_id": self.seq,
                  "chat": {"id": who, "type": "private"}, **extra}}}
        self.shop.process(update)
        return update

    def state(self, who=100):
        return self.store.session(who)

    def order_count(self):
        return self.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def choose(self):
        # 3 blocks cherry compote = 396000, 1 block cornichons = 121600.
        for _ in range(3):
            self.callback("qty:p22:1")
        self.callback("qty:p07:1")

    def prepare_order(self):
        self.choose()
        self.callback("checkout")
        self.message("Магазин Марлен")
        self.message(contact={"user_id": 100, "phone_number": "998901234567"})
        self.message(location={"latitude": 41.3111, "longitude": 69.2797})
        self.message("Алайский базар, вход со двора")
        return self.state()["draft"]["nonce"]

    def test_catalog_pack_sizes_and_prices(self):
        self.assertEqual(len(self.catalog.products), 27)
        self.assertEqual(sum(p["block_price"] for p in self.catalog.products.values()), 4_298_200)
        self.assertEqual(self.catalog.products["p01"]["units_per_block"], 10)
        self.assertEqual(self.catalog.products["p03"]["units_per_block"], 18)
        self.assertEqual(self.catalog.products["p05"]["units_per_block"], 18)
        self.assertEqual(self.catalog.products["p22"]["block_price"], 132000)
        self.assertEqual(self.catalog.products["p25"]["units_per_block"], 4)

    def test_whole_blocks_only(self):
        for bad in (1.5, "2", -1, True, 1000):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.catalog.lines({"p22": bad})
        with self.assertRaises(ValueError):
            self.catalog.lines({"unknown": 1})

    def test_typed_fraction_and_negative_rejected(self):
        self.callback("set:p22")
        for text in ("1.5", "1,5", "-1", "1000", "2 банки", "+3"):
            self.message(text)
            self.assertEqual(self.state()["cart"], {})
            self.assertEqual(self.state()["stage"], "quantity")
        self.message("12")
        self.assertEqual(self.state()["cart"], {"p22": 12})

    def test_duplicate_update_does_not_add_twice(self):
        update = self.callback("qty:p22:1")
        self.shop.process(update)
        self.assertEqual(self.state()["cart"], {"p22": 1})

    def test_minimum_order_enforced(self):
        self.callback("qty:p22:1")
        self.callback("checkout")
        self.assertEqual(self.state()["stage"], "shopping")
        self.assertEqual(self.order_count(), 0)

    def test_exact_minimum_order_is_accepted(self):
        for _ in range(4):
            self.callback("qty:p01:1")
        self.callback("checkout")
        self.assertEqual(self.state()["stage"], "name")

    def test_just_below_minimum_is_rejected(self):
        for _ in range(3):
            self.callback("qty:p01:1")
        self.callback("qty:p07:1")
        self.callback("checkout")
        self.assertEqual(self.state()["stage"], "shopping")

    def test_no_owner_blocks_checkout(self):
        self.shop.admin = 0
        self.choose()
        self.callback("checkout")
        self.assertEqual(self.state()["stage"], "shopping")

    def test_requires_real_location(self):
        self.choose()
        self.callback("checkout")
        self.message("Магазин")
        self.message("+998 90 123 45 67")
        for payload in ({"text": "Ташкент"}, {"location": {"latitude": 100, "longitude": 69}},
                        {"location": {"latitude": float("nan"), "longitude": 69}}):
            self.message(**payload)
            self.assertEqual(self.state()["stage"], "location")
            self.assertEqual(self.order_count(), 0)

    def test_other_person_contact_rejected(self):
        self.choose()
        self.callback("checkout")
        self.message("Магазин")
        self.message(contact={"user_id": 200, "phone_number": "998901234567"})
        self.assertEqual(self.state()["stage"], "phone")

    def test_full_checkout_price_and_duplicate_confirmation(self):
        nonce = self.prepare_order()
        self.assertEqual(self.state()["stage"], "confirm")
        self.assertEqual(self.order_count(), 0)
        self.callback("submit:"+nonce)
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 1)
        row = self.store.db.execute("SELECT * FROM orders").fetchone()
        payload = json.loads(row["payload"])
        self.assertEqual(payload["total"], 517600)
        self.assertEqual(payload["delivery_date"], "24.10.2026")
        self.assertEqual(sum(p["units"] for p in payload["lines"]), 20)
        self.assertEqual(self.state()["cart"], {})
        queued = self.store.db.execute("SELECT * FROM outbox WHERE order_id IS NOT NULL").fetchall()
        self.assertEqual(len(queued), 1)
        text = json.loads(queued[0]["params"])["text"]
        self.assertIn("3 блока / 12 шт.", text)
        self.assertIn("517 600 сум", text)
        self.assertIn("maps.google.com/?q=41.3111,69.2797", text)

    def test_cancel_preserves_cart_and_invalidates_confirmation(self):
        nonce = self.prepare_order()
        self.message("/cancel")
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 0)
        self.assertTrue(self.state()["cart"])

    def test_edit_cart_invalidates_old_confirmation(self):
        nonce = self.prepare_order()
        self.callback("qty:p22:1")
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 0)

    def test_catalog_update_requires_new_confirmation(self):
        nonce = self.prepare_order()
        self.catalog.version = "changed"
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 0)
        self.assertEqual(self.state()["stage"], "shopping")

    def test_noon_crossing_requires_confirmation_of_new_day(self):
        nonce = self.prepare_order()
        self.now = self.now.replace(hour=12, minute=0)
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 0)
        self.assertEqual(self.state()["draft"]["delivery_date"], "25.10.2026")
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 1)

    def test_after_noon_delivery_is_next_day(self):
        self.now = self.now.replace(hour=14)
        nonce = self.prepare_order()
        self.assertEqual(self.state()["draft"]["delivery_date"], "25.10.2026")
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 1)

    def test_restart_keeps_cart_and_order_queue(self):
        nonce = self.prepare_order()
        self.store.db.close()
        self.store = Store(self.path)
        self.shop = Shop(self.store, self.catalog, 900, clock=lambda: self.now)
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 1)
        self.store.db.close()
        self.store = Store(self.path)
        self.shop = Shop(self.store, self.catalog, 900, clock=lambda: self.now)
        self.assertEqual(self.order_count(), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE order_id IS NOT NULL AND done=0").fetchone()[0], 1)

    def test_admin_failure_retains_order_for_retry(self):
        nonce = self.prepare_order()
        self.callback("submit:"+nonce)
        api = FakeTelegram(fail_chat=900)
        flush_outbox(self.shop, api)
        row = self.store.db.execute("SELECT * FROM orders").fetchone()
        self.assertFalse(row["notified"])
        self.assertFalse(any("передан менеджеру" in p.get("text", "") for _, p in api.calls))
        api.fail_chat = None
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET next_try=0")
        flush_outbox(self.shop, api)
        flush_outbox(self.shop, api)
        self.assertTrue(self.store.db.execute("SELECT notified FROM orders").fetchone()[0])
        self.assertEqual(sum(1 for m,p in api.calls if m == "sendMessage" and p["chat_id"] == 900), 1)
        self.assertTrue(any("передан менеджеру" in p.get("text", "") for _,p in api.calls))

    def test_customer_cannot_change_status(self):
        nonce = self.prepare_order()
        self.callback("submit:"+nonce)
        self.callback("status:1:accepted", who=200)
        self.assertEqual(self.store.db.execute("SELECT status FROM orders").fetchone()[0], "new")
        self.callback("status:1:accepted", who=900)
        self.assertEqual(self.store.db.execute("SELECT status FROM orders").fetchone()[0], "accepted")
        self.callback("status:1:delivered", who=900)
        self.callback("status:1:accepted", who=900)
        self.assertEqual(self.store.db.execute("SELECT status FROM orders").fetchone()[0], "delivered")

    def test_cannot_replay_another_users_confirmation(self):
        nonce = self.prepare_order()
        self.callback("submit:"+nonce, who=200)
        self.assertEqual(self.order_count(), 0)
        self.callback("submit:"+nonce)
        self.assertEqual(self.order_count(), 1)

    def test_all_products_fit_telegram_message(self):
        self.choose()
        self.callback("checkout")
        state = self.state()
        state["cart"] = {pid: 999 for pid in self.catalog.products}
        state["draft"].update(name="<"*100, phone="+998901234567", location={"latitude": 41.3, "longitude": 69.2},
                               address="<"*200, delivery_date="24.10.2026")
        text = order_text(self.shop.payload(100, state), 123456789)
        # Telegram's 4096-character limit applies after parsing HTML entities.
        import html
        import re
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.assertLessEqual(len(plain), 4096)

    def test_phone_normalization(self):
        self.assertEqual(phone_number("90 123 45 67"), "+998901234567")
        self.assertEqual(phone_number("+998 (90) 123-45-67"), "+998901234567")
        for value in ("9981234567", "abc998901234567", "+9989", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                phone_number(value)

    def test_html_input_is_escaped(self):
        nonce = self.prepare_order()
        state = self.state()
        state["draft"]["name"] = "<b>Али</b> & Co"
        with self.store.db:
            self.store.save(100, state)
        self.callback("submit:"+nonce)
        p = json.loads(self.store.db.execute("SELECT payload FROM orders").fetchone()[0])
        self.assertIn("&lt;b&gt;Али&lt;/b&gt; &amp; Co", order_text(p))

    def test_group_message_does_not_create_session(self):
        self.shop.process({"update_id": 1000, "message": {"chat": {"id": -100, "type": "group"},
                           "from": {"id": 100}, "text": "/start"}})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
