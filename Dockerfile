FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt fastapi uvicorn httpx python-multipart websockets

COPY . .

RUN mkdir -p /app/outputs

EXPOSE 8080 8502