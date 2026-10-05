#!/bin/sh

echo "Starting Flask..."

gunicorn \
    --bind 127.0.0.1:5000 \
    --workers 1 \
    app:app &

echo "Starting Nginx on port ${PORT:-8080}..."

sed "s/\${PORT}/${PORT:-8080}/g" \
    /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf

exec nginx -g "daemon off;"
