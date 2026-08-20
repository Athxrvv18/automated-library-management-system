from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    redirect,
    url_for,
    session,
    flash
)

import sqlite3
import os
import smtplib
from functools import wraps
from email.message import EmailMessage
from datetime import datetime, timedelta


# ============================================================
# APPLICATION CONFIGURATION
# ============================================================

app = Flask(__name__)

APP_SECRET = os.getenv("LIBRARY_SECRET_KEY")

if not APP_SECRET:
    raise RuntimeError(
        "LIBRARY_SECRET_KEY is not configured. "
        "Set it before starting the server."
    )

app.secret_key = APP_SECRET

DATABASE = "library.db"

LOAN_DAYS = 7
FINE_PER_DAY = 5

DEVICE_ID = "LIBRARY_PI_01"


# ============================================================
# ADMIN LOGIN
# ============================================================

ADMIN_USERNAME = os.getenv("LIBRARY_ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("LIBRARY_ADMIN_PASSWORD")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError(
        "LIBRARY_ADMIN_USERNAME and LIBRARY_ADMIN_PASSWORD "
        "must be configured before starting the server."
    )


# ============================================================
# EMAIL CONFIGURATION
# ============================================================

EMAIL_ENABLED = os.getenv(
    "LIBRARY_EMAIL_ENABLED",
    "false"
).lower() == "true"

EMAIL_ADDRESS = os.getenv(
    "LIBRARY_EMAIL_ADDRESS",
    ""
)

EMAIL_PASSWORD = os.getenv(
    "LIBRARY_EMAIL_PASSWORD",
    ""
)

SMTP_SERVER = os.getenv(
    "LIBRARY_SMTP_SERVER",
    "smtp.gmail.com"
)

SMTP_PORT = int(
    os.getenv(
        "LIBRARY_SMTP_PORT",
        "587"
    )
)


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db_connection():

    connection = sqlite3.connect(
        DATABASE
    )

    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# DATABASE MIGRATION HELPERS
# ============================================================

def get_table_columns(cursor, table_name):

    cursor.execute(
        f"PRAGMA table_info({table_name})"
    )

    columns = cursor.fetchall()

    return [
        column["name"]
        for column in columns
    ]


def add_column_if_missing(
    cursor,
    table_name,
    column_name,
    column_definition
):

    columns = get_table_columns(
        cursor,
        table_name
    )

    if column_name not in columns:

        cursor.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN {column_name}
            {column_definition}
            """
        )

        print(
            f"✅ Added missing column "
            f"{table_name}.{column_name}"
        )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def initialize_database():

    connection = get_db_connection()

    cursor = connection.cursor()

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL
        )
    """)

    # --------------------------------------------------------
    # BOOKS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS books (
            book_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT,
            available INTEGER DEFAULT 1,
            issued_to TEXT,
            issue_date TEXT,
            due_date TEXT,
            return_date TEXT
        )
    """)

    # --------------------------------------------------------
    # TRANSACTIONS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            action TEXT NOT NULL,
            book_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            fine REAL DEFAULT 0,
            timestamp TEXT NOT NULL
        )
    """)

    # --------------------------------------------------------
    # MIGRATE OLD DATABASE
    # --------------------------------------------------------

    # This protects us if library.db was created using
    # an older version without the new columns.

    add_column_if_missing(
        cursor,
        "books",
        "available",
        "INTEGER DEFAULT 1"
    )

    add_column_if_missing(
        cursor,
        "books",
        "issued_to",
        "TEXT"
    )

    add_column_if_missing(
        cursor,
        "books",
        "issue_date",
        "TEXT"
    )

    add_column_if_missing(
        cursor,
        "books",
        "due_date",
        "TEXT"
    )

    add_column_if_missing(
        cursor,
        "books",
        "return_date",
        "TEXT"
    )

    add_column_if_missing(
        cursor,
        "transactions",
        "device_id",
        "TEXT DEFAULT 'LIBRARY_PI_01'"
    )

    add_column_if_missing(
        cursor,
        "transactions",
        "action",
        "TEXT DEFAULT 'unknown'"
    )

    add_column_if_missing(
        cursor,
        "transactions",
        "book_id",
        "TEXT DEFAULT ''"
    )

    add_column_if_missing(
        cursor,
        "transactions",
        "user_id",
        "TEXT DEFAULT ''"
    )

    add_column_if_missing(
        cursor,
        "transactions",
        "fine",
        "REAL DEFAULT 0"
    )

    add_column_if_missing(
        cursor,
        "transactions",
        "timestamp",
        "TEXT DEFAULT ''"
    )

    connection.commit()

    connection.close()

    print("✅ Database initialized and migrated")


# ============================================================
# SAMPLE DATA
# ============================================================

def add_sample_data():

    connection = get_db_connection()

    cursor = connection.cursor()

    # --------------------------------------------------------
    # NOTE:
    # Existing users are NOT overwritten.
    # This prevents us from changing your working email data.
    # --------------------------------------------------------

    users = [
        (
            "USER101",
            "Library User",
            "your-email@example.com"
        ),
        (
            "USER102",
            "Test Student",
            "your-second-email@example.com"
        )
    ]

    for user in users:

        cursor.execute("""
            INSERT OR IGNORE INTO users
            (
                user_id,
                name,
                email
            )
            VALUES (?, ?, ?)
        """, user)

    books = [
        (
            "BOOK001",
            "Python Programming",
            "John Smith"
        ),
        (
            "BOOK002",
            "Data Structures",
            "Robert Brown"
        ),
        (
            "BOOK003",
            "Database Systems",
            "David Miller"
        ),
        (
            "BOOK004",
            "Computer Networks",
            "James Wilson"
        ),
        (
            "BOOK005",
            "Operating Systems",
            "Michael Davis"
        )
    ]

    for book in books:

        cursor.execute("""
            INSERT OR IGNORE INTO books
            (
                book_id,
                title,
                author
            )
            VALUES (?, ?, ?)
        """, book)

    connection.commit()

    connection.close()


# ============================================================
# DATE FORMAT
# ============================================================

def format_date(date_string):

    if not date_string:

        return "-"

    try:

        date_object = datetime.fromisoformat(
            date_string
        )

        return date_object.strftime(
            "%d %b %Y, %I:%M %p"
        )

    except Exception:

        return date_string


# ============================================================
# FINE CALCULATION
# ============================================================

def calculate_fine(
    due_date_string,
    current_date=None
):

    if not due_date_string:

        return 0, 0

    try:

        due_date = datetime.fromisoformat(
            due_date_string
        )

    except Exception:

        return 0, 0

    if current_date is None:

        current_date = datetime.now()

    if current_date.date() <= due_date.date():

        return 0, 0

    late_days = (
        current_date.date()
        - due_date.date()
    ).days

    fine = late_days * FINE_PER_DAY

    return late_days, fine


# ============================================================
# EMAIL
# ============================================================

