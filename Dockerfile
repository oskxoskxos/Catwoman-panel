FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY --from=ghcr.io/xtls/xray-core:26.9.30 /usr/local/bin/xray /usr/local/bin/xray

COPY . .

RUN chmod +x /app/start.sh

ENV PYTHONUNBUFFERED=1

CMD ["/app/start.sh"]
