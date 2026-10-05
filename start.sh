#!/bin/sh

echo "Starting Xray..."

/usr/local/bin/xray/xray run \
    -config /app/xray/config.json &

XRAY_PID=$!

echo "Xray started with PID $XRAY_PID"

echo "Starting Flask..."

gunicorn \
    --bind 127.0.0.1:5000 \
    --workers 1 \
    app:app &

FLASK_PID=$!

echo "Flask started with PID $FLASK_PID"

echo "Preparing Nginx configuration..."

sed "s/\${PORT}/${PORT:-8080}/g" \
    /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf

echo "Starting Nginx..."

exec nginx -g "daemon off;"