def send_email(
    to_email,
    subject,
    body
):

    print("\n📧 Preparing email...")

    print(
        "Recipient:",
        to_email
    )

    print(
        "Subject:",
        subject
    )

    if not EMAIL_ENABLED:

        print(
            "⚠️ Email system disabled"
        )

        return False

    if not EMAIL_ADDRESS:

        print(
            "❌ Sender email missing"
        )

        return False

    if not EMAIL_PASSWORD:

        print(
            "❌ Gmail App Password missing"
        )

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
            timeout=20
        ) as server:

            server.ehlo()

            server.starttls()

            server.ehlo()

            server.login(
                EMAIL_ADDRESS,
                EMAIL_PASSWORD
            )

            server.send_message(
                message
            )

        print(
            "✅ Email sent successfully"
        )

        return True

    except smtplib.SMTPAuthenticationError:

        print(
            "❌ Gmail authentication failed"
        )

        return False

    except Exception as error:

        print(
            "❌ Email sending failed:",
            error
        )

        return False


# ============================================================
# ADMIN LOGIN DECORATOR
# ============================================================

def login_required(function):

    @wraps(function)
    def decorated_function(
        *args,
        **kwargs
    ):

        if not session.get(
            "admin_logged_in"
        ):

            return redirect(
                url_for("login")
            )

        return function(
            *args,
            **kwargs
        )

    return decorated_function


# ============================================================
# LOGIN PAGE
# ============================================================

LOGIN_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>ALMS | Admin Login</title>


<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    min-height: 100vh;

    display: flex;

    align-items: center;

    justify-content: center;

    font-family:
        Arial,
        sans-serif;

    background:
        linear-gradient(
            135deg,
            #111827,
            #312e81
        );

}

.login-container {

    width: 100%;

    max-width: 430px;

    padding: 20px;

}

.login-card {

    background: white;

    border-radius: 22px;

    padding: 40px;

    box-shadow:
        0 25px 70px
        rgba(0,0,0,0.3);

}

.logo {

    width: 65px;

    height: 65px;

    margin: 0 auto 20px;

    display: flex;

    align-items: center;

    justify-content: center;

    border-radius: 18px;

    background:
        linear-gradient(
            135deg,
            #4f46e5,
            #7c3aed
        );

    font-size: 30px;

}

h1 {

    text-align: center;

    margin: 0;

    font-size: 25px;

}

.subtitle {

    text-align: center;

    color: #6b7280;

    font-size: 13px;

    margin:
        8px 0 30px;

}

label {

    display: block;

    font-size: 12px;

    font-weight: 700;

    margin-bottom: 7px;

}

input {

    width: 100%;

    padding: 13px;

    border:
        1px solid #d1d5db;

    border-radius: 9px;

    margin-bottom: 17px;

    outline: none;

}

input:focus {

    border-color: #4f46e5;

    box-shadow:
        0 0 0 3px
        rgba(79,70,229,0.1);

}

button {

    width: 100%;

    padding: 13px;

    border: none;

    border-radius: 9px;

    background: #4f46e5;

    color: white;

    font-weight: 700;

    cursor: pointer;

}

button:hover {

    background: #4338ca;

}

.error {

    background: #fee2e2;

    color: #b91c1c;

    padding: 11px;

    border-radius: 8px;

    margin-bottom: 18px;

    font-size: 12px;

}

.demo {

    text-align: center;

    margin-top: 18px;

    color: #9ca3af;

    font-size: 11px;

}

</style>

</head>


<body>

<div class="login-container">

<div class="login-card">


<div class="logo">
📚
</div>


<h1>
ALMS Admin
</h1>


<div class="subtitle">
Automated Library Management System
</div>


{% with messages = get_flashed_messages() %}

{% if messages %}

{% for message in messages %}

<div class="error">
{{ message }}
</div>

{% endfor %}

{% endif %}

{% endwith %}


<form
    method="POST"
    action="/login"
>


<label>
Username
</label>

<input
    type="text"
    name="username"
    placeholder="Enter username"
    required
>


<label>
Password
</label>

<input
    type="password"
    name="password"
    placeholder="Enter password"
    required
>


<button type="submit">

Login to Dashboard

</button>


</form>


<div class="demo">

Administrator access is protected by your configured credentials.

</div>


</div>

</div>

</body>

</html>
"""


# ============================================================
# LOGIN ROUTE
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if (
            username == ADMIN_USERNAME
            and
            password == ADMIN_PASSWORD
        ):

            session[
                "admin_logged_in"
            ] = True

            return redirect(
                url_for("dashboard")
            )

        flash(
            "Invalid username or password."
        )

    return render_template_string(
        LOGIN_HTML
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# ISSUE BOOK
# ============================================================

def process_issue(data):

    device_id = data.get(
        "device_id"
    )

    book_id = data.get(
        "book_id"
    )

    user_id = data.get(
        "user_id"
    )

    if (
        not device_id
        or
        not book_id
        or
        not user_id
    ):

        return {
            "success": False,
            "message":
                "device_id, book_id and user_id required"
        }

    connection = get_db_connection()

    cursor = connection.cursor()

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    cursor.execute("""
        SELECT *
        FROM users
        WHERE user_id = ?
    """, (user_id,))

    user = cursor.fetchone()

    if not user:

        connection.close()

        return {
            "success": False,
            "message":
                "User not found"
        }

    # --------------------------------------------------------
    # BOOK
    # --------------------------------------------------------

    cursor.execute("""
        SELECT *
        FROM books
        WHERE book_id = ?
    """, (book_id,))

    book = cursor.fetchone()

    if not book:

        connection.close()

        return {
            "success": False,
            "message":
                "Book not found"
        }

    # --------------------------------------------------------
    # AVAILABILITY
    # --------------------------------------------------------

    if book["available"] == 0:

        connection.close()

        return {
            "success": False,
            "message":
                "Book is already issued"
        }

    # --------------------------------------------------------
    # DATE
    # --------------------------------------------------------

    issue_date = datetime.now()

    due_date = (
        issue_date
        + timedelta(
            days=LOAN_DAYS
        )
    )

    issue_text = (
        issue_date.isoformat()
    )

    due_text = (
        due_date.isoformat()
    )

    # --------------------------------------------------------
    # UPDATE BOOK
    # --------------------------------------------------------

    cursor.execute("""
        UPDATE books
        SET
            available = 0,
            issued_to = ?,
            issue_date = ?,
            due_date = ?,
            return_date = NULL
        WHERE book_id = ?
    """, (
        user_id,
        issue_text,
        due_text,
        book_id
    ))

    # --------------------------------------------------------
    # TRANSACTION
    # --------------------------------------------------------

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
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        device_id,
        "issue",
        book_id,
        user_id,
        0,
        issue_text
    ))

    connection.commit()

    connection.close()

    # --------------------------------------------------------
    # EMAIL
    # --------------------------------------------------------

    subject = (
        "Book Issued Successfully"
    )

    body = f"""
Hello {user["name"]},

Your book has been issued successfully.

Book:
{book["title"]}

Book ID:
{book_id}

Issue Date:
{format_date(issue_text)}

Due Date:
{format_date(due_text)}

Loan Period:
{LOAN_DAYS} days

Fine after due date:
₹{FINE_PER_DAY} per day

Thank you,

