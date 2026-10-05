import os
import json
import uuid
import time
import base64
import secrets
import sqlite3
import subprocess
from datetime import datetime, timezone
from urllib.parse import quote

from flask import (
    Flask,
    request,
    session,
    redirect,
    url_for,
    render_template,
    jsonify,
    Response,
)

# =========================================================
# CATWOMAN PANEL
# =========================================================

app = Flask(__name__)

app.secret_key = "catwoman-panel-secret-key"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"

DB_DIR = "/app/data"
DB_PATH = os.path.join(DB_DIR, "panel.db")

XRAY_PATH = "/usr/local/bin/xray/xray"
XRAY_CONFIG = "/app/xray/config.json"
XRAY_API = "127.0.0.1:10085"

WS_PATH = "/xray-ws"

os.makedirs(DB_DIR, exist_ok=True)
os.makedirs("/app/xray", exist_ok=True)


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            uuid TEXT NOT NULL UNIQUE,
            limit_bytes INTEGER NOT NULL DEFAULT 10737418240,
            used_bytes INTEGER NOT NULL DEFAULT 0,
            xray_base INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            sub_token TEXT UNIQUE,
            created_at TEXT NOT NULL
        )
    """)

    # Migration for old databases
    migrations = {
        "xray_base": "INTEGER NOT NULL DEFAULT 0",
        "sub_token": "TEXT",
        "limit_bytes": "INTEGER NOT NULL DEFAULT 10737418240",
        "used_bytes": "INTEGER NOT NULL DEFAULT 0",
        "expires_at": "TEXT",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "created_at": "TEXT",
    }

    for column, definition in migrations.items():
        if not column_exists(conn, "users", column):
            conn.execute(
                f"ALTER TABLE users ADD COLUMN {column} {definition}"
            )

    # Give old users a secure subscription token
    users = conn.execute(
        "SELECT id FROM users WHERE sub_token IS NULL OR sub_token = ''"
    ).fetchall()

    for user in users:
        token = generate_token()

        while conn.execute(
            "SELECT 1 FROM users WHERE sub_token = ?",
            (token,)
        ).fetchone():
            token = generate_token()

        conn.execute(
            "UPDATE users SET sub_token = ? WHERE id = ?",
            (token, user["id"])
        )

    conn.commit()
    conn.close()


def generate_token():
    return "cw_" + secrets.token_urlsafe(32)


init_db()


# =========================================================
# AUTH
# =========================================================

def logged_in():
    return session.get("admin") is True


def login_required():
    if not logged_in():
        return redirect(url_for("login"))

    return None


# =========================================================
# HELPERS
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def parse_date(value):
    if not value:
        return None

    try:
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None


def gb_to_bytes(gb):
    try:
        return int(float(gb) * 1024 * 1024 * 1024)
    except Exception:
        return 0


def bytes_to_gb(value):
    return round(int(value or 0) / (1024 ** 3), 2)


def days_left(expires_at):
    if not expires_at:
        return None

    dt = parse_date(expires_at)

    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    seconds = (dt - now_utc()).total_seconds()

    if seconds <= 0:
        return 0

    return int(seconds // 86400)


def hours_left(expires_at):
    if not expires_at:
        return None

    dt = parse_date(expires_at)

    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    seconds = (dt - now_utc()).total_seconds()

    if seconds <= 0:
        return 0

    return int(seconds // 3600)


def format_expiry(expires_at):
    if not expires_at:
        return "نامحدود"

    dt = parse_date(expires_at)

    if not dt:
        return "نامشخص"

    return dt.strftime("%Y-%m-%d %H:%M")


# =========================================================
# XRAY STATS
# =========================================================

def xray_stats():
    """
    Returns all Xray traffic statistics.
    """

    try:
        result = subprocess.run(
            [
                XRAY_PATH,
                "api",
                "statsquery",
                "--server=" + XRAY_API
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            print("Xray stats error:", result.stderr)
            return {}

        data = json.loads(result.stdout)

        output = {}

        for item in data.get("stat", []):
            name = item.get("name")
            value = item.get("value", 0)

            try:
                value = int(value)
            except Exception:
                value = 0

            output[name] = value

        return output

    except Exception as e:
        print("Stats exception:", e)
        return {}


def get_user_xray_total(user):
    """
    Xray identifies traffic statistics by:
    user>>>email>>>traffic>>>uplink
    user>>>email>>>traffic>>>downlink
    """

    stats = xray_stats()

    email = f"user-{user['id']}"

    uplink_key = f"user>>>{email}>>>traffic>>>uplink"
    downlink_key = f"user>>>{email}>>>traffic>>>downlink"

    uplink = int(stats.get(uplink_key, 0))
    downlink = int(stats.get(downlink_key, 0))

    return uplink + downlink


def sync_user_usage(user_id):
    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    if not user:
        conn.close()
        return None

    current = get_user_xray_total(user)

    previous_base = int(user["xray_base"] or 0)
    used = int(user["used_bytes"] or 0)

    # Xray counters can reset after Xray restart.
    if current >= previous_base:
        delta = current - previous_base
    else:
        delta = current

    used += max(delta, 0)

    conn.execute(
        """
        UPDATE users
        SET used_bytes = ?,
            xray_base = ?
        WHERE id = ?
        """,
        (used, current, user_id)
    )

    conn.commit()

    updated = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    conn.close()

    return updated


def sync_all_users():
    conn = db()

    users = conn.execute(
        "SELECT id FROM users"
    ).fetchall()

    conn.close()

    for user in users:
        sync_user_usage(user["id"])


# =========================================================
# LIMIT CHECK
# =========================================================

def user_should_be_disabled(user):
    used = int(user["used_bytes"] or 0)
    limit = int(user["limit_bytes"] or 0)

    if limit > 0 and used >= limit:
        return True

    if user["expires_at"]:
        dt = parse_date(user["expires_at"])

        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            if dt <= now_utc():
                return True

    return False


def check_limits():
    conn = db()

    users = conn.execute(
        "SELECT * FROM users WHERE active = 1"
    ).fetchall()

    changed = False

    for user in users:
        updated = sync_user_usage(user["id"])

        if updated and user_should_be_disabled(updated):
            conn.execute(
                "UPDATE users SET active = 0 WHERE id = ?",
                (updated["id"],)
            )
            changed = True

    conn.commit()
    conn.close()

    if changed:
        rebuild_xray_config()


# =========================================================
# XRAY CONFIG
# =========================================================

def build_xray_config():
    conn = db()

    users = conn.execute(
        """
        SELECT *
        FROM users
        WHERE active = 1
        ORDER BY id ASC
        """
    ).fetchall()

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

        # Xray API
        "api": {
            "tag": "api",
            "services": [
                "StatsService"
            ]
        },

        # Traffic statistics
        "stats": {},

        # Enable user traffic statistics
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            }
        },

        "inbounds": [

            # VLESS WebSocket
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
                        "path": WS_PATH
                    }
                }
            },

            # Local Xray API
            {
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",

                "settings": {
                    "address": "127.0.0.1"
                },

                "tag": "api"
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
        },

        "outbounds": [
            {
                "protocol": "freedom",
                "tag": "direct"
            },

            {
                "protocol": "blackhole",
                "tag": "block"
            }
        ]
    }

    with open(XRAY_CONFIG, "w", encoding="utf-8") as f:
        json.dump(
            config,
            f,
            ensure_ascii=False,
            indent=2
        )

    return config


def restart_xray():
    """
    Rebuild config and restart Xray.

    IMPORTANT:
    We intentionally DO NOT set xray_base=0 here.
    If Xray resets its counters, sync logic detects
    current < previous_base and starts from current.
    """

    build_xray_config()

    try:
        subprocess.run(
            ["pkill", "-f", XRAY_PATH],
            capture_output=True,
            timeout=5
        )
    except Exception:
        pass

    time.sleep(1)

    try:
        subprocess.Popen(
            [
                XRAY_PATH,
                "run",
                "-config",
                XRAY_CONFIG
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        time.sleep(1)

        print("Xray restarted successfully.")

    except Exception as e:
        print("Xray restart error:", e)


def rebuild_xray_config():
    build_xray_config()


# =========================================================
# PUBLIC HOST / SUBSCRIPTION
# =========================================================

def public_host():
    """
    Railway/Nginx terminates HTTPS before Flask.
    The public URL is therefore HTTPS on port 443.
    """

    host = request.headers.get("X-Forwarded-Host")

    if not host:
        host = request.host

    # Remove accidental port
    host = host.split(":")[0]

    return host


def make_vless_link(user):
    host = public_host()

    name = quote(
        str(user["name"]),
        safe=""
    )

    path = quote(
        WS_PATH,
        safe=""
    )

    return (
        f"vless://{user['uuid']}"
        f"@{host}:443"
        f"?encryption=none"
        f"&security=tls"
        f"&type=ws"
        f"&host={quote(host, safe='')}"
        f"&path={path}"
        f"#{name}"
    )


def make_subscription(user):
    """
    Standard Base64 subscription format.

    Each line is a VLESS URI.
    """

    vless = make_vless_link(user)

    encoded = base64.b64encode(
        vless.encode("utf-8")
    ).decode("utf-8")

    return encoded


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def index():
    if logged_in():
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (
            username == ADMIN_USERNAME
            and password == ADMIN_PASSWORD
        ):
            session["admin"] = True
            return redirect(url_for("dashboard"))

        return render_template(
            "login.html",
            error="نام کاربری یا رمز عبور اشتباه است."
        )

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/dashboard")
def dashboard():

    guard = login_required()

    if guard:
        return guard

    check_limits()

    conn = db()

    users = conn.execute(
        """
        SELECT *
        FROM users
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    user_list = []

    for user in users:

        limit = int(user["limit_bytes"] or 0)
        used = int(user["used_bytes"] or 0)

        if limit > 0:
            percent = min(
                100,
                round((used / limit) * 100, 1)
            )

            remaining = max(
                0,
                limit - used
            )
        else:
            percent = 0
            remaining = 0

        user_list.append({
            "id": user["id"],
            "name": user["name"],
            "uuid": user["uuid"],
            "limit_bytes": limit,
            "used_bytes": used,
            "remaining_bytes": remaining,
            "limit_gb": bytes_to_gb(limit),
            "used_gb": bytes_to_gb(used),
            "remaining_gb": bytes_to_gb(remaining),
            "percent": percent,
            "active": bool(user["active"]),
            "days_left": days_left(user["expires_at"]),
            "hours_left": hours_left(user["expires_at"]),
            "expires_at": user["expires_at"],
            "expires_text": format_expiry(user["expires_at"]),
            "sub_token": user["sub_token"],
            "sub_url": url_for(
                "subscription",
                token=user["sub_token"],
                _external=True
            ),
            "portal_url": url_for(
                "subscriber_portal",
                token=user["sub_token"],
                _external=True
            )
        })

    return render_template(
        "dashboard.html",
        users=user_list
    )


