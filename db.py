"""SQLite 資料表與所有資料操作（積分、簽到、訂單、入帳對帳）。

兩個原則：
- 金額一律用整數儲存（USDT ×10^6），避免小數誤差。
- 時間一律存 Unix 秒數，顯示或匯出時才轉成當地時間。
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

UNIT = 10**6  # 1 USDT
OFFSET_STEP = 100  # 識別尾數的最小單位：0.0001 USDT
MAX_OFFSET = 999  # 識別尾數最多加到 0.0999 USDT
RESERVE_GRACE = 600  # 訂單到期後再監看 10 分鐘，這段時間金額也不會分配給別人
STARTUP_GRACE = 86400  # 機器人重啟後第一次查帳，往回補查 24 小時內的訂單
CLOCK_SKEW = 60  # 容許主機時間與鏈上時間的誤差（秒）
MAX_PRICE = 1_000_000
MAX_POINTS = 1_000_000

STATUS_TEXT = {"pending": "待付款", "paid": "已完成", "expired": "已逾期", "cancelled": "已取消"}
METHOD_TEXT = {"points": "積分兌換", "usdt": "USDT"}
REASON_TEXT = {"checkin": "簽到", "redeem": "兌換商品"}

SCHEMA = """
-- 用戶與目前積分餘額
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,           -- Telegram 用戶 ID
    username   TEXT,
    full_name  TEXT,
    points     INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    panel_id   INTEGER,                       -- 對話中「機器人唯一那一則訊息」的編號
    panel_tag  TEXT                           -- 那一則訊息目前顯示的是什麼（例如 pay:訂單編號）
);

-- 簽到紀錄：同一人同一天只會有一筆
CREATE TABLE IF NOT EXISTS checkins (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    checkin_date TEXT    NOT NULL,            -- YYYY-MM-DD（當地日期）
    points       INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE (user_id, checkin_date)
);

-- 積分異動明細：每一次加分、扣分都留一筆
CREATE TABLE IF NOT EXISTS points_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(user_id),
    change        INTEGER NOT NULL,           -- 正數加分、負數扣分
    balance_after INTEGER NOT NULL,
    reason        TEXT    NOT NULL,           -- checkin / redeem
    ref           TEXT,                       -- 簽到日期或訂單編號
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_user ON points_ledger(user_id, id);

-- 商品設定（業主在後台修改）
CREATE TABLE IF NOT EXISTS products (
    code        TEXT PRIMARY KEY,             -- A / B
    name        TEXT    NOT NULL,
    points_cost INTEGER NOT NULL DEFAULT 0,   -- 兌換所需積分，0＝不開放積分兌換
    price_units INTEGER NOT NULL DEFAULT 0,   -- USDT 價格 ×10^6，0＝不開放 USDT 付款
    updated_at  INTEGER NOT NULL
);

-- 訂單
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no     TEXT    NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    product_code TEXT    NOT NULL REFERENCES products(code),
    pay_method   TEXT    NOT NULL CHECK (pay_method IN ('points', 'usdt')),
    points_spent INTEGER NOT NULL DEFAULT 0,
    amount_units INTEGER NOT NULL DEFAULT 0,  -- 應付 USDT ×10^6（已含識別尾數）
    status       TEXT    NOT NULL CHECK (status IN ('pending', 'paid', 'expired', 'cancelled')),
    txid         TEXT,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    paid_at      INTEGER,
    pay_address  TEXT                         -- 下單當下的收款地址；業主之後改地址，這張單仍認原地址
);
CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id, id);
-- 同一時間不會有兩張待付款訂單是相同金額，這是「用金額認訂單」的根本保證
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_pending_amount
    ON orders(amount_units) WHERE status = 'pending';

-- 鏈上入帳紀錄：每筆交易只處理一次；order_no 為空＝沒有對應到訂單
CREATE TABLE IF NOT EXISTS payments (
    txid         TEXT PRIMARY KEY,
    from_addr    TEXT    NOT NULL,
    to_addr      TEXT    NOT NULL,
    amount_units INTEGER NOT NULL,
    block_ts     INTEGER NOT NULL,
    order_no     TEXT,
    created_at   INTEGER NOT NULL
);

-- 其他設定：welcome_text 主選單訊息、channel_url 頻道連結、usdt_address 收款地址、
-- checkin_points 每日簽到積分、amount_seq 識別尾數序號
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class InsufficientPoints(Exception):
    """積分不足。"""


class NotAvailable(Exception):
    """商品尚未開放這種付款方式。"""


