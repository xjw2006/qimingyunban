FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r 应用/requirements.txt

CMD uvicorn 应用.main:app --host 0.0.0.0 --port $PORT
