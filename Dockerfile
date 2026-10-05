FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN curl -L \
    https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip \
    -o /tmp/xray.zip \
    && unzip /tmp/xray.zip -d /usr/local/bin/xray \
    && chmod +x /usr/local/bin/xray/xray \
    && rm /tmp/xray.zip

RUN rm -f /etc/nginx/sites-enabled/default

COPY nginx.conf /etc/nginx/templates/default.conf.template

RUN chmod +x /app/start.sh

ENV PYTHONUNBUFFERED=1

CMD ["/app/start.sh"]
