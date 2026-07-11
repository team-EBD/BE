FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# 기동 전에 alembic 마이그레이션을 적용한다 (scripts/start.sh 참고).
CMD ["sh", "scripts/start.sh"]
