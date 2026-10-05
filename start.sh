#!/bin/sh

echo "Starting Xray..."

xray run -config /app/xray/config.json &

XRAY_PID=$!

echo "Xray started with PID $XRAY_PID"

echo "Starting Flask panel..."

exec gunicorn --bind 0.0.0.0:${PORT:-8080} app:app