# =========================================================
# SETTINGS
# =========================================================

@app.route("/settings")
def settings():

    guard = login_required()

    if guard:
        return guard

    conn = db()

    total = conn.execute(
        "SELECT COUNT(*) AS count FROM users"
    ).fetchone()["count"]

    active = conn.execute(
        "SELECT COUNT(*) AS count FROM users WHERE active = 1"
    ).fetchone()["count"]

    conn.close()

    return render_template(
        "settings.html",
        total_users=total,
        active_users=active,
        username=ADMIN_USERNAME
    )


# =========================================================
# CREATE USER
# =========================================================

@app.route("/users/create", methods=["POST"])
def create_user():

    guard = login_required()

    if guard:
        return guard

    name = request.form.get("name", "").strip()
    limit_gb = request.form.get("limit_gb", "10").strip()
    days = request.form.get("days", "30").strip()

    if not name:
        return redirect(url_for("dashboard"))

    try:
        limit_gb_value = float(limit_gb)
    except Exception:
        limit_gb_value = 10

    try:
        days_value = int(days)
    except Exception:
        days_value = 30

    if limit_gb_value <= 0:
        limit_gb_value = 10

    if days_value < 0:
        days_value = 30

    user_uuid = str(uuid.uuid4())
    sub_token = generate_token()

    if days_value == 0:
        expires_at = None
    else:
        expires_at = (
            now_utc().timestamp()
            + days_value * 86400
        )

        expires_at = datetime.fromtimestamp(
            expires_at,
            tz=timezone.utc
        ).isoformat()

    conn = db()

    conn.execute(
        """
        INSERT INTO users
        (
            name,
            uuid,
            limit_bytes,
            used_bytes,
            xray_base,
            expires_at,
            active,
            sub_token,
            created_at
        )
        VALUES (?, ?, ?, 0, 0, ?, 1, ?, ?)
        """,
        (
            name,
            user_uuid,
            gb_to_bytes(limit_gb_value),
            expires_at,
            sub_token,
            iso_now()
        )
    )

    conn.commit()
    conn.close()

    restart_xray()

    return redirect(url_for("dashboard"))