Automated Library Management System
"""

    email_sent = send_email(
        user["email"],
        subject,
        body
    )

    return {
        "success": True,
        "message":
            "Book issued successfully",
        "book_id": book_id,
        "user_id": user_id,
        "issue_date": issue_text,
        "due_date": due_text,
        "fine_per_day":
            FINE_PER_DAY,
        "email_sent":
            email_sent
    }


# ============================================================
# RETURN BOOK
# ============================================================

def process_return(data):

    device_id = data.get(
        "device_id"
    )

    book_id = data.get(
        "book_id"
    )

    user_id = data.get(
        "user_id"
    )

    if (
        not device_id
        or
        not book_id
        or
        not user_id
    ):

        return {
            "success": False,
            "message":
                "device_id, book_id and user_id required"
        }

    connection = get_db_connection()

    cursor = connection.cursor()

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    cursor.execute("""
        SELECT *
        FROM users
        WHERE user_id = ?
    """, (user_id,))

    user = cursor.fetchone()

    if not user:

        connection.close()

        return {
            "success": False,
            "message":
                "User not found"
        }

    # --------------------------------------------------------
    # BOOK
    # --------------------------------------------------------

    cursor.execute("""
        SELECT *
        FROM books
        WHERE book_id = ?
    """, (book_id,))

    book = cursor.fetchone()

    if not book:

        connection.close()

        return {
            "success": False,
            "message":
                "Book not found"
        }

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if book["available"] == 1:

        connection.close()

        return {
            "success": False,
            "message":
                "Book is already available"
        }

    if book["issued_to"] != user_id:

        connection.close()

        return {
            "success": False,
            "message":
                "Book was issued to another user"
        }

    # --------------------------------------------------------
    # FINE
    # --------------------------------------------------------

    return_date = datetime.now()

    late_days, fine = calculate_fine(
        book["due_date"],
        return_date
    )

    return_text = (
        return_date.isoformat()
    )

    # --------------------------------------------------------
    # UPDATE
    # --------------------------------------------------------

    cursor.execute("""
        UPDATE books
        SET
            available = 1,
            issued_to = NULL,
            issue_date = NULL,
            due_date = NULL,
            return_date = ?
        WHERE book_id = ?
    """, (
        return_text,
        book_id
    ))

    # --------------------------------------------------------
    # TRANSACTION
    # --------------------------------------------------------

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
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        device_id,
        "return",
        book_id,
        user_id,
        fine,
        return_text
    ))

    connection.commit()

    connection.close()

    # --------------------------------------------------------
    # EMAIL
    # --------------------------------------------------------

    subject = (
        "Book Returned Successfully"
    )

    body = f"""
Hello {user["name"]},

Your book has been returned successfully.

Book:
{book["title"]}

Book ID:
{book_id}

Due Date:
{format_date(book["due_date"])}

Return Date:
{format_date(return_text)}

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
        subject,
        body
    )

    return {
        "success": True,
        "message":
            "Book returned successfully",
        "book_id": book_id,
        "user_id": user_id,
        "return_date":
            return_text,
        "late_days":
            late_days,
        "fine":
            fine,
        "email_sent":
            email_sent
    }


# ============================================================
# DASHBOARD HTML
# ============================================================

DASHBOARD_HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
ALMS | Admin Dashboard
</title>


<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>


<style>

/* ============================================================
   GLOBAL
   ============================================================ */

* {
    box-sizing: border-box;
}

:root {

    --bg: #f4f7fb;

    --sidebar: #111827;

    --sidebar2: #1f2937;

    --primary: #4f46e5;

    --primary2: #6366f1;

    --success: #16a34a;

    --warning: #d97706;

    --danger: #dc2626;

    --text: #111827;

    --muted: #6b7280;

    --border: #e5e7eb;

    --white: #ffffff;

    --shadow:
        0 8px 30px
        rgba(15, 23, 42, 0.07);
}

body {

    margin: 0;

    font-family:
        Inter,
        Arial,
        sans-serif;

    background: var(--bg);

    color: var(--text);

}


/* ============================================================
   LAYOUT
   ============================================================ */

.app {

    min-height: 100vh;

    display: flex;

}


/* ============================================================
   SIDEBAR
   ============================================================ */

.sidebar {

    width: 250px;

    min-height: 100vh;

    position: fixed;

    top: 0;

    left: 0;

    bottom: 0;

    background: var(--sidebar);

    color: white;

    padding: 22px 15px;

    z-index: 50;

}

.brand {

    display: flex;

    align-items: center;

    gap: 11px;

    padding:
        4px 10px 28px;

}

.brand-icon {

    width: 42px;

    height: 42px;

    border-radius: 12px;

    display: flex;

    align-items: center;

    justify-content: center;

    background:
        linear-gradient(
            135deg,
            #4f46e5,
            #8b5cf6
        );

    font-size: 21px;

}

.brand-title {

    font-size: 18px;

    font-weight: 800;

}

.brand-subtitle {

    color: #9ca3af;

    font-size: 10px;

}

.nav-label {

    color: #6b7280;

    font-size: 10px;

    font-weight: 700;

    letter-spacing: 1px;

    padding:
        14px 12px 7px;

}

.nav-link {

    display: flex;

    align-items: center;

    gap: 12px;

    color: #d1d5db;

    text-decoration: none;

    padding: 11px 12px;

    border-radius: 9px;

    margin-bottom: 4px;

    font-size: 13px;

    transition: .2s;

}

.nav-link:hover {

    background:
        var(--sidebar2);

    color: white;

}

.nav-link.active {

    background:
        linear-gradient(
            90deg,
            #4f46e5,
            #6366f1
        );

    color: white;

}

.sidebar-bottom {

    position: absolute;

    bottom: 18px;

    left: 15px;

    right: 15px;

}

.server-card {

    background:
        #1f2937;

    padding: 14px;

    border-radius: 12px;

}

.server-title {

    font-size: 11px;

    font-weight: 700;

}

.server-status {

    color: #4ade80;

    font-size: 10px;

    margin-top: 5px;

}


/* ============================================================
   MAIN
   ============================================================ */

.main {

    width:
        calc(100% - 250px);

    margin-left: 250px;

}


/* ============================================================
   TOP BAR
   ============================================================ */

.topbar {

    height: 76px;

    background: white;

    border-bottom:
        1px solid var(--border);

    padding:
        0 30px;

    display: flex;

    align-items: center;

    justify-content: space-between;

}

.page-title {

    font-size: 20px;

    font-weight: 800;

}

.page-subtitle {

    font-size: 11px;

    color: var(--muted);

    margin-top: 4px;

}

.admin {

    display: flex;

    align-items: center;

    gap: 10px;

}

.avatar {

    width: 39px;

    height: 39px;

    border-radius: 50%;

    background:
        linear-gradient(
            135deg,
            #4f46e5,
            #7c3aed
        );

    color: white;

    display: flex;

    align-items: center;

    justify-content: center;

    font-weight: 800;

}


/* ============================================================
   CONTENT
   ============================================================ */

.content {

    padding: 28px;

}


/* ============================================================
   STAT CARDS
   ============================================================ */

.stats {

    display: grid;

    grid-template-columns:
        repeat(5, 1fr);

    gap: 17px;

    margin-bottom: 22px;

}

.stat {

    background: white;

    border:
        1px solid var(--border);

    border-radius: 15px;

    padding: 19px;

    box-shadow: var(--shadow);

}

.stat-icon {

    width: 40px;

    height: 40px;

    display: flex;

    align-items: center;

    justify-content: center;

    border-radius: 11px;

    font-size: 18px;

}

.purple {
    background: #ede9fe;
}

