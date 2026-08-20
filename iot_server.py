from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    redirect,
    url_for,
    session,
    flash,
)
import os
import smtplib
from functools import wraps
from email.message import EmailMessage
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import IntegrityError


# ============================================================
# APPLICATION CONFIGURATION
# ============================================================

app = Flask(__name__)

SECRET_KEY = os.getenv("LIBRARY_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("LIBRARY_SECRET_KEY is not configured.")

app.secret_key = SECRET_KEY

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured.")

LOAN_DAYS = 7
FINE_PER_DAY = 5
DEVICE_ID = os.getenv("LIBRARY_DEVICE_ID", "LIBRARY_PI_01")


# ============================================================
# ADMIN LOGIN
# ============================================================

ADMIN_USERNAME = os.getenv("LIBRARY_ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("LIBRARY_ADMIN_PASSWORD")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError(
        "LIBRARY_ADMIN_USERNAME and LIBRARY_ADMIN_PASSWORD "
        "must be configured."
    )


# ============================================================
# EMAIL CONFIGURATION
# ============================================================

EMAIL_ENABLED = (
    os.getenv("LIBRARY_EMAIL_ENABLED", "false").lower() == "true"
)

EMAIL_ADDRESS = os.getenv("LIBRARY_EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("LIBRARY_EMAIL_PASSWORD", "")
SMTP_SERVER = os.getenv("LIBRARY_SMTP_SERVER", "smtp.gmail.com")

try:
    SMTP_PORT = int(os.getenv("LIBRARY_SMTP_PORT", "587"))
except ValueError:
    SMTP_PORT = 587


# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=15,
    )


def initialize_database():
    """Create the PostgreSQL tables if they do not exist."""

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS books (
                book_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                author TEXT,
                available INTEGER NOT NULL DEFAULT 1,
                issued_to TEXT,
                issue_date TIMESTAMP,
                due_date TIMESTAMP,
                return_date TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id BIGSERIAL PRIMARY KEY,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                book_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                fine NUMERIC(10,2) NOT NULL DEFAULT 0,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Add columns if an older PostgreSQL schema already exists.
        migrations = [
            ("books", "available", "INTEGER NOT NULL DEFAULT 1"),
            ("books", "issued_to", "TEXT"),
            ("books", "issue_date", "TIMESTAMP"),
            ("books", "due_date", "TIMESTAMP"),
            ("books", "return_date", "TIMESTAMP"),
            ("transactions", "device_id", "TEXT NOT NULL DEFAULT 'LIBRARY_PI_01'"),
            ("transactions", "action", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("transactions", "book_id", "TEXT NOT NULL DEFAULT ''"),
            ("transactions", "user_id", "TEXT NOT NULL DEFAULT ''"),
            ("transactions", "fine", "NUMERIC(10,2) NOT NULL DEFAULT 0"),
            ("transactions", "timestamp", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        ]

        for table, column, definition in migrations:
            cursor.execute("""
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = %s
                  AND column_name = %s
            """, (table, column))

            if cursor.fetchone() is None:
                cursor.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
                )

        connection.commit()
        cursor.close()

        print("✅ PostgreSQL database initialized")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# Initialize when Gunicorn imports this module.
initialize_database()


# ============================================================
# HELPERS
# ============================================================

def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return wrapper


def format_date(value):
    if not value:
        return "-"

    if isinstance(value, datetime):
        return value.strftime("%d %b %Y, %I:%M %p")

    try:
        return datetime.fromisoformat(str(value)).strftime(
            "%d %b %Y, %I:%M %p"
        )
    except Exception:
        return str(value)


def parse_datetime(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def calculate_fine(due_date, current_date=None):
    due = parse_datetime(due_date)

    if not due:
        return 0, 0

    now = current_date or datetime.now()

    # Fine starts after the 7-day due date.
    if now.date() <= due.date():
        return 0, 0

    late_days = (now.date() - due.date()).days
    return late_days, late_days * FINE_PER_DAY


def row_to_dict(row):
    return dict(row) if row else None


# ============================================================
# EMAIL
# ============================================================

def send_email(to_email, subject, body):
    print(f"📧 Email recipient: {to_email}")
    print(f"📧 Subject: {subject}")

    if not EMAIL_ENABLED:
        print("⚠️ Email system disabled")
        return False

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("❌ Email credentials are missing")
        return False

    if not to_email:
        print("❌ Recipient email is missing")
        return False

    try:
        message = EmailMessage()
        message["From"] = EMAIL_ADDRESS
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(
            SMTP_SERVER,
            SMTP_PORT,
            timeout=20,
        ) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(message)

        print("✅ Email sent")
        return True

    except Exception as error:
        print("❌ Email error:", error)
        return False


# ============================================================
# ISSUE BOOK
# ============================================================

def process_issue(data):
    device_id = data.get("device_id") or DEVICE_ID
    book_id = str(data.get("book_id", "")).strip()
    user_id = str(data.get("user_id", "")).strip()

    if not book_id or not user_id:
        return {
            "success": False,
            "message": "book_id and user_id are required",
        }

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM users
            WHERE user_id = %s
        """, (user_id,))
        user = cursor.fetchone()

        if not user:
            return {
                "success": False,
                "message": "User not found",
            }

        cursor.execute("""
            SELECT *
            FROM books
            WHERE book_id = %s
            FOR UPDATE
        """, (book_id,))
        book = cursor.fetchone()

        if not book:
            return {
                "success": False,
                "message": "Book not found",
            }

        if int(book["available"]) == 0:
            return {
                "success": False,
                "message": "Book is already issued",
            }

        issue_date = datetime.now()
        due_date = issue_date + timedelta(days=LOAN_DAYS)

        cursor.execute("""
            UPDATE books
            SET
                available = 0,
                issued_to = %s,
                issue_date = %s,
                due_date = %s,
                return_date = NULL
            WHERE book_id = %s
        """, (
            user_id,
            issue_date,
            due_date,
            book_id,
        ))

        cursor.execute("""
            INSERT INTO transactions
            (
                device_id,
                action,
                book_id,
                user_id,
                fine,
                timestamp
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            device_id,
            "issue",
            book_id,
            user_id,
            0,
            issue_date,
        ))

        connection.commit()

        issue_text = issue_date.isoformat()
        due_text = due_date.isoformat()

        email_body = f"""
Hello {user["name"]},

Your book has been issued successfully.

Book:
{book["title"]}

Book ID:
{book_id}

Issue Date:
{format_date(issue_date)}

Due Date:
{format_date(due_date)}

Loan Period:
{LOAN_DAYS} days

Fine after due date:
₹{FINE_PER_DAY} per day

Thank you,

Automated Library Management System
"""

        email_sent = send_email(
            user["email"],
            "Book Issued Successfully",
            email_body,
        )

        return {
            "success": True,
            "message": "Book issued successfully",
            "book_id": book_id,
            "user_id": user_id,
            "issue_date": issue_text,
            "due_date": due_text,
            "fine_per_day": FINE_PER_DAY,
            "email_sent": email_sent,
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# RETURN BOOK
# ============================================================

def process_return(data):
    device_id = data.get("device_id") or DEVICE_ID
    book_id = str(data.get("book_id", "")).strip()
    user_id = str(data.get("user_id", "")).strip()

    if not book_id or not user_id:
        return {
            "success": False,
            "message": "book_id and user_id are required",
        }

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM users
            WHERE user_id = %s
        """, (user_id,))
        user = cursor.fetchone()

        if not user:
            return {
                "success": False,
                "message": "User not found",
            }

        cursor.execute("""
            SELECT *
            FROM books
            WHERE book_id = %s
            FOR UPDATE
        """, (book_id,))
        book = cursor.fetchone()

        if not book:
            return {
                "success": False,
                "message": "Book not found",
            }

        if int(book["available"]) == 1:
            return {
                "success": False,
                "message": "Book is already available",
            }

        if book["issued_to"] != user_id:
            return {
                "success": False,
                "message": "Book was issued to another user",
            }

        return_date = datetime.now()
        late_days, fine = calculate_fine(
            book["due_date"],
            return_date,
        )

        original_due_date = book["due_date"]
        return_text = return_date.isoformat()

        cursor.execute("""
            UPDATE books
            SET
                available = 1,
                issued_to = NULL,
                issue_date = NULL,
                due_date = NULL,
                return_date = %s
            WHERE book_id = %s
        """, (
            return_date,
            book_id,
        ))

        cursor.execute("""
            INSERT INTO transactions
            (
                device_id,
                action,
                book_id,
                user_id,
                fine,
                timestamp
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            device_id,
            "return",
            book_id,
            user_id,
            fine,
            return_date,
        ))

        connection.commit()

        email_body = f"""
Hello {user["name"]},

Your book has been returned successfully.

Book:
{book["title"]}

Book ID:
{book_id}

Due Date:
{format_date(original_due_date)}

Return Date:
{format_date(return_date)}

Days Late:
{late_days}

Fine:
₹{fine}

Fine Rate:
₹{FINE_PER_DAY} per day

Thank you,

Automated Library Management System
"""

        email_sent = send_email(
            user["email"],
            "Book Returned Successfully",
            email_body,
        )

        return {
            "success": True,
            "message": "Book returned successfully",
            "book_id": book_id,
            "user_id": user_id,
            "return_date": return_text,
            "late_days": late_days,
            "fine": fine,
            "email_sent": email_sent,
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# LOGIN
# ============================================================

LOGIN_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALMS | Admin Login</title>
<style>
*{box-sizing:border-box}
body{
    margin:0;
    min-height:100vh;
    display:flex;
    align-items:center;
    justify-content:center;
    font-family:Arial,sans-serif;
    background:linear-gradient(135deg,#eef2ff,#f8fafc);
}
.card{
    width:420px;
    max-width:92%;
    background:#fff;
    padding:34px;
    border-radius:20px;
    box-shadow:0 20px 60px rgba(15,23,42,.12);
}
.logo{
    width:58px;height:58px;border-radius:16px;
    display:flex;align-items:center;justify-content:center;
    background:#4f46e5;color:#fff;font-size:28px;
    margin-bottom:16px;
}
h1{margin:0;color:#111827}
.sub{color:#6b7280;margin:8px 0 24px;font-size:13px}
label{display:block;font-size:12px;font-weight:700;margin:14px 0 6px}
input{
    width:100%;padding:12px;border:1px solid #dbe1ea;
    border-radius:10px;font-size:14px;outline:none;
}
input:focus{border-color:#4f46e5}
button{
    width:100%;margin-top:20px;padding:13px;
    border:0;border-radius:10px;background:#4f46e5;
    color:white;font-weight:700;cursor:pointer;
}
.error{
    padding:10px;border-radius:9px;background:#fee2e2;
    color:#b91c1c;font-size:12px;margin-bottom:12px;
}
</style>
</head>
<body>
<div class="card">
<div class="logo">📚</div>
<h1>ALMS Admin</h1>
<div class="sub">Automated Library Management System</div>
{% with messages=get_flashed_messages() %}
{% for message in messages %}
<div class="error">{{ message }}</div>
{% endfor %}
{% endwith %}
<form method="POST">
<label>Username</label>
<input name="username" required autocomplete="username">
<label>Password</label>
<input type="password" name="password" required autocomplete="current-password">
<button type="submit">Login to Dashboard</button>
</form>
</div>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("dashboard"))

        flash("Invalid username or password.")

    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# DASHBOARD
# ============================================================

DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALMS | Admin Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{box-sizing:border-box}
:root{
 --bg:#f4f7fb;--sidebar:#111827;--primary:#4f46e5;
 --text:#111827;--muted:#6b7280;--border:#e5e7eb;
 --danger:#dc2626;--success:#16a34a;
}
body{margin:0;font-family:Arial,sans-serif;background:var(--bg);color:var(--text)}
.sidebar{
 position:fixed;left:0;top:0;bottom:0;width:240px;
 background:var(--sidebar);color:#fff;padding:22px 15px;
}
.brand{font-size:22px;font-weight:800;padding:8px 12px 30px}
.brand small{display:block;font-size:10px;color:#9ca3af;margin-top:4px}
.nav-title{font-size:10px;color:#9ca3af;margin:14px 10px 7px}
.nav{
 display:block;color:#d1d5db;text-decoration:none;
 padding:11px 12px;border-radius:9px;font-size:13px;margin-bottom:4px;
}
.nav:hover,.nav.active{background:#1f2937;color:#fff}
.main{margin-left:240px;min-height:100vh}
.topbar{
 height:76px;background:#fff;border-bottom:1px solid var(--border);
 display:flex;align-items:center;justify-content:space-between;padding:0 28px;
}
.title{font-size:20px;font-weight:800}.sub{font-size:11px;color:var(--muted);margin-top:4px}
.admin{font-size:12px}
.content{padding:25px}
.flash{padding:11px 14px;background:#ecfdf5;color:#047857;border-radius:9px;margin-bottom:18px}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:15px;margin-bottom:20px}
.stat,.panel,.chart{
 background:#fff;border:1px solid var(--border);border-radius:15px;
 box-shadow:0 7px 25px rgba(15,23,42,.05);
}
.stat{padding:18px}.stat-label{font-size:10px;color:var(--muted);font-weight:700}
.stat-number{font-size:26px;font-weight:800;margin-top:7px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:20px}
.chart{padding:18px;height:320px}.chart h3{margin:0;font-size:14px}
.chart-box{height:255px}
.forms{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:20px}
.panel{overflow:hidden;margin-bottom:20px}
.panel-head{
 padding:17px 20px;border-bottom:1px solid var(--border);
 display:flex;align-items:center;justify-content:space-between;gap:15px;
}
.panel-head h3{margin:0;font-size:15px}.hint{font-size:10px;color:var(--muted);margin-top:4px}
.panel-body{padding:20px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.full{grid-column:1/-1}
label{font-size:10px;font-weight:800;display:block;margin-bottom:5px}
input{
 width:100%;padding:10px;border:1px solid var(--border);
 border-radius:8px;outline:none
}
input:focus{border-color:var(--primary)}
.btn{
 border:0;border-radius:8px;padding:9px 12px;
 background:var(--primary);color:#fff;font-weight:700;cursor:pointer;
 text-decoration:none;font-size:11px;display:inline-block
}
.btn-danger{background:#fee2e2;color:#b91c1c}
.btn-secondary{background:#f3f4f6;color:#374151}
.btn-full{width:100%;margin-top:14px}
.search{max-width:230px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:850px}
th{
 text-align:left;background:#f9fafb;color:#6b7280;
 padding:11px 15px;font-size:10px;text-transform:uppercase
}
td{padding:12px 15px;border-top:1px solid #f1f3f5;font-size:12px}
.badge{padding:5px 8px;border-radius:20px;font-size:10px;font-weight:800}
.available{background:#dcfce7;color:#15803d}
.issued{background:#fef3c7;color:#b45309}
.overdue{background:#fee2e2;color:#b91c1c}
.fine{color:#dc2626;font-weight:800}
.actions{display:flex;gap:6px}
@media(max-width:1100px){.stats{grid-template-columns:repeat(3,1fr)}}
@media(max-width:850px){
 .sidebar{width:65px}.brand{font-size:0}.brand small,.nav span{display:none}
 .nav{text-align:center}.main{margin-left:65px}.forms,.grid2{grid-template-columns:1fr}
}
@media(max-width:650px){
 .content{padding:15px}.stats{grid-template-columns:1fr 1fr}
 .form-grid{grid-template-columns:1fr}.full{grid-column:auto}
 .topbar{padding:0 15px}
}
</style>
</head>
<body>

<aside class="sidebar">
<div class="brand">📚 ALMS<small>Automated Library</small></div>
<div class="nav-title">MAIN</div>
<a class="nav active" href="/dashboard">📊 <span>Dashboard</span></a>
<a class="nav" href="#users">👥 <span>Users</span></a>
<a class="nav" href="#books">📚 <span>Books</span></a>
<a class="nav" href="#transactions">📋 <span>Transactions</span></a>
<div class="nav-title">SYSTEM</div>
<a class="nav" href="/api/books" target="_blank">🔗 <span>Book API</span></a>
<a class="nav" href="/api/users" target="_blank">👤 <span>User API</span></a>
<a class="nav" href="/logout">🚪 <span>Logout</span></a>
</aside>

<main class="main">
<header class="topbar">
<div><div class="title">Library Dashboard</div><div class="sub">Automated Library Management System</div></div>
<div class="admin">Administrator · IoT Control Panel</div>
</header>

<div class="content">

{% with messages=get_flashed_messages() %}
{% for message in messages %}
<div class="flash">✅ {{ message }}</div>
{% endfor %}
{% endwith %}

<section class="stats">
<div class="stat"><div class="stat-label">TOTAL USERS</div><div class="stat-number">{{ total_users }}</div></div>
<div class="stat"><div class="stat-label">TOTAL BOOKS</div><div class="stat-number">{{ total_books }}</div></div>
<div class="stat"><div class="stat-label">AVAILABLE</div><div class="stat-number">{{ available_books }}</div></div>
<div class="stat"><div class="stat-label">ISSUED</div><div class="stat-number">{{ issued_books }}</div></div>
<div class="stat"><div class="stat-label">ACTIVE FINES</div><div class="stat-number">₹{{ total_fines }}</div></div>
</section>

<section class="grid2">
<div class="chart">
<h3>📚 Library Status</h3>
<div class="chart-box"><canvas id="libraryChart"></canvas></div>
</div>
<div class="chart">
<h3>📊 Transactions</h3>
<div class="chart-box"><canvas id="transactionChart"></canvas></div>
</div>
</section>

<section class="forms">

<div class="panel">
<div class="panel-head"><div><h3>👤 Register New User</h3><div class="hint">Create a library member</div></div></div>
<div class="panel-body">
<form method="POST" action="/dashboard/register-user">
<div class="form-grid">
<div><label>USER ID</label><input name="user_id" placeholder="USER103" required></div>
<div><label>NAME</label><input name="name" placeholder="Student Name" required></div>
<div class="full"><label>EMAIL</label><input type="email" name="email" placeholder="student@gmail.com" required></div>
</div>
<button class="btn btn-full" type="submit">+ Register User</button>
</form>
</div>
</div>

<div class="panel">
<div class="panel-head"><div><h3>📚 Register New Book</h3><div class="hint">Add a library book</div></div></div>
<div class="panel-body">
<form method="POST" action="/dashboard/register-book">
<div class="form-grid">
<div><label>BOOK ID</label><input name="book_id" placeholder="BOOK006" required></div>
<div><label>AUTHOR</label><input name="author" placeholder="Author"></div>
<div class="full"><label>TITLE</label><input name="title" placeholder="Machine Learning" required></div>
</div>
<button class="btn btn-full" type="submit">+ Register Book</button>
</form>
</div>
</div>

</section>

<section class="panel" id="users">
<div class="panel-head">
<div><h3>👥 User Management</h3><div class="hint">Register, edit and remove members</div></div>
<input class="search" id="userSearch" placeholder="🔎 Search users...">
</div>
<div class="table-wrap">
<table id="usersTable">
<thead><tr><th>User ID</th><th>Name</th><th>Email</th><th>Action</th></tr></thead>
<tbody>
{% for user in users %}
<tr>
<td><b>{{ user.user_id }}</b></td><td>{{ user.name }}</td><td>{{ user.email }}</td>
<td><div class="actions">
<a class="btn btn-secondary" href="/dashboard/edit-user/{{ user.user_id }}">Edit</a>
<form method="POST" action="/dashboard/delete-user/{{ user.user_id }}" onsubmit="return confirm('Delete this user?')">
<button class="btn btn-danger" type="submit">Delete</button>
</form>
</div></td>
</tr>
{% else %}
<tr><td colspan="4" style="text-align:center">No users found.</td></tr>
{% endfor %}
</tbody>
</table>
</div>
</section>

<section class="panel" id="books">
<div class="panel-head">
<div><h3>📚 Book Management</h3><div class="hint">Monitor books, due dates and fines</div></div>
<input class="search" id="bookSearch" placeholder="🔎 Search books...">
</div>
<div class="table-wrap">
<table id="booksTable">
<thead><tr><th>Book</th><th>Author</th><th>Status</th><th>Issued To</th><th>Due Date</th><th>Fine</th><th>Action</th></tr></thead>
<tbody>
{% for book in books %}
<tr>
<td><b>{{ book.title }}</b><div style="color:#6b7280">{{ book.book_id }}</div></td>
<td>{{ book.author or "-" }}</td>
<td>
{% if book.available == 1 %}
<span class="badge available">Available</span>
{% elif book.current_fine > 0 %}
<span class="badge overdue">Overdue</span>
{% else %}
<span class="badge issued">Issued</span>
{% endif %}
</td>
<td>{{ book.issued_to or "-" }}</td>
<td>{{ format_date(book.due_date) }}</td>
<td>
{% if book.current_fine > 0 %}
<span class="fine">₹{{ book.current_fine }}</span>
<div style="color:#6b7280">{{ book.late_days }} day(s) late</div>
{% else %}₹0{% endif %}
</td>
<td>
<div class="actions">
<a class="btn btn-secondary" href="/dashboard/edit-book/{{ book.book_id }}">Edit</a>
{% if book.available == 1 %}
<form method="POST" action="/dashboard/delete-book/{{ book.book_id }}" onsubmit="return confirm('Delete this book?')">
<button class="btn btn-danger" type="submit">Delete</button>
</form>
{% endif %}
</div>
</td>
</tr>
{% else %}
<tr><td colspan="7" style="text-align:center">No books found.</td></tr>
{% endfor %}
</tbody>
</table>
</div>
</section>

<section class="panel" id="transactions">
<div class="panel-head">
<div><h3>📋 Transaction History</h3><div class="hint">Latest issue and return activity</div></div>
<input class="search" id="transactionSearch" placeholder="🔎 Search...">
</div>
<div class="table-wrap">
<table id="transactionsTable">
<thead><tr><th>ID</th><th>Action</th><th>Book</th><th>User</th><th>Fine</th><th>Date</th></tr></thead>
<tbody>
{% for transaction in transactions %}
<tr>
<td>#{{ transaction.id }}</td>
<td>
{% if transaction.action == "issue" %}
<span class="badge issued">Issue</span>
{% else %}
<span class="badge available">Return</span>
{% endif %}
</td>
<td>{{ transaction.book_id }}</td>
<td>{{ transaction.user_id }}</td>
<td>{% if transaction.fine > 0 %}<span class="fine">₹{{ transaction.fine }}</span>{% else %}₹0{% endif %}</td>
<td>{{ format_date(transaction.timestamp) }}</td>
</tr>
{% else %}
<tr><td colspan="6" style="text-align:center">No transactions found.</td></tr>
{% endfor %}
</tbody>
</table>
</div>
</section>

</div>
</main>

<script>
function searchTable(inputId, tableId){
    const input=document.getElementById(inputId);
    const table=document.getElementById(tableId);
    if(!input || !table) return;
    input.addEventListener("input", function(){
        const q=this.value.toLowerCase();
        table.querySelectorAll("tbody tr").forEach(row=>{
            row.style.display=row.innerText.toLowerCase().includes(q) ? "" : "none";
        });
    });
}
searchTable("userSearch","usersTable");
searchTable("bookSearch","booksTable");
searchTable("transactionSearch","transactionsTable");

new Chart(document.getElementById("libraryChart"),{
    type:"doughnut",
    data:{
        labels:["Available","Issued"],
        datasets:[{data:[{{ available_books }},{{ issued_books }}]}]
    },
    options:{responsive:true,maintainAspectRatio:false}
});

new Chart(document.getElementById("transactionChart"),{
    type:"bar",
    data:{
        labels:["Issues","Returns"],
        datasets:[{label:"Transactions",data:[{{ issue_count }},{{ return_count }}]}]
    },
    options:{responsive:true,maintainAspectRatio:false,scales:{y:{beginAtZero:true,ticks:{precision:0}}}}
});
</script>
</body>
</html>
"""


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM users
            ORDER BY user_id
        """)
        users = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT *
            FROM books
            ORDER BY book_id
        """)
        raw_books = cursor.fetchall()

        books = []

        for row in raw_books:
            book = dict(row)
            if int(book["available"]) == 0:
                late_days, fine = calculate_fine(book["due_date"])
            else:
                late_days, fine = 0, 0

            book["late_days"] = late_days
            book["current_fine"] = fine
            books.append(book)

        cursor.execute("""
            SELECT *
            FROM transactions
            ORDER BY id DESC
            LIMIT 100
        """)
        transactions = [dict(row) for row in cursor.fetchall()]

    finally:
        connection.close()

    total_users = len(users)
    total_books = len(books)
    available_books = sum(1 for book in books if int(book["available"]) == 1)
    issued_books = sum(1 for book in books if int(book["available"]) == 0)
    total_fines = sum(book["current_fine"] for book in books)

    issue_count = sum(
        1 for transaction in transactions
        if transaction["action"] == "issue"
    )

    return_count = sum(
        1 for transaction in transactions
        if transaction["action"] == "return"
    )

    return render_template_string(
        DASHBOARD_HTML,
        users=users,
        books=books,
        transactions=transactions,
        total_users=total_users,
        total_books=total_books,
        available_books=available_books,
        issued_books=issued_books,
        total_fines=total_fines,
        issue_count=issue_count,
        return_count=return_count,
        format_date=format_date,
    )


# ============================================================
# USER MANAGEMENT
# ============================================================

@app.route("/dashboard/register-user", methods=["POST"])
@login_required
def register_user():
    user_id = request.form.get("user_id", "").strip()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()

    if not user_id or not name or not email:
        flash("All user fields are required.")
        return redirect(url_for("dashboard") + "#users")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO users (user_id, name, email)
            VALUES (%s, %s, %s)
        """, (user_id, name, email))

        connection.commit()
        flash(f"User {user_id} registered successfully.")

    except IntegrityError:
        connection.rollback()
        flash(f"User ID {user_id} already exists.")

    finally:
        connection.close()

    return redirect(url_for("dashboard") + "#users")


EDIT_USER_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Edit User | ALMS</title>
<style>
body{margin:0;background:#f4f7fb;font-family:Arial;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#fff;width:430px;max-width:92%;padding:30px;border-radius:16px;box-shadow:0 15px 40px rgba(0,0,0,.1)}
h2{margin-top:0}label{display:block;font-size:12px;font-weight:bold;margin:15px 0 6px}
input{width:100%;padding:11px;border:1px solid #ddd;border-radius:8px;box-sizing:border-box}
button{width:100%;padding:12px;margin-top:20px;border:0;border-radius:8px;background:#4f46e5;color:#fff;font-weight:bold}
a{display:block;text-align:center;margin-top:15px;color:#4f46e5;text-decoration:none}
</style>
</head>
<body>
<div class="card">
<h2>👤 Edit User</h2>
<form method="POST">
<label>User ID</label>
<input value="{{ user.user_id }}" disabled>
<label>Name</label>
<input name="name" value="{{ user.name }}" required>
<label>Email</label>
<input type="email" name="email" value="{{ user.email }}" required>
<button type="submit">Save Changes</button>
</form>
<a href="/dashboard#users">← Back to Dashboard</a>
</div>
</body>
</html>
"""


@app.route("/dashboard/edit-user/<user_id>", methods=["GET", "POST"])
@login_required
def edit_user(user_id):
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()

            if not name or not email:
                flash("Name and email are required.")
                return redirect(url_for("edit_user", user_id=user_id))

            cursor.execute("""
                UPDATE users
                SET name = %s, email = %s
                WHERE user_id = %s
            """, (name, email, user_id))

            connection.commit()
            flash(f"User {user_id} updated successfully.")
            return redirect(url_for("dashboard") + "#users")

        cursor.execute("""
            SELECT *
            FROM users
            WHERE user_id = %s
        """, (user_id,))

        user = cursor.fetchone()

    finally:
        connection.close()

    if not user:
        flash("User not found.")
        return redirect(url_for("dashboard"))

    return render_template_string(
        EDIT_USER_HTML,
        user=dict(user),
    )


@app.route("/dashboard/delete-user/<user_id>", methods=["POST"])
@login_required
def delete_user(user_id):
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT COUNT(*) AS count
            FROM books
            WHERE issued_to = %s
        """, (user_id,))

        issued_count = int(cursor.fetchone()["count"])

        if issued_count > 0:
            flash("Cannot delete user because they have an issued book.")
            return redirect(url_for("dashboard") + "#users")

        cursor.execute("""
            DELETE FROM users
            WHERE user_id = %s
        """, (user_id,))

        connection.commit()
        flash(f"User {user_id} deleted.")

    finally:
        connection.close()

    return redirect(url_for("dashboard") + "#users")


# ============================================================
# BOOK MANAGEMENT
# ============================================================

@app.route("/dashboard/register-book", methods=["POST"])
@login_required
def register_book():
    book_id = request.form.get("book_id", "").strip()
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()

    if not book_id or not title:
        flash("Book ID and title are required.")
        return redirect(url_for("dashboard") + "#books")

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO books
            (
                book_id,
                title,
                author,
                available
            )
            VALUES (%s, %s, %s, 1)
        """, (book_id, title, author))

        connection.commit()
        flash(f"Book {book_id} registered successfully.")

    except IntegrityError:
        connection.rollback()
        flash(f"Book ID {book_id} already exists.")

    finally:
        connection.close()

    return redirect(url_for("dashboard") + "#books")


EDIT_BOOK_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Edit Book | ALMS</title>
<style>
body{margin:0;background:#f4f7fb;font-family:Arial;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#fff;width:430px;max-width:92%;padding:30px;border-radius:16px;box-shadow:0 15px 40px rgba(0,0,0,.1)}
h2{margin-top:0}label{display:block;font-size:12px;font-weight:bold;margin:15px 0 6px}
input{width:100%;padding:11px;border:1px solid #ddd;border-radius:8px;box-sizing:border-box}
button{width:100%;padding:12px;margin-top:20px;border:0;border-radius:8px;background:#4f46e5;color:#fff;font-weight:bold}
a{display:block;text-align:center;margin-top:15px;color:#4f46e5;text-decoration:none}
.warning{background:#fff7ed;color:#9a3412;padding:10px;border-radius:8px;font-size:11px;margin-top:15px}
</style>
</head>
<body>
<div class="card">
<h2>📚 Edit Book</h2>
<form method="POST">
<label>Book ID</label>
<input value="{{ book.book_id }}" disabled>
<label>Title</label>
<input name="title" value="{{ book.title }}" required>
<label>Author</label>
<input name="author" value="{{ book.author or '' }}">
<button type="submit">Save Changes</button>
</form>
{% if book.available == 0 %}
<div class="warning">⚠️ This book is currently issued.</div>
{% endif %}
<a href="/dashboard#books">← Back to Dashboard</a>
</div>
</body>
</html>
"""


@app.route("/dashboard/edit-book/<book_id>", methods=["GET", "POST"])
@login_required
def edit_book(book_id):
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            author = request.form.get("author", "").strip()

            if not title:
                flash("Title is required.")
                return redirect(url_for("edit_book", book_id=book_id))

            cursor.execute("""
                UPDATE books
                SET title = %s, author = %s
                WHERE book_id = %s
            """, (title, author, book_id))

            connection.commit()
            flash(f"Book {book_id} updated successfully.")
            return redirect(url_for("dashboard") + "#books")

        cursor.execute("""
            SELECT *
            FROM books
            WHERE book_id = %s
        """, (book_id,))

        book = cursor.fetchone()

    finally:
        connection.close()

    if not book:
        flash("Book not found.")
        return redirect(url_for("dashboard"))

    return render_template_string(
        EDIT_BOOK_HTML,
        book=dict(book),
    )


@app.route("/dashboard/delete-book/<book_id>", methods=["POST"])
@login_required
def delete_book(book_id):
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM books
            WHERE book_id = %s
        """, (book_id,))

        book = cursor.fetchone()

        if not book:
            flash("Book not found.")
            return redirect(url_for("dashboard") + "#books")

        if int(book["available"]) == 0:
            flash("Cannot delete an issued book.")
            return redirect(url_for("dashboard") + "#books")

        cursor.execute("""
            DELETE FROM books
            WHERE book_id = %s
        """, (book_id,))

        connection.commit()
        flash(f"Book {book_id} deleted.")

    finally:
        connection.close()

    return redirect(url_for("dashboard") + "#books")


# ============================================================
# IOT API
# ============================================================

@app.route("/api/update", methods=["POST"])
def receive_data():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "message": "No JSON data received",
        }), 400

    action = str(data.get("action", "")).lower().strip()

    print("\n========================================")
    print("📡 IoT DATA RECEIVED")
    print("Device ID:", data.get("device_id"))
    print("Action:", action)
    print("Book ID:", data.get("book_id"))
    print("User ID:", data.get("user_id"))
    print("========================================")

    try:
        if action == "issue":
            result = process_issue(data)
        elif action == "return":
            result = process_return(data)
        else:
            result = {
                "success": False,
                "message": "Unknown action",
            }

        return jsonify(result)

    except Exception as error:
        app.logger.exception("IoT API error")
        return jsonify({
            "success": False,
            "message": "Server error while processing request",
            "error": str(error),
        }), 500


# ============================================================
# READ APIs
# ============================================================

@app.route("/api/books", methods=["GET"])
def get_books():
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM books
            ORDER BY book_id
        """)

        rows = cursor.fetchall()

        result = []

        for row in rows:
            book = dict(row)

            if int(book["available"]) == 0:
                late_days, fine = calculate_fine(book["due_date"])
            else:
                late_days, fine = 0, 0

            book["late_days"] = late_days
            book["current_fine"] = fine
            result.append(book)

        return jsonify(result)

    finally:
        connection.close()


@app.route("/api/users", methods=["GET"])
def get_users():
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM users
            ORDER BY user_id
        """)

        return jsonify([
            dict(row)
            for row in cursor.fetchall()
        ])

    finally:
        connection.close()


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM transactions
            ORDER BY id DESC
            LIMIT 500
        """)

        return jsonify([
            dict(row)
            for row in cursor.fetchall()
        ])

    finally:
        connection.close()


# ============================================================
# TEST OVERDUE API
# ============================================================

@app.route("/api/test/make-overdue/<book_id>/<int:days>", methods=["POST"])
@login_required
def make_book_overdue(book_id, days):
    if days < 1:
        return jsonify({
            "success": False,
            "message": "Days must be at least 1",
        }), 400

    connection = get_db_connection()

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT *
            FROM books
            WHERE book_id = %s
        """, (book_id,))

        book = cursor.fetchone()

        if not book:
            return jsonify({
                "success": False,
                "message": "Book not found",
            }), 404

        if int(book["available"]) == 1:
            return jsonify({
                "success": False,
                "message": "Book must be issued first",
            }), 400

        new_due_date = datetime.now() - timedelta(days=days)

        cursor.execute("""
            UPDATE books
            SET due_date = %s
            WHERE book_id = %s
        """, (new_due_date, book_id))

        connection.commit()

        return jsonify({
            "success": True,
            "message": "Book made overdue",
            "book_id": book_id,
            "new_due_date": new_due_date.isoformat(),
            "simulated_late_days": days,
        })

    finally:
        connection.close()


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    connection = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()

        return jsonify({
            "success": True,
            "status": "online",
            "database": "postgresql",
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "status": "database_error",
            "error": str(error),
        }), 500

    finally:
        if connection:
            connection.close()


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    if session.get("admin_logged_in"):
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# ============================================================
# LOCAL START
# ============================================================

if __name__ == "__main__":
    print("========================================")
    print("📚 AUTOMATED LIBRARY MANAGEMENT SYSTEM")
    print("========================================")
    print("Database: PostgreSQL")
    print("Loan Period:", LOAN_DAYS, "days")
    print("Fine: ₹", FINE_PER_DAY, "per day")
    print("Email:", "Enabled" if EMAIL_ENABLED else "Disabled")
    print("========================================")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=True,
    )
