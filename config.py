"""從 .env 讀取設定，並在啟動時就把明顯的錯誤擋下來。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

import tron


class ConfigError(Exception):
    """設定有誤；訊息會直接顯示給部署的人看。"""


@dataclass(frozen=True)
class Config:
    """部署層級的設定（從 .env 讀）。

    主選單訊息、商品名稱與價格、頻道連結、USDT 收款地址不在這裡，
    那些由業主在機器人的 /set 頁面設定，存在資料庫。
    """

    bot_token: str
    owner_ids: frozenset[int]  # 空集合＝尚未設定業主，沒有人能用 /set
    channel_button_text: str
    usdt_contract: str
    tron_api_url: str
    trongrid_api_key: str
    db_path: str
    tz: ZoneInfo
    order_expire_minutes: int
    poll_interval_seconds: int


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _int(name: str, default: int, minimum: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} 必須是整數，目前是「{raw}」") from None
    if value < minimum:
        raise ConfigError(f"{name} 不可小於 {minimum}")
    return value


def _owner_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ConfigError(f"OWNER_IDS 只能填 Telegram 數字 ID（用逗號分隔），目前有「{part}」")
        ids.add(int(part))
    return frozenset(ids)


def load(env_file: str | None = ".env") -> Config:
    if env_file:
        load_dotenv(env_file)

    token = _get("BOT_TOKEN")
    if not token:
        raise ConfigError("尚未設定 BOT_TOKEN。請複製 .env.example 為 .env，填入向 @BotFather 申請的金鑰。")

    usdt_contract = _get("USDT_CONTRACT", tron.MAINNET_USDT)
    if not tron.is_valid_address(usdt_contract):
        raise ConfigError("USDT_CONTRACT 不是有效的合約地址。")

    tron_api_url = _get("TRON_API_URL", tron.MAINNET_API).rstrip("/")
    if not tron_api_url.startswith("https://"):
        raise ConfigError("TRON_API_URL 必須是 https:// 開頭的網址。")

    tz_name = _get("TIMEZONE", "Asia/Taipei")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"TIMEZONE「{tz_name}」不是有效的時區名稱，例如 Asia/Taipei。") from None

    return Config(
        bot_token=token,
        owner_ids=_owner_ids(_get("OWNER_IDS")),
        channel_button_text=_get("CHANNEL_BUTTON_TEXT", "頻道連結"),
        usdt_contract=usdt_contract,
        tron_api_url=tron_api_url,
        trongrid_api_key=_get("TRONGRID_API_KEY"),
        db_path=_get("DB_PATH", "data/bot.db"),
        tz=tz,
        order_expire_minutes=_int("ORDER_EXPIRE_MINUTES", 30, 5),
        poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 15, 5),
    )
