"""風險規則檢查：會進版本控管的檔案裡不可以出現機密。

這不是功能測試，所以放在 meta-tests/，不和 tests/ 混在一起。
執行方式：.venv/bin/python -m unittest discover -s meta-tests
（deploy.sh 每次部署前也會自動跑一次。）
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 名稱 -> 樣式。只要會被提交的檔案裡出現符合的內容就算違規。
PATTERNS = {
    "Telegram 機器人金鑰": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "私鑰區塊": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "已填值的金鑰設定": re.compile(r"^(BOT_TOKEN|TRONGRID_API_KEY)=\S+", re.M),
}
# 這些設定檔會放真正的金鑰，必須被版本控管忽略
MUST_BE_IGNORED = [".env", "server.env"]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def files_that_would_be_committed() -> list[Path]:
    """已追蹤的檔案，加上還沒追蹤但也沒被忽略的檔案（也就是 git add 之後會進去的全部）。"""
    result = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [ROOT / name for name in result.stdout.split("\0") if name]


@unittest.skipUnless((ROOT / ".git").exists(), "不在版本控管倉庫內（例如部署後的映像），略過")
class NoSecretsInRepoTest(unittest.TestCase):
    def test_files_that_would_be_committed_contain_no_secrets(self):
        files = files_that_would_be_committed()
        self.assertGreater(len(files), 5, "沒有掃到檔案，檢查本身可能壞了")
        found = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(text):
                    found.append(f"{path.relative_to(ROOT)}：{label}")
        self.assertEqual(found, [], "以下檔案含有機密，不可提交")

    def test_files_holding_real_secrets_are_ignored(self):
        for name in MUST_BE_IGNORED:
            with self.subTest(name=name):
                self.assertEqual(git("check-ignore", "-q", name).returncode, 0, f"{name} 沒有被版本控管忽略")

    def test_the_scanner_really_detects_a_token(self):
        # 在執行時才組出一段假金鑰，確認樣式抓得到（直接寫在檔案裡會被自己掃到）
        fake = "12345678" + ":" + "A" * 35
        self.assertRegex(f'TOKEN = "{fake}"', PATTERNS["Telegram 機器人金鑰"])
        self.assertRegex("BOT_TOKEN=" + fake, PATTERNS["已填值的金鑰設定"])
        self.assertNotRegex("BOT_TOKEN=\nOWNER_IDS=1", PATTERNS["已填值的金鑰設定"])
        self.assertNotRegex('bot_token="1:TEST"', PATTERNS["Telegram 機器人金鑰"])


if __name__ == "__main__":
    unittest.main()
