#!/bin/sh

echo "Starting Xray..."

/usr/local/bin/xray/xray run -config /app/xray/config.json &

echo "Starting Flask..."

gunicorn --bind 127.0.0.1:5000 app:app &

echo "Starting Nginx..."

exec nginx -g "daemon off;"
