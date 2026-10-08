"""測試共用的假物件：假的 Telegram 傳輸層、測試用地址、假的鏈上入帳資料。

所有測試都不會真的連到 Telegram 或波場鏈。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import time
from zoneinfo import ZoneInfo

from telegram.request import BaseRequest

import config
import tron

TZ = ZoneInfo("Asia/Taipei")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ids = itertools.count(1000)


def make_address(seed: str) -> str:
    """產生一個格式與校驗碼都正確的測試用波場地址。"""
    payload = b"\x41" + hashlib.sha256(seed.encode()).digest()[:20]
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return out


SHOP_ADDR = make_address("shop")
SENDER_ADDR = make_address("customer")

OWNER = {"id": 900, "first_name": "老闆", "username": "boss"}
OWNER2 = {"id": 901, "first_name": "店長"}
ALICE = {"id": 100, "first_name": "小明", "username": "ming"}
BOB = {"id": 200, "first_name": "小華"}


def make_cfg(**overrides) -> config.Config:
    values = dict(
        bot_token="1:TEST",
        owner_ids=frozenset({OWNER["id"]}),
        channel_button_text="頻道連結",
        usdt_contract=tron.MAINNET_USDT,
        tron_api_url="https://tron.test",
        trongrid_api_key="",
        db_path=":memory:",
        tz=TZ,
        order_expire_minutes=30,
        poll_interval_seconds=15,
    )
    values.update(overrides)
    return config.Config(**values)


def tron_item(
    txid: str,
    amount_units: int,
    ts: int,
    *,
    to: str = SHOP_ADDR,
    contract: str = tron.MAINNET_USDT,
    type_: str = "Transfer",
) -> dict:
    """一筆 TronGrid 格式的入帳紀錄（格式取自實際查詢結果）。"""
    return {
        "transaction_id": txid,
        "token_info": {"symbol": "USDT", "address": contract, "decimals": 6, "name": "Tether USD"},
        "block_timestamp": ts * 1000,
        "from": SENDER_ADDR,
        "to": to,
        "type": type_,
        "value": str(amount_units),
    }


class FakeRequest(BaseRequest):
    """攔下機器人要送給 Telegram 的每一個請求並記錄下來。

    errors 可以指定某個方法要回報的錯誤：填文字＝每次都失敗；
    填函式＝依這次請求的參數決定（回傳錯誤文字，或回傳 None 表示成功）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []  # （方法名稱, 參數）
        self.errors: dict = {}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 1.0

    async def do_request(self, url, method, request_data=None, *args, **kwargs) -> tuple[int, bytes]:
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data and request_data.contains_files:
            params["_files"] = [(f[0], f[1]) for f in request_data.multipart_data.values()]
        self.calls.append((api, params))

        error = self.errors.get(api)
        if callable(error):
            error = error(params)
        if error:
            body = {"ok": False, "error_code": 400, "description": error}
            return 400, json.dumps(body).encode()

        if api == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "測試機器人", "username": "test_bot"}
        elif api in ("sendMessage", "editMessageText", "sendDocument"):
            message_id = params.get("message_id") or next(_ids)
            params["_message_id"] = message_id  # 方便測試核對「那一則訊息」的編號
            result = {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": int(params.get("chat_id", 0)), "type": "private"},
                "text": params.get("text", ""),
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


def message_update(user: dict, text: str | None = None, **content) -> dict:
    """模擬用戶傳一則訊息給機器人：文字、指令，或其他內容（例如 location=...）。"""
    message = {
        "message_id": next(_ids),
        "date": int(time.time()),
        "chat": {"id": user["id"], "type": "private"},
        "from": {"is_bot": False, **user},
        **content,
    }
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": next(_ids), "message": message}


def callback_update(user: dict, data: str, message_id: int = 500, *, accessible: bool = True) -> dict:
    """模擬用戶按下某則訊息上的按鈕。accessible=False 代表那則訊息太舊，Telegram 不再附上內容。"""
    message = {"message_id": message_id, "date": 0, "chat": {"id": user["id"], "type": "private"}}
    if accessible:
        message.update(date=int(time.time()), text="測試中")
    return {
        "update_id": next(_ids),
        "callback_query": {
            "id": str(next(_ids)),
            "from": {"is_bot": False, **user},
            "chat_instance": "test",
            "data": data,
            "message": message,
        },
    }
