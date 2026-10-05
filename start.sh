#!/bin/sh

echo "Starting Xray..."

xray run -config /app/xray/config.json &

echo "Starting Flask..."

gunicorn --bind 127.0.0.1:8080 app:app &

echo "Starting Nginx..."

exec nginx -g "daemon off;"
