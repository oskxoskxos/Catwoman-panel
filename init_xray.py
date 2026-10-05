import os
import sqlite3
import json

DB_PATH = "/app/data/panel.db"
CONFIG_PATH = "/app/xray/config.json"

os.makedirs("/app/data", exist_ok=True)
os.makedirs("/app/xray", exist_ok=True)

conn = sqlite3.connect(DB_PATH)

conn.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    uuid TEXT NOT NULL UNIQUE,
    limit_bytes INTEGER NOT NULL,
    used_bytes INTEGER NOT NULL DEFAULT 0,
    xray_base INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)
""")

conn.commit()

conn.close()

print("Database initialized.")

if not os.path.exists(CONFIG_PATH):

    config = {
        "log": {
            "loglevel": "warning"
        },

        "api": {
            "tag": "api",
            "services": [
                "StatsService"
            ]
        },

        "stats": {},

        "policy": {
            "levels": {
                "0": {
                    "handshake": 60,
                    "connIdle": 300,
                    "uplinkOnly": 1,
                    "downlinkOnly": 1,
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                    "statsUserOnline": True,
                    "bufferSize": 4
                }
            }
        },

        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10000,
                "protocol": "vless",

                "settings": {
                    "clients": [],
                    "decryption": "none"
                },

                "streamSettings": {
                    "network": "ws",
                    "security": "none",

                    "wsSettings": {
                        "path": "/xray-ws"
                    }
                }
            },

            {
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "tunnel",

                "settings": {
                    "services": [
                        "StatsService"
                    ]
                },

                "tag": "api"
            }
        ],

        "outbounds": [
            {
                "protocol": "freedom",
                "tag": "direct"
            }
        ],

        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": [
                        "api"
                    ],
                    "outboundTag": "api"
                }
            ]
        }
    }

    with open(
        CONFIG_PATH,
        "w"
    ) as f:

        json.dump(
            config,
            f,
            indent=2
        )

print("Xray configuration initialized.")
