FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirement.txt ./
RUN pip install --no-cache-dir -r requirement.txt

COPY alembic.ini main.py ./
COPY migrations ./migrations
COPY app ./app

EXPOSE 8000

# SQLite 只适合单进程写入，生产演示环境固定使用一个 Uvicorn 进程。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
