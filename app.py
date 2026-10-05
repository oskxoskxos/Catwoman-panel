from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import sqlite3
import uuid
import os
import json
import subprocess
import signal
import time
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock

app = Flask(__name__)

app.secret_key = "my-simple-secret-key"

USERNAME = "admin"
PASSWORD = "admin123"

DB_PATH = "/app/data/panel.db"
XRAY_CONFIG = "/app/xray/config.json"
XRAY_BINARY = "/usr/local/bin/xray/xray"

XRAY_API = "127.0.0.1:10085"
XRAY_PATH = "/xray-ws"

xray_lock = Lock()


# -------------------------------------------------
# DATABASE
# -------------------------------------------------

def db():
    os.makedirs("/app/data", exist_ok=True)

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_database():

    conn = db()

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

    count = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    if count == 0:

        now = datetime.now(timezone.utc)

        conn.execute("""
            INSERT INTO users
            (name, uuid, limit_bytes, used_bytes, xray_base,
             expires_at, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "کاربر اول",
            "7f6c4e2a-1b9d-4a73-8c5e-2d1f6b9a4e30",
            10 * 1024 * 1024 * 1024,
            0,
            0,
            (now + timedelta(days=30)).isoformat(),
            1,
            now.isoformat()
        ))

        conn.commit()

    conn.close()


# -------------------------------------------------
# XRAY STATS
# -------------------------------------------------

def get_xray_stats():

    try:

        result = subprocess.run(
            [
                XRAY_BINARY,
                "api",
                "statsquery",
                "--server=" + XRAY_API
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return {}

        data = json.loads(result.stdout)

        stats = {}

        for item in data.get("stat", []):

            name = item.get("name", "")
            value = int(item.get("value", 0))

            if not name.startswith("user>>>"):
                continue

            parts = name.split(">>>")

            if len(parts) != 4:
                continue

            email = parts[1]
            direction = parts[3]

            if email not in stats:
                stats[email] = {
                    "uplink": 0,
                    "downlink": 0
                }

            if direction == "uplink":
                stats[email]["uplink"] = value

            elif direction == "downlink":
                stats[email]["downlink"] = value

        return stats

    except Exception:

        return {}


def sync_usage():

    stats = get_xray_stats()

    if not stats:
        return

    conn = db()

    users = conn.execute(
        "SELECT * FROM users"
    ).fetchall()

    for user in users:

        email = f"user-{user['id']}"

        current = stats.get(
            email,
            {
                "uplink": 0,
                "downlink": 0
            }
        )

        current_total = (
            current["uplink"] +
            current["downlink"]
        )

        base = user["xray_base"]

        # اگر Xray ریست شده باشد
        if current_total < base:

            delta = current_total

        else:

            delta = current_total - base

        if delta > 0:

            new_used = user["used_bytes"] + delta

            conn.execute("""
                UPDATE users
                SET used_bytes = ?,
                    xray_base = ?
                WHERE id = ?
            """, (
                new_used,
                current_total,
                user["id"]
            ))

    conn.commit()
    conn.close()


# -------------------------------------------------
# DISABLE EXPIRED / FULL USERS
# -------------------------------------------------

def check_limits():

    sync_usage()

    conn = db()

    users = conn.execute(
        "SELECT * FROM users WHERE active = 1"
    ).fetchall()

    changed = False

    now = datetime.now(timezone.utc)

    for user in users:

        expired = False

        if user["used_bytes"] >= user["limit_bytes"]:
            expired = True

        try:

            expires_at = datetime.fromisoformat(
                user["expires_at"]
            )

            if expires_at <= now:
                expired = True

        except Exception:
            pass

        if expired:

            conn.execute("""
                UPDATE users
                SET active = 0
                WHERE id = ?
            """, (
                user["id"],
            ))

            changed = True

    conn.commit()
    conn.close()

    if changed:
        restart_xray()


# -------------------------------------------------
# XRAY CONFIG
# -------------------------------------------------

def build_xray_config():

    conn = db()

    users = conn.execute("""
        SELECT *
        FROM users
        WHERE active = 1
    """).fetchall()

    conn.close()

    clients = []

    for user in users:

        clients.append({
            "id": user["uuid"],
            "level": 0,
            "email": f"user-{user['id']}"
        })

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
            },

            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True
            }
        },

        "inbounds": [

            {
                "listen": "127.0.0.1",
                "port": 10000,
                "protocol": "vless",

                "settings": {
                    "clients": clients,
                    "decryption": "none"
                },

                "streamSettings": {
                    "network": "ws",
                    "security": "none",

                    "wsSettings": {
                        "path": XRAY_PATH
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
                    ],
                    "tag": "api"
                },

                "tag": "api"
            }

        ],

        "outbounds": [

            {
                "protocol": "freedom",
                "tag": "direct"
            },

            {
                "protocol": "blackhole",
                "tag": "blocked"
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

    os.makedirs(
        "/app/xray",
        exist_ok=True
    )

    with open(
        XRAY_CONFIG,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            config,
            f,
            indent=2,
            ensure_ascii=False
        )


def reset_xray_bases():

    conn = db()

    conn.execute("""
        UPDATE users
        SET xray_base = 0
    """)

    conn.commit()
    conn.close()


def restart_xray():

    with xray_lock:

        try:

            # اول مصرف فعلی را ذخیره می‌کنیم
            sync_usage()

        except Exception:
            pass

        build_xray_config()

        pid_file = "/tmp/xray.pid"

        old_pid = None

        if os.path.exists(pid_file):

            try:

                with open(pid_file, "r") as f:
                    old_pid = int(f.read().strip())

            except Exception:
                old_pid = None

        if old_pid:

            try:

                os.kill(
                    old_pid,
                    signal.SIGTERM
                )

            except Exception:
                pass

            for _ in range(20):

                try:

                    os.kill(
                        old_pid,
                        0
                    )

                    time.sleep(0.1)

                except Exception:
                    break

        process = subprocess.Popen(
            [
                XRAY_BINARY,
                "run",
                "-config",
                XRAY_CONFIG
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        with open(
            pid_file,
            "w"
        ) as f:

            f.write(
                str(process.pid)
            )

        # چون Xray جدید از صفر شروع شده
        reset_xray_bases()


# -------------------------------------------------
# BACKGROUND WORKER
# -------------------------------------------------

def background_worker():

    time.sleep(5)

    while True:

        try:
            check_limits()
        except Exception:
            pass

        time.sleep(15)


# -------------------------------------------------
# AUTH
# -------------------------------------------------

@app.route("/")
def index():

    if "logged_in" not in session:
        return redirect(
            url_for("login")
        )

    return redirect(
        url_for("dashboard")
    )


@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        )

        password = request.form.get(
            "password",
            ""
        )

        if (
            username == USERNAME
            and
            password == PASSWORD
        ):

            session["logged_in"] = True

            return redirect(
                url_for("dashboard")
            )

        return render_template(
            "login.html",
            error="نام کاربری یا رمز عبور اشتباه است."
        )

    return render_template(
        "login.html"
    )


# -------------------------------------------------
# DASHBOARD
# -------------------------------------------------

@app.route("/dashboard")
def dashboard():

    if "logged_in" not in session:
        return redirect(
            url_for("login")
        )

    check_limits()

    conn = db()

    users = conn.execute("""
        SELECT *
        FROM users
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        username=USERNAME,
        users=users
    )


