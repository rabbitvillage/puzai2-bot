"""TRC-20 USDT 入帳查詢。

只「讀取」鏈上資料：不保管私鑰、不會轉出任何款項。
資料來源是 TronGrid 的公開查詢介面。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("tron")

MAINNET_API = "https://api.trongrid.io"
MAINNET_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT 在波場主網的官方合約
USDT_DECIMALS = 6
PAGE_SIZE = 200
MAX_PAGES = 20

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class TronError(Exception):
    """TronGrid 回傳異常。"""


@dataclass(frozen=True)
class Transfer:
    txid: str
    from_addr: str
    to_addr: str
    amount_units: int  # USDT ×10^6
    block_ts: int  # 鏈上時間（秒）


def is_valid_address(addr: str) -> bool:
    """檢查波場地址的格式與校驗碼，避免設定檔打錯字把錢收到別的地址。"""
    if len(addr) != 34 or not addr.startswith("T"):
        return False
    num = 0
    for ch in addr:
        idx = _B58.find(ch)
        if idx < 0:
            return False
        num = num * 58 + idx
    try:
        raw = num.to_bytes(25, "big")
    except OverflowError:
        return False
    payload, checksum = raw[:21], raw[21:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
    return payload[0] == 0x41 and digest[:4] == checksum


def parse_transfers(items: list, address: str, contract: str) -> list[Transfer]:
    """把 TronGrid 回傳的清單轉成 Transfer，並濾掉所有不該算數的紀錄。

    一定要同時符合：是轉帳事件、代幣合約是指定的 USDT（防同名假幣）、
    收款方是我們的地址、金額是正整數。
    """
    out: list[Transfer] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "Transfer":
            continue
        token = item.get("token_info") or {}
        if token.get("address") != contract or token.get("decimals") != USDT_DECIMALS:
            continue
        if item.get("to") != address:
            continue
        txid, value, ts = item.get("transaction_id"), item.get("value"), item.get("block_timestamp")
        if not isinstance(txid, str) or not txid:
            continue
        if not isinstance(value, str) or not value.isascii() or not value.isdigit():
            continue
        if not isinstance(ts, int) or int(value) <= 0:
            continue
        out.append(
            Transfer(
                txid=txid,
                from_addr=str(item.get("from") or ""),
                to_addr=address,
                amount_units=int(value),
                block_ts=ts // 1000,
            )
        )
    return out


async def fetch_incoming(
    client: httpx.AsyncClient,
    *,
    api_url: str,
    address: str,
    contract: str,
    since_ts: int,
    api_key: str = "",
) -> list[Transfer]:
    """查詢 since_ts（秒）之後、已確認的 USDT 入帳，由舊到新。"""
    url = f"{api_url}/v1/accounts/{address}/transactions/trc20"
    params: dict[str, str | int] = {
        "only_to": "true",
        "only_confirmed": "true",
        "limit": PAGE_SIZE,
        "contract_address": contract,
        "order_by": "block_timestamp,asc",
        "min_timestamp": since_ts * 1000,
    }
    headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
    out: list[Transfer] = []
    for _ in range(MAX_PAGES):
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict) or not body.get("success"):
            raise TronError(f"TronGrid 回傳失敗：{str(body)[:200]}")
        out.extend(parse_transfers(body.get("data") or [], address, contract))
        fingerprint = (body.get("meta") or {}).get("fingerprint")
        if not fingerprint:
            return out
        params["fingerprint"] = fingerprint
    log.warning("入帳筆數超過單次查詢上限（%d 筆），較新的入帳會在下一輪處理", MAX_PAGES * PAGE_SIZE)
    return out
