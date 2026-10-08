"""唯讀查詢工具：只能看資料，不會改到任何資料。

用法：
    python query.py                      列出所有可用的查詢
    python query.py 今日簽到
    python query.py 用戶訂單 123456789    需要參數的查詢，參數接在名稱後面
    python query.py --sql "SELECT ..."   自訂查詢語法
    python query.py --db 路徑 今日簽到     指定資料庫檔案（預設 data/bot.db）

每個查詢的用途、怎麼解讀、什麼情況不能拿結果做決定，寫在 Docs/custom-queries/README.md。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_STATUS = (
    "CASE o.status WHEN 'pending' THEN '待付款' WHEN 'paid' THEN '已完成'"
    " WHEN 'expired' THEN '已逾期' WHEN 'cancelled' THEN '已取消' END"
)
_METHOD = "CASE o.pay_method WHEN 'points' THEN '積分兌換' ELSE 'USDT' END"
_USDT = "CASE WHEN o.amount_units > 0 THEN printf('%.4f', o.amount_units / 1000000.0) ELSE '' END"
_ORDER_COLUMNS = (
    f"o.order_no AS 訂單編號, o.user_id AS 用戶ID, u.full_name AS 名稱, p.name AS 商品,"
    f" {_METHOD} AS 付款方式, o.points_spent AS 使用積分, {_USDT} AS 應付USDT, {_STATUS} AS 狀態,"
    f" o.created_at AS 建立時間, o.expires_at AS 期限時間, o.paid_at AS 完成時間"
)
_ORDER_FROM = "FROM orders o JOIN users u ON u.user_id = o.user_id JOIN products p ON p.code = o.product_code"

# 名稱 -> （說明, 需要的參數, 查詢語法）
# 欄位名稱以「時間」結尾的會自動轉成當地時間；:today 是今天的日期，:offset 是時區差。
QUERIES: dict[str, tuple[str, list[str], str]] = {
    "商品設定": (
        "目前的商品積分與價格設定",
        [],
        "SELECT code AS 代碼, name AS 名稱, points_cost AS 所需積分,"
        " printf('%.2f', price_units / 1000000.0) AS USDT價格, updated_at AS 更新時間 FROM products ORDER BY code",
    ),
    "業主設定": (
        "業主在 /set 頁面設定的內容：主選單訊息、頻道連結、收款地址、每日簽到積分",
        [],
        "SELECT CASE key WHEN 'welcome_text' THEN '主選單訊息' WHEN 'channel_url' THEN '頻道連結'"
        " WHEN 'usdt_address' THEN 'USDT收款地址' WHEN 'checkin_points' THEN '每日簽到積分' END AS 項目,"
        " CASE WHEN value = '' THEN '（未設定）' ELSE value END AS 內容 FROM settings"
        " WHERE key IN ('welcome_text', 'channel_url', 'usdt_address', 'checkin_points') ORDER BY key",
    ),
    "最近加入用戶": (
        "最近 20 位第一次使用機器人的人（可用來查某人的 Telegram ID）",
        [],
        "SELECT user_id AS 用戶ID, username AS 帳號, full_name AS 名稱, points AS 目前積分,"
        " created_at AS 加入時間 FROM users ORDER BY created_at DESC, user_id DESC LIMIT 20",
    ),
    "積分排行": (
        "積分最多的前 20 位用戶",
        [],
        "SELECT user_id AS 用戶ID, username AS 帳號, full_name AS 名稱, points AS 目前積分,"
        " created_at AS 加入時間 FROM users ORDER BY points DESC, user_id LIMIT 20",
    ),
    "今日簽到": (
        "今天有哪些人簽到",
        [],
        "SELECT c.user_id AS 用戶ID, u.username AS 帳號, u.full_name AS 名稱, c.points AS 獲得積分,"
        " c.created_at AS 簽到時間 FROM checkins c JOIN users u ON u.user_id = c.user_id"
        " WHERE c.checkin_date = :today ORDER BY c.id",
    ),
    "每日簽到統計": (
        "最近 30 天每天的簽到人數與發出的積分",
        [],
        "SELECT checkin_date AS 日期, COUNT(*) AS 簽到人數, SUM(points) AS 發出積分"
        " FROM checkins GROUP BY checkin_date ORDER BY checkin_date DESC LIMIT 30",
    ),
    "用戶積分明細": (
        "某位用戶最近 50 筆積分加減紀錄",
        ["用戶ID"],
        "SELECT id AS 編號, change AS 異動積分, balance_after AS 異動後餘額,"
        " CASE reason WHEN 'checkin' THEN '簽到' WHEN 'redeem' THEN '兌換商品' ELSE reason END AS 原因,"
        " ref AS 關聯, created_at AS 時間 FROM points_ledger WHERE user_id = :p1 ORDER BY id DESC LIMIT 50",
    ),
    "用戶訂單": (
        "某位用戶最近 50 筆訂單",
        ["用戶ID"],
        f"SELECT {_ORDER_COLUMNS} {_ORDER_FROM} WHERE o.user_id = :p1 ORDER BY o.id DESC LIMIT 50",
    ),
    "查訂單": (
        "用訂單編號查單一訂單（含收款地址與鏈上交易序號）",
        ["訂單編號"],
        f"SELECT {_ORDER_COLUMNS}, o.pay_address AS 收款地址, o.txid AS 鏈上交易序號"
        f" {_ORDER_FROM} WHERE o.order_no = :p1",
    ),
    "待付款訂單": (
        "目前還在等待 USDT 付款的訂單",
        [],
        f"SELECT {_ORDER_COLUMNS} {_ORDER_FROM} WHERE o.status = 'pending' ORDER BY o.id",
    ),
    "已完成訂單": (
        "最近 50 筆已完成（已兌換或已付款）的訂單",
        [],
        f"SELECT {_ORDER_COLUMNS}, o.txid AS 鏈上交易序號 {_ORDER_FROM}"
        " WHERE o.status = 'paid' ORDER BY o.paid_at DESC, o.id DESC LIMIT 50",
    ),
    "未對應入帳": (
        "收款地址收到、但對不到任何訂單的 USDT 入帳（最近 50 筆）",
        [],
        "SELECT txid AS 鏈上交易序號, from_addr AS 付款地址, to_addr AS 收款地址,"
        " printf('%.4f', amount_units / 1000000.0) AS 金額USDT,"
        " block_ts AS 鏈上時間, created_at AS 記錄時間 FROM payments WHERE order_no IS NULL"
        " ORDER BY block_ts DESC LIMIT 50",
    ),
    "每日USDT實收": (
        "最近 60 筆「日期×商品」的 USDT 成交筆數與金額",
        [],
        "SELECT date(o.paid_at, 'unixepoch', :offset) AS 日期, p.name AS 商品, COUNT(*) AS 筆數,"
        " printf('%.4f', SUM(o.amount_units) / 1000000.0) AS 實收USDT"
        " FROM orders o JOIN products p ON p.code = o.product_code"
        " WHERE o.status = 'paid' AND o.pay_method = 'usdt' GROUP BY 1, 2 ORDER BY 1 DESC, 2 LIMIT 60",
    ),
    "積分對帳": (
        "檢查每位用戶的積分餘額是否等於明細加總；沒有任何資料列才是正常",
        [],
        "SELECT u.user_id AS 用戶ID, u.full_name AS 名稱, u.points AS 目前積分,"
        " COALESCE(SUM(l.change), 0) AS 明細加總 FROM users u"
        " LEFT JOIN points_ledger l ON l.user_id = u.user_id"
        " GROUP BY u.user_id HAVING u.points != COALESCE(SUM(l.change), 0)",
    ),
}


def connect(path: str) -> sqlite3.Connection:
    """以唯讀方式開啟資料庫：就算查詢語法寫錯成刪除或修改，也會被資料庫拒絕。"""
    if not Path(path).is_file():
        raise SystemExit(f"找不到資料庫檔案：{path}")
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def run(conn: sqlite3.Connection, sql: str, args: list[str], tz: ZoneInfo) -> tuple[list[str], list[list]]:
    now = datetime.now(tz)
    hours = now.utcoffset().total_seconds() / 3600
    params = {"today": now.date().isoformat(), "offset": f"{hours:+g} hours"}
    params.update({f"p{i}": value for i, value in enumerate(args, start=1)})
    cursor = conn.execute(sql, params)
    headers = [column[0] for column in cursor.description or []]
    rows = []
    for row in cursor:
        values = []
        for header, value in zip(headers, row):
            if header.endswith("時間") and isinstance(value, int):
                value = datetime.fromtimestamp(value, tz).strftime("%Y-%m-%d %H:%M:%S")
            values.append("" if value is None else value)
        rows.append(values)
    return headers, rows


def _width(text: str) -> int:
    """中文字在終端機佔兩格，對齊時要算進去。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def format_table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(value) for value in row] for row in rows]
    widths = [max([_width(h)] + [_width(row[i]) for row in cells]) for i, h in enumerate(headers)]

    def line(values: list[str]) -> str:
        return "  ".join(v + " " * (w - _width(v)) for v, w in zip(values, widths)).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in cells]
    out.append(f"（共 {len(cells)} 筆）")
    return "\n".join(out)


