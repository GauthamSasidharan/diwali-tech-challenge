import csv
import io
import os
import random
import re
import secrets
import time
import requests
import sqlite3
from urllib.parse import quote
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, redirect, render_template,
                   request, session, url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
# Set SECRET_KEY in the environment for production. The fallback only keeps local dev working.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

# Admin login (HTTP Basic). Set ADMIN_PASSWORD in the environment for real use.
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "diwali123")

IS_PROD = os.environ.get("APP_ENV") == "production"
if IS_PROD and (app.secret_key == "dev-only-change-me" or ADMIN_PASSWORD == "diwali123"):
    raise RuntimeError("Set SECRET_KEY and ADMIN_PASSWORD before running in production.")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,
)

# Public address of the hosted site, e.g. https://yourname.pythonanywhere.com
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
DB = os.path.join(BASE_DIR, "campaign.db")
GOOGLE_SHEET_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbwa6YlqlEVKuiw1qlEuFT65KEQs4RtkuTeIzWWo8Pp2GK9PFbB8SnL0Nj5hvRx6R4PI/exec"

DEPARTMENTS = [
    "IT",
    "Digital Marketing",
    "Automation",
    "Building Management System",
]

# Score -> possible offers (one is picked at random, equal chance).
OFFER_RULES = {
    10: ["19% OFF", "20% OFF"],
    9:  ["15% OFF", "16% OFF", "17% OFF", "18% OFF"],
    8:  ["11% OFF", "12% OFF", "13% OFF", "14% OFF"],
    7:  ["7% OFF", "8% OFF", "9% OFF", "10% OFF"],
}
FREE_WORKSHOP = "FREE WORKSHOP"

QUIZ_SECONDS = 60      # total time allowed for the whole quiz
GRACE_SECONDS = 2      # allowance for network delay on the last submit

# "answer" is the zero-based index of the correct option.
QUESTIONS = [
    {"id": 1, "question": "Which of these is an input device?",
     "options": ["Monitor", "Keyboard", "Speaker", "Printer"], "answer": 1},
    {"id": 2, "question": "Which language is used to structure the content of a web page?",
     "options": ["Python", "HTML", "SQL", "C"], "answer": 1},
    {"id": 3, "question": "Which of these is a programming language?",
     "options": ["Chrome", "Windows", "Python", "Excel"], "answer": 2},
    {"id": 4, "question": "A popular web browser?",
     "options": ["Notepad", "Paint", "Calculator", "Chrome"], "answer": 3},
    {"id": 5, "question": "Which of the following is an online shopping site?",
     "options": ["Amazon", "You Tube", "Spotify", "Google Maps"], "answer": 0},
    {"id": 6, "question": "A PLC is mainly used in which field?",
     "options": ["Photo editing", "Industrial automation", "Video streaming", "Web hosting"], "answer": 1},
    {"id": 7, "question": "A Building Management System (BMS) can control which of these?",
     "options": ["Social media posts", "Email servers only", "HVAC, lighting and access control", "Mobile app stores"], "answer": 2},
    {"id": 8, "question": "Which device measures temperature in an automated system?",
     "options": ["Relay", "Motor", "Router", "Thermocouple"], "answer": 3},
    {"id": 9, "question": "What does AI stand for?",
     "options": ["Automated Internet", "Artificial Intelligence", "Applied Input", "Advanced Interface"], "answer": 1},
    {"id": 10, "question": "Which of these is the strongest password?",
     "options": ["password123", "diwali2025", "My$Lamp#Glows!92", "12345678"], "answer": 2},
]
QUESTIONS_BY_ID = {q["id"]: q for q in QUESTIONS}


# ---------- database ----------
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            qualification TEXT NOT NULL,
            phone TEXT NOT NULL,
            score INTEGER,
            total INTEGER,
            department TEXT,
            offer TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# Runs on import, so it also works with `flask run` and gunicorn (not only `python app.py`).
init_db()


# ---------- helpers ----------
def offer_for_score(score):
    options = OFFER_RULES.get(min(score, 10))
    return random.choice(options) if options else FREE_WORKSHOP


def quiz_finished():
    order = session.get("order")
    return bool(order) and session.get("question_index", 0) >= len(order)


