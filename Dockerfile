# Официальный образ Playwright: Python + Chromium уже установлены
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# Состояние (сессия Telegram, лиды, processed/archived) хранится в томе /app/data
ENV DATA_DIR=/app/data \
    HEADLESS=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Непрерывный режим: цикл «сбор + рассылка» каждые 6 часов
CMD ["python", "main.py", "--loop", "--every-hours", "6"]
