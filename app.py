from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import uuid

app = Flask(__name__)

app.secret_key = "my-simple-secret-key"

USERNAME = "admin"
PASSWORD = "admin123"

XRAY_UUID = "7f6c4e2a-1b9d-4a73-8c5e-2d1f6b9a4e30"
XRAY_PATH = "/xray-ws"


@app.route("/")
def index():

    if "logged_in" not in session:
        return redirect(url_for("login"))

    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == USERNAME and password == PASSWORD:

            session["logged_in"] = True

            return redirect(url_for("dashboard"))

        return render_template(
            "login.html",
            error="نام کاربری یا رمز عبور اشتباه است."
        )

    return render_template("login.html")


@app.route("/dashboard")
def dashboard():

    if "logged_in" not in session:
        return redirect(url_for("login"))

    return render_template(
        "dashboard.html",
        username=USERNAME
    )


@app.route("/settings")
def settings():

    if "logged_in" not in session:
        return redirect(url_for("login"))

    return render_template(
        "settings.html",
        username=USERNAME
    )


@app.route("/generate-config")
def generate_config():

    if "logged_in" not in session:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    host = request.host.split(":")[0]

    config = (
        f"vless://{XRAY_UUID}@{host}:443"
        f"?encryption=none"
        f"&security=tls"
        f"&type=ws"
        f"&host={host}"
        f"&path=%2Fxray-ws"
        f"#Xray-Panel"
    )

    return jsonify({
        "uuid": XRAY_UUID,
        "host": host,
        "path": XRAY_PATH,
        "config": config
    })


@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("login"))


if __name__ == "__main__":

    import os

    port = int(os.environ.get("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port
            )
