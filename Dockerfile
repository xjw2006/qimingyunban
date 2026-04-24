FROM python:3.11-slim

WORKDIR /app

COPY 应用/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY 应用/ .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]