.blue {
    background: #dbeafe;
}

.green {
    background: #dcfce7;
}

.orange {
    background: #ffedd5;
}

.red {
    background: #fee2e2;
}

.stat-label {

    color: var(--muted);

    font-size: 10px;

    font-weight: 700;

    margin-top: 13px;

}

.stat-number {

    font-size: 26px;

    font-weight: 800;

    margin-top: 4px;

}


/* ============================================================
   CHARTS
   ============================================================ */

.chart-grid {

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 20px;

    margin-bottom: 22px;

}

.chart-card {

    background: white;

    border:
        1px solid var(--border);

    border-radius: 15px;

    box-shadow: var(--shadow);

    padding: 20px;

}

.chart-title {

    font-size: 15px;

    font-weight: 800;

    margin-bottom: 5px;

}

.chart-subtitle {

    color: var(--muted);

    font-size: 11px;

    margin-bottom: 15px;

}

.chart-container {

    height: 260px;

}


/* ============================================================
   PANEL
   ============================================================ */

.panel {

    background: white;

    border:
        1px solid var(--border);

    border-radius: 15px;

    box-shadow: var(--shadow);

    margin-bottom: 22px;

    overflow: hidden;

}

.panel-header {

    padding:
        18px 20px;

    border-bottom:
        1px solid var(--border);

    display: flex;

    align-items: center;

    justify-content: space-between;

    gap: 15px;

}

.panel-title {

    font-size: 15px;

    font-weight: 800;

}

.panel-subtitle {

    color: var(--muted);

    font-size: 10px;

    margin-top: 3px;

}

.panel-body {

    padding: 20px;

}


/* ============================================================
   FORMS
   ============================================================ */

.forms {

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 20px;

}

.form-grid {

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 13px;

}

.form-group {

    display: flex;

    flex-direction: column;

}

.form-group.full {

    grid-column:
        1 / -1;

}

.form-group label {

    font-size: 11px;

    font-weight: 700;

    margin-bottom: 6px;

}

.form-group input {

    padding: 11px;

    border:
        1px solid var(--border);

    border-radius: 8px;

    outline: none;

    font-size: 12px;

}

.form-group input:focus {

    border-color:
        var(--primary);

    box-shadow:
        0 0 0 3px
        rgba(79,70,229,.1);

}

.button {

    border: none;

    padding: 10px 14px;

    border-radius: 8px;

    background:
        var(--primary);

    color: white;

    font-size: 12px;

    font-weight: 700;

    cursor: pointer;

}

.button:hover {

    background:
        #4338ca;

}

.button-danger {

    background:
        #fee2e2;

    color:
        #b91c1c;

}

.button-danger:hover {

    background:
        #fecaca;

}

.button-secondary {

    background:
        #f3f4f6;

    color:
        #374151;

}

.button-success {

    background:
        #dcfce7;

    color:
        #15803d;

}

.full-button {

    width: 100%;

    margin-top: 15px;

}


/* ============================================================
   TABLE
   ============================================================ */

.table-wrap {

    overflow-x: auto;

}

table {

    width: 100%;

    min-width: 850px;

    border-collapse:
        collapse;

}

th {

    background:
        #f9fafb;

    color:
        #6b7280;

    text-align:
        left;

    padding:
        12px 17px;

    font-size: 10px;

    text-transform:
        uppercase;

    border-bottom:
        1px solid var(--border);

}

td {

    padding:
        13px 17px;

    font-size: 12px;

    border-bottom:
        1px solid #f1f3f5;

}

tr:hover td {

    background:
        #fafbff;

}

.id {

    font-weight: 800;

}

.muted {

    color:
        var(--muted);

}

.status {

    display: inline-flex;

    padding:
        5px 9px;

    border-radius:
        20px;

    font-size:
        10px;

    font-weight:
        800;

}

.available {

    background:
        #dcfce7;

    color:
        #15803d;

}

.issued {

    background:
        #fef3c7;

    color:
        #b45309;

}

.overdue {

    background:
        #fee2e2;

    color:
        #b91c1c;

}

.fine {

    color:
        #dc2626;

    font-weight:
        800;

}

.action-buttons {

    display: flex;

    gap: 6px;

}


/* ============================================================
   SEARCH
   ============================================================ */

.search {

    width: 230px;

    padding:
        9px 11px;

    border:
        1px solid var(--border);

    border-radius:
        8px;

    outline:
        none;

    font-size:
        11px;

}

.search:focus {

    border-color:
        var(--primary);

}


/* ============================================================
   FLASH
   ============================================================ */

.flash {

    background:
        #ecfdf5;

    color:
        #047857;

    border:
        1px solid #a7f3d0;

    padding:
        11px 14px;

    border-radius:
        9px;

    margin-bottom:
        18px;

    font-size:
        12px;

}


/* ============================================================
   RESPONSIVE
   ============================================================ */

@media(max-width:1200px) {

    .stats {

        grid-template-columns:
            repeat(3, 1fr);

    }

}

@media(max-width:900px) {

    .sidebar {

        width: 70px;

    }

    .brand-title,
    .brand-subtitle,
    .nav-link span:last-child,
    .nav-label,
    .sidebar-bottom {

        display: none;

    }

    .brand {

        justify-content:
            center;

    }

    .nav-link {

        justify-content:
            center;

    }

    .main {

        width:
            calc(100% - 70px);

        margin-left:
            70px;

    }

    .forms,
    .chart-grid {

        grid-template-columns:
            1fr;

    }

}

@media(max-width:650px) {

    .content {

        padding:
            15px;

    }

    .topbar {

        padding:
            0 15px;

    }

    .stats {

        grid-template-columns:
            repeat(2, 1fr);

    }

    .form-grid {

        grid-template-columns:
            1fr;

    }

    .form-group.full {

        grid-column:
            auto;

    }

}

@media(max-width:420px) {

    .stats {

        grid-template-columns:
            1fr;

    }

}

</style>

</head>


<body>


<div class="app">


<!-- ========================================================
     SIDEBAR
     ======================================================== -->

<aside class="sidebar">


<div class="brand">

<div class="brand-icon">
📚
</div>

<div>

<div class="brand-title">
ALMS
</div>

<div class="brand-subtitle">
Automated Library
</div>

</div>

</div>


<div class="nav-label">
MAIN
</div>


<a
    class="nav-link active"
    href="/dashboard"
>

<span>📊</span>
<span>Dashboard</span>

</a>


<a
    class="nav-link"
    href="#users"
>

<span>👥</span>
<span>Users</span>

</a>


<a
    class="nav-link"
    href="#books"
>

<span>📚</span>
<span>Books</span>

</a>


<a
    class="nav-link"
    href="#transactions"
>

<span>📋</span>
<span>Transactions</span>

</a>


<div class="nav-label">
SYSTEM
</div>


<a
    class="nav-link"
    href="/api/books"
    target="_blank"
>

<span>🔗</span>
<span>Book API</span>

</a>


<a
    class="nav-link"
    href="/api/users"
    target="_blank"
>

<span>👤</span>
<span>User API</span>

</a>


<a
    class="nav-link"
    href="/logout"
>

<span>🚪</span>
<span>Logout</span>

</a>


<div class="sidebar-bottom">

<div class="server-card">

