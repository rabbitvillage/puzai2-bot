"""唯讀查詢工具測試：每個內建查詢都能執行，而且真的改不了資料。"""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import query
from db import DB, parse_price
from tests.fakes import SHOP_ADDR, TZ


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "bot.db")
        self.db = DB(self.path, TZ)
        self.db.upsert_user(1, "ming", "小明")
        self.db.upsert_user(2, None, "小華")
        self.db.set_points_cost("A", 1)
        self.db.set_price("B", parse_price("19.9"))
        self.db.checkin(1)
        self.points_order = self.db.redeem_with_points(1, "A")
        self.db.set_setting("usdt_address", SHOP_ADDR)
        self.usdt_order, _ = self.db.create_usdt_order(2, "B", SHOP_ADDR, 1800)
        self.db.record_transfer("tx-unmatched", "TFROM", "TTO", 5_000_000, self.usdt_order["created_at"] + 5)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_query(self, name: str, *args: str) -> list[dict]:
        conn = query.connect(self.path)
        try:
            headers, rows = query.run(conn, query.QUERIES[name][2], list(args), TZ)
        finally:
            conn.close()
        return [dict(zip(headers, row)) for row in rows]

    def test_every_builtin_query_runs(self):
        for name, (_, params, _) in query.QUERIES.items():
            with self.subTest(name=name):
                self.run_query(name, *["1"] * len(params))

    def test_results_are_readable(self):
        (today,) = self.run_query("今日簽到")
        self.assertEqual((today["用戶ID"], today["名稱"], today["獲得積分"]), (1, "小明", 1))
        self.assertRegex(today["簽到時間"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")  # 時間已轉成看得懂的格式

        (pending,) = self.run_query("待付款訂單")
        self.assertEqual((pending["訂單編號"], pending["狀態"], pending["應付USDT"]),
                         (self.usdt_order["order_no"], "待付款", "19.9001"))

        (order,) = self.run_query("查訂單", self.points_order["order_no"])
        self.assertEqual((order["付款方式"], order["狀態"], order["使用積分"], order["應付USDT"]),
                         ("積分兌換", "已完成", 1, ""))

        (pending_detail,) = self.run_query("查訂單", self.usdt_order["order_no"])
        self.assertEqual(pending_detail["收款地址"], SHOP_ADDR)

        settings = {row["項目"]: row["內容"] for row in self.run_query("業主設定")}
        self.assertEqual(settings, {"USDT收款地址": SHOP_ADDR, "每日簽到積分": "1"})

        (unmatched,) = self.run_query("未對應入帳")
        self.assertEqual((unmatched["鏈上交易序號"], unmatched["金額USDT"]), ("tx-unmatched", "5.0000"))

        ledger = self.run_query("用戶積分明細", "1")
        self.assertEqual([row["原因"] for row in ledger], ["兌換商品", "簽到"])
        self.assertEqual(self.run_query("用戶訂單", "2")[0]["名稱"], "小華")

    def test_points_reconciliation_flags_only_mismatches(self):
        self.assertEqual(self.run_query("積分對帳"), [])  # 正常情況下沒有任何資料列
        self.db.conn.execute("UPDATE users SET points = 99 WHERE user_id = 2")  # 模擬有人直接改了餘額
        (bad,) = self.run_query("積分對帳")
        self.assertEqual((bad["用戶ID"], bad["目前積分"], bad["明細加總"]), (2, 99, 0))

    def test_connection_is_read_only(self):
        conn = query.connect(self.path)
        try:
            for sql in ("DELETE FROM users", "UPDATE users SET points = 999", "DROP TABLE orders",
                        "INSERT INTO settings VALUES ('x', 'y')"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.OperationalError):
                    conn.execute(sql)
        finally:
            conn.close()
        self.assertEqual(self.db.get_points(1), 0)
        self.assertIsNotNone(self.db.get_user(2))

    def test_command_line(self):
        def cli(*args: str) -> str:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                query.main(["--db", self.path, *args])
            return out.getvalue()

        self.assertIn("今日簽到", cli())  # 不帶參數會列出所有查詢
        table = cli("積分排行")
        self.assertIn("小明", table)
        self.assertIn("（共 2 筆）", table)
        self.assertIn("（共 1 筆）", cli("--sql", "SELECT COUNT(*) AS 用戶數 FROM users"))
        for bad in (["不存在的查詢"], ["用戶訂單"], ["--sql", "DELETE FROM users"], ["--sql"]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                cli(*bad)
        with self.assertRaises(SystemExit):
            query.main(["--db", str(Path(self.tmp.name) / "missing.db"), "積分排行"])


if __name__ == "__main__":
    unittest.main()
