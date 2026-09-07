# Официальный образ Playwright: Python + Chromium уже установлены
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# Состояние (сессия Telegram, лиды, processed/archived) хранится в томе /app/data
ENV DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

USER root
# xvfb: 2ГИС показывает captcha на headless-браузере, поэтому запускаем
# headful-браузер внутри виртуального дисплея
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Непрерывный режим: каждый цикл — следующий город РФ, пачка в 50 лидов
CMD ["xvfb-run", "-a", "python", "main.py", "--loop", "--rotate-cities", "--every-hours", "6"]