<div class="server-title">
📡 IoT Server
</div>

<div class="server-status">
● ONLINE
</div>

</div>

</div>


</aside>


<!-- ========================================================
     MAIN
     ======================================================== -->

<main class="main">


<header class="topbar">


<div>

<div class="page-title">
Library Dashboard
</div>

<div class="page-subtitle">
Automated Library Management System
</div>

</div>


<div class="admin">

<div style="text-align:right;">

<strong style="font-size:12px;">
Administrator
</strong>

<div
    class="muted"
    style="font-size:9px;"
>

IoT Control Panel

</div>

</div>


<div class="avatar">
A
</div>

</div>


</header>


<div class="content">


<!-- ========================================================
     FLASH MESSAGE
     ======================================================== -->

{% with messages =
get_flashed_messages()
%}

{% if messages %}

{% for message in messages %}

<div class="flash">

✅ {{ message }}

</div>

{% endfor %}

{% endif %}

{% endwith %}


<!-- ========================================================
     STATISTICS
     ======================================================== -->

<section class="stats">


<div class="stat">

<div class="stat-icon purple">
👥
</div>

<div class="stat-label">
TOTAL USERS
</div>

<div class="stat-number">
{{ total_users }}
</div>

</div>


<div class="stat">

<div class="stat-icon blue">
📚
</div>

<div class="stat-label">
TOTAL BOOKS
</div>

<div class="stat-number">
{{ total_books }}
</div>

</div>


<div class="stat">

<div class="stat-icon green">
✓
</div>

<div class="stat-label">
AVAILABLE
</div>

<div class="stat-number">
{{ available_books }}
</div>

</div>


<div class="stat">

<div class="stat-icon orange">
📖
</div>

<div class="stat-label">
ISSUED
</div>

<div class="stat-number">
{{ issued_books }}
</div>

</div>


<div class="stat">

<div class="stat-icon red">
₹
</div>

<div class="stat-label">
ACTIVE FINES
</div>

<div class="stat-number">
₹{{ total_fines }}
</div>

</div>


</section>


<!-- ========================================================
     CHARTS
     ======================================================== -->

<section class="chart-grid">


<div class="chart-card">

<div class="chart-title">
📚 Library Status
</div>

<div class="chart-subtitle">
Current book availability
</div>

<div class="chart-container">

<canvas id="libraryChart"></canvas>

</div>

</div>


<div class="chart-card">

<div class="chart-title">
📊 Transactions
</div>

<div class="chart-subtitle">
Issue and return activity
</div>

<div class="chart-container">

<canvas id="transactionChart"></canvas>

</div>

</div>


</section>


<!-- ========================================================
     REGISTRATION FORMS
     ======================================================== -->

<section class="forms">


<!-- USER -->

<div class="panel">

<div class="panel-header">

<div>

<div class="panel-title">
👤 Register New User
</div>

<div class="panel-subtitle">
Create a library member account
</div>

</div>

</div>


<div class="panel-body">

<form
    method="POST"
    action="/dashboard/register-user"
>


<div class="form-grid">


<div class="form-group">

<label>
USER ID
</label>

<input
    name="user_id"
    placeholder="USER103"
    required
>

</div>


<div class="form-group">

<label>
NAME
</label>

<input
    name="name"
    placeholder="Student Name"
    required
>

</div>


<div class="form-group full">

<label>
EMAIL
</label>

<input
    type="email"
    name="email"
    placeholder="student@gmail.com"
    required
>

</div>


</div>


<button
    class="button full-button"
    type="submit"
>

+ Register User

</button>


</form>

</div>

</div>


<!-- BOOK -->

<div class="panel">

<div class="panel-header">

<div>

<div class="panel-title">
📚 Register New Book
</div>

<div class="panel-subtitle">
Add a book to the library collection
</div>

</div>

</div>


<div class="panel-body">

<form
    method="POST"
    action="/dashboard/register-book"
>


<div class="form-grid">


<div class="form-group">

<label>
BOOK ID
</label>

<input
    name="book_id"
    placeholder="BOOK006"
    required
>

</div>


<div class="form-group">

<label>
AUTHOR
</label>

<input
    name="author"
    placeholder="Author"
>

</div>


<div class="form-group full">

<label>
TITLE
</label>

<input
    name="title"
    placeholder="Machine Learning"
    required
>

</div>


</div>


<button
    class="button full-button"
    type="submit"
>

+ Register Book

</button>


</form>

</div>

</div>


</section>


<!-- ========================================================
     USERS TABLE
     ======================================================== -->

<section
    class="panel"
    id="users"
>


<div class="panel-header">

<div>

<div class="panel-title">
👥 User Management
</div>

<div class="panel-subtitle">
Register, edit and remove library members
</div>

</div>


<input
    class="search"
    id="userSearch"
    placeholder="🔎 Search users..."
>

</div>


<div class="table-wrap">

<table id="usersTable">


<thead>

<tr>

<th>User ID</th>

<th>Name</th>

<th>Email</th>

<th>Action</th>

</tr>

</thead>


<tbody>

{% for user in users %}

<tr>


<td class="id">
{{ user.user_id }}
</td>


<td>
{{ user.name }}
</td>


<td>
{{ user.email }}
</td>


<td>

<div class="action-buttons">

<a
    class="button button-secondary"
    href="/dashboard/edit-user/{{ user.user_id }}"
    style="text-decoration:none;"
>

Edit

</a>


<form
    method="POST"
    action="/dashboard/delete-user/{{ user.user_id }}"
    onsubmit="
        return confirm(
            'Delete this user?'
        );
    "
>

<button
    type="submit"
    class="button button-danger"
>

Delete

</button>

</form>

</div>

</td>


</tr>

{% else %}

<tr>

<td
    colspan="4"
    style="text-align:center;"
>

No users found.

</td>

</tr>

{% endfor %}

</tbody>


</table>

</div>

</section>


<!-- ========================================================
     BOOK TABLE
     ======================================================== -->

<section
    class="panel"
    id="books"
>


<div class="panel-header">

<div>

<div class="panel-title">
📚 Book Management
</div>

<div class="panel-subtitle">
Edit, delete and monitor library books
</div>

</div>


<input
    class="search"
    id="bookSearch"
    placeholder="🔎 Search books..."
>

</div>


<div class="table-wrap">

<table id="booksTable">


<thead>

<tr>

<th>Book</th>

<th>Author</th>

<th>Status</th>

<th>Issued To</th>

<th>Due Date</th>

<th>Fine</th>

<th>Action</th>

</tr>

</thead>


<tbody>

{% for book in books %}

<tr>


<td>

<strong>
{{ book.title }}
</strong>

<div class="muted">
{{ book.book_id }}
</div>

</td>


<td>
{{ book.author or "-" }}
</td>


<td>

{% if book.available == 1 %}

<span class="status available">
Available
</span>

{% elif book.current_fine > 0 %}

<span class="status overdue">
Overdue
</span>

{% else %}

<span class="status issued">
Issued
</span>

{% endif %}

</td>


<td>
{{ book.issued_to or "-" }}
</td>


<td>
{{ format_date(book.due_date) }}
</td>


<td>

{% if book.current_fine > 0 %}

<span class="fine">
₹{{ book.current_fine }}
</span>

<div class="muted">
{{ book.late_days }} day(s) late
</div>

