FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN curl -L \
    https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip \
    -o /tmp/xray.zip \
    && apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && unzip /tmp/xray.zip -d /usr/local/bin/xray \
    && chmod +x /usr/local/bin/xray/xray \
    && ln -s /usr/local/bin/xray/xray /usr/local/bin/xray-bin \
    && rm /tmp/xray.zip \
    && apt-get purge -y unzip \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN rm -f /etc/nginx/sites-enabled/default
COPY nginx.conf /etc/nginx/templates/default.conf.template

RUN chmod +x /app/start.sh

ENV PYTHONUNBUFFERED=1

CMD ["/app/start.sh"]
