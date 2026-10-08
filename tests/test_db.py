"""資料層測試：積分、簽到、訂單金額分配、入帳對帳。"""
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import db as db_module
from db import (
    CLOCK_SKEW,
    DB,
    RESERVE_GRACE,
    InsufficientPoints,
    NoAmountSlot,
    NotAvailable,
    PriceChanged,
    fmt_usdt,
    parse_price,
)
from tests.fakes import SHOP_ADDR, TZ, make_address

T0 = int(datetime(2026, 10, 7, 12, 0, tzinfo=TZ).timestamp())  # 固定的測試時間
HALF_HOUR = 1800


class DBTestCase(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:", TZ)
        for user_id, name in ((1, "小明"), (2, "小華"), (3, "小美")):
            self.db.upsert_user(user_id, None, name)

    def tearDown(self):
        self.db.close()

    def count(self, table: str) -> int:
        return self.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def give_points(self, user_id: int, points: int) -> None:
        with self.db.tx():
            self.db._add_points(user_id, points, "checkin", "測試", T0)


class PriceTest(unittest.TestCase):
    def test_parse_price(self):
        self.assertEqual(parse_price("9.9"), 9_900_000)
        self.assertEqual(parse_price(" 10 "), 10_000_000)
        self.assertEqual(parse_price("0.01"), 10_000)
        self.assertEqual(parse_price("0"), 0)

    def test_parse_price_rejects_bad_input(self):
        for bad in ("abc", "", "-1", "1.234", "NaN", "inf", "1e9", "1e30", "9,9"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_price(bad)

    def test_fmt_usdt(self):
        self.assertEqual(fmt_usdt(9_900_000), "9.90")
        self.assertEqual(fmt_usdt(19_900_100, 4), "19.9001")


class CheckinTest(DBTestCase):
    def test_once_per_day(self):
        self.assertEqual(self.db.checkin(1, now=T0), (True, 1, 1))
        self.assertEqual(self.db.checkin(1, now=T0 + 3600), (False, 0, 1))  # 同一天再簽不給分
        self.assertEqual(self.db.checkin(1, now=T0 + 86400), (True, 1, 2))  # 隔天可以再簽
        self.assertEqual(self.count("checkins"), 2)
        self.assertEqual(self.count("points_ledger"), 2)

    def test_day_boundary_follows_local_timezone(self):
        # 台北 23:30 與隔天 00:30：以 UTC 看是同一天，但以台北時間看是兩天，兩次都要算
        before = int(datetime(2026, 10, 7, 23, 30, tzinfo=TZ).timestamp())
        after = int(datetime(2026, 10, 8, 0, 30, tzinfo=TZ).timestamp())
        self.assertTrue(self.db.checkin(1, now=before)[0])
        self.assertTrue(self.db.checkin(1, now=after)[0])
        dates = [r[0] for r in self.db.conn.execute("SELECT checkin_date FROM checkins ORDER BY id")]
        self.assertEqual(dates, ["2026-10-07", "2026-10-08"])

    def test_reward_follows_owner_setting(self):
        self.db.set_setting("checkin_points", "5")
        self.assertEqual(self.db.checkin(1, now=T0), (True, 5, 5))
        ledger = self.db.conn.execute("SELECT change, balance_after, reason, ref FROM points_ledger").fetchone()
        self.assertEqual(tuple(ledger), (5, 5, "checkin", "2026-10-07"))

    def test_users_are_independent(self):
        self.db.checkin(1, now=T0)
        self.assertEqual(self.db.checkin(2, now=T0), (True, 1, 1))


class RedeemTest(DBTestCase):
    def test_not_available_until_cost_is_set(self):
        self.give_points(1, 100)
        with self.assertRaises(NotAvailable):
            self.db.redeem_with_points(1, "A", now=T0)

    def test_insufficient_points_changes_nothing(self):
        self.db.set_points_cost("A", 10)
        self.give_points(1, 9)
        with self.assertRaises(InsufficientPoints):
            self.db.redeem_with_points(1, "A", now=T0)
        self.assertEqual(self.db.get_points(1), 9)
        self.assertEqual(self.count("orders"), 0)
        self.assertEqual(self.count("points_ledger"), 1)  # 只有一開始加分那一筆

    def test_success_deducts_points_and_creates_paid_order(self):
        self.db.set_points_cost("A", 10)
        self.give_points(1, 25)
        order = self.db.redeem_with_points(1, "A", expected_cost=10, now=T0)
        self.assertEqual((order["status"], order["pay_method"], order["points_spent"]), ("paid", "points", 10))
        self.assertTrue(order["order_no"].startswith("A261007"))
        self.assertEqual(self.db.get_points(1), 15)
        last = self.db.conn.execute("SELECT * FROM points_ledger ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((last["change"], last["balance_after"], last["reason"], last["ref"]),
                         (-10, 15, "redeem", order["order_no"]))

    def test_cost_changed_since_confirmation(self):
        self.db.set_points_cost("A", 10)
        self.give_points(1, 100)
        with self.assertRaises(PriceChanged):
            self.db.redeem_with_points(1, "A", expected_cost=3, now=T0)
        self.assertEqual(self.db.get_points(1), 100)
        self.assertEqual(self.count("orders"), 0)

    def test_product_b_has_no_points_option(self):
        self.db.set_price("B", parse_price("20"))
        self.give_points(1, 10_000)
        with self.assertRaises(NotAvailable):
            self.db.redeem_with_points(1, "B", now=T0)


class UsdtOrderTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.db.set_price("B", parse_price("20"))

    def new_order(self, user_id: int = 1, now: int = T0):
        return self.db.create_usdt_order(user_id, "B", SHOP_ADDR, HALF_HOUR, now=now)[0]

    def test_not_available_without_price(self):
        with self.assertRaises(NotAvailable):
            self.db.create_usdt_order(1, "A", SHOP_ADDR, HALF_HOUR, now=T0)

    def test_not_available_without_receiving_address(self):
        with self.assertRaises(NotAvailable):
            self.db.create_usdt_order(1, "B", "", HALF_HOUR, now=T0)
        self.assertEqual(self.count("orders"), 0)

    def test_order_remembers_the_address_it_was_issued_with(self):
        first = self.new_order(1)
        self.assertEqual(first["pay_address"], SHOP_ADDR)
        # 業主改了地址之後，已經開出去的那張待付款訂單仍然是原本的地址
        again, created = self.db.create_usdt_order(1, "B", make_address("new"), HALF_HOUR, now=T0 + 60)
        self.assertFalse(created)
        self.assertEqual(again["pay_address"], SHOP_ADDR)

    def test_each_pending_order_gets_unique_amount(self):
        first, second = self.new_order(1), self.new_order(2)
        self.assertEqual(first["amount_units"], 20_000_100)  # 20.0001
        self.assertEqual(second["amount_units"], 20_000_200)  # 20.0002
        self.assertEqual(first["expires_at"], T0 + HALF_HOUR)

    def test_same_user_same_product_reuses_pending_order(self):
        first = self.new_order(1)
        again, created = self.db.create_usdt_order(1, "B", SHOP_ADDR, HALF_HOUR, now=T0 + 60)
        self.assertFalse(created)
        self.assertEqual(again["order_no"], first["order_no"])
        self.assertEqual(self.count("orders"), 1)

    def test_amount_stays_reserved_after_expiry_then_frees(self):
        first = self.new_order(1)
        self.db.expire_orders(T0 + HALF_HOUR)
        # 把序號歸零，逼分配器重新從 20.0001 開始挑
        self.db.set_setting("amount_seq", "0")
        soon = self.new_order(2, now=T0 + HALF_HOUR + 1)
        self.assertNotEqual(soon["amount_units"], first["amount_units"])  # 剛到期的金額還不能給別人
        self.db.set_setting("amount_seq", "0")
        later = self.new_order(3, now=T0 + HALF_HOUR + RESERVE_GRACE + 1)
        self.assertEqual(later["amount_units"], first["amount_units"])  # 保留期過後才能重複使用

    def test_no_slot_when_all_amounts_taken(self):
        with mock.patch.object(db_module, "MAX_OFFSET", 2):
            self.new_order(1)
            self.new_order(2)
            with self.assertRaises(NoAmountSlot):
                self.new_order(3)
        self.assertEqual(self.count("orders"), 2)

    def test_cancel_only_by_owner_of_pending_order(self):
        order = self.new_order(1)
        self.assertFalse(self.db.cancel_order(order["order_no"], 2))  # 別人不能取消
        self.assertTrue(self.db.cancel_order(order["order_no"], 1))
        self.assertFalse(self.db.cancel_order(order["order_no"], 1))  # 不能重複取消
        self.assertEqual(self.db.get_order(order["order_no"])["status"], "cancelled")

    def test_expire_orders(self):
        order = self.new_order(1)
        self.assertEqual(self.db.expire_orders(T0 + HALF_HOUR - 1), [])
        expired = self.db.expire_orders(T0 + HALF_HOUR)
        self.assertEqual([o["order_no"] for o in expired], [order["order_no"]])
        self.assertEqual(self.db.get_order(order["order_no"])["status"], "expired")
        self.assertEqual(self.db.expire_orders(T0 + HALF_HOUR + 99), [])  # 不會重複回報


class MatchTransferTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.db.set_price("B", parse_price("20"))
        self.order = self.db.create_usdt_order(1, "B", SHOP_ADDR, HALF_HOUR, now=T0)[0]
        self.amount = self.order["amount_units"]

    def record(self, txid: str, amount: int, ts: int, to: str = SHOP_ADDR):
        return self.db.record_transfer(txid, "TFROM", to, amount, ts, now=ts + 60)

    def status(self) -> str:
        return self.db.get_order(self.order["order_no"])["status"]

    def test_exact_amount_within_window_marks_paid(self):
        paid = self.record("tx1", self.amount, T0 + 120)
        self.assertEqual((paid["status"], paid["txid"], paid["paid_at"]), ("paid", "tx1", T0 + 180))
        payment = self.db.conn.execute("SELECT * FROM payments WHERE txid = 'tx1'").fetchone()
        self.assertEqual(payment["order_no"], self.order["order_no"])

    def test_wrong_amount_is_recorded_but_not_matched(self):
        self.assertIsNone(self.record("tx1", self.amount - 100, T0 + 120))  # 少 0.0001
        self.assertIsNone(self.record("tx2", 20_000_000, T0 + 120))  # 沒帶識別尾數
        self.assertEqual(self.status(), "pending")
        unmatched = self.db.conn.execute("SELECT COUNT(*) FROM payments WHERE order_no IS NULL").fetchone()[0]
        self.assertEqual(unmatched, 2)

    def test_same_transaction_is_processed_once(self):
        self.assertIsNotNone(self.record("tx1", self.amount, T0 + 120))
        self.assertIsNone(self.record("tx1", self.amount, T0 + 120))
        self.assertEqual(self.count("payments"), 1)

    def test_second_transfer_of_same_amount_does_not_match_paid_order(self):
        self.record("tx1", self.amount, T0 + 120)
        self.assertIsNone(self.record("tx2", self.amount, T0 + 180))
        self.assertEqual(self.db.get_order(self.order["order_no"])["txid"], "tx1")

    def test_time_window(self):
        self.assertIsNone(self.record("early", self.amount, T0 - CLOCK_SKEW - 1))  # 訂單建立前的轉帳
        self.assertIsNone(self.record("late", self.amount, T0 + HALF_HOUR + 1))  # 期限過後的轉帳
        self.assertEqual(self.status(), "pending")
        self.assertIsNotNone(self.record("edge", self.amount, T0 + HALF_HOUR))  # 剛好壓線算數

    def test_paid_in_time_but_seen_after_status_became_expired(self):
        self.db.expire_orders(T0 + HALF_HOUR + 5)
        self.assertEqual(self.status(), "expired")
        self.assertIsNotNone(self.record("tx1", self.amount, T0 + HALF_HOUR - 10))
        self.assertEqual(self.status(), "paid")

    def test_paid_after_user_cancelled_still_counts(self):
        self.db.cancel_order(self.order["order_no"], 1)
        self.assertIsNotNone(self.record("tx1", self.amount, T0 + 300))
        self.assertEqual(self.status(), "paid")

    def test_two_users_are_told_apart_by_amount(self):
        other = self.db.create_usdt_order(2, "B", SHOP_ADDR, HALF_HOUR, now=T0)[0]
        paid = self.record("tx1", other["amount_units"], T0 + 120)
        self.assertEqual(paid["user_id"], 2)
        self.assertEqual(self.status(), "pending")

    def test_right_amount_to_a_different_address_does_not_match(self):
        self.assertIsNone(self.record("tx1", self.amount, T0 + 120, to=make_address("someone-else")))
        self.assertEqual(self.status(), "pending")
        self.assertIsNotNone(self.record("tx2", self.amount, T0 + 120))  # 付到訂單指定的地址才算

    def test_watch_targets(self):
        watching = [(SHOP_ADDR, T0 - CLOCK_SKEW)]
        self.assertEqual(self.db.watch_targets(T0 + 10), watching)
        self.db.expire_orders(T0 + HALF_HOUR)
        self.assertEqual(self.db.watch_targets(T0 + HALF_HOUR + RESERVE_GRACE - 1), watching)  # 到期後再看一陣子
        self.assertEqual(self.db.watch_targets(T0 + HALF_HOUR + RESERVE_GRACE + 1), [])  # 之後就不用查了
        # 重啟補查用較長的回溯時間
        self.assertEqual(self.db.watch_targets(T0 + 7200, grace=86400), watching)

    def test_watch_targets_empty_when_everything_paid(self):
        self.record("tx1", self.amount, T0 + 120)
        self.assertEqual(self.db.watch_targets(T0 + 200), [])

    def test_watch_targets_covers_old_and_new_address_during_a_change(self):
        new_address = make_address("new")
        self.db.create_usdt_order(2, "B", new_address, HALF_HOUR, now=T0 + 300)
        self.assertEqual(
            sorted(self.db.watch_targets(T0 + 400)),
            sorted([(SHOP_ADDR, T0 - CLOCK_SKEW), (new_address, T0 + 300 - CLOCK_SKEW)]),
        )


class PanelAndSettingsTest(DBTestCase):
    def test_panel_is_remembered_per_user(self):
        self.assertEqual(self.db.get_panel(1), (None, ""))
        self.db.set_panel(1, 55, "pay:B261007000001")
        self.db.set_panel(2, 66)
        self.assertEqual(self.db.get_panel(1), (55, "pay:B261007000001"))
        self.assertEqual(self.db.get_panel(2), (66, ""))
        self.db.upsert_user(1, "ming", "小明改名")  # 更新用戶資料不會把記錄洗掉
        self.assertEqual(self.db.get_panel(1), (55, "pay:B261007000001"))

    def test_panel_for_unknown_user_creates_a_row(self):
        self.assertEqual(self.db.get_panel(999), (None, ""))
        self.db.set_panel(999, 77)
        self.assertEqual(self.db.get_panel(999), (77, ""))
        self.assertEqual(self.db.get_points(999), 0)

    def test_owner_editable_settings_have_sensible_defaults(self):
        self.assertEqual(self.db.welcome_text(), "測試中")
        self.assertEqual((self.db.channel_url(), self.db.usdt_address()), ("", ""))
        self.db.set_setting("welcome_text", "歡迎")
        self.db.set_product_name("A", "VIP 月卡")
        self.assertEqual(self.db.welcome_text(), "歡迎")
        self.assertEqual(self.db.get_product("A")["name"], "VIP 月卡")
        self.assertEqual(self.db.get_product("B")["name"], "商品B")

    def test_stats_counts_orders_paid_today(self):
        self.db.set_points_cost("A", 1)
        self.give_points(1, 2)
        self.db.redeem_with_points(1, "A", now=T0 - 86400)  # 昨天的訂單
        self.db.redeem_with_points(1, "A", now=T0)
        stats = self.db.stats(now=T0 + 60)
        self.assertEqual((stats["orders_paid"], stats["orders_paid_today"]), (2, 1))

    def test_recent_paid_orders(self):
        self.db.set_points_cost("A", 1)
        self.give_points(1, 1)
        self.give_points(2, 1)
        first = self.db.redeem_with_points(1, "A", now=T0)
        second = self.db.redeem_with_points(2, "A", now=T0 + 60)
        recent = self.db.recent_paid_orders()
        self.assertEqual([o["order_no"] for o in recent], [second["order_no"], first["order_no"]])
        self.assertEqual((recent[0]["full_name"], recent[0]["product_name"]), ("小華", "商品A"))


class UpgradeTest(unittest.TestCase):
    def test_database_from_earlier_version_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            old = sqlite3.connect(path)
            old.executescript(
                """
                CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
                    points INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
                CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL, product_code TEXT NOT NULL, pay_method TEXT NOT NULL,
                    points_spent INTEGER NOT NULL DEFAULT 0, amount_units INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, txid TEXT, created_at INTEGER NOT NULL, expires_at INTEGER,
                    paid_at INTEGER);
                INSERT INTO users VALUES (1, 'ming', '小明', 7, 100, 100);
                """
            )
            old.commit()
            old.close()

            db = DB(path, TZ)
            self.assertEqual(db.get_points(1), 7)  # 原有資料還在
            self.assertEqual(db.get_panel(1), (None, ""))
            db.set_panel(1, 55, "menu")
            db.set_price("B", 1_000_000)
            order, _ = db.create_usdt_order(1, "B", SHOP_ADDR, HALF_HOUR)
            self.assertEqual(order["pay_address"], SHOP_ADDR)
            db.close()

            again = DB(path, TZ)  # 再開一次不會重複加欄位，也不會出錯
            self.assertEqual(again.get_panel(1), (55, "menu"))
            again.close()


class ExportTest(DBTestCase):
    def test_export_uses_readable_labels_and_local_time(self):
        self.db.set_points_cost("A", 1)
        self.db.checkin(1, now=T0)
        order = self.db.redeem_with_points(1, "A", now=T0)
        title, headers, rows = self.db.export("orders")
        row = dict(zip(headers, rows[0]))
        self.assertEqual(title, "訂單")
        self.assertEqual(row["訂單編號"], order["order_no"])
        self.assertEqual((row["商品"], row["付款方式"], row["狀態"]), ("商品A", "積分兌換", "已完成"))
        self.assertEqual(row["建立時間"], "2026-10-07 12:00:00")
        self.assertEqual(row["應付USDT"], "")

        _, headers, rows = self.db.export("points_ledger")
        self.assertEqual([dict(zip(headers, r))["原因"] for r in rows], ["簽到", "兌換商品"])
        for name in db_module.EXPORTS:
            _, headers, rows = self.db.export(name)
            for r in rows:
                self.assertEqual(len(r), len(headers))


if __name__ == "__main__":
    unittest.main()