{% else %}

₹0

{% endif %}

</td>


<td>

<div class="action-buttons">

<a
    class="button button-secondary"
    href="/dashboard/edit-book/{{ book.book_id }}"
    style="text-decoration:none;"
>

Edit

</a>


{% if book.available == 1 %}

<form
    method="POST"
    action="/dashboard/delete-book/{{ book.book_id }}"
    onsubmit="
        return confirm(
            'Delete this book?'
        );
    "
>

<button
    type="submit"
    class="button button-danger"
>

Delete

</button>

</form>

{% endif %}

</div>

</td>


</tr>

{% else %}

<tr>

<td
    colspan="7"
    style="text-align:center;"
>

No books found.

</td>

</tr>

{% endfor %}

</tbody>


</table>

</div>

</section>


<!-- ========================================================
     TRANSACTIONS
     ======================================================== -->

<section
    class="panel"
    id="transactions"
>


<div class="panel-header">

<div>

<div class="panel-title">
📋 Transaction History
</div>

<div class="panel-subtitle">
Latest issue and return activity
</div>

</div>


<input
    class="search"
    id="transactionSearch"
    placeholder="🔎 Search..."
>

</div>


<div class="table-wrap">

<table id="transactionsTable">


<thead>

<tr>

<th>ID</th>

<th>Action</th>

<th>Book</th>

<th>User</th>

<th>Fine</th>

<th>Date</th>

</tr>

</thead>


<tbody>

{% for transaction in transactions %}

<tr>

<td class="id">
#{{ transaction.id }}
</td>


<td>

{% if transaction.action == "issue" %}

<span
    class="status"
    style="
        background:#dbeafe;
        color:#1d4ed8;
    "
>

Issue

</span>

{% else %}

<span
    class="status"
    style="
        background:#dcfce7;
        color:#15803d;
    "
>

Return

</span>

{% endif %}

</td>


<td>
{{ transaction.book_id }}
</td>


<td>
{{ transaction.user_id }}
</td>


<td>

{% if transaction.fine > 0 %}

<span class="fine">
₹{{ transaction.fine }}
</span>

{% else %}

₹0

{% endif %}

</td>


<td>

{{ format_date(
    transaction.timestamp
) }}

</td>


</tr>

{% else %}

<tr>

<td
    colspan="6"
    style="text-align:center;"
>

No transactions yet.

</td>

</tr>

{% endfor %}

</tbody>


</table>

</div>

</section>


</div>

</main>

</div>


<script>

/* ============================================================
   SEARCH
   ============================================================ */

function setupSearch(
    inputId,
    tableId
) {

    const input =
        document.getElementById(
            inputId
        );

    const table =
        document.getElementById(
            tableId
        );

    if (!input || !table) {
        return;
    }

    input.addEventListener(
        "keyup",
        function() {

            const filter =
                input.value
                .toLowerCase();

            const rows =
                table
                .querySelectorAll(
                    "tbody tr"
                );

            rows.forEach(
                function(row) {

                    const text =
                        row.textContent
                        .toLowerCase();

                    row.style.display =
                        text.includes(filter)
                        ? ""
                        : "none";

                }
            );

        }
    );

}


setupSearch(
    "userSearch",
    "usersTable"
);

setupSearch(
    "bookSearch",
    "booksTable"
);

setupSearch(
    "transactionSearch",
    "transactionsTable"
);


/* ============================================================
   LIBRARY STATUS CHART
   ============================================================ */

const libraryChart =
    document.getElementById(
        "libraryChart"
    );


if (libraryChart) {

    new Chart(
        libraryChart,
        {

            type: "doughnut",

            data: {

                labels: [
                    "Available",
                    "Issued",
                    "Overdue"
                ],

                datasets: [

                    {

                        data: [
                            {{ available_books }},
                            {{ normal_issued_books }},
                            {{ overdue_books }}
                        ]

                    }

                ]

            },

            options: {

                responsive: true,

                maintainAspectRatio: false,

                plugins: {

                    legend: {

                        position: "bottom"

                    }

                }

            }

        }
    );

}


/* ============================================================
   TRANSACTION CHART
   ============================================================ */

const transactionChart =
    document.getElementById(
        "transactionChart"
    );


if (transactionChart) {

    new Chart(
        transactionChart,
        {

            type: "bar",

            data: {

                labels: [
                    "Issues",
                    "Returns"
                ],

                datasets: [

                    {

                        label:
                            "Transactions",

                        data: [
                            {{ issue_count }},
                            {{ return_count }}
                        ]

                    }

                ]

            },

            options: {

                responsive: true,

                maintainAspectRatio: false,

                plugins: {

                    legend: {
                        display: false
                    }

                },

                scales: {

                    y: {

                        beginAtZero: true,

                        ticks: {

                            precision: 0

                        }

                    }

                }

            }

        }
    );

}


/* ============================================================
   SMOOTH SCROLL
   ============================================================ */

document
    .querySelectorAll(
        'a[href^="#"]'
    )
    .forEach(
        function(anchor) {

            anchor.addEventListener(
                "click",
                function(event) {

                    const target =
                        document.querySelector(
                            this.getAttribute(
                                "href"
                            )
                        );

                    if (target) {

                        event.preventDefault();

                        target.scrollIntoView({
                            behavior: "smooth"
                        });

                    }

                }
            );

        }
    );

</script>


</body>