def usage() -> str:
    lines = ["可用的查詢：", ""]
    for name, (description, params, _) in QUERIES.items():
        label = " ".join([name] + [f"<{p}>" for p in params])
        lines.append(f"  {label + ' ' * max(1, 26 - _width(label))}{description}")
    lines += ["", "用法：python query.py 查詢名稱 [參數]", '自訂：python query.py --sql "SELECT ..."']
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    load_dotenv()
    args = list(argv)
    db_path = os.getenv("DB_PATH", "").strip() or "data/bot.db"
    if "--db" in args:
        i = args.index("--db")
        if i + 1 >= len(args):
            raise SystemExit("--db 後面要接資料庫檔案路徑")
        db_path = args[i + 1]
        del args[i : i + 2]
    if not args:
        print(usage())
        return 0

    if args[0] == "--sql":
        if len(args) < 2:
            raise SystemExit('--sql 後面要接查詢語法，例如 --sql "SELECT COUNT(*) FROM users"')
        sql, sql_args = args[1], args[2:]
    else:
        if args[0] not in QUERIES:
            raise SystemExit(f"沒有「{args[0]}」這個查詢。\n\n{usage()}")
        _, params, sql = QUERIES[args[0]]
        sql_args = args[1:]
        if len(sql_args) != len(params):
            raise SystemExit(f"「{args[0]}」需要參數：{'、'.join(params) or '（不需要參數）'}")

    tz = ZoneInfo(os.getenv("TIMEZONE", "").strip() or "Asia/Taipei")
    conn = connect(db_path)
    try:
        headers, rows = run(conn, sql, sql_args, tz)
    except sqlite3.Error as e:
        raise SystemExit(f"查詢失敗：{e}") from None
    finally:
        conn.close()
    print(format_table(headers, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
