"""設定讀取測試：金鑰一定要從設定檔或環境變數來，程式本身不帶任何金鑰。"""
import os
import unittest
from unittest import mock

import config
import tron

KEYS = ["BOT_TOKEN", "OWNER_IDS", "CHANNEL_BUTTON_TEXT", "TRONGRID_API_KEY", "ORDER_EXPIRE_MINUTES",
        "POLL_INTERVAL_SECONDS", "TIMEZONE", "DB_PATH", "USDT_CONTRACT", "TRON_API_URL"]


class ConfigTest(unittest.TestCase):
    def load(self, **env: str) -> config.Config:
        clean = {k: v for k, v in os.environ.items() if k not in KEYS}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            return config.load(env_file=None)

    def test_token_is_required_and_has_no_builtin_default(self):
        with self.assertRaises(config.ConfigError) as caught:
            self.load()
        self.assertIn("BOT_TOKEN", str(caught.exception))
        self.assertFalse(hasattr(config, "DEV_BOT_TOKEN"))  # 程式裡不可以再有寫死的金鑰

    def test_defaults(self):
        cfg = self.load(BOT_TOKEN="1:TEST")
        self.assertEqual(cfg.bot_token, "1:TEST")
        self.assertEqual(cfg.owner_ids, frozenset())
        self.assertEqual(cfg.channel_button_text, "頻道連結")
        self.assertEqual((cfg.usdt_contract, cfg.tron_api_url), (tron.MAINNET_USDT, tron.MAINNET_API))
        self.assertEqual((cfg.order_expire_minutes, cfg.poll_interval_seconds), (30, 15))
        self.assertEqual((str(cfg.tz), cfg.db_path), ("Asia/Taipei", "data/bot.db"))

    def test_owner_ids(self):
        cfg = self.load(BOT_TOKEN="1:TEST", OWNER_IDS=" 111, 222，333 ,")  # 全形逗號也接受
        self.assertEqual(cfg.owner_ids, frozenset({111, 222, 333}))

    def test_bad_values_are_rejected_with_a_clear_message(self):
        bad = [
            {"OWNER_IDS": "abc"},
            {"OWNER_IDS": "@someone"},
            {"ORDER_EXPIRE_MINUTES": "soon"},
            {"ORDER_EXPIRE_MINUTES": "1"},
            {"POLL_INTERVAL_SECONDS": "0"},
            {"TIMEZONE": "Mars/Olympus"},
            {"TRON_API_URL": "http://insecure.example"},
            {"USDT_CONTRACT": "not-an-address"},
        ]
        for env in bad:
            with self.subTest(env=env), self.assertRaises(config.ConfigError):
                self.load(BOT_TOKEN="1:TEST", **env)


if __name__ == "__main__":
    unittest.main()
