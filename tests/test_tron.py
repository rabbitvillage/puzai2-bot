"""鏈上查詢測試：地址檢查、入帳紀錄過濾、分頁查詢。"""
import unittest

import httpx

import tron
from tests.fakes import SHOP_ADDR, make_address, tron_item


class AddressTest(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(tron.is_valid_address(tron.MAINNET_USDT))
        self.assertTrue(tron.is_valid_address(SHOP_ADDR))

    def test_invalid(self):
        typo = SHOP_ADDR[:-1] + ("a" if SHOP_ADDR[-1] != "a" else "b")  # 最後一碼打錯
        swapped = SHOP_ADDR[:10] + SHOP_ADDR[11] + SHOP_ADDR[10] + SHOP_ADDR[12:]  # 兩碼對調
        cases = ["", "T", SHOP_ADDR[:-1], SHOP_ADDR + "1", typo, "0" + SHOP_ADDR[1:],
                 "1" + SHOP_ADDR[1:], "z" * 34, SHOP_ADDR.replace(SHOP_ADDR[5], "0", 1)]
        if swapped != SHOP_ADDR:
            cases.append(swapped)
        for bad in cases:
            with self.subTest(bad=bad):
                self.assertFalse(tron.is_valid_address(bad))


class ParseTest(unittest.TestCase):
    def parse(self, *items):
        return tron.parse_transfers(list(items), SHOP_ADDR, tron.MAINNET_USDT)

    def test_good_transfer(self):
        (transfer,) = self.parse(tron_item("tx1", 19_900_100, 1_790_000_000))
        self.assertEqual(transfer.txid, "tx1")
        self.assertEqual(transfer.amount_units, 19_900_100)
        self.assertEqual(transfer.block_ts, 1_790_000_000)
        self.assertEqual(transfer.to_addr, SHOP_ADDR)

    def test_rejects_everything_that_is_not_a_real_usdt_deposit(self):
        ts = 1_790_000_000
        fake_token = tron_item("fake", 1_000_000, ts, contract=make_address("fake-usdt"))  # 同名假幣
        other_receiver = tron_item("other", 1_000_000, ts, to=make_address("someone-else"))
        approval = tron_item("approval", 1_000_000, ts, type_="Approval")  # 授權事件不是轉帳
        zero = tron_item("zero", 0, ts)  # 0 元的釣魚轉帳
        wrong_decimals = tron_item("decimals", 1_000_000, ts)
        wrong_decimals["token_info"]["decimals"] = 18
        bad_value = tron_item("badvalue", 1, ts)
        bad_value["value"] = "1.5"
        negative = tron_item("negative", 1, ts)
        negative["value"] = "-100"
        no_txid = tron_item("", 1_000_000, ts)
        no_ts = tron_item("nots", 1_000_000, ts)
        del no_ts["block_timestamp"]
        self.assertEqual(
            self.parse(fake_token, other_receiver, approval, zero, wrong_decimals, bad_value,
                       negative, no_txid, no_ts, "不是物件", None),
            [],
        )


class FetchTest(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, handler, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await tron.fetch_incoming(
                client,
                api_url="https://tron.test",
                address=SHOP_ADDR,
                contract=tron.MAINNET_USDT,
                since_ts=1_790_000_000,
                **kwargs,
            )

    async def test_follows_pagination_and_sends_expected_query(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "fingerprint" not in request.url.params:
                return httpx.Response(200, json={
                    "data": [tron_item("tx1", 100, 1_790_000_010)],
                    "success": True,
                    "meta": {"fingerprint": "NEXT", "page_size": 1},
                })
            return httpx.Response(200, json={
                "data": [tron_item("tx2", 200, 1_790_000_020)],
                "success": True,
                "meta": {"page_size": 1},
            })

        transfers = await self.fetch(handler, api_key="KEY123")
        self.assertEqual([t.txid for t in transfers], ["tx1", "tx2"])
        self.assertEqual(len(requests), 2)
        first = requests[0]
        self.assertEqual(first.url.path, f"/v1/accounts/{SHOP_ADDR}/transactions/trc20")
        params = first.url.params
        self.assertEqual(params["only_confirmed"], "true")  # 只算已確認的交易
        self.assertEqual(params["only_to"], "true")
        self.assertEqual(params["contract_address"], tron.MAINNET_USDT)
        self.assertEqual(params["min_timestamp"], "1790000000000")  # 介面用的是毫秒
        self.assertEqual(first.headers["TRON-PRO-API-KEY"], "KEY123")
        self.assertEqual(requests[1].url.params["fingerprint"], "NEXT")

    async def test_no_api_key_header_when_not_configured(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": [], "success": True, "meta": {}})

        self.assertEqual(await self.fetch(handler), [])
        self.assertNotIn("TRON-PRO-API-KEY", seen[0].headers)

    async def test_http_error_raises(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await self.fetch(lambda request: httpx.Response(429, json={"error": "too many requests"}))

    async def test_unsuccessful_body_raises(self):
        with self.assertRaises(tron.TronError):
            await self.fetch(lambda request: httpx.Response(200, json={"success": False, "error": "bad"}))


if __name__ == "__main__":
    unittest.main()