def time_left():
    started = session.get("started_at")
    if started is None:
        return float(QUIZ_SECONDS)
    return started + QUIZ_SECONDS - time.time()   # can go negative once time is up


def finish_quiz(timed_out=False):
    session["question_index"] = len(session["order"])
    if timed_out:
        session["timed_out"] = True


def csv_safe(value):
    """Stop spreadsheet formula injection (cells starting with = + - @)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def require_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        ok = (auth and auth.type == "basic"
              and secrets.compare_digest(auth.username or "", ADMIN_USER)
              and secrets.compare_digest(auth.password or "", ADMIN_PASSWORD))
        if not ok:
            return Response("Login required", 401,
                            {"WWW-Authenticate": 'Basic realm="Campaign admin"'})
        return view(*args, **kwargs)
    return wrapper


# ---------- routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.post("/start")
def start():
    name = request.form.get("name", "").strip()[:80]
    qualification = request.form.get("qualification", "").strip()[:80]
    if not name or not qualification:
        return redirect(url_for("index"))

    session.clear()
    session["name"] = name
    session["qualification"] = qualification

    # Store only ids + shuffled option positions. Correct answers stay on the server,
    # because Flask's session cookie is signed but NOT encrypted (users can read it).
    ids = [q["id"] for q in QUESTIONS]
    random.shuffle(ids)
    order = []
    for qid in ids:
        positions = list(range(4))
        random.shuffle(positions)
        order.append({"id": qid, "opts": positions})

    session["order"] = order
    session["question_index"] = 0
    session["score"] = 0
    return redirect(url_for("quiz"))


@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    order = session.get("order")
    if not order:
        return redirect(url_for("index"))

    index = session.get("question_index", 0)
    if index >= len(order):
        return redirect(url_for("lamp"))

    # Clock starts when the first question is shown; refresh won't reset it.
    if "started_at" not in session:
        session["started_at"] = time.time()

    if time_left() + GRACE_SECONDS <= 0:
        finish_quiz(timed_out=True)
        return redirect(url_for("lamp"))

    current = order[index]
    question = QUESTIONS_BY_ID[current["id"]]

    if request.method == "POST":
        # Ignore stale/duplicate submits (back button, double tap).
        if request.form.get("qid", type=int) == current["id"]:
            selected = request.form.get("answer", type=int)
            if selected == question["answer"]:
                session["score"] = session.get("score", 0) + 1
            session["question_index"] = index + 1
        return redirect(url_for("quiz"))

    return render_template(
        "quiz.html",
        question={
            "id": question["id"],
            "question": question["question"],
            "options": [(pos, question["options"][pos]) for pos in current["opts"]],
        },
        number=index + 1,
        total=len(order),
        seconds_left=round(max(0.0, time_left()), 1),
    )


@app.route("/quiz/timeup")
def quiz_timeup():
    """The browser sends people here when its countdown reaches 0."""
    if not session.get("order"):
        return redirect(url_for("index"))
    if quiz_finished():
        return redirect(url_for("lamp"))
    if time_left() <= 1:
        finish_quiz(timed_out=True)
        return redirect(url_for("lamp"))
    return redirect(url_for("quiz"))


@app.route("/lamp")
def lamp():
    if not quiz_finished():
        return redirect(url_for("quiz") if session.get("order") else url_for("index"))
    return render_template("lamp.html", score=session["score"],
                           total=len(session["order"]),
                           timed_out=session.get("timed_out", False))


@app.route("/phone", methods=["GET", "POST"])
def phone():
    if not quiz_finished():
        return redirect(url_for("index"))
    if "token" in session:  # already submitted, don't create a duplicate lead
        return redirect(url_for("departments"))

    if request.method == "POST":
        raw = request.form.get("phone", "").strip()
        digits = re.sub(r"\D", "", raw)
        if not 10 <= len(digits) <= 15 or not request.form.get("consent"):
            return render_template(
                "phone.html",
                error="Please enter a valid mobile number and accept the consent box.")

        phone_number = ("+" if raw.startswith("+") else "") + digits
        key = digits[-10:]   # match "9876543210", "+919876543210" as the same person

        conn = get_db()
        existing = conn.execute(
            "SELECT * FROM leads WHERE substr(phone, -10) = ? ORDER BY (offer IS NULL), id LIMIT 1",
            (key,)).fetchone()

        # One attempt per phone number: show the earlier offer instead of a new one.
        if existing and existing["offer"]:
            conn.close()
            session["token"] = existing["token"]
            session["name"] = existing["name"]
            session["department"] = existing["department"]
            session["offer"] = existing["offer"]
            session["already_played"] = True
            return redirect(url_for("offer"))

        if existing:
            # Started before but never picked a department: reuse that lead.
            token = existing["token"]
            conn.execute(
                "UPDATE leads SET name = ?, qualification = ?, score = ?, total = ? WHERE id = ?",
                (session["name"], session["qualification"], session.get("score", 0),
                 len(session["order"]), existing["id"]))
        else:
            token = secrets.token_urlsafe(9)
            conn.execute(
                """INSERT INTO leads (token, name, qualification, phone, score, total, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (token, session["name"], session["qualification"], phone_number,
                 session.get("score", 0), len(session["order"]),
                 datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        session["token"] = token
        return redirect(url_for("departments"))

    return render_template("phone.html")


@app.route("/departments")
def departments():
    if "token" not in session:
        return redirect(url_for("index"))
    if "offer" in session:
        return redirect(url_for("offer"))
    return render_template("departments.html", departments=DEPARTMENTS)


@app.post("/choose-department")
def choose_department():
    if "token" not in session:
        return redirect(url_for("index"))
    if "offer" in session:  # prevent re-rolling the offer
        return redirect(url_for("offer"))

    department = request.form.get("department")
    if department not in DEPARTMENTS:
        abort(400)

    offer = offer_for_score(session.get("score", 0))
    conn = get_db()
    conn.execute("UPDATE leads SET department = ?, offer = ? WHERE token = ?",
                 (department, offer, session["token"]))
    
    # Lead
    lead = conn.execute("SELECT * FROM leads WHERE token = ?", (session["token"],)).fetchone()
    
    conn.commit()
    conn.close()

    # Google Sheet
    if lead:
        payload = {
            "name": lead["name"],
            "qualification": lead["qualification"],
            "phone": lead["phone"],
            "score": lead["score"],
            "total": lead["total"],
            "department": department,
            "offer": offer,
            "created_at": lead["created_at"]
        }
        try:
            requests.post(GOOGLE_SHEET_WEBHOOK_URL, json=payload, timeout=5)
        except Exception as e:
            print("Google Sheet Error:", e)

    session["department"] = department
    session["offer"] = offer
    return redirect(url_for("offer"))

    department = request.form.get("department")
    if department not in DEPARTMENTS:
        abort(400)

    offer = offer_for_score(session.get("score", 0))
    conn = get_db()
    conn.execute("UPDATE leads SET department = ?, offer = ? WHERE token = ?",
                 (department, offer, session["token"]))
    conn.commit()
    conn.close()

    session["department"] = department
    session["offer"] = offer
    return redirect(url_for("offer"))


@app.route("/offer")
def offer():
    if "offer" not in session:
        return redirect(url_for("index"))

    link = SITE_URL or url_for("index", _external=True)
    message = ("I just played the Diwali Tech Challenge and unlocked a special offer! "
               "\U0001FA94 Try it yourself: " + link)
    whatsapp_url = "https://wa.me/?text=" + quote(message)

    return render_template("offer.html", name=session.get("name"),
                           department=session.get("department"),
                           offer=session.get("offer"), whatsapp_url=whatsapp_url,
                           already_played=session.get("already_played", False))

@app.route("/admin")
@require_admin
def admin():
    conn = get_db()
    leads = conn.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin.html", leads=leads)


@app.route("/admin/export")
@require_admin
def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Qualification", "Phone", "Score",
                     "Total", "Department", "Offer", "Created At"])
    for r in rows:
        writer.writerow([r["id"], csv_safe(r["name"]), csv_safe(r["qualification"]),
                         csv_safe(r["phone"]), r["score"], r["total"],
                         csv_safe(r["department"]), csv_safe(r["offer"]), r["created_at"]])

    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=diwali_leads.csv"})


if __name__ == "__main__":
    app.run(debug=not IS_PROD)
