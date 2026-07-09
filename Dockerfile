FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Схема БД применяется при старте бота (db.init_db) либо вручную: make init-db.
CMD ["python", "-m", "app.delivery.bot"]