</html>
"""


# ============================================================
# DASHBOARD
# ============================================================

@app.route(
    "/dashboard",
    methods=["GET"]
)
@login_required
def dashboard():

    connection = get_db_connection()

    cursor = connection.cursor()

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    cursor.execute("""
        SELECT *
        FROM users
        ORDER BY user_id
    """)

    users = [
        dict(row)
        for row in cursor.fetchall()
    ]

    # --------------------------------------------------------
    # BOOKS
    # --------------------------------------------------------

    cursor.execute("""
        SELECT *
        FROM books
        ORDER BY book_id
    """)

    raw_books = cursor.fetchall()

    books = []

    for row in raw_books:

        book = dict(row)

        if book["available"] == 0:

            late_days, fine = calculate_fine(
                book["due_date"]
            )

        else:

            late_days = 0
            fine = 0

        book["late_days"] = late_days

        book["current_fine"] = fine

        books.append(book)

    # --------------------------------------------------------
    # TRANSACTIONS
    # --------------------------------------------------------

    cursor.execute("""
        SELECT *
        FROM transactions
        ORDER BY id DESC
        LIMIT 100
    """)

    transactions = [
        dict(row)
        for row in cursor.fetchall()
    ]

    connection.close()

    # --------------------------------------------------------
    # STATISTICS
    # --------------------------------------------------------

    total_users = len(users)

    total_books = len(books)

    available_books = sum(
        1
        for book in books
        if book["available"] == 1
    )

    issued_books = sum(
        1
        for book in books
        if book["available"] == 0
    )

    overdue_books = sum(
        1
        for book in books
        if (
            book["available"] == 0
            and
            book["current_fine"] > 0
        )
    )

    normal_issued_books = (
        issued_books
        - overdue_books
    )

    total_fines = sum(
        book["current_fine"]
        for book in books
    )

    issue_count = sum(
        1
        for transaction in transactions
        if transaction["action"] == "issue"
    )

    return_count = sum(
        1
        for transaction in transactions
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

        overdue_books=overdue_books,

        normal_issued_books=
            normal_issued_books,

        total_fines=total_fines,

        issue_count=issue_count,

        return_count=return_count,

        format_date=format_date
    )


# ============================================================
# REGISTER USER
# ============================================================

@app.route(
    "/dashboard/register-user",
    methods=["POST"]
)
@login_required
def register_user():

    user_id = request.form.get(
        "user_id",
        ""
    ).strip()

    name = request.form.get(
        "name",
        ""
    ).strip()

    email = request.form.get(
        "email",
        ""
    ).strip()

    if (
        not user_id
        or
        not name
        or
        not email
    ):

        flash(
            "All user fields are required."
        )

        return redirect(
            url_for("dashboard")
        )

    connection = get_db_connection()

    cursor = connection.cursor()

    try:

        cursor.execute("""
            INSERT INTO users
            (
                user_id,
                name,
                email
            )
            VALUES (?, ?, ?)
        """, (
            user_id,
            name,
            email
        ))

        connection.commit()

        flash(
            f"User {user_id} registered successfully."
        )

    except sqlite3.IntegrityError:

        flash(
            f"User ID {user_id} already exists."
        )

    finally:

        connection.close()

    return redirect(
        url_for("dashboard")
        + "#users"
    )


# ============================================================
# EDIT USER
# ============================================================

EDIT_USER_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<title>
Edit User | ALMS
</title>

<style>

body {

    margin: 0;

    min-height: 100vh;

    display: flex;

    align-items: center;

    justify-content: center;

    background: #f4f7fb;

    font-family: Arial;

}

.card {

    background: white;

    width: 420px;

    max-width: 90%;

    padding: 30px;

    border-radius: 16px;

    box-shadow:
        0 15px 40px
        rgba(0,0,0,.1);

}

h2 {
    margin-top: 0;
}

label {

    display: block;

    font-size: 12px;

    font-weight: bold;

    margin:
        15px 0 6px;

}

input {

    width: 100%;

    padding: 11px;

    border:
        1px solid #ddd;

    border-radius: 8px;

}

button {

    width: 100%;

    padding: 12px;

    margin-top: 20px;

    border: none;

    border-radius: 8px;

    background: #4f46e5;

    color: white;

    font-weight: bold;

}

a {

    display: block;

    text-align: center;

    margin-top: 15px;

    color: #4f46e5;

    text-decoration: none;

}

</style>

</head>

<body>

<div class="card">

<h2>
👤 Edit User
</h2>

<form
    method="POST"
>


<label>
User ID
</label>

<input
    value="{{ user.user_id }}"
    disabled
>


<label>
Name
</label>

<input
    name="name"
    value="{{ user.name }}"
    required
>


<label>
Email
</label>

<input
    type="email"
    name="email"
    value="{{ user.email }}"
    required
>


<button type="submit">
Save Changes
</button>

</form>


<a href="/dashboard#users">
← Back to Dashboard
</a>


</div>

</body>

</html>
"""


@app.route(
    "/dashboard/edit-user/<user_id>",
    methods=["GET", "POST"]
)
@login_required
def edit_user(user_id):

    connection = get_db_connection()

    cursor = connection.cursor()

    if request.method == "POST":

        name = request.form.get(
            "name",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip()

        cursor.execute("""
            UPDATE users
            SET
                name = ?,
                email = ?
            WHERE user_id = ?
        """, (
            name,
            email,
            user_id
        ))

        connection.commit()

        connection.close()

        flash(
            f"User {user_id} updated successfully."
        )

        return redirect(
            url_for("dashboard")
            + "#users"
        )

    cursor.execute("""
        SELECT *
        FROM users
        WHERE user_id = ?
    """, (user_id,))

    user = cursor.fetchone()

    connection.close()

    if not user:

        flash(
            "User not found."
        )

        return redirect(
            url_for("dashboard")
        )

    return render_template_string(
        EDIT_USER_HTML,
        user=dict(user)
    )


# ============================================================
# DELETE USER
# ============================================================

@app.route(
    "/dashboard/delete-user/<user_id>",
    methods=["POST"]
)
@login_required
def delete_user(user_id):

    connection = get_db_connection()

    cursor = connection.cursor()

    # Prevent deleting a user who currently has a book.

    cursor.execute("""
        SELECT COUNT(*)
        FROM books
        WHERE issued_to = ?
    """, (user_id,))

    issued_count = cursor.fetchone()[0]

    if issued_count > 0:

        connection.close()

        flash(
            "Cannot delete user because "
            "they currently have an issued book."
        )

        return redirect(
            url_for("dashboard")
            + "#users"
        )

    cursor.execute("""
        DELETE FROM users
        WHERE user_id = ?
    """, (user_id,))

    connection.commit()

    connection.close()

    flash(
        f"User {user_id} deleted."
    )

    return redirect(
        url_for("dashboard")
        + "#users"
    )


# ============================================================
# REGISTER BOOK
# ============================================================

@app.route(
    "/dashboard/register-book",
    methods=["POST"]
)
@login_required
def register_book():

    book_id = request.form.get(
        "book_id",
        ""
    ).strip()

    title = request.form.get(
        "title",
        ""
    ).strip()

    author = request.form.get(
        "author",
        ""
    ).strip()

    if (
        not book_id
        or
        not title
    ):

        flash(
            "Book ID and title are required."
        )

        return redirect(
            url_for("dashboard")
        )

    connection = get_db_connection()

    cursor = connection.cursor()

    try:

        cursor.execute("""
            INSERT INTO books
            (
                book_id,
                title,
                author,
                available
            )
            VALUES (?, ?, ?, 1)
        """, (
            book_id,
            title,
            author
        ))

        connection.commit()

        flash(
            f"Book {book_id} registered successfully."
        )

    except sqlite3.IntegrityError:

        flash(
            f"Book ID {book_id} already exists."
        )

    finally:

        connection.close()

    return redirect(
        url_for("dashboard")
        + "#books"
    )


# ============================================================
# EDIT BOOK
# ============================================================

EDIT_BOOK_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<title>
Edit Book | ALMS
</title>

<style>

body {

    margin: 0;

    min-height: 100vh;

    display: flex;

    align-items: center;

    justify-content: center;

    background: #f4f7fb;

    font-family: Arial;

}

.card {

    background: white;

    width: 430px;

    max-width: 90%;

    padding: 30px;

    border-radius: 16px;

    box-shadow:
        0 15px 40px
        rgba(0,0,0,.1);

}

h2 {
    margin-top: 0;
}

label {

    display: block;

    font-size: 12px;

    font-weight: bold;

    margin:
        15px 0 6px;

}

input {

    width: 100%;

    padding: 11px;

    border:
        1px solid #ddd;

    border-radius: 8px;

}

button {

    width: 100%;

    padding: 12px;

    margin-top: 20px;

    border: none;

    border-radius: 8px;

    background: #4f46e5;

    color: white;

    font-weight: bold;

}

a {

    display: block;

    text-align: center;

    margin-top: 15px;

    color: #4f46e5;

    text-decoration: none;

}

.warning {

    background: #fff7ed;

    color: #9a3412;

    padding: 10px;

    border-radius: 8px;

    font-size: 11px;

    margin-top: 15px;

}

</style>

</head>

<body>

<div class="card">

<h2>
📚 Edit Book
</h2>

<form
    method="POST"
>


<label>
Book ID
</label>

<input
    value="{{ book.book_id }}"
    disabled
>


<label>
Title
</label>

<input
    name="title"
    value="{{ book.title }}"
    required
>


<label>
Author
</label>

<input
    name="author"
    value="{{ book.author or '' }}"
>


<button type="submit">
Save Changes
</button>

</form>


{% if book.available == 0 %}

<div class="warning">

⚠️ This book is currently issued.
Only its title and author can be changed.

</div>

{% endif %}


<a href="/dashboard#books">
← Back to Dashboard
</a>


</div>

</body>

</html>
"""


@app.route(
    "/dashboard/edit-book/<book_id>",
    methods=["GET", "POST"]
)
@login_required
def edit_book(book_id):

    connection = get_db_connection()

    cursor = connection.cursor()

    if request.method == "POST":

        title = request.form.get(
            "title",
            ""
        ).strip()

        author = request.form.get(
            "author",
            ""
        ).strip()

        cursor.execute("""
            UPDATE books
            SET
                title = ?,
                author = ?
            WHERE book_id = ?
        """, (
            title,
            author,
            book_id
        ))

        connection.commit()

        connection.close()

        flash(
            f"Book {book_id} updated successfully."
        )

        return redirect(
            url_for("dashboard")
            + "#books"
        )

    cursor.execute("""
        SELECT *
        FROM books
        WHERE book_id = ?
    """, (book_id,))

    book = cursor.fetchone()

    connection.close()

    if not book:

        flash(
            "Book not found."
        )

        return redirect(
            url_for("dashboard")
        )

    return render_template_string(
        EDIT_BOOK_HTML,
        book=dict(book)
    )


# ============================================================
# DELETE BOOK
# ============================================================

@app.route(
    "/dashboard/delete-book/<book_id>",
    methods=["POST"]
)
@login_required
def delete_book(book_id):

    connection = get_db_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT available
        FROM books
        WHERE book_id = ?
    """, (book_id,))

    book = cursor.fetchone()

    if not book:

        connection.close()

        flash(
            "Book not found."
        )

        return redirect(
            url_for("dashboard")
        )

    if book["available"] == 0:

        connection.close()

        flash(
            "Cannot delete an issued book."
        )

        return redirect(
            url_for("dashboard")
            + "#books"
        )

    cursor.execute("""
        DELETE FROM books
        WHERE book_id = ?
    """, (book_id,))

    connection.commit()

    connection.close()

    flash(
        f"Book {book_id} deleted."
    )

    return redirect(
        url_for("dashboard")
        + "#books"
    )