class NoAmountSlot(Exception):
    """同時間待付款訂單太多，暫時分配不出不重複的金額。"""


class PriceChanged(Exception):
    """用戶按下確認時，所需積分已經和他看到的不一樣了。"""


def parse_price(text: str) -> int:
    """把業主輸入的價格轉成整數單位，例如 '9.9' -> 9900000。格式不對會丟 ValueError。"""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError("請輸入數字，例如 9.9") from None
    if not value.is_finite():
        raise ValueError("請輸入數字，例如 9.9")
    if value < 0:
        raise ValueError("價格不可為負數")
    if value > MAX_PRICE:  # 先擋掉超大的數字，後面檢查小數位數時才不會算到溢位
        raise ValueError(f"價格不可超過 {MAX_PRICE}")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError("價格最多 2 位小數")
    return int(value * UNIT)


def fmt_usdt(units: int, places: int = 2) -> str:
    """整數單位轉成顯示用字串。價格用 2 位小數，應付金額用 4 位。"""
    return f"{Decimal(units) / UNIT:.{places}f}"


class DB:
    def __init__(self, path: str, tz: ZoneInfo):
        self.tz = tz
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        # 舊版資料庫升級：補上後來才新增的欄位（已有的資料不會動到）
        self._ensure_column("users", "panel_id", "INTEGER")
        self._ensure_column("users", "panel_tag", "TEXT")
        self._ensure_column("orders", "pay_address", "TEXT")
        now = int(time.time())
        # 商品預設「未開放」，一定要業主在後台設定價格後才能賣，避免用錯誤的預設價成交
        self.conn.executemany(
            "INSERT OR IGNORE INTO products (code, name, points_cost, price_units, updated_at)"
            " VALUES (?, ?, 0, 0, ?)",
            [("A", "商品A", now), ("B", "商品B", now)],
        )
        self.conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('checkin_points', '1')")

    def close(self) -> None:
        self.conn.close()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def tx(self):
        """交易區塊：中途出錯就整個復原，不會出現「扣了積分卻沒有訂單」這種半套狀態。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------- 時間 ----------

    def today(self, now: int | None = None) -> str:
        return datetime.fromtimestamp(now or int(time.time()), self.tz).date().isoformat()

    def fmt_time(self, ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime(fmt)

    # ---------- 設定 ----------

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def checkin_points(self) -> int:
        return int(self.get_setting("checkin_points", "1"))

    def welcome_text(self) -> str:
        return self.get_setting("welcome_text") or "測試中"

    def channel_url(self) -> str:
        return self.get_setting("channel_url")

    def usdt_address(self) -> str:
        """目前的收款地址；空字串＝尚未設定，USDT 付款不開放。"""
        return self.get_setting("usdt_address")

    # ---------- 商品 ----------

    def get_product(self, code: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM products WHERE code = ?", (code,)).fetchone()

    def set_product_name(self, code: str, name: str) -> None:
        self.conn.execute(
            "UPDATE products SET name = ?, updated_at = ? WHERE code = ?", (name, int(time.time()), code)
        )

    def set_points_cost(self, code: str, points: int) -> None:
        self.conn.execute(
            "UPDATE products SET points_cost = ?, updated_at = ? WHERE code = ?",
            (points, int(time.time()), code),
        )

    def set_price(self, code: str, price_units: int) -> None:
        self.conn.execute(
            "UPDATE products SET price_units = ?, updated_at = ? WHERE code = ?",
            (price_units, int(time.time()), code),
        )

    # ---------- 用戶與積分 ----------

    def upsert_user(self, user_id: int, username: str | None, full_name: str | None) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO users (user_id, username, full_name, points, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,"
            " full_name = excluded.full_name, updated_at = excluded.updated_at",
            (user_id, username, full_name, now, now),
        )

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def get_points(self, user_id: int) -> int:
        row = self.get_user(user_id)
        return row["points"] if row else 0

    def get_panel(self, user_id: int) -> tuple[int | None, str]:
        """對話中機器人那一則訊息的（編號, 目前顯示的畫面）。"""
        row = self.get_user(user_id)
        return (row["panel_id"], row["panel_tag"] or "") if row else (None, "")

    def set_panel(self, user_id: int, panel_id: int | None, tag: str = "") -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO users (user_id, points, created_at, updated_at, panel_id, panel_tag)"
            " VALUES (?, 0, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET panel_id = excluded.panel_id, panel_tag = excluded.panel_tag",
            (user_id, now, now, panel_id, tag),
        )

    def _add_points(self, user_id: int, change: int, reason: str, ref: str, now: int) -> int:
        """加減積分並寫入異動明細，回傳異動後餘額。必須在 tx() 內呼叫。"""
        cur = self.conn.execute(
            "UPDATE users SET points = points + ?, updated_at = ? WHERE user_id = ? AND points + ? >= 0",
            (change, now, user_id, change),
        )
        if cur.rowcount != 1:
            raise InsufficientPoints
        balance = self.get_points(user_id)
        self.conn.execute(
            "INSERT INTO points_ledger (user_id, change, balance_after, reason, ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, change, balance, reason, ref, now),
        )
        return balance

    def checkin(self, user_id: int, now: int | None = None) -> tuple[bool, int, int]:
        """簽到。回傳（是否成功, 這次獲得的積分, 目前積分）；當天已簽過則回傳 (False, 0, 目前積分)。"""
        now = now or int(time.time())
        today = self.today(now)
        reward = self.checkin_points()
        with self.tx():
            already = self.conn.execute(
                "SELECT 1 FROM checkins WHERE user_id = ? AND checkin_date = ?", (user_id, today)
            ).fetchone()
            if already:
                return False, 0, self.get_points(user_id)
            self.conn.execute(
                "INSERT INTO checkins (user_id, checkin_date, points, created_at) VALUES (?, ?, ?, ?)",
                (user_id, today, reward, now),
            )
            balance = self._add_points(user_id, reward, "checkin", today, now)
        return True, reward, balance

    # ---------- 訂單 ----------

    def _new_order_no(self, code: str, now: int) -> str:
        day = datetime.fromtimestamp(now, self.tz).strftime("%y%m%d")
        while True:
            order_no = f"{code}{day}{secrets.randbelow(10**6):06d}"
            if not self.conn.execute("SELECT 1 FROM orders WHERE order_no = ?", (order_no,)).fetchone():
                return order_no

    def get_order(self, order_no: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()

    def list_orders(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT o.*, p.name AS product_name FROM orders o JOIN products p ON p.code = o.product_code"
            " WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    def redeem_with_points(
        self, user_id: int, code: str, expected_cost: int | None = None, now: int | None = None
    ) -> sqlite3.Row:
        """用積分兌換：扣積分與建立訂單在同一個交易內完成。

        expected_cost 是用戶在確認畫面上看到的積分數；若業主剛好在這中間改了設定，
        就不扣款、請用戶重新確認，避免被扣到沒同意過的數字。
        """
        now = now or int(time.time())
        with self.tx():
            cost = self.get_product(code)["points_cost"]
            if cost <= 0:
                raise NotAvailable
            if expected_cost is not None and cost != expected_cost:
                raise PriceChanged
            order_no = self._new_order_no(code, now)
            self._add_points(user_id, -cost, "redeem", order_no, now)
            self.conn.execute(
                "INSERT INTO orders (order_no, user_id, product_code, pay_method, points_spent,"
                " status, created_at, paid_at) VALUES (?, ?, ?, 'points', ?, 'paid', ?, ?)",
                (order_no, user_id, code, cost, now, now),
            )
        return self.get_order(order_no)

    def _alloc_amount(self, base_units: int, now: int) -> int:
        """在價格後面加一個很小的「識別尾數」，讓每張待付款訂單的應付金額都不一樣。

        尾數用輪流的方式分配（不是每次都從最小的開始），所以同一個金額要隔很久才會再出現，
        就算有人逾期後才付款，也不會被誤認成別人的新訂單。
        """
        reserved = {
            row[0]
            for row in self.conn.execute(
                "SELECT amount_units FROM orders WHERE pay_method = 'usdt'"
                " AND (status = 'pending' OR expires_at > ?)",
                (now - RESERVE_GRACE,),
            )
        }
        seq = int(self.get_setting("amount_seq", "0"))
        for _ in range(MAX_OFFSET):
            seq = seq % MAX_OFFSET + 1
            amount = base_units + seq * OFFSET_STEP
            if amount not in reserved:
                self.set_setting("amount_seq", str(seq))
                return amount
        raise NoAmountSlot

    def create_usdt_order(
        self, user_id: int, code: str, address: str, expire_seconds: int, now: int | None = None
    ) -> tuple[sqlite3.Row, bool]:
        """建立 USDT 待付款訂單，回傳（訂單, 是否為新建）。

        同一人同一商品若已有未到期的待付款訂單，直接回傳那一張，不重複開單。
        address 是下單當下的收款地址，會記在訂單上：之後業主就算改了地址，
        這張訂單仍然只認當時給客人看的那個地址。
        """
        now = now or int(time.time())
        with self.tx():
            existing = self.conn.execute(
                "SELECT * FROM orders WHERE user_id = ? AND product_code = ? AND pay_method = 'usdt'"
                " AND status = 'pending' AND expires_at > ? ORDER BY id DESC LIMIT 1",
                (user_id, code, now),
            ).fetchone()
            if existing:
                return existing, False
            price = self.get_product(code)["price_units"]
            if price <= 0 or not address:
                raise NotAvailable
            amount = self._alloc_amount(price, now)
            order_no = self._new_order_no(code, now)
            self.conn.execute(
                "INSERT INTO orders (order_no, user_id, product_code, pay_method, amount_units,"
                " status, created_at, expires_at, pay_address) VALUES (?, ?, ?, 'usdt', ?, 'pending', ?, ?, ?)",
                (order_no, user_id, code, amount, now, now + expire_seconds, address),
            )
        return self.get_order(order_no), True

    def cancel_order(self, order_no: str, user_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE order_no = ? AND user_id = ? AND status = 'pending'",
            (order_no, user_id),
        )
        return cur.rowcount == 1

    def expire_orders(self, now: int | None = None) -> list[sqlite3.Row]:
        """把過了付款期限的待付款訂單改成已逾期，回傳這次被改的訂單。"""
        now = now or int(time.time())
        with self.tx():
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE status = 'pending' AND expires_at <= ?", (now,)
            ).fetchall()
            self.conn.execute(
                "UPDATE orders SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?", (now,)
            )
        return rows

    # ---------- 入帳對帳 ----------

    def watch_targets(self, now: int | None = None, grace: int = RESERVE_GRACE) -> list[tuple[str, int]]:
        """需要查帳的（收款地址, 起始時間）清單；沒有訂單要監看就回傳空清單。

        起始時間是該地址最早一張仍在監看中的訂單的建立時間。
        通常只有一個地址；業主剛改過地址時，舊地址還有訂單在等付款，就會同時查兩個。
        """
        now = now or int(time.time())
        rows = self.conn.execute(
            "SELECT pay_address, MIN(created_at) FROM orders"
            " WHERE pay_method = 'usdt' AND pay_address IS NOT NULL"
            " AND (status = 'pending' OR (status IN ('expired', 'cancelled') AND expires_at > ?))"
            " GROUP BY pay_address ORDER BY pay_address",
            (now - grace,),
        ).fetchall()
        return [(row[0], row[1] - CLOCK_SKEW) for row in rows]

    def record_transfer(
        self,
        txid: str,
        from_addr: str,
        to_addr: str,
        amount_units: int,
        block_ts: int,
        now: int | None = None,
    ) -> sqlite3.Row | None:
        """記錄一筆鏈上入帳並嘗試對應訂單。

        回傳「這次剛被標記為已付款」的訂單；交易已處理過、或對不到訂單時回傳 None。
        對應條件：收款地址是訂單當時指定的地址、金額完全相同，
        且鏈上時間落在訂單的建立時間與付款期限之間。
        就算訂單狀態已被改成逾期或被用戶取消，只要錢確實在期限內進來，仍然算付款成功，
        不讓「錢收了卻沒有訂單」的情況發生。
        """
        now = now or int(time.time())
        with self.tx():
            if self.conn.execute("SELECT 1 FROM payments WHERE txid = ?", (txid,)).fetchone():
                return None
            order = self.conn.execute(
                "SELECT * FROM orders WHERE pay_method = 'usdt' AND pay_address = ? AND amount_units = ?"
                " AND status != 'paid' AND created_at <= ? AND expires_at >= ?"
                " ORDER BY id DESC LIMIT 1",
                (to_addr, amount_units, block_ts + CLOCK_SKEW, block_ts),
            ).fetchone()
            order_no = order["order_no"] if order else None
            if order:
                self.conn.execute(
                    "UPDATE orders SET status = 'paid', txid = ?, paid_at = ? WHERE id = ?",
                    (txid, now, order["id"]),
                )
            self.conn.execute(
                "INSERT INTO payments (txid, from_addr, to_addr, amount_units, block_ts, order_no, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (txid, from_addr, to_addr, amount_units, block_ts, order_no, now),
            )
        return self.get_order(order_no) if order_no else None

    # ---------- 後台 ----------

    def recent_paid_orders(self, limit: int = 10) -> list[sqlite3.Row]:
        """所有用戶最近完成的訂單（給業主看）。"""
        return self.conn.execute(
            "SELECT o.*, p.name AS product_name, u.full_name, u.username FROM orders o"
            " JOIN products p ON p.code = o.product_code JOIN users u ON u.user_id = o.user_id"
            " WHERE o.status = 'paid' ORDER BY o.paid_at DESC, o.id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def stats(self, now: int | None = None) -> dict[str, int]:
        def one(sql: str, *args) -> int:
            return self.conn.execute(sql, args).fetchone()[0]

        now = now or int(time.time())
        local = datetime.fromtimestamp(now, self.tz)
        day_start = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "checkins_today": one("SELECT COUNT(*) FROM checkins WHERE checkin_date = ?", self.today(now)),
            "orders_paid": one("SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
            "orders_paid_today": one("SELECT COUNT(*) FROM orders WHERE status = 'paid' AND paid_at >= ?", day_start),
            "orders_pending": one("SELECT COUNT(*) FROM orders WHERE status = 'pending'"),
        }

    def export(self, name: str) -> tuple[str, list[str], list[list]]:
        """匯出一張表單，回傳（表單名稱, 欄位名稱, 資料列）。時間與代碼都轉成看得懂的中文。"""
        title, columns, sql = EXPORTS[name]
        rows = [
            [convert(self, value) if convert else value for (_, convert), value in zip(columns, row)]
            for row in self.conn.execute(sql)
        ]
        return title, [header for header, _ in columns], rows


def _time(db: DB, value: int | None) -> str:
    return db.fmt_time(value, "%Y-%m-%d %H:%M:%S") if value else ""


def _usdt(db: DB, value: int | None) -> str:
    return fmt_usdt(value, 4) if value else ""


def _label(mapping: dict[str, str]):
    return lambda db, value: mapping.get(value, value)


# 後台可匯出的表單：名稱、（欄位名稱, 轉換方式）、查詢語法
EXPORTS: dict[str, tuple[str, list[tuple], str]] = {
    "users": (
        "用戶積分",
        [("用戶ID", None), ("帳號", None), ("名稱", None), ("目前積分", None), ("加入時間", _time)],
        "SELECT user_id, username, full_name, points, created_at FROM users ORDER BY created_at, user_id",
    ),
    "checkins": (
        "簽到紀錄",
        [("編號", None), ("用戶ID", None), ("帳號", None), ("名稱", None), ("簽到日期", None),
         ("獲得積分", None), ("簽到時間", _time)],
        "SELECT c.id, c.user_id, u.username, u.full_name, c.checkin_date, c.points, c.created_at"
        " FROM checkins c JOIN users u ON u.user_id = c.user_id ORDER BY c.id",
    ),
    "points_ledger": (
        "積分異動",
        [("編號", None), ("用戶ID", None), ("帳號", None), ("名稱", None), ("異動積分", None),
         ("異動後餘額", None), ("原因", _label(REASON_TEXT)), ("關聯", None), ("時間", _time)],
        "SELECT l.id, l.user_id, u.username, u.full_name, l.change, l.balance_after, l.reason, l.ref,"
        " l.created_at FROM points_ledger l JOIN users u ON u.user_id = l.user_id ORDER BY l.id",
    ),
    "orders": (
        "訂單",
        [("訂單編號", None), ("用戶ID", None), ("帳號", None), ("名稱", None), ("商品", None),
         ("付款方式", _label(METHOD_TEXT)), ("使用積分", None), ("應付USDT", _usdt),
         ("狀態", _label(STATUS_TEXT)), ("收款地址", None), ("鏈上交易序號", None), ("建立時間", _time),
         ("付款期限", _time), ("完成時間", _time)],
        "SELECT o.order_no, o.user_id, u.username, u.full_name, p.name, o.pay_method, o.points_spent,"
        " o.amount_units, o.status, o.pay_address, o.txid, o.created_at, o.expires_at, o.paid_at"
        " FROM orders o JOIN users u ON u.user_id = o.user_id JOIN products p ON p.code = o.product_code"
        " ORDER BY o.id",
    ),
    "payments": (
        "USDT入帳紀錄",
        [("鏈上交易序號", None), ("付款地址", None), ("收款地址", None), ("金額USDT", _usdt),
         ("鏈上時間", _time), ("對應訂單", None), ("記錄時間", _time)],
        "SELECT txid, from_addr, to_addr, amount_units, block_ts, order_no, created_at"
        " FROM payments ORDER BY block_ts, txid",
    ),
}
