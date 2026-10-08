#!/usr/bin/env bash
# 一鍵部署到荷蘭主機。用法：./deploy.sh
#
# 流程：本機測試 → 上傳程式並在主機上建置映像 → 上傳主機設定 → 在新映像內再跑一次測試 → 通過才更換容器。
# 只會動到主機上名為 puzai2-bot 的容器、映像，以及 /root/puzai2-bot 資料夾，不會碰主機上的其他服務。
#
# 設定：本機的 server.env 會被送上主機當作 .env（改設定＝改 server.env 後重跑本腳本）。
# 資料：資料庫放在主機的 /root/puzai2-bot/data，重新部署不會被覆蓋；
#       每次部署前會自動備份一份到 data/backups/（不會自動刪除舊備份）。
set -euo pipefail

HOST="${DEPLOY_HOST:-puzai-nl}"
NAME="puzai2-bot"
REMOTE_DIR="/root/${NAME}"
FILES=(Dockerfile requirements.txt .env.example bot.py config.py db.py tron.py query.py tests)

cd "$(dirname "$0")"

# 防呆：server.env 沒有金鑰就不部署，否則新容器會啟動失敗、機器人會整個停擺
if [ -f server.env ] && ! grep -q '^BOT_TOKEN=..*' server.env; then
  echo "server.env 裡沒有 BOT_TOKEN（機器人金鑰），已停止部署。"
  exit 1
fi

echo "==> 1/4 本機測試"
if ! out=$(.venv/bin/python -m unittest -q 2>&1); then
  echo "${out}" | tail -40
  echo "本機測試失敗，已停止部署。"
  exit 1
fi
echo "${out}" | tail -3
# 風險檢查：會進版本控管的檔案不可含金鑰等機密
if ! out=$(.venv/bin/python -m unittest discover -s meta-tests -q 2>&1); then
  echo "${out}" | tail -40
  echo "風險檢查沒過（會進版本控管的檔案裡有機密），已停止部署。"
  exit 1
fi

echo "==> 2/4 上傳程式並在主機上建置映像（主機：${HOST}）"
COPYFILE_DISABLE=1 tar --no-xattrs --exclude '__pycache__' -czf - "${FILES[@]}" |
  ssh "${HOST}" "mkdir -p '${REMOTE_DIR}/data' && docker build --quiet --label 'app=${NAME}' --tag '${NAME}:latest' -"

if [ -f server.env ]; then
  echo "==> 上傳主機設定 server.env"
  ssh "${HOST}" "cat > '${REMOTE_DIR}/.env'" < server.env
fi

echo "==> 3/4 在新映像內測試，通過才更換容器"
ssh "${HOST}" NAME="${NAME}" REMOTE_DIR="${REMOTE_DIR}" bash -s <<'REMOTE'
set -euo pipefail
cd "${REMOTE_DIR}"

if ! out=$(docker run --rm --network none "${NAME}:latest" python -m unittest -q 2>&1); then
  echo "${out}" | tail -40
  echo "映像內測試失敗，已停止部署；線上的容器沒有被更動。"
  exit 1
fi
echo "${out}" | tail -3

# 更換容器前先完整備份資料庫（新版可能會升級資料表結構，這是保險）。備份放在 data/backups/
if [ -f data/bot.db ]; then
  python3 - <<'PY'
import pathlib
import sqlite3
import time

pathlib.Path("data/backups").mkdir(exist_ok=True)
target = "data/backups/bot-" + time.strftime("%Y%m%d-%H%M%S") + ".db"
src, dst = sqlite3.connect("data/bot.db"), sqlite3.connect(target)
src.backup(dst)
dst.close()
src.close()
print("資料庫已備份：" + target)
PY
fi

# 本機沒有 server.env 時，第一次部署由範本建立一份空白設定
if [ ! -f .env ]; then
  docker run --rm --network none "${NAME}:latest" cat .env.example > .env
  echo "已由範本建立設定檔 ${REMOTE_DIR}/.env"
fi
chown -R 10002:10002 data .env
chmod 700 data
chmod 600 .env

# 容器一換掉，舊的紀錄就看不到了；先把上一版的紀錄存下來（只保留最近這一份）
mkdir -p logs
docker logs "${NAME}" > logs/previous.log 2>&1 || true

docker rm -f "${NAME}" >/dev/null 2>&1 || true
docker run -d --name "${NAME}" \
  --restart unless-stopped \
  --memory 256m \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v "${REMOTE_DIR}/data:/app/data" \
  -v "${REMOTE_DIR}/.env:/app/.env:ro" \
  "${NAME}:latest" >/dev/null
# 只清除本專案（帶有 app=puzai2-bot 標籤）已經沒在用的舊映像
docker image prune -f --filter "label=app=${NAME}" >/dev/null

echo "==> 4/4 啟動狀態"
sleep 8
docker ps --all --filter "name=^${NAME}$" --format '容器狀態：{{.Status}}'
docker logs --tail 12 "${NAME}" 2>&1
REMOTE