# -------------------------------------------------
# CREATE USER
# -------------------------------------------------

@app.route(
    "/users/create",
    methods=["POST"]
)
def create_user():

    if "logged_in" not in session:
        return redirect(
            url_for("login")
        )

    name = request.form.get(
        "name",
        ""
    ).strip()

    volume_gb = float(
        request.form.get(
            "volume",
            "10"
        )
    )

    days = int(
        request.form.get(
            "days",
            "30"
        )
    )

    if not name:
        name = "کاربر جدید"

    if volume_gb <= 0:
        volume_gb = 1

    if days <= 0:
        days = 1

    user_uuid = str(
        uuid.uuid4()
    )

    now = datetime.now(
        timezone.utc
    )

    expires_at = (
        now +
        timedelta(
            days=days
        )
    )

    conn = db()

    conn.execute("""
        INSERT INTO users
        (name, uuid, limit_bytes,
         used_bytes, xray_base,
         expires_at, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        name,
        user_uuid,
        int(
            volume_gb *
            1024 *
            1024 *
            1024
        ),
        0,
        0,
        expires_at.isoformat(),
        1,
        now.isoformat()
    ))

    conn.commit()
    conn.close()

    restart_xray()

    return redirect(
        url_for("dashboard")
    )


# -------------------------------------------------
# EDIT USER
# -------------------------------------------------

@app.route(
    "/users/<int:user_id>/edit",
    methods=["POST"]
)
def edit_user(user_id):

    if "logged_in" not in session:
        return redirect(
            url_for("login")
        )

    name = request.form.get(
        "name",
        ""
    ).strip()

    volume_gb = float(
        request.form.get(
            "volume",
            "10"
        )
    )

    days = int(
        request.form.get(
            "days",
            "30"
        )
    )

    active = int(
        request.form.get(
            "active",
            "1"
        )
    )

    if volume_gb <= 0:
        volume_gb = 1

    if days <= 0:
        days = 1

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    if not user:

        conn.close()

        return redirect(
            url_for("dashboard")
        )

    # تعداد روز از همین لحظه محاسبه می‌شود
    expires_at = (
        datetime.now(timezone.utc)
        +
        timedelta(days=days)
    )

    conn.execute("""
        UPDATE users
        SET name = ?,
            limit_bytes = ?,
            expires_at = ?,
            active = ?
        WHERE id = ?
    """, (
        name,
        int(
            volume_gb *
            1024 *
            1024 *
            1024
        ),
        expires_at.isoformat(),
        active,
        user_id
    ))

    conn.commit()
    conn.close()

    restart_xray()

    return redirect(
        url_for("dashboard")
    )


# -------------------------------------------------
# DELETE USER
# -------------------------------------------------

@app.route(
    "/users/<int:user_id>/delete",
    methods=["POST"]
)
def delete_user(user_id):

    if "logged_in" not in session:
        return redirect(
            url_for("login")
        )

    sync_usage()

    conn = db()

    conn.execute(
        "DELETE FROM users WHERE id = ?",
        (user_id,)
    )

    conn.commit()
    conn.close()

    restart_xray()

    return redirect(
        url_for("dashboard")
    )


# -------------------------------------------------
# RESET USAGE
# -------------------------------------------------

@app.route(
    "/users/<int:user_id>/reset",
    methods=["POST"]
)
def reset_usage(user_id):

    if "logged_in" not in session:
        return redirect(
            url_for("login")
        )

    sync_usage()

    conn = db()

    conn.execute("""
        UPDATE users
        SET used_bytes = 0,
            xray_base = 0,
            active = 1
        WHERE id = ?
    """, (
        user_id,
    ))

    conn.commit()
    conn.close()

    restart_xray()

    return redirect(
        url_for("dashboard")
    )


# -------------------------------------------------
# GENERATE CONFIG
# -------------------------------------------------

@app.route(
    "/users/<int:user_id>/config"
)
def user_config(user_id):

    if "logged_in" not in session:

        return jsonify({
            "error": "Unauthorized"
        }), 401

    check_limits()

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    conn.close()

    if not user:

        return jsonify({
            "error": "User not found"
        }), 404

    host = request.host.split(":")[0]

    config = (
        f"vless://{user['uuid']}@{host}:443"
        f"?encryption=none"
        f"&security=tls"
        f"&type=ws"
        f"&host={host}"
        f"&path=%2Fxray-ws"
        f"#{user['name']}"
    )

    return jsonify({

        "id": user["id"],

        "name": user["name"],

        "uuid": user["uuid"],

        "config": config

    })


# -------------------------------------------------
# LOGOUT
# -------------------------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# -------------------------------------------------
# STARTUP
# -------------------------------------------------

init_database()

try:
    build_xray_config()
except Exception:
    pass

worker = Thread(
    target=background_worker,
    daemon=True
)

worker.start()


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8080
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
                )
