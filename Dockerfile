FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 用非管理員身分執行。資料庫放在 /app/data，部署時掛載主機上的資料夾，重建容器資料也不會消失。
RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir /app/data \
    && chown bot:bot /app/data

COPY bot.py config.py db.py tron.py query.py .env.example ./
COPY tests ./tests

USER bot
CMD ["python", "bot.py"]