# =========================================================
# EDIT USER
# =========================================================

@app.route("/users/<int:user_id>/edit", methods=["POST"])
def edit_user(user_id):

    guard = login_required()

    if guard:
        return guard

    name = request.form.get("name", "").strip()
    limit_gb = request.form.get("limit_gb", "10").strip()
    days = request.form.get("days", "").strip()

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    if not user:
        conn.close()
        return redirect(url_for("dashboard"))

    try:
        limit_value = float(limit_gb)
    except Exception:
        limit_value = bytes_to_gb(user["limit_bytes"])

    if limit_value <= 0:
        limit_value = bytes_to_gb(user["limit_bytes"])

    expires_at = user["expires_at"]

    if days:
        try:
            days_value = int(days)

            expires_at = (
                now_utc().timestamp()
                + days_value * 86400
            )

            expires_at = datetime.fromtimestamp(
                expires_at,
                tz=timezone.utc
            ).isoformat()

        except Exception:
            pass

    conn.execute(
        """
        UPDATE users
        SET name = ?,
            limit_bytes = ?,
            expires_at = ?
        WHERE id = ?
        """,
        (
            name or user["name"],
            gb_to_bytes(limit_value),
            expires_at,
            user_id
        )
    )

    conn.commit()
    conn.close()

    restart_xray()

    return