# ============================================================
# IoT API
# ============================================================

@app.route(
    "/api/update",
    methods=["POST"]
)
def receive_data():

    data = request.get_json(
        silent=True
    )

    if not data:

        return jsonify({
            "success": False,
            "message":
                "No JSON data received"
        }), 400

    print(
        "\n========================================"
    )

    print(
        "📡 IoT DATA RECEIVED"
    )

    print(
        "========================================"
    )

    print(
        "Device ID:",
        data.get("device_id")
    )

    print(
        "Action:",
        data.get("action")
    )

    print(
        "Book ID:",
        data.get("book_id")
    )

    print(
        "User ID:",
        data.get("user_id")
    )

    print(
        "========================================"
    )

    action = data.get(
        "action"
    )

    if action == "issue":

        result = process_issue(
            data
        )

    elif action == "return":

        result = process_return(
            data
        )

    else:

        result = {
            "success": False,
            "message":
                "Unknown action"
        }

    return jsonify(
        result
    )


# ============================================================
# BOOK API
# ============================================================

@app.route(
    "/api/books",
    methods=["GET"]
)
def get_books():

    connection = get_db_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM books
        ORDER BY book_id
    """)

    rows = cursor.fetchall()

    connection.close()

    result = []

    for row in rows:

        book = dict(row)

        if book["available"] == 0:

            late_days, fine = calculate_fine(
                book["due_date"]
            )

        else:

            late_days = 0
            fine = 0

        book["late_days"] = late_days

        book["current_fine"] = fine

        result.append(book)

    return jsonify(
        result
    )


# ============================================================
# USER API
# ============================================================

@app.route(
    "/api/users",
    methods=["GET"]
)
def get_users():

    connection = get_db_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM users
        ORDER BY user_id
    """)

    rows = cursor.fetchall()

    connection.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# ============================================================
# TRANSACTION API
# ============================================================

@app.route(
    "/api/transactions",
    methods=["GET"]
)
def get_transactions():

    connection = get_db_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM transactions
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()

    connection.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# ============================================================
# TEST OVERDUE API
# ============================================================

@app.route(
    "/api/test/make-overdue/<book_id>/<int:days>",
    methods=["POST"]
)
@login_required
def make_book_overdue(
    book_id,
    days
):

    connection = get_db_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM books
        WHERE book_id = ?
    """, (book_id,))

    book = cursor.fetchone()

    if not book:

        connection.close()

        return jsonify({
            "success": False,
            "message":
                "Book not found"
        }), 404

    if book["available"] == 1:

        connection.close()

        return jsonify({
            "success": False,
            "message":
                "Book must be issued first"
        }), 400

    new_due_date = (
        datetime.now()
        - timedelta(
            days=days
        )
    )

    cursor.execute("""
        UPDATE books
        SET due_date = ?
        WHERE book_id = ?
    """, (
        new_due_date.isoformat(),
        book_id
    ))

    connection.commit()

    connection.close()

    return jsonify({

        "success": True,

        "message":
            "Book made overdue",

        "book_id":
            book_id,

        "new_due_date":
            new_due_date.isoformat(),

        "simulated_late_days":
            days

    })


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    if session.get(
        "admin_logged_in"
    ):

        return redirect(
            url_for("dashboard")
        )

    return redirect(
        url_for("login")
    )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================
# Gunicorn imports this module instead of executing it as
# __main__. Therefore the database must be initialized when the
# Flask application is imported.
#
# INSERT OR IGNORE is used by add_sample_data(), so this is safe
# to run on every application start.
# ============================================================

initialize_database()
add_sample_data()


# ============================================================
# SERVER START
# ============================================================

if __name__ == "__main__":

    print(
        "\n========================================"
    )

    print(
        "📚 AUTOMATED LIBRARY MANAGEMENT SYSTEM"
    )

    print(
        "========================================"
    )

    print(
        "Dashboard:"
    )

    print(
        "http://127.0.0.1:5000/dashboard"
    )

    print(
        "Login:"
    )

    print(
        "http://127.0.0.1:5000/login"
    )

    print(
        "IoT API:"
    )

    print(
        "http://127.0.0.1:5000/api/update"
    )

    print(
        "Loan Period:",
        LOAN_DAYS,
        "days"
    )

    print(
        "Fine:",
        "₹" + str(FINE_PER_DAY),
        "per day"
    )

    print(
        "Email:",
        "Enabled"
        if EMAIL_ENABLED
        else "Disabled"
    )

    print(
        "Admin:",
        ADMIN_USERNAME
    )

    print(
        "========================================\n"
    )

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False
    )