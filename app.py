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
# CONFIG & PATHS
# =========================================================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "catwoman-panel-super-secret-key-123456")

ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin123")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DB_DIR, "panel.db")

XRAY_DIR = os.path.join(BASE_DIR, "xray")
XRAY_PATH = "/usr/local/bin/xray/xray"
XRAY_CONFIG = os.path.join(XRAY_DIR, "config.json")
XRAY_API = "127.0.0.1:10085"

WS_PATH = "/xray-ws"

os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(XRAY_DIR, exist_ok=True)


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table, column):
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)
    except Exception:
        return False


def generate_token():
    return "cw_" + secrets.token_urlsafe(32)


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
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
            except Exception:
                pass

    users = conn.execute("SELECT id FROM users WHERE sub_token IS NULL OR sub_token = ''").fetchall()
    for user in users:
        token = generate_token()
        while conn.execute("SELECT 1 FROM users WHERE sub_token = ?", (token,)).fetchone():
            token = generate_token()
        conn.execute("UPDATE users SET sub_token = ? WHERE id = ?", (token, user["id"]))

    conn.commit()
    conn.close()


init_db()


# =========================================================
# AUTH & HELPERS
# =========================================================

def logged_in():
    return session.get("admin") is True


def login_required():
    if not logged_in():
        return redirect(url_for("login"))
    return None


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def parse_date(value):
    if not value:
        return None
    try:
        value = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except Exception:
        return None


def gb_to_bytes(gb):
    try:
        return int(float(gb) * 1024 * 1024 * 1024)
    except Exception:
        return 0


def bytes_to_gb(value):
    try:
        return round(int(value or 0) / (1024 ** 3), 2)
    except Exception:
        return 0


def days_left(expires_at):
    if not expires_at:
        return None
    dt = parse_date(expires_at)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (dt - now_utc()).total_seconds()
    return max(0, int(seconds // 86400))


def hours_left(expires_at):
    if not expires_at:
        return None
    dt = parse_date(expires_at)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (dt - now_utc()).total_seconds()
    return max(0, int(seconds // 3600))


def format_expiry(expires_at):
    if not expires_at:
        return "نامحدود"
    dt = parse_date(expires_at)
    if not dt:
        return "نامشخص"
    return dt.strftime("%Y-%m-%d %H:%M")


# =========================================================
# XRAY STATS & SAFE CALLS
# =========================================================

def xray_stats():
    if not os.path.exists(XRAY_PATH):
        return {}
    try:
        result = subprocess.run(
            [XRAY_PATH, "api", "statsquery", "--server=" + XRAY_API],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        output = {}
        for item in data.get("stat", []):
            name = item.get("name")
            try:
                output[name] = int(item.get("value", 0))
            except Exception:
                output[name] = 0
        return output
    except Exception:
        return {}


def get_user_xray_total(user):
    stats = xray_stats()
    email = f"user-{user['id']}"
    uplink = int(stats.get(f"user>>>{email}>>>traffic>>>uplink", 0))
    downlink = int(stats.get(f"user>>>{email}>>>traffic>>>downlink", 0))
    return uplink + downlink


def sync_user_usage(user_id):
    try:
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            conn.close()
            return None

        current = get_user_xray_total(user)
        previous_base = int(user["xray_base"] or 0)
        used = int(user["used_bytes"] or 0)

        delta = current - previous_base if current >= previous_base else current
        used += max(delta, 0)

        conn.execute(
            "UPDATE users SET used_bytes = ?, xray_base = ? WHERE id = ?",
            (used, current, user_id)
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        return updated
    except Exception:
        return None


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
    try:
        conn = db()
        users = conn.execute("SELECT * FROM users WHERE active = 1").fetchall()
        changed = False
        for user in users:
            updated = sync_user_usage(user["id"])
            if updated and user_should_be_disabled(updated):
                conn.execute("UPDATE users SET active = 0 WHERE id = ?", (updated["id"],))
                changed = True
        conn.commit()
        conn.close()
        if changed:
            rebuild_xray_config()
    except Exception as e:
        print(f"Limit check warning: {e}")


def build_xray_config():
    try:
        conn = db()
        users = conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY id ASC").fetchall()
        conn.close()

        clients = [{"id": u["uuid"], "level": 0, "email": f"user-{u['id']}"} for u in users]
        config = {
            "log": {"loglevel": "warning"},
            "api": {"tag": "api", "services": ["StatsService"]},
            "stats": {},
            "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
            "inbounds": [
                {
                    "listen": "127.0.0.1",
                    "port": 10000,
                    "protocol": "vless",
                    "settings": {"clients": clients, "decryption": "none"},
                    "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": WS_PATH}}
                },
                {
                    "listen": "127.0.0.1",
                    "port": 10085,
                    "protocol": "dokodemo-door",
                    "settings": {"address": "127.0.0.1"},
                    "tag": "api"
                }
            ],
            "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
            "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}]
        }
        with open(XRAY_CONFIG, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Build config warning: {e}")


def restart_xray():
    build_xray_config()
    if not os.path.exists(XRAY_PATH):
        return
    try:
        subprocess.run(["pkill", "-f", XRAY_PATH], capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.Popen([XRAY_PATH, "run", "-config", XRAY_CONFIG], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("Xray run warning:", e)


def rebuild_xray_config():
    build_xray_config()


# =========================================================
# SUBSCRIPTION & LINKS
# =========================================================

def public_host():
    host = request.headers.get("X-Forwarded-Host") or request.host
    return host.split(":")[0]


def make_vless_link(user):
    host = public_host()
    name = quote(str(user["name"]), safe="")
    path = quote(WS_PATH, safe="")
    return f"vless://{user['uuid']}@{host}:443?encryption=none&security=tls&type=ws&host={quote(host, safe='')}&path={path}#{name}"


# =========================================================
# ROUTES
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
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="نام کاربری یا رمز عبور اشتباه است.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    guard = login_required()
    if guard:
        return guard

    check_limits()

    conn = db()
    users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()

    user_list = []
    for user in users:
        limit = int(user["limit_bytes"] or 0)
        used = int(user["used_bytes"] or 0)
        percent = min(100, round((used / limit) * 100, 1)) if limit > 0 else 0
        remaining = max(0, limit - used) if limit > 0 else 0

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
            "sub_url": url_for("subscription", token=user["sub_token"], _external=True),
            "portal_url": url_for("subscriber_portal", token=user["sub_token"], _external=True)
        })

    return render_template("dashboard.html", users=user_list)


@app.route("/settings")
def settings():
    guard = login_required()
    if guard:
        return guard

    conn = db()
    total = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    active = conn.execute("SELECT COUNT(*) AS count FROM users WHERE active = 1").fetchone()["count"]
    conn.close()

    return render_template("settings.html", total_users=total, active_users=active, username=ADMIN_USERNAME)


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
        limit_gb_val = float(limit_gb)
    except Exception:
        limit_gb_val = 10.0

    try:
        days_val = int(days)
    except Exception:
        days_val = 30

    expires_at = None
    if days_val > 0:
        expires_at = datetime.fromtimestamp(now_utc().timestamp() + days_val * 86400, tz=timezone.utc).isoformat()

    conn = db()
    conn.execute(
        """
        INSERT INTO users (name, uuid, limit_bytes, used_bytes, xray_base, expires_at, active, sub_token, created_at)
        VALUES (?, ?, ?, 0, 0, ?, 1, ?, ?)
        """,
        (name, str(uuid.uuid4()), gb_to_bytes(limit_gb_val), expires_at, generate_token(), iso_now())
    )
    conn.commit()
    conn.close()

    restart_xray()
    return redirect(url_for("dashboard"))


@app.route("/users/<int:user_id>/edit", methods=["POST"])
def edit_user(user_id):
    guard = login_required()
    if guard:
        return guard

    name = request.form.get("name", "").strip()
    limit_gb = request.form.get("limit_gb", "").strip()
    days = request.form.get("days", "").strip()

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return redirect(url_for("dashboard"))

    try:
        limit_val = float(limit_gb) if limit_gb else bytes_to_gb(user["limit_bytes"])
    except Exception:
        limit_val = bytes_to_gb(user["limit_bytes"])

    expires_at = user["expires_at"]
    if days:
        try:
            expires_at = datetime.fromtimestamp(now_utc().timestamp() + int(days) * 86400, tz=timezone.utc).isoformat()
        except Exception:
            pass

    conn.execute(
        "UPDATE users SET name = ?, limit_bytes = ?, expires_at = ? WHERE id = ?",
        (name or user["name"], gb_to_bytes(limit_val), expires_at, user_id)
    )
    conn.commit()
    conn.close()

    restart_xray()
    return redirect(url_for("dashboard"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    guard = login_required()
    if guard:
        return guard

    conn = db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    restart_xray()
    return redirect(url_for("dashboard"))


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
def toggle_user(user_id):
    guard = login_required()
    if guard:
        return guard

    conn = db()
    user = conn.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        new_status = 0 if user["active"] else 1
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (new_status, user_id))
        conn.commit()
    conn.close()

    restart_xray()
    return redirect(url_for("dashboard"))


@app.route("/users/<int:user_id>/reset", methods=["POST"])
def reset_usage(user_id):
    guard = login_required()
    if guard:
        return guard

    conn = db()
    conn.execute("UPDATE users SET used_bytes = 0, xray_base = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    return redirect(url_for("dashboard"))


# =========================================================
# CLIENT SUBSCRIPTION LINKS
# =========================================================

@app.route("/sub/<token>")
def subscription(token):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE sub_token = ? AND active = 1", (token,)).fetchone()
    conn.close()

    if not user or user_should_be_disabled(user):
        return Response("User Disabled or Expired", status=403, mimetype="text/plain")

    vless_link = make_vless_link(user)
    b64_link = base64.b64encode(vless_link.encode("utf-8")).decode("utf-8")
    return Response(b64_link, mimetype="text/plain")


@app.route("/portal/<token>")
def subscriber_portal(token):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE sub_token = ?", (token,)).fetchone()
    conn.close()

    if not user:
        return "کاربر یافت نشد", 404

    limit = int(user["limit_bytes"] or 0)
    used = int(user["used_bytes"] or 0)
    percent = min(100, round((used / limit) * 100, 1)) if limit > 0 else 0

    return render_template(
        "portal.html" if os.path.exists(os.path.join(BASE_DIR, "templates", "portal.html")) else "dashboard.html",
        user={
            "name": user["name"],
            "used_gb": bytes_to_gb(used),
            "limit_gb": bytes_to_gb(limit),
            "percent": percent,
            "expires_text": format_expiry(user["expires_at"]),
            "config": make_vless_link(user)
        }
    )


# =========================================================
# RUNNER
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)