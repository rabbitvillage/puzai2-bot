# HANDOFF『交接文件』

## Current Task『目前任務』

舖仔2號案：Telegram 機器人 `@vita4th_bot`（簽到積分＋商品A積分兌換／USDT＋商品B僅 TRC-20 USDT＋業主專用頁面）。
程式已完成、已部署到荷蘭主機，並推上 GitHub 私有儲存庫 `rabbitvillage/puzai2-bot`。等待實機驗收；USDT 付款問題使用者指示先不處理。

## Current Scope『目前範圍』

- In scope『範圍內』：
  - 專案資料夾內的機器人程式、測試、文件
  - 荷蘭主機 `puzai-nl` 上的 `puzai2-bot` 容器、映像與 `/root/puzai2-bot` 資料夾
  - GitHub 兔子村帳號的私有儲存庫 `rabbitvillage/puzai2-bot`
- Out of scope『範圍外，不可動』：
  - 荷蘭主機上的 `gouer-rust` 三個容器與 `/root/gouer-*` 資料夾（另一隻機器人 `@idztbot`）
  - 主機的系統設定與系統套件

## Completed『已完成』

- 五顆按鈕主選單、每日簽到、積分帳、商品A（積分或 USDT）、商品B（僅 USDT）、查詢訂單
- 對話只留機器人的一則訊息：操作時就地修改、用戶訊息看完就刪、主動通知換成新的一則
- 業主專用頁面 `/set`：只認業主 ID；可設定主選單訊息、頻道連結、商品名稱、積分、價格、每日簽到積分、USDT 收款地址；最近訂單；匯出 Excel『試算表』
- USDT 入帳偵測（唯讀查鏈、用「金額尾數」認訂單、每張訂單記住當時的收款地址）
- 118 個自動測試與 3 項機密風險檢查；一鍵部署腳本（部署前自動備份資料庫、保留上一版紀錄）；唯讀查詢工具
- 已部署並運行；業主目前設為開發者本人
- 實機已確認：`/set` 設定收款地址與價格、開出 USDT 訂單、畫面全程是同一則訊息（2026-10-07 06:54–06:56）
- 實機已確認：`/set` 修改主選單訊息、商品名稱、積分、價格（2026-10-07 07:43–07:44）；連續運行約 27 小時無程式錯誤
- 機器人金鑰已移出程式，只放在不進版本控管的 `server.env`（2026-10-08）
- 尚未確認：真實 USDT 入帳的自動對帳與通知（唯一一張真實訂單到期未付；另有一筆金額不符的入帳列為未對應）

## Current Blocker Or Open Question『目前卡住的事』

- 沒有程式上的阻礙。待使用者在 Telegram 實機驗收（見 Next Step）。
- USDT 付款：使用者於 2026-10-08 表示已測試過，此問題先不處理。
- 待確認的設計假設（詳見 FR）：商品A是「積分或 USDT 二選一」；每日簽到預設 1 積分；不自動發貨；業主的新訂單通知也只留一則。

## Next Step『下一步』

1. 在 `/set` 試其餘項目：頻道連結、匯出表單（在手機上開啟檔案）。
2. 確認設計假設（見 FR）。
3. 上線前：到 @BotFather 換發新的機器人金鑰並更新 `server.env`；確認正式業主 ID。

## Active FR『進行中的功能修訂紀錄』

- [FR-20261007-tg-shop-bot](Docs/FR/ACTIVE.md) - 已上線，等待實機驗收

## Important Files『重要檔案』

- `bot.py` - 主程式；所有畫面都經過 `render`『單一訊息出口』
- `db.py` - 資料表與帳務邏輯；付款判定規則在 `record_transfer`『記錄入帳並對應訂單』
- `tron.py` - 唯讀查詢鏈上入帳
- `config.py` - 讀取部署層級設定（金鑰必須由設定檔提供）
- `server.env` - 荷蘭主機的設定，**含機器人金鑰**（不進版本控管，換電腦時要另外帶過去）；`.env.example` - 各項設定說明
- `meta-tests/risk/` - 風險規則檢查：會進版本控管的檔案不可含機密
- `deploy.sh` - 一鍵部署；`query.py` - 唯讀查詢
- [Docs/custom-queries/README.md](Docs/custom-queries/README.md) - 想查什麼就用哪個指令

## Safe Commands『安全指令』

```bash
# 跑全部測試（不連網）
.venv/bin/python -m unittest

# 風險檢查：會進版本控管的檔案不可含機密
.venv/bin/python -m unittest discover -s meta-tests

# 部署到荷蘭主機：測試 → 建置 → 主機上再測一次 → 備份資料庫 → 通過才換上線
./deploy.sh

# 看主機上機器人的狀態與最近紀錄
ssh puzai-nl docker ps --filter name=puzai2-bot
ssh puzai-nl docker logs --tail 50 puzai2-bot
# 紀錄裡出現「開始監看…入帳」＝有訂單在等付款、正在查鏈；「暫停查鏈」＝目前沒有訂單在等

# 上一次部署之前那個容器的紀錄（每次部署會覆蓋）
ssh puzai-nl tail -50 /root/puzai2-bot/logs/previous.log

# 列出所有唯讀查詢
ssh puzai-nl docker exec puzai2-bot python query.py

# 推送到 GitHub（兔子村帳號的私有儲存庫）
git push
```

設定分兩處：

- 業主自己在機器人裡用 `/set` 改：主選單訊息、頻道連結、商品名稱、積分、價格、每日簽到積分、USDT 收款地址。
- 改 `server.env` 後執行 `./deploy.sh`：業主名單、頻道按鈕文字、查詢金鑰、付款期限、查帳間隔、時區。

## Traps To Avoid『要避開的坑』

- 主機上的容器在跑時，**不要在本機執行 `python bot.py`**：同一把金鑰同時只能有一個程式收訊息，兩邊會互搶。
- 不要直接在主機上改 `/root/puzai2-bot/.env`：下次部署會被 `server.env` 覆蓋。
- 機器人金鑰只能放在 `server.env`／`.env`（不進版本控管），不要再寫進程式；`deploy.sh` 與 `meta-tests` 會擋。
- 這個專案的提交者身分是靠 `.git/config` 裡引用 `~/.gitconfig-rabbitvillage` 才變成兔子村帳號。重新複製專案後要再設一次，否則提交會掛在另一個帳號名下（全域那條依遠端網址切換的規則對 GitHub 網址不會生效）。
- 提交（簽署）、推送、部署都要經 1Password『密碼管理工具』授權；視窗沒按到會看到「communication with agent failed」，重跑一次即可。
- 要送訊息給用戶一律走 `render`『單一訊息出口』，不要直接呼叫發訊息的函式，否則對話會多出第二則。
- Telegram 不允許刪除超過 48 小時的訊息；程式遇到這種舊訊息會改成一行提示，這是預期行為不是錯誤。
- 收款地址存在資料庫、由業主在 `/set` 更改；每張訂單記住下單當時的地址。不要改成「只認目前地址」，否則改地址後舊訂單會對不到帳。
- 商品預設「未開放」，要先在 `/set` 設定積分或價格（USDT 還要先設收款地址）才會出現購買按鈕。
- 不要手動改資料庫裡的積分餘額：餘額與 `points_ledger`『積分異動明細』必須一致，可用查詢「積分對帳」檢查。
- 待付款、已逾期、已取消的訂單都不可出貨；「未對應入帳」不能直接當成某張訂單的款項。
- 資料表有新增欄位時，要在 `db.py` 的 `_ensure_column`『補欄位』加一行，主機上的舊資料庫才會自動升級。
- 每次部署都會重啟機器人（約 2 秒）。需要跨重啟保留的狀態要存資料庫，不要只放記憶體；「正在等業主輸入哪一項」就是靠資料庫裡的 `panel_tag`『畫面標記』判斷。
- `deploy.sh` 裡變數後面若緊接中文或全形符號，要寫成 `${變數}`，否則本機的命令列直譯器會解析錯誤。
- 主機沒有 `rsync`、`git`、`docker compose`；部署腳本用 `tar` 串流加 `docker build`，請沿用。
- 主機上的資料庫備份在 `/root/puzai2-bot/data/backups/`，不會自動清理；要刪請先確認，不可用強制遞迴刪除。

## Last Updated『最後更新』

- 2026-10-08 10:15 Asia/Taipei
