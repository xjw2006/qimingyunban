FROM python:3.11-slim

WORKDIR /app

COPY . .

# 创建数据目录并确保可写
RUN mkdir -p /tmp

RUN pip install --no-cache-dir -r app/requirements.txt

CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT
