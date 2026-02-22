import calendar
import base64
import io
import mimetypes
import hashlib
import os
import sqlite3
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_paste_button import paste_image_button


DB_PATH = Path("trading_journal.db")
TRADE_IMAGE_DIR = Path("trade_images")
LOGO_CANDIDATES = [
    Path("logo.png"),
    Path("logo.jpg"),
    Path("logo.jpeg"),
    Path("assets/logo.png"),
    Path("assets/logo.jpg"),
]
PAGE_ORDER = {"landing": 0, "login": 1, "register": 2, "app": 3}
DEBUG_DEFAULT = os.getenv("APP_DEBUG", "0").strip() == "1"
THEME_PRESETS = {
    "Midnight": {
        "bg_color": "#0f1117",
        "surface_color": "#1f2333",
        "text_color": "#f6f8ff",
        "accent_color": "#5b7cfa",
    },
    "Forest": {
        "bg_color": "#0f1714",
        "surface_color": "#1c2b24",
        "text_color": "#eefcf4",
        "accent_color": "#25a86c",
    },
    "Slate": {
        "bg_color": "#14161c",
        "surface_color": "#262a35",
        "text_color": "#f0f3ff",
        "accent_color": "#8a6bff",
    },
}


@dataclass
class TradeInput:
    trade_date: str
    account_id: int
    symbol: str
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    fees: float
    tags: str
    notes: str
    image_path: str
    manual_net_pnl: float | None = None


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column_name: str, definition: str) -> None:
    existing_cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column_name not in existing_cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        conn.commit()


def migrate_accounts_table_if_needed(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if not row or not row["sql"]:
        return

    schema_sql = row["sql"]
    has_legacy_unique_name = "name TEXT NOT NULL UNIQUE" in schema_sql
    has_user_scoped_unique = "UNIQUE (user_id, name)" in schema_sql
    if not has_legacy_unique_name and has_user_scoped_unique:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            broker TEXT,
            account_type TEXT,
            description TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE (user_id, name)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO accounts_new (id, user_id, name, broker, account_type, description, created_at)
        SELECT id, user_id, name, broker, account_type, description, created_at
        FROM accounts
        """
    )
    conn.execute("DROP TABLE accounts")
    conn.execute("ALTER TABLE accounts_new RENAME TO accounts")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            broker TEXT,
            account_type TEXT,
            description TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE (user_id, name)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            trade_date TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            fees REAL NOT NULL DEFAULT 0,
            gross_pnl REAL NOT NULL,
            net_pnl REAL NOT NULL,
            tags TEXT,
            notes TEXT,
            image_path TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_cashflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            flow_date TEXT NOT NULL,
            flow_type TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remember_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_themes (
            user_id INTEGER PRIMARY KEY,
            theme_name TEXT NOT NULL,
            bg_color TEXT NOT NULL,
            surface_color TEXT NOT NULL,
            text_color TEXT NOT NULL,
            accent_color TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_theme_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            profile_name TEXT NOT NULL,
            bg_color TEXT NOT NULL,
            surface_color TEXT NOT NULL,
            text_color TEXT NOT NULL,
            accent_color TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, profile_name),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.commit()

    # Backward compatibility for existing DBs created before auth.
    ensure_column(conn, "accounts", "user_id", "user_id INTEGER")
    ensure_column(conn, "trades", "user_id", "user_id INTEGER")
    ensure_column(conn, "trades", "image_path", "image_path TEXT")
    migrate_accounts_table_if_needed(conn)


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        120000,
    ).hex()


def create_user(conn: sqlite3.Connection, username: str, email: str, password: str) -> tuple[bool, str]:
    username = username.strip()
    email = email.strip()
    if len(username) < 3:
        return False, "Username must be at least 3 characters."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."

    salt = hashlib.sha256(f"{username}{time.time()}".encode("utf-8")).hexdigest()[:32]
    password_hash = hash_password(password, salt)
    now = datetime.now().isoformat(timespec="seconds")

    try:
        cursor = conn.execute(
            """
            INSERT INTO users (username, email, password_hash, password_salt, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, email, password_hash, salt, now),
        )
        user_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO accounts (user_id, name, broker, account_type, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, "Main", "Unknown", "Cash", "Default account", now),
        )
        conn.commit()
        return True, "Account created."
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if "users.username" in str(exc) or "users.username" in repr(exc) or "UNIQUE constraint failed: users.username" in str(exc):
            return False, "Username already exists."
        return False, "Could not create account. Please try again."


def authenticate_user(conn: sqlite3.Connection, username: str, password: str) -> tuple[bool, str, int | None]:
    row = conn.execute(
        "SELECT id, username, password_hash, password_salt FROM users WHERE username = ?",
        (username.strip(),),
    ).fetchone()
    if not row:
        return False, "Invalid username or password.", None

    candidate_hash = hash_password(password, row["password_salt"])
    if candidate_hash != row["password_hash"]:
        return False, "Invalid username or password.", None
    return True, "Login successful.", int(row["id"])


def create_remember_token(conn: sqlite3.Connection, user_id: int, days_valid: int = 30) -> str:
    raw = f"{uuid.uuid4().hex}{uuid.uuid4().hex}"
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = datetime.now()
    expires = now + timedelta(days=days_valid)
    conn.execute(
        """
        INSERT INTO remember_tokens (user_id, token_hash, expires_at, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, token_hash, expires.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
    )
    conn.commit()
    return raw


def authenticate_with_remember_token(
    conn: sqlite3.Connection, raw_token: str
) -> tuple[bool, int | None, str | None]:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        SELECT rt.user_id, rt.expires_at, u.username
        FROM remember_tokens rt
        JOIN users u ON u.id = rt.user_id
        WHERE rt.token_hash = ?
        """,
        (token_hash,),
    ).fetchone()
    if not row:
        return False, None, None

    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at < datetime.now():
        conn.execute("DELETE FROM remember_tokens WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return False, None, None

    return True, int(row["user_id"]), str(row["username"])


def revoke_remember_token(conn: sqlite3.Connection, raw_token: str | None) -> None:
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    conn.execute("DELETE FROM remember_tokens WHERE token_hash = ?", (token_hash,))
    conn.commit()


def get_accounts(conn: sqlite3.Connection, user_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM accounts WHERE user_id = ? ORDER BY name",
        conn,
        params=(user_id,),
    )


def get_user_theme(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        """
        SELECT theme_name, bg_color, surface_color, text_color, accent_color
        FROM user_themes
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    if not row:
        default = THEME_PRESETS["Midnight"].copy()
        default["theme_name"] = "Midnight"
        return default
    return {
        "theme_name": str(row["theme_name"]),
        "bg_color": str(row["bg_color"]),
        "surface_color": str(row["surface_color"]),
        "text_color": str(row["text_color"]),
        "accent_color": str(row["accent_color"]),
    }


def save_user_theme(conn: sqlite3.Connection, user_id: int, theme: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO user_themes (user_id, theme_name, bg_color, surface_color, text_color, accent_color, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            theme_name=excluded.theme_name,
            bg_color=excluded.bg_color,
            surface_color=excluded.surface_color,
            text_color=excluded.text_color,
            accent_color=excluded.accent_color,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            theme["theme_name"],
            theme["bg_color"],
            theme["surface_color"],
            theme["text_color"],
            theme["accent_color"],
            now,
        ),
    )
    conn.commit()


def save_user_theme_profile(conn: sqlite3.Connection, user_id: int, profile_name: str, theme: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO user_theme_profiles
            (user_id, profile_name, bg_color, surface_color, text_color, accent_color, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, profile_name) DO UPDATE SET
            bg_color=excluded.bg_color,
            surface_color=excluded.surface_color,
            text_color=excluded.text_color,
            accent_color=excluded.accent_color,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            profile_name.strip(),
            theme["bg_color"],
            theme["surface_color"],
            theme["text_color"],
            theme["accent_color"],
            now,
        ),
    )
    conn.commit()


def list_user_theme_profiles(conn: sqlite3.Connection, user_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT profile_name
        FROM user_theme_profiles
        WHERE user_id = ?
        ORDER BY profile_name
        """,
        (user_id,),
    ).fetchall()
    return [str(row["profile_name"]) for row in rows]


def load_user_theme_profile(conn: sqlite3.Connection, user_id: int, profile_name: str) -> dict | None:
    row = conn.execute(
        """
        SELECT profile_name, bg_color, surface_color, text_color, accent_color
        FROM user_theme_profiles
        WHERE user_id = ? AND profile_name = ?
        """,
        (user_id, profile_name),
    ).fetchone()
    if not row:
        return None
    return {
        "theme_name": "Custom",
        "bg_color": str(row["bg_color"]),
        "surface_color": str(row["surface_color"]),
        "text_color": str(row["text_color"]),
        "accent_color": str(row["accent_color"]),
    }


def apply_user_theme(theme: dict) -> None:
    bg = theme["bg_color"]
    surface = theme["surface_color"]
    text = theme["text_color"]
    accent = theme["accent_color"]
    st.markdown(
        f"""
        <style>
        [data-testid="stAppViewContainer"] {{
            background:
                radial-gradient(circle at 8% 10%, color-mix(in srgb, {accent} 22%, transparent) 0%, transparent 26%),
                radial-gradient(circle at 92% 4%, color-mix(in srgb, {surface} 50%, transparent) 0%, transparent 22%),
                linear-gradient(160deg, color-mix(in srgb, {bg} 90%, #06080f 10%) 0%, {bg} 100%) !important;
        }}
        section.main > div[data-testid="stMainBlockContainer"] {{
            max-width: 1500px;
            padding-top: 0.35rem !important;
            padding-left: 0.65rem !important;
            padding-right: 0.9rem !important;
        }}
        [data-testid="stSidebar"] {{
            background:
                linear-gradient(
                    180deg,
                    color-mix(in srgb, {surface} 88%, black 12%) 0%,
                    color-mix(in srgb, {surface} 80%, black 20%) 100%
                ) !important;
            border-right: 1px solid color-mix(in srgb, {accent} 30%, #283048 70%);
        }}
        [data-testid="stSidebar"] * {{
            color: {text} !important;
        }}
        h1, h2, h3, h4, h5, h6, p, label, span, div {{
            color: {text};
        }}
        h1 {{
            font-weight: 800 !important;
            letter-spacing: 0.2px;
        }}
        .stCaption {{
            color: color-mix(in srgb, {text} 72%, #8a93a5 28%) !important;
        }}
        [data-testid="stVerticalBlock"] > [data-testid="element-container"] {{
            margin-bottom: 0.35rem;
        }}
        [data-testid="stMetric"] {{
            background:
                linear-gradient(
                    180deg,
                    color-mix(in srgb, {surface} 92%, black 8%) 0%,
                    color-mix(in srgb, {surface} 86%, black 14%) 100%
                );
            border: 1px solid color-mix(in srgb, {accent} 38%, #1f2432 62%);
            border-radius: 14px;
            box-shadow: 0 8px 18px rgba(0, 0, 0, 0.18);
            padding: 0.5rem 0.55rem;
        }}
        [data-testid="stMetricLabel"] {{
            font-weight: 600 !important;
            color: color-mix(in srgb, {text} 80%, #9aa6bf 20%) !important;
        }}
        [data-testid="stMetricValue"] {{
            font-weight: 700 !important;
        }}
        .stTabs [data-baseweb="tab-list"] {{
            gap: 0.5rem;
            border-bottom: 1px solid color-mix(in srgb, {accent} 24%, #2b3141 76%);
            padding-bottom: 0.2rem;
        }}
        .stTabs [data-baseweb="tab"] {{
            border-radius: 9px 9px 0 0;
            background: transparent;
            padding: 0.45rem 0.7rem;
            font-weight: 600;
        }}
        .stTabs [aria-selected="true"] {{
            background: color-mix(in srgb, {surface} 70%, #121622 30%) !important;
            border: 1px solid color-mix(in srgb, {accent} 45%, #293047 55%) !important;
            border-bottom: 1px solid color-mix(in srgb, {surface} 70%, #121622 30%) !important;
        }}
        div[data-baseweb="input"] > div,
        div[data-baseweb="base-input"] > div,
        div[data-baseweb="select"] > div,
        .stDateInput > div > div {{
            background: color-mix(in srgb, {surface} 85%, #0d111b 15%) !important;
            border: 1px solid color-mix(in srgb, {accent} 30%, #2c3345 70%) !important;
            border-radius: 10px !important;
        }}
        textarea, input {{
            color: {text} !important;
        }}
        [data-testid="stDataFrame"] {{
            border: 1px solid color-mix(in srgb, {accent} 25%, #2b3142 75%);
            border-radius: 12px;
            overflow: hidden;
        }}
        [data-testid="stExpander"] {{
            border: 1px solid color-mix(in srgb, {accent} 30%, #2d3448 70%);
            border-radius: 12px;
            background: color-mix(in srgb, {surface} 86%, black 14%);
        }}
        .stButton button {{
            border-radius: 10px !important;
            background: color-mix(in srgb, {surface} 82%, black 18%) !important;
            color: {text} !important;
            border-color: color-mix(in srgb, {accent} 55%, #444 45%) !important;
            transition: transform 0.15s ease, filter 0.15s ease, box-shadow 0.15s ease;
        }}
        .stButton button[kind="primary"] {{
            background: linear-gradient(
                90deg,
                color-mix(in srgb, {accent} 90%, #ffffff 10%),
                color-mix(in srgb, {accent} 75%, #000000 25%)
            ) !important;
            color: #ffffff !important;
        }}
        .stButton button:hover {{
            filter: brightness(1.08);
            transform: translateY(-1px);
            box-shadow: 0 8px 16px rgba(0, 0, 0, 0.25);
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def get_trades(conn: sqlite3.Connection, user_id: int) -> pd.DataFrame:
    query = """
        SELECT
            t.*,
            a.name AS account_name
        FROM trades t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.user_id = ?
        ORDER BY t.trade_date DESC, t.id DESC
    """
    return pd.read_sql_query(query, conn, params=(user_id,))


def get_cashflows(conn: sqlite3.Connection, user_id: int) -> pd.DataFrame:
    query = """
        SELECT
            c.*,
            a.name AS account_name
        FROM account_cashflows c
        JOIN accounts a ON a.id = c.account_id
        WHERE c.user_id = ?
        ORDER BY c.flow_date DESC, c.id DESC
    """
    return pd.read_sql_query(query, conn, params=(user_id,))


def calculate_pnl(
    side: str, quantity: float, entry_price: float, exit_price: float, fees: float
) -> tuple[float, float]:
    if side == "Long":
        gross = (exit_price - entry_price) * quantity
    else:
        gross = (entry_price - exit_price) * quantity
    net = gross - fees
    return gross, net


def save_trade_image(uploaded_file, user_id: int, pasted_image_bytes: bytes | None = None) -> str:
    if uploaded_file is None and pasted_image_bytes is None:
        return ""

    TRADE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    user_dir = TRADE_IMAGE_DIR / f"user_{user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)

    if pasted_image_bytes is not None:
        suffix = ".png"
    else:
        suffix = Path(uploaded_file.name).suffix.lower() or ".png"

    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:10]}{suffix}"
    destination = user_dir / filename
    if pasted_image_bytes is not None:
        destination.write_bytes(pasted_image_bytes)
    else:
        destination.write_bytes(uploaded_file.getvalue())
    return str(destination)


def get_next_available_trade_id(conn: sqlite3.Connection) -> int:
    ids = [int(row["id"]) for row in conn.execute("SELECT id FROM trades ORDER BY id").fetchall()]
    expected = 1
    for current in ids:
        if current == expected:
            expected += 1
        elif current > expected:
            break
    return expected


def save_trade(conn: sqlite3.Connection, user_id: int, trade: TradeInput) -> None:
    if trade.manual_net_pnl is not None:
        net = float(trade.manual_net_pnl)
        gross = net + float(trade.fees)
    else:
        gross, net = calculate_pnl(
            trade.side, trade.quantity, trade.entry_price, trade.exit_price, trade.fees
        )
    now = datetime.now().isoformat(timespec="seconds")
    next_trade_id = get_next_available_trade_id(conn)
    conn.execute(
        """
        INSERT INTO trades (
            id, user_id, trade_date, account_id, symbol, side, quantity, entry_price, exit_price,
            fees, gross_pnl, net_pnl, tags, notes, image_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            next_trade_id,
            user_id,
            trade.trade_date,
            trade.account_id,
            trade.symbol.upper().strip(),
            trade.side,
            trade.quantity,
            trade.entry_price,
            trade.exit_price,
            trade.fees,
            gross,
            net,
            trade.tags.strip(),
            trade.notes.strip(),
            trade.image_path.strip(),
            now,
        ),
    )
    conn.commit()


def update_trade(
    conn: sqlite3.Connection,
    user_id: int,
    trade_id: int,
    trade_date: str,
    account_id: int,
    symbol: str,
    side: str,
    quantity: float,
    entry_price: float,
    exit_price: float,
    fees: float,
    tags: str,
    notes: str,
    new_image_path: str = "",
    clear_image: bool = False,
) -> bool:
    row = conn.execute(
        "SELECT image_path FROM trades WHERE id = ? AND user_id = ?",
        (trade_id, user_id),
    ).fetchone()
    if not row:
        return False

    old_image_path = row["image_path"] or ""
    gross, net = calculate_pnl(side, quantity, entry_price, exit_price, fees)
    final_image_path = old_image_path
    if new_image_path.strip():
        final_image_path = new_image_path.strip()
        if old_image_path and old_image_path != final_image_path:
            old_file = Path(old_image_path)
            if old_file.exists():
                old_file.unlink(missing_ok=True)
    elif clear_image:
        final_image_path = ""
        if old_image_path:
            old_file = Path(old_image_path)
            if old_file.exists():
                old_file.unlink(missing_ok=True)

    result = conn.execute(
        """
        UPDATE trades
        SET trade_date = ?, account_id = ?, symbol = ?, side = ?, quantity = ?,
            entry_price = ?, exit_price = ?, fees = ?, gross_pnl = ?, net_pnl = ?,
            tags = ?, notes = ?, image_path = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            trade_date,
            account_id,
            symbol.upper().strip(),
            side,
            quantity,
            entry_price,
            exit_price,
            fees,
            gross,
            net,
            tags.strip(),
            notes.strip(),
            final_image_path,
            trade_id,
            user_id,
        ),
    )
    conn.commit()
    return result.rowcount > 0


def add_account(
    conn: sqlite3.Connection,
    user_id: int,
    name: str,
    broker: str,
    account_type: str,
    description: str,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO accounts (user_id, name, broker, account_type, description, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, name.strip(), broker.strip(), account_type.strip(), description.strip(), now),
    )
    conn.commit()


def delete_trade(conn: sqlite3.Connection, trade_id: int, user_id: int) -> bool:
    row = conn.execute(
        "SELECT image_path FROM trades WHERE id = ? AND user_id = ?",
        (trade_id, user_id),
    ).fetchone()
    result = conn.execute("DELETE FROM trades WHERE id = ? AND user_id = ?", (trade_id, user_id))
    conn.commit()
    deleted = result.rowcount > 0
    if row and row["image_path"]:
        image_file = Path(row["image_path"])
        if image_file.exists():
            image_file.unlink(missing_ok=True)
    return deleted


def add_cashflow(
    conn: sqlite3.Connection,
    user_id: int,
    account_id: int,
    flow_date: str,
    flow_type: str,
    amount: float,
    note: str,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    signed_amount = amount if flow_type == "Deposit" else -amount
    conn.execute(
        """
        INSERT INTO account_cashflows (user_id, account_id, flow_date, flow_type, amount, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, account_id, flow_date, flow_type, signed_amount, note.strip(), now),
    )
    conn.commit()


def delete_account(conn: sqlite3.Connection, user_id: int, account_id: int) -> bool:
    conn.execute("DELETE FROM trades WHERE user_id = ? AND account_id = ?", (user_id, account_id))
    conn.execute("DELETE FROM account_cashflows WHERE user_id = ? AND account_id = ?", (user_id, account_id))
    result = conn.execute("DELETE FROM accounts WHERE user_id = ? AND id = ?", (user_id, account_id))
    conn.commit()
    return result.rowcount > 0


def account_metrics(trades_df: pd.DataFrame, cashflows_df: pd.DataFrame) -> dict:
    cash_total = float(cashflows_df["amount"].sum()) if not cashflows_df.empty else 0.0
    if trades_df.empty:
        return {
            "total_net": 0.0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_net": 0.0,
            "account_balance": cash_total,
            "win_streak": 0,
            "best_win_streak": 0,
        }

    wins = int((trades_df["net_pnl"] > 0).sum())
    losses = int((trades_df["net_pnl"] < 0).sum())
    total = len(trades_df)
    total_net = float(trades_df["net_pnl"].sum())

    ordered = trades_df.copy()
    ordered["trade_date"] = pd.to_datetime(ordered["trade_date"])
    ordered = ordered.sort_values(["trade_date", "id"])

    current_streak = 0
    best_streak = 0
    for pnl in ordered["net_pnl"].tolist():
        if pnl > 0:
            current_streak += 1
            best_streak = max(best_streak, current_streak)
        else:
            current_streak = 0

    return {
        "total_net": total_net,
        "wins": wins,
        "losses": losses,
        "win_rate": float((wins / total) * 100 if total else 0),
        "avg_net": float(trades_df["net_pnl"].mean()),
        "account_balance": total_net + cash_total,
        "win_streak": current_streak,
        "best_win_streak": best_streak,
    }


def render_pnl_calendar(trades_df: pd.DataFrame, month: int, year: int) -> None:
    st.subheader("P&L Calendar")
    temp = trades_df.copy()
    if not temp.empty:
        temp["trade_date"] = pd.to_datetime(temp["trade_date"]).dt.date
        month_days = temp[
            (pd.to_datetime(temp["trade_date"]).dt.month == month)
            & (pd.to_datetime(temp["trade_date"]).dt.year == year)
        ]
        day_summary = (
            month_days.groupby("trade_date", as_index=False)
            .agg(net_pnl=("net_pnl", "sum"), trades=("id", "count"))
            .sort_values("trade_date")
        )
        day_map = {
            row["trade_date"].day: {"net_pnl": float(row["net_pnl"]), "trades": int(row["trades"])}
            for _, row in day_summary.iterrows()
        }
    else:
        day_map = {}

    weeks = calendar.monthcalendar(year, month)

    st.markdown(
        """
        <style>
        .pnl-wrap {
            border: 1px solid #232733;
            border-radius: 12px;
            background: radial-gradient(circle at top left, #1b202d 0%, #121620 70%);
            padding: 14px;
        }
        .pnl-grid {
            display: grid;
            grid-template-columns: repeat(8, minmax(90px, 1fr));
            gap: 8px;
        }
        .pnl-head {
            color: #9ba3b4;
            font-size: 13px;
            font-weight: 600;
            letter-spacing: 0.4px;
            padding: 4px 6px 8px 6px;
        }
        .pnl-head-last {
            background: linear-gradient(90deg, #5536d6 0%, #7b4ef2 100%);
            color: #f3edff;
            border-radius: 8px;
            text-align: center;
        }
        .day-cell, .week-cell {
            border: 1px solid #2b313f;
            border-radius: 10px;
            min-height: 92px;
            padding: 8px;
            background: rgba(15, 18, 25, 0.7);
        }
        .day-num {
            color: #eef3ff;
            font-size: 17px;
            font-weight: 700;
        }
        .day-pnl {
            margin-top: 20px;
            font-size: 15px;
            font-weight: 700;
        }
        .day-trades {
            margin-top: 3px;
            color: #91a0b8;
            font-size: 12px;
        }
        .pnl-pos { color: #2acc74; }
        .pnl-neg { color: #ef5350; }
        .pnl-flat { color: #7f8ca3; }
        .week-cell {
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .week-pos {
            background: linear-gradient(180deg, rgba(30, 114, 69, 0.72) 0%, rgba(25, 82, 53, 0.85) 100%);
            border-color: #226e49;
        }
        .week-neg {
            background: linear-gradient(180deg, rgba(138, 39, 45, 0.75) 0%, rgba(97, 26, 30, 0.9) 100%);
            border-color: #8d313b;
        }
        .week-flat {
            background: rgba(35, 39, 49, 0.55);
        }
        .week-body {
            text-align: center;
        }
        .week-label {
            color: #b6bfce;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.8px;
        }
        .week-pnl {
            margin-top: 8px;
            font-size: 30px;
            font-weight: 800;
            line-height: 1.05;
        }
        .week-trades {
            margin-top: 10px;
            color: #c3cad7;
            font-size: 12px;
            font-weight: 600;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    headers = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "P&L"]
    html_parts = ['<div class="pnl-wrap"><div class="pnl-grid">']

    for i, h in enumerate(headers):
        extra = " pnl-head-last" if i == len(headers) - 1 else ""
        html_parts.append(f'<div class="pnl-head{extra}">{h}</div>')

    for week in weeks:
        week_pnl = 0.0
        week_trades = 0
        for day in week[:7]:
            if day == 0:
                html_parts.append('<div class="day-cell"></div>')
                continue

            day_data = day_map.get(day, {"net_pnl": 0.0, "trades": 0})
            day_pnl = day_data["net_pnl"]
            day_trades = day_data["trades"]
            week_pnl += day_pnl
            week_trades += day_trades

            pnl_class = "pnl-flat"
            if day_pnl > 0:
                pnl_class = "pnl-pos"
            elif day_pnl < 0:
                pnl_class = "pnl-neg"

            pnl_html = ""
            if day_trades > 0:
                pnl_html = (
                    f'<div class="day-pnl {pnl_class}">${day_pnl:,.0f}</div>'
                    f'<div class="day-trades">{day_trades} trade{"s" if day_trades != 1 else ""}</div>'
                )

            html_parts.append(f'<div class="day-cell"><div class="day-num">{day}</div>{pnl_html}</div>')

        week_class = "week-flat"
        week_pnl_class = "pnl-flat"
        if week_pnl > 0:
            week_class = "week-pos"
            week_pnl_class = "pnl-pos"
        elif week_pnl < 0:
            week_class = "week-neg"
            week_pnl_class = "pnl-neg"

        html_parts.append(
            f"""
            <div class="week-cell {week_class}">
                <div class="week-body">
                    <div class="week-label">WEEK</div>
                    <div class="week-pnl {week_pnl_class}">${week_pnl:,.0f}</div>
                    <div class="week-trades">{week_trades} trade{"s" if week_trades != 1 else ""}</div>
                </div>
            </div>
            """
        )

    html_parts.append("</div></div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def get_logo_path() -> Path | None:
    for candidate in LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def get_logo_data_uri() -> str | None:
    logo_path = get_logo_path()
    if not logo_path:
        return None
    mime_type = mimetypes.guess_type(str(logo_path))[0] or "image/png"
    encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def get_transition_animation(from_page: str | None, to_page: str) -> str:
    if not from_page or from_page == to_page:
        return "fade"
    from_idx = PAGE_ORDER.get(from_page, 0)
    to_idx = PAGE_ORDER.get(to_page, 0)
    if to_idx > from_idx:
        return "forward"
    return "backward"


def navigate_to(page: str) -> None:
    current_page = st.session_state.get("page", "landing")
    st.session_state["pending_transition_animation"] = get_transition_animation(current_page, page)
    st.session_state["page"] = page
    st.rerun()


def report_exception(context: str, exc: Exception) -> None:
    st.error(f"{context}: {exc}")
    if st.session_state.get("debug_mode", False):
        st.exception(exc)
        st.code(traceback.format_exc(), language="text")


def apply_pending_transition() -> None:
    transition = st.session_state.get("pending_transition_animation")
    if not transition:
        return
    if transition == "forward":
        anim = "screenWipeLeft 420ms cubic-bezier(0.2, 0.8, 0.2, 1)"
    elif transition == "backward":
        anim = "screenWipeRight 420ms cubic-bezier(0.2, 0.8, 0.2, 1)"
    else:
        anim = "screenFadeIn 320ms ease-out"
    st.markdown(
        f"""
        <style>
        @keyframes screenFadeIn {{
            0% {{ opacity: 0.55; visibility: visible; }}
            100% {{ opacity: 0; visibility: hidden; }}
        }}
        @keyframes screenWipeLeft {{
            0% {{ transform: translateX(-100%); opacity: 0.95; visibility: visible; }}
            60% {{ transform: translateX(0%); opacity: 0.95; visibility: visible; }}
            100% {{ transform: translateX(100%); opacity: 0; visibility: hidden; }}
        }}
        @keyframes screenWipeRight {{
            0% {{ transform: translateX(100%); opacity: 0.95; visibility: visible; }}
            60% {{ transform: translateX(0%); opacity: 0.95; visibility: visible; }}
            100% {{ transform: translateX(-100%); opacity: 0; visibility: hidden; }}
        }}
        .page-transition-overlay {{
            position: fixed;
            inset: 0;
            z-index: 999999;
            pointer-events: none;
            background: linear-gradient(110deg, rgba(84, 57, 223, 0.52), rgba(14, 114, 95, 0.42));
            animation: {anim} forwards;
            will-change: transform, opacity;
        }}
        </style>
        <div class="page-transition-overlay"></div>
        """,
        unsafe_allow_html=True,
    )
    st.session_state["pending_transition_animation"] = None


def inject_responsive_css() -> None:
    st.markdown(
        """
        <style>
        @media (max-width: 900px) {
            section.main > div[data-testid="stMainBlockContainer"] {
                padding-left: 0.75rem !important;
                padding-right: 0.75rem !important;
            }
            div[data-testid="stHorizontalBlock"] {
                flex-wrap: wrap !important;
                row-gap: 0.5rem !important;
            }
            div[data-testid="column"] {
                min-width: 100% !important;
                flex: 1 1 100% !important;
            }
            .landing-title {
                font-size: 38px !important;
            }
            .load-logo,
            .load-logo-fallback {
                width: 140px !important;
                height: 140px !important;
            }
            .welcome-name {
                font-size: 40px !important;
            }
            .pnl-wrap {
                overflow-x: auto !important;
            }
            .pnl-grid {
                min-width: 980px !important;
            }
        }
        @media (max-width: 600px) {
            .landing-title {
                font-size: 34px !important;
            }
            .welcome-name {
                font-size: 34px !important;
            }
            .welcome-sub {
                font-size: 14px !important;
            }
            .auth-title {
                font-size: 30px !important;
            }
            .day-cell, .week-cell {
                min-height: 80px !important;
            }
            button, input, textarea, [data-baseweb="select"] {
                font-size: 16px !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_fullscreen_welcome(username: str) -> None:
    st.markdown(
        f"""
        <style>
        .welcome-fullscreen {{
            position: fixed;
            inset: 0;
            z-index: 999998;
            background:
                radial-gradient(circle at 20% 20%, rgba(95, 68, 249, 0.28), transparent 32%),
                radial-gradient(circle at 80% 20%, rgba(19, 153, 117, 0.22), transparent 30%),
                linear-gradient(160deg, #070b15 0%, #0b1320 55%, #080d17 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            animation: welcomeBg 1.6s ease-out forwards;
        }}
        .welcome-content {{
            text-align: center;
            color: #f7fbff;
            transform: translateY(10px) scale(0.96);
            animation: welcomeText 1.6s cubic-bezier(0.2, 0.8, 0.2, 1) forwards;
        }}
        .welcome-label {{
            font-size: 14px;
            color: #9db6df;
            letter-spacing: 2px;
            margin-bottom: 10px;
        }}
        .welcome-name {{
            font-size: 58px;
            font-weight: 900;
            line-height: 1;
            text-shadow: 0 8px 25px rgba(20, 24, 40, 0.45);
        }}
        .welcome-sub {{
            margin-top: 10px;
            font-size: 16px;
            color: #bfd2ef;
        }}
        @keyframes welcomeText {{
            0% {{ opacity: 0; transform: translateY(14px) scale(0.96); }}
            25% {{ opacity: 1; transform: translateY(0) scale(1); }}
            80% {{ opacity: 1; transform: translateY(0) scale(1); }}
            100% {{ opacity: 0; transform: translateY(-10px) scale(1.02); }}
        }}
        @keyframes welcomeBg {{
            0% {{ opacity: 0; }}
            15% {{ opacity: 1; }}
            88% {{ opacity: 1; }}
            100% {{ opacity: 0; }}
        }}
        </style>
        <div class="welcome-fullscreen">
            <div class="welcome-content">
                <div class="welcome-label">WELCOME</div>
                <div class="welcome-name">{username}</div>
                <div class="welcome-sub">Your trading journal is ready.</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    time.sleep(1.6)
    st.session_state["show_welcome_once"] = False
    st.rerun()


def render_loading_screen() -> None:
    logo_data_uri = get_logo_data_uri()
    logo_html = (
        f'<img src="{logo_data_uri}" alt="Logo" class="load-logo" />'
        if logo_data_uri
        else '<div class="load-logo-fallback">LOGO</div>'
    )

    st.markdown(
        f"""
        <style>
        .load-wrap {{
            min-height: 70vh;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
            animation: fadeIn 0.7s ease-out;
        }}
        .load-logo {{
            width: 180px;
            height: 180px;
            object-fit: cover;
            border-radius: 20px;
            border: 2px solid #3a4256;
            box-shadow: 0 14px 35px rgba(0, 0, 0, 0.45);
            animation: floatPulse 1.8s ease-in-out infinite;
        }}
        .load-logo-fallback {{
            width: 180px;
            height: 180px;
            border-radius: 20px;
            border: 2px solid #3a4256;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            color: #dce4ff;
            background: #1a2030;
            animation: floatPulse 1.8s ease-in-out infinite;
        }}
        .load-title {{
            margin-top: 16px;
            color: #f6f8ff;
            font-size: 28px;
            font-weight: 800;
            letter-spacing: 0.3px;
        }}
        .load-sub {{
            color: #9ca8bc;
            font-size: 14px;
            margin-top: 6px;
            margin-bottom: 18px;
        }}
        .load-line {{
            width: min(360px, 88vw);
            height: 8px;
            border-radius: 999px;
            overflow: hidden;
            background: #212838;
            border: 1px solid #31394b;
        }}
        .load-line::before {{
            content: "";
            display: block;
            height: 100%;
            width: 40%;
            border-radius: 999px;
            background: linear-gradient(90deg, #5a3de6, #8c65ff);
            animation: slideLine 1.2s ease-in-out infinite;
        }}
        @keyframes floatPulse {{
            0% {{ transform: translateY(0) scale(1); }}
            50% {{ transform: translateY(-8px) scale(1.03); }}
            100% {{ transform: translateY(0) scale(1); }}
        }}
        @keyframes slideLine {{
            0% {{ transform: translateX(-120%); }}
            100% {{ transform: translateX(320%); }}
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(8px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        </style>
        <div class="load-wrap">
            {logo_html}
            <div class="load-title">Trading Journal</div>
            <div class="load-sub">Loading your workspace...</div>
            <div class="load-line"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    time.sleep(1.6)
    st.session_state["landing_loaded"] = True
    st.rerun()


def render_landing_page() -> None:
    st.markdown(
        """
        <style>
        .landing-logo {
            display: flex;
            justify-content: center;
            margin-top: 10px;
            margin-bottom: 12px;
        }
        .landing-logo img {
            width: 190px;
            border-radius: 20px;
            border: 2px solid #3a4256;
            box-shadow: 0 14px 35px rgba(0, 0, 0, 0.35);
            animation: logoPulse 2.3s ease-in-out infinite;
        }
        .landing-title {
            font-size: 52px;
            font-weight: 800;
            color: #f6f8ff;
            line-height: 1;
            margin-top: 4px;
            margin-bottom: 4px;
            text-align: center;
        }
        .landing-subtitle {
            color: #9ca8bc;
            font-size: 17px;
            margin-bottom: 16px;
            text-align: center;
        }
        .landing-chip-row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            justify-content: center;
            margin-bottom: 18px;
        }
        .landing-chip {
            border: 1px solid #2f3648;
            border-radius: 999px;
            padding: 6px 12px;
            color: #b6c2d8;
            font-size: 12px;
            background: rgba(16, 20, 31, 0.75);
        }
        .landing-panel {
            border: 1px solid #2d3342;
            border-radius: 16px;
            padding: 22px;
            background:
                radial-gradient(circle at 15% 5%, rgba(88, 61, 228, 0.18), transparent 30%),
                linear-gradient(160deg, #131826 0%, #11141d 55%, #0f1118 100%);
            animation: fadeUp 0.65s ease-out;
        }
        .stButton button {
            transition: transform 0.2s ease, box-shadow 0.2s ease;
            border-radius: 10px;
        }
        .stButton button:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 18px rgba(40, 56, 96, 0.35);
        }
        @keyframes logoPulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.04); }
            100% { transform: scale(1); }
        }
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(12px); }
            to { opacity: 1; transform: translateY(0); }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, center_col, _ = st.columns([1, 1.35, 1])
    with center_col:
        with st.container(border=True):
            logo_path = get_logo_path()
            if logo_path:
                logo_data_uri = get_logo_data_uri()
                if logo_data_uri:
                    st.markdown(
                        f'<div class="landing-logo"><img src="{logo_data_uri}" alt="Logo"></div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.warning("Logo not found. Add `logo.png` in this folder to use your custom logo.")

            st.markdown('<div class="landing-title">Trading Journal</div>', unsafe_allow_html=True)
            st.markdown(
                """
                <div class="landing-chip-row">
                    <span class="landing-chip">Live P&L Calendar</span>
                    <span class="landing-chip">Image Trade Notes</span>
                    <span class="landing-chip">Multi-Account Tracking</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            c1, c2 = st.columns(2)
            if c1.button("Login", use_container_width=True):
                navigate_to("login")
            if c2.button("Register", use_container_width=True):
                navigate_to("register")


def render_login_page(conn: sqlite3.Connection) -> None:
    st.markdown(
        """
        <style>
        .auth-title {
            font-size: 36px;
            font-weight: 800;
            line-height: 1.1;
            color: #f4f7ff;
            margin-bottom: 6px;
            text-align: center;
        }
        .auth-subtitle {
            font-size: 14px;
            color: #9ba8bf;
            text-align: center;
            margin-bottom: 14px;
        }
        .auth-panel {
            border: 1px solid #2c3344;
            border-radius: 18px;
            padding: 20px;
            background:
                radial-gradient(circle at 15% 10%, rgba(96, 67, 255, 0.16), transparent 35%),
                linear-gradient(160deg, #121827 0%, #0f141f 60%, #0d1119 100%);
            box-shadow: 0 14px 36px rgba(0, 0, 0, 0.38);
            animation: authFade 0.45s ease-out;
        }
        div[data-testid="stForm"] {
            border: 1px solid #2e3546;
            border-radius: 14px;
            background: rgba(15, 19, 29, 0.72);
            padding: 16px 14px 6px 14px;
        }
        .auth-footnote {
            text-align: center;
            color: #9ca9c2;
            font-size: 13px;
            margin-top: 10px;
        }
        @keyframes authFade {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _, auth_col, _ = st.columns([1, 1.25, 1])
    with auth_col:
        with st.container(border=True):
            st.markdown('<div class="auth-title">Welcome Back</div>', unsafe_allow_html=True)
            st.markdown('<div class="auth-subtitle">Sign in to access your trading journal.</div>', unsafe_allow_html=True)

            with st.form("login_form"):
                username = st.text_input("Username", placeholder="Enter your username")
                password = st.text_input("Password", type="password", placeholder="Enter your password")
                remember_me = st.checkbox("Remember me for 30 days", value=False)
                submitted = st.form_submit_button("Sign In", use_container_width=True)
                if submitted:
                    ok, msg, user_id = authenticate_user(conn, username, password)
                    if ok and user_id is not None:
                        st.session_state["auth_user_id"] = user_id
                        st.session_state["auth_username"] = username.strip()
                        st.session_state["show_welcome_once"] = True
                        if remember_me:
                            raw_token = create_remember_token(conn, user_id, days_valid=30)
                            st.session_state["remember_token"] = raw_token
                            st.query_params["rt"] = raw_token
                        else:
                            st.session_state["remember_token"] = None
                            if "rt" in st.query_params:
                                del st.query_params["rt"]
                        st.success(msg)
                        navigate_to("app")
                    else:
                        st.error(msg)

            b1, b2 = st.columns(2)
            if b1.button("Back to Home", use_container_width=True):
                navigate_to("landing")
            if b2.button("Go to Register", use_container_width=True):
                navigate_to("register")
            st.markdown('<div class="auth-footnote">Secure local login for your shared app.</div>', unsafe_allow_html=True)


def render_register_page(conn: sqlite3.Connection) -> None:
    st.markdown(
        """
        <style>
        .auth-panel-register {
            border: 1px solid #2c3344;
            border-radius: 18px;
            padding: 20px;
            background:
                radial-gradient(circle at 85% 10%, rgba(28, 156, 110, 0.14), transparent 35%),
                linear-gradient(160deg, #121827 0%, #0f141f 60%, #0d1119 100%);
            box-shadow: 0 14px 36px rgba(0, 0, 0, 0.38);
            animation: authFade 0.45s ease-out;
        }
        .auth-title {
            font-size: 36px;
            font-weight: 800;
            line-height: 1.1;
            color: #f4f7ff;
            margin-bottom: 4px;
            text-align: center;
        }
        .auth-subtitle {
            font-size: 14px;
            color: #9ba8bf;
            text-align: center;
            margin-bottom: 16px;
        }
        div[data-testid="stForm"] {
            border: 1px solid #2e3546;
            border-radius: 14px;
            background: rgba(15, 19, 29, 0.72);
            padding: 16px 14px 6px 14px;
        }
        @keyframes authFade {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _, auth_col, _ = st.columns([1, 1.25, 1])
    with auth_col:
        with st.container(border=True):
            st.markdown('<div class="auth-title">Create Account</div>', unsafe_allow_html=True)
            st.markdown('<div class="auth-subtitle">Set up your profile to start journaling trades.</div>', unsafe_allow_html=True)

            with st.form("register_form"):
                username = st.text_input("Username", placeholder="Choose a username")
                email = st.text_input("Email (optional)", placeholder="name@email.com")
                password = st.text_input("Password", type="password", placeholder="At least 6 characters")
                confirm = st.text_input("Confirm Password", type="password", placeholder="Repeat password")
                submitted = st.form_submit_button("Create Account", use_container_width=True)
                if submitted:
                    if password != confirm:
                        st.error("Passwords do not match.")
                    else:
                        ok, msg = create_user(conn, username, email, password)
                        if ok:
                            st.success(msg)
                            navigate_to("login")
                        else:
                            st.error(msg)

            b1, b2 = st.columns(2)
            if b1.button("Back to Home", use_container_width=True):
                navigate_to("landing")
            if b2.button("Go to Login", use_container_width=True):
                navigate_to("login")


def render_dashboard(conn: sqlite3.Connection, user_id: int) -> None:
    saved_theme = get_user_theme(conn, user_id)
    if st.session_state.get("theme_editor_user_id") != user_id:
        st.session_state["theme_editor_user_id"] = user_id
        st.session_state["theme_name"] = saved_theme["theme_name"]
        st.session_state["theme_bg_color"] = saved_theme["bg_color"]
        st.session_state["theme_surface_color"] = saved_theme["surface_color"]
        st.session_state["theme_text_color"] = saved_theme["text_color"]
        st.session_state["theme_accent_color"] = saved_theme["accent_color"]
        st.session_state["theme_last_preset"] = saved_theme["theme_name"]

    if st.session_state.get("theme_reset_requested"):
        base = THEME_PRESETS["Midnight"]
        st.session_state["theme_name"] = "Midnight"
        st.session_state["theme_bg_color"] = base["bg_color"]
        st.session_state["theme_surface_color"] = base["surface_color"]
        st.session_state["theme_text_color"] = base["text_color"]
        st.session_state["theme_accent_color"] = base["accent_color"]
        st.session_state["theme_preset_select"] = "Midnight"
        st.session_state["theme_last_preset"] = "Midnight"
        st.session_state["theme_reset_requested"] = False

    active_theme = {
        "theme_name": st.session_state.get("theme_name", "Midnight"),
        "bg_color": st.session_state.get("theme_bg_color", THEME_PRESETS["Midnight"]["bg_color"]),
        "surface_color": st.session_state.get("theme_surface_color", THEME_PRESETS["Midnight"]["surface_color"]),
        "text_color": st.session_state.get("theme_text_color", THEME_PRESETS["Midnight"]["text_color"]),
        "accent_color": st.session_state.get("theme_accent_color", THEME_PRESETS["Midnight"]["accent_color"]),
    }
    apply_user_theme(active_theme)

    st.title("Trading Journal")
    st.caption("Track trades, accounts, and daily P&L in one place.")
    if st.session_state.get("show_welcome_once"):
        st.markdown(
            f"""
            <style>
            .welcome-banner {{
                border: 1px solid #2f6a4b;
                background: linear-gradient(90deg, rgba(24, 88, 58, 0.9), rgba(20, 66, 46, 0.9));
                color: #e8fff2;
                border-radius: 10px;
                padding: 10px 14px;
                font-weight: 700;
                margin-bottom: 10px;
                animation: welcomePop 420ms ease-out;
            }}
            @keyframes welcomePop {{
                0% {{ opacity: 0; transform: translateY(-8px) scale(0.98); }}
                100% {{ opacity: 1; transform: translateY(0) scale(1); }}
            }}
            </style>
            <div class="welcome-banner">Welcome {st.session_state.get('auth_username', '')}</div>
            """,
            unsafe_allow_html=True,
        )
        st.session_state["show_welcome_once"] = False

    with st.sidebar:
        st.markdown(f"**User:** {st.session_state.get('auth_username', 'Unknown')}")
        st.toggle("Debug Mode", key="debug_mode")
        with st.popover("Themes", use_container_width=True):
            saved_profiles = list_user_theme_profiles(conn, user_id)
            st.caption(f"Active: {st.session_state.get('theme_name', 'Custom')}")

            st.markdown("Preset")
            preset_options = list(THEME_PRESETS.keys()) + ["Custom"]
            default_preset = (
                active_theme["theme_name"] if active_theme["theme_name"] in preset_options else "Custom"
            )
            preset = st.selectbox(
                "Preset",
                options=preset_options,
                index=preset_options.index(default_preset),
                key="theme_preset_select",
            )

            if st.session_state.get("theme_last_preset") != preset:
                if preset in THEME_PRESETS:
                    st.session_state["theme_name"] = preset
                    st.session_state["theme_bg_color"] = THEME_PRESETS[preset]["bg_color"]
                    st.session_state["theme_surface_color"] = THEME_PRESETS[preset]["surface_color"]
                    st.session_state["theme_text_color"] = THEME_PRESETS[preset]["text_color"]
                    st.session_state["theme_accent_color"] = THEME_PRESETS[preset]["accent_color"]
                else:
                    st.session_state["theme_name"] = "Custom"
                st.session_state["theme_last_preset"] = preset

            st.markdown("Colors")
            st.color_picker("Background", key="theme_bg_color")
            st.color_picker("Surface", key="theme_surface_color")
            st.color_picker("Text", key="theme_text_color")
            st.color_picker("Accent", key="theme_accent_color")

            t1, t2 = st.columns(2)
            if t1.button("Save Theme", use_container_width=True):
                try:
                    theme_to_save = {
                        "theme_name": preset if preset in THEME_PRESETS else "Custom",
                        "bg_color": st.session_state["theme_bg_color"],
                        "surface_color": st.session_state["theme_surface_color"],
                        "text_color": st.session_state["theme_text_color"],
                        "accent_color": st.session_state["theme_accent_color"],
                    }
                    save_user_theme(conn, user_id, theme_to_save)
                    st.session_state["theme_name"] = theme_to_save["theme_name"]
                    st.success("Theme saved.")
                except Exception as exc:
                    report_exception("Save theme failed", exc)
            if t2.button("Reset", use_container_width=True):
                st.session_state["theme_reset_requested"] = True
                st.rerun()

            st.markdown("---")
            st.markdown("Theme Profiles")
            profile_name = st.text_input(
                "Profile Name",
                value=st.session_state.get("theme_profile_name", ""),
                key="theme_profile_name",
                placeholder="My Theme",
            )
            selected_profile = st.selectbox(
                "Saved Profiles",
                options=["Select..."] + saved_profiles,
                key="theme_profile_select",
            )

            p1, p2 = st.columns(2)
            if p1.button("Save As", use_container_width=True):
                if not profile_name.strip():
                    st.warning("Enter a profile name first.")
                else:
                    try:
                        profile_theme = {
                            "theme_name": "Custom",
                            "bg_color": st.session_state["theme_bg_color"],
                            "surface_color": st.session_state["theme_surface_color"],
                            "text_color": st.session_state["theme_text_color"],
                            "accent_color": st.session_state["theme_accent_color"],
                        }
                        save_user_theme_profile(conn, user_id, profile_name, profile_theme)
                        st.success(f"Saved profile '{profile_name.strip()}'.")
                        st.rerun()
                    except Exception as exc:
                        report_exception("Save theme profile failed", exc)
            if p2.button("Load Profile", use_container_width=True):
                if selected_profile == "Select...":
                    st.warning("Choose a saved profile first.")
                else:
                    try:
                        loaded = load_user_theme_profile(conn, user_id, selected_profile)
                        if not loaded:
                            st.warning("Theme profile not found.")
                        else:
                            st.session_state["theme_name"] = "Custom"
                            st.session_state["theme_bg_color"] = loaded["bg_color"]
                            st.session_state["theme_surface_color"] = loaded["surface_color"]
                            st.session_state["theme_text_color"] = loaded["text_color"]
                            st.session_state["theme_accent_color"] = loaded["accent_color"]
                            st.session_state["theme_preset_select"] = "Custom"
                            st.session_state["theme_last_preset"] = "Custom"
                            save_user_theme(conn, user_id, loaded)
                            st.success(f"Loaded profile '{selected_profile}'.")
                            st.rerun()
                    except Exception as exc:
                        report_exception("Load theme profile failed", exc)
        if st.button("Logout", use_container_width=True):
            try:
                revoke_remember_token(conn, st.session_state.get("remember_token"))
                st.session_state["auth_user_id"] = None
                st.session_state["auth_username"] = None
                st.session_state["remember_token"] = None
                if "rt" in st.query_params:
                    del st.query_params["rt"]
                navigate_to("landing")
            except Exception as exc:
                report_exception("Logout failed", exc)

    accounts_df = get_accounts(conn, user_id)
    trades_df = get_trades(conn, user_id)
    cashflows_df = get_cashflows(conn, user_id)

    account_options = ["All Accounts"] + accounts_df["name"].tolist() if not accounts_df.empty else ["All Accounts"]
    selected_dashboard_account = st.selectbox(
        "Stats Account Scope",
        options=account_options,
        key="dashboard_stats_account_scope",
    )

    scoped_trades = trades_df
    scoped_cashflows = cashflows_df
    if selected_dashboard_account != "All Accounts" and not accounts_df.empty:
        selected_account_id = int(
            accounts_df.loc[accounts_df["name"] == selected_dashboard_account, "id"].iloc[0]
        )
        scoped_trades = trades_df[trades_df["account_id"] == selected_account_id]
        scoped_cashflows = cashflows_df[cashflows_df["account_id"] == selected_account_id]

    m = account_metrics(scoped_trades, scoped_cashflows)
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Total Net P&L", f"${m['total_net']:,.2f}")
    c2.metric("Win Rate", f"{m['win_rate']:.1f}%")
    c3.metric("Wins", m["wins"])
    c4.metric("Losses", m["losses"])
    c5.metric("Avg Net/Trade", f"${m['avg_net']:,.2f}")
    c6.metric("Account Balance", f"${m['account_balance']:,.2f}")
    c7.metric("Win Streak", m["win_streak"], delta=f"Best {m['best_win_streak']}")

    tab1, tab2, tab3, tab4 = st.tabs(["Add Trade", "Journal", "Calendar", "Accounts"])

    with tab1:
        st.subheader("New Trade")
        if accounts_df.empty:
            st.error("Create an account first.")
        else:
            col_a, col_b, col_c = st.columns(3)
            trade_date = col_a.date_input("Date", value=date.today(), key="trade_date_input")
            account_name = col_b.selectbox(
                "Account",
                options=accounts_df["name"].tolist(),
                key="trade_account_select",
            )
            side = col_c.selectbox("Side", options=["Long", "Short"], key="trade_side_select")

            col_d, col_e, col_f = st.columns(3)
            symbol = col_d.text_input("Symbol", placeholder="AAPL", key="trade_symbol_input")
            quantity = col_e.number_input(
                "Quantity", min_value=0.0, value=1.0, step=1.0, key="trade_qty_input"
            )
            fees = col_f.number_input("Fees", min_value=0.0, value=0.0, step=0.01, key="trade_fees_input")

            col_g, col_h = st.columns(2)
            manual_pnl_mode = st.checkbox("Add a P&NL", value=False, key="trade_manual_pnl_mode")
            entry_price = col_g.number_input(
                "Entry Price",
                min_value=0.0,
                value=0.0,
                step=0.01,
                key="trade_entry_input",
                disabled=manual_pnl_mode,
            )
            exit_price = col_h.number_input(
                "Exit Price",
                min_value=0.0,
                value=0.0,
                step=0.01,
                key="trade_exit_input",
                disabled=manual_pnl_mode,
            )
            manual_net_pnl = None
            if manual_pnl_mode:
                manual_net_pnl = st.number_input(
                    "Net P&L",
                    value=0.0,
                    step=0.01,
                    key="trade_manual_net_pnl",
                    help="Used when Entry/Exit are disabled.",
                )

            tags = st.text_input("Tags", placeholder="breakout, earnings, setup-A", key="trade_tags_input")
            notes = st.text_area(
                "Notes",
                placeholder="Trade thesis, mistakes, lessons.",
                key="trade_notes_input",
            )

            paste_widget_key = f"paste_trade_image_{st.session_state.get('paste_widget_version', 0)}"
            pasted_result = paste_image_button("Paste Trade Image", key=paste_widget_key)
            pending_pasted = st.session_state.get("pending_trade_pasted_image_bytes")
            if pasted_result.image_data is not None:
                if pending_pasted is None:
                    img_buffer = io.BytesIO()
                    pasted_result.image_data.save(img_buffer, format="PNG")
                    st.session_state["pending_trade_pasted_image_bytes"] = img_buffer.getvalue()
                    pending_pasted = st.session_state["pending_trade_pasted_image_bytes"]
                else:
                    st.info("You already pasted an image. Click x to remove it before pasting another.")

            trade_image = st.file_uploader(
                "Trade Screenshot/Image (optional)",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=False,
                key="trade_image_upload",
            )

            if pending_pasted:
                st.caption("Pasted image preview")
                p_col_l, p_col_r = st.columns([20, 1])
                with p_col_r:
                    if st.button("x", key="clear_pasted_image_btn", help="Remove pasted image"):
                        st.session_state["pending_trade_pasted_image_bytes"] = None
                        st.session_state["paste_widget_version"] = st.session_state.get("paste_widget_version", 0) + 1
                        st.rerun()
                with p_col_l:
                    st.image(pending_pasted, width=180)

            if st.button("Save Trade", key="save_trade_btn"):
                if not symbol.strip():
                    st.warning("Symbol is required.")
                elif quantity <= 0:
                    st.warning("Quantity must be greater than 0.")
                elif (not manual_pnl_mode) and (entry_price <= 0 or exit_price <= 0):
                    st.warning("Entry and exit price must be greater than 0.")
                else:
                    try:
                        image_path = save_trade_image(
                            trade_image,
                            user_id=user_id,
                            pasted_image_bytes=st.session_state.get("pending_trade_pasted_image_bytes"),
                        )
                        account_id = int(accounts_df.loc[accounts_df["name"] == account_name, "id"].iloc[0])
                        trade_input = TradeInput(
                            trade_date=str(trade_date),
                            account_id=account_id,
                            symbol=symbol,
                            side=side,
                            quantity=float(quantity),
                            entry_price=float(entry_price) if not manual_pnl_mode else 0.0,
                            exit_price=float(exit_price) if not manual_pnl_mode else 0.0,
                            fees=float(fees),
                            tags=tags,
                            notes=notes,
                            image_path=image_path,
                            manual_net_pnl=float(manual_net_pnl) if manual_pnl_mode else None,
                        )
                        save_trade(conn, user_id, trade_input)
                        st.session_state["pending_trade_pasted_image_bytes"] = None
                        st.session_state["paste_widget_version"] = st.session_state.get("paste_widget_version", 0) + 1
                        st.success("Trade saved.")
                        st.rerun()
                    except Exception as exc:
                        report_exception("Save trade failed", exc)

    with tab2:
        st.subheader("Trade Journal")
        if trades_df.empty:
            st.info("No trades yet.")
        else:
            f1, f2, f3 = st.columns(3)
            selected_account = f1.selectbox("Account Filter", options=["All"] + accounts_df["name"].tolist())
            selected_symbol = f2.text_input("Symbol Filter", placeholder="Optional symbol")
            selected_tag = f3.text_input("Tag Filter", placeholder="Optional tag")

            filtered = trades_df.copy()
            if selected_account != "All":
                filtered = filtered[filtered["account_name"] == selected_account]
            if selected_symbol.strip():
                filtered = filtered[
                    filtered["symbol"].str.upper().str.contains(selected_symbol.upper().strip(), na=False)
                ]
            if selected_tag.strip():
                filtered = filtered[filtered["tags"].str.contains(selected_tag, case=False, na=False)]

            filtered_display = filtered.copy()
            filtered_display["has_image"] = filtered_display["image_path"].fillna("").str.strip().ne("")
            st.dataframe(
                filtered_display[
                    [
                        "id",
                        "trade_date",
                        "account_name",
                        "symbol",
                        "side",
                        "quantity",
                        "entry_price",
                        "exit_price",
                        "fees",
                        "gross_pnl",
                        "net_pnl",
                        "tags",
                        "notes",
                        "has_image",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

            image_trades = filtered[filtered["image_path"].fillna("").str.strip() != ""]
            if not image_trades.empty:
                st.markdown("Trade image preview")
                preview_options = [
                    f"#{int(row['id'])} | {row['trade_date']} | {row['symbol']} | {row['account_name']}"
                    for _, row in image_trades.iterrows()
                ]
                selected_preview = st.selectbox(
                    "Select trade",
                    options=preview_options,
                    key="trade_image_preview_select",
                )
                selected_trade_id = int(selected_preview.split("|")[0].replace("#", "").strip())
                selected_image_path = image_trades.loc[
                    image_trades["id"] == selected_trade_id, "image_path"
                ].iloc[0]
                if selected_image_path and Path(selected_image_path).exists():
                    img_col_l, img_col_m, img_col_r = st.columns([1, 2, 1])
                    with img_col_m:
                        st.image(str(selected_image_path), use_container_width=True)
                else:
                    st.warning("Image file not found for this trade.")

            st.markdown("Delete trade by ID")
            d_col1, d_col2, d_col3 = st.columns([1, 3, 2])
            trade_id_delete = d_col1.number_input("Trade ID", min_value=1, step=1, value=1)
            confirm_delete_trade = d_col2.checkbox(
                "Confirm trade deletion",
                value=False,
                key="confirm_trade_delete_checkbox",
            )
            if d_col3.button("Delete Trade", type="primary"):
                if not confirm_delete_trade:
                    st.warning("Tick confirm before deleting trade.")
                else:
                    try:
                        deleted = delete_trade(conn, int(trade_id_delete), user_id)
                        if deleted:
                            st.success(f"Deleted trade {trade_id_delete}.")
                        else:
                            st.warning("Trade not found for this account.")
                        st.rerun()
                    except Exception as exc:
                        report_exception("Delete trade failed", exc)

            if not filtered.empty:
                chart_df = (
                    filtered.sort_values("trade_date")
                    .assign(cumulative_net=lambda x: x["net_pnl"].cumsum())
                )
                fig = px.line(
                    chart_df,
                    x="trade_date",
                    y="cumulative_net",
                    title="Equity Curve (Filtered)",
                    markers=True,
                )
                st.plotly_chart(fig, use_container_width=True)

            st.markdown("Edit trade")
            if not trades_df.empty:
                edit_options = [
                    f"#{int(row['id'])} | {row['trade_date']} | {row['symbol']} | {row['account_name']}"
                    for _, row in trades_df.iterrows()
                ]
                selected_edit = st.selectbox(
                    "Select trade to edit",
                    options=edit_options,
                    key="edit_trade_select",
                )
                edit_trade_id = int(selected_edit.split("|")[0].replace("#", "").strip())
                edit_row = trades_df[trades_df["id"] == edit_trade_id].iloc[0]

                if st.session_state.get("edit_trade_loaded_id") != edit_trade_id:
                    st.session_state["edit_trade_loaded_id"] = edit_trade_id
                    st.session_state["edit_trade_date"] = pd.to_datetime(edit_row["trade_date"]).date()
                    st.session_state["edit_trade_account_name"] = edit_row["account_name"]
                    st.session_state["edit_trade_side"] = edit_row["side"]
                    st.session_state["edit_trade_symbol"] = str(edit_row["symbol"])
                    st.session_state["edit_trade_qty"] = float(edit_row["quantity"])
                    st.session_state["edit_trade_entry"] = float(edit_row["entry_price"])
                    st.session_state["edit_trade_exit"] = float(edit_row["exit_price"])
                    st.session_state["edit_trade_fees"] = float(edit_row["fees"])
                    st.session_state["edit_trade_tags"] = str(edit_row["tags"] or "")
                    st.session_state["edit_trade_notes"] = str(edit_row["notes"] or "")
                    st.session_state["pending_edit_pasted_image_bytes"] = None
                    st.session_state["edit_paste_widget_version"] = st.session_state.get(
                        "edit_paste_widget_version", 0
                    ) + 1

                e1, e2, e3 = st.columns(3)
                edit_date = e1.date_input("Edit Date", key="edit_trade_date")
                edit_account_name = e2.selectbox(
                    "Edit Account",
                    options=accounts_df["name"].tolist(),
                    key="edit_trade_account_name",
                )
                edit_side = e3.selectbox("Edit Side", options=["Long", "Short"], key="edit_trade_side")

                e4, e5, e6 = st.columns(3)
                edit_symbol = e4.text_input("Edit Symbol", key="edit_trade_symbol")
                edit_qty = e5.number_input(
                    "Edit Quantity", min_value=0.0, step=1.0, key="edit_trade_qty"
                )
                edit_fees = e6.number_input("Edit Fees", min_value=0.0, step=0.01, key="edit_trade_fees")

                e7, e8 = st.columns(2)
                edit_entry = e7.number_input(
                    "Edit Entry Price", min_value=0.0, step=0.01, key="edit_trade_entry"
                )
                edit_exit = e8.number_input(
                    "Edit Exit Price", min_value=0.0, step=0.01, key="edit_trade_exit"
                )

                edit_tags = st.text_input("Edit Tags", key="edit_trade_tags")
                edit_notes = st.text_area("Edit Notes", key="edit_trade_notes")

                current_image_path = str(edit_row["image_path"] or "")
                if current_image_path and Path(current_image_path).exists():
                    st.caption("Current image")
                    st.image(current_image_path, width=180)

                edit_paste_key = f"edit_paste_trade_image_{st.session_state.get('edit_paste_widget_version', 0)}"
                edit_pasted_result = paste_image_button("Paste New Image", key=edit_paste_key)
                pending_edit_pasted = st.session_state.get("pending_edit_pasted_image_bytes")
                if edit_pasted_result.image_data is not None:
                    if pending_edit_pasted is None:
                        edit_buf = io.BytesIO()
                        edit_pasted_result.image_data.save(edit_buf, format="PNG")
                        st.session_state["pending_edit_pasted_image_bytes"] = edit_buf.getvalue()
                        pending_edit_pasted = st.session_state["pending_edit_pasted_image_bytes"]
                    else:
                        st.info("You already pasted an image for edit. Click x to replace it.")

                edit_upload = st.file_uploader(
                    "Upload New Image (optional)",
                    type=["png", "jpg", "jpeg", "webp"],
                    accept_multiple_files=False,
                    key="edit_trade_image_upload",
                )

                if pending_edit_pasted:
                    st.caption("New pasted image preview")
                    ep1, ep2 = st.columns([20, 1])
                    with ep2:
                        if st.button("x", key="clear_edit_pasted_image_btn", help="Remove pasted image"):
                            st.session_state["pending_edit_pasted_image_bytes"] = None
                            st.session_state["edit_paste_widget_version"] = st.session_state.get(
                                "edit_paste_widget_version", 0
                            ) + 1
                            st.rerun()
                    with ep1:
                        st.image(pending_edit_pasted, width=180)

                clear_existing_image = st.checkbox(
                    "Remove current image",
                    value=False,
                    key="edit_trade_clear_existing_image",
                )

                if st.button("Save Trade Changes", key="save_trade_changes_btn", type="primary"):
                    if not edit_symbol.strip():
                        st.warning("Symbol is required.")
                    elif edit_qty <= 0:
                        st.warning("Quantity must be greater than 0.")
                    elif edit_entry <= 0 or edit_exit <= 0:
                        st.warning("Entry and exit price must be greater than 0.")
                    else:
                        try:
                            new_image_path = save_trade_image(
                                edit_upload,
                                user_id=user_id,
                                pasted_image_bytes=st.session_state.get("pending_edit_pasted_image_bytes"),
                            )
                            edit_account_id = int(
                                accounts_df.loc[accounts_df["name"] == edit_account_name, "id"].iloc[0]
                            )
                            updated = update_trade(
                                conn=conn,
                                user_id=user_id,
                                trade_id=edit_trade_id,
                                trade_date=str(edit_date),
                                account_id=edit_account_id,
                                symbol=edit_symbol,
                                side=edit_side,
                                quantity=float(edit_qty),
                                entry_price=float(edit_entry),
                                exit_price=float(edit_exit),
                                fees=float(edit_fees),
                                tags=edit_tags,
                                notes=edit_notes,
                                new_image_path=new_image_path,
                                clear_image=clear_existing_image,
                            )
                            if updated:
                                st.session_state["pending_edit_pasted_image_bytes"] = None
                                st.session_state["edit_paste_widget_version"] = st.session_state.get(
                                    "edit_paste_widget_version", 0
                                ) + 1
                                st.success(f"Updated trade {edit_trade_id}.")
                                st.rerun()
                            else:
                                st.warning("Trade not found for this account.")
                        except Exception as exc:
                            report_exception("Update trade failed", exc)

    with tab3:
        current = date.today()
        c_month, c_year = st.columns(2)
        month = c_month.selectbox(
            "Month",
            options=list(range(1, 13)),
            index=current.month - 1,
            format_func=lambda x: calendar.month_name[x],
        )
        year = c_year.number_input("Year", min_value=2000, max_value=2100, value=current.year, step=1)
        render_pnl_calendar(trades_df, month=int(month), year=int(year))

        if not trades_df.empty:
            month_df = trades_df.copy()
            month_df["trade_date"] = pd.to_datetime(month_df["trade_date"])
            month_df = month_df[
                (month_df["trade_date"].dt.month == int(month)) & (month_df["trade_date"].dt.year == int(year))
            ]
            if not month_df.empty:
                by_symbol = month_df.groupby("symbol", as_index=False)["net_pnl"].sum().sort_values(
                    "net_pnl", ascending=False
                )
                st.subheader("Monthly P&L by Symbol")
                st.bar_chart(by_symbol, x="symbol", y="net_pnl")

    with tab4:
        st.subheader("Accounts")
        if not accounts_df.empty:
            trade_pnl_by_account = (
                trades_df.groupby("account_id", as_index=False)["net_pnl"]
                .sum()
                .rename(columns={"net_pnl": "trade_net_pnl"})
            )
            cash_by_account = (
                cashflows_df.groupby("account_id", as_index=False)["amount"]
                .sum()
                .rename(columns={"amount": "net_transfers"})
                if not cashflows_df.empty
                else pd.DataFrame(columns=["account_id", "net_transfers"])
            )
            account_view = (
                accounts_df.rename(columns={"id": "account_id"})
                .merge(trade_pnl_by_account, on="account_id", how="left")
                .merge(cash_by_account, on="account_id", how="left")
            )
            account_view["trade_net_pnl"] = account_view["trade_net_pnl"].fillna(0.0)
            account_view["net_transfers"] = account_view["net_transfers"].fillna(0.0)
            account_view["est_balance"] = account_view["trade_net_pnl"] + account_view["net_transfers"]
            st.dataframe(
                account_view[
                    [
                        "account_id",
                        "name",
                        "broker",
                        "account_type",
                        "description",
                        "net_transfers",
                        "trade_net_pnl",
                        "est_balance",
                        "created_at",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("Add account")
        with st.form("account_form", clear_on_submit=True):
            a1, a2, a3 = st.columns(3)
            name = a1.text_input("Account Name", placeholder="Futures-1")
            broker = a2.text_input("Broker", placeholder="Interactive Brokers")
            account_type = a3.text_input("Type", placeholder="Margin")
            description = st.text_area("Description", placeholder="Risk rules, strategy, objectives.")
            add_submitted = st.form_submit_button("Add Account")
            if add_submitted:
                if not name.strip():
                    st.warning("Account name is required.")
                else:
                    try:
                        add_account(conn, user_id, name, broker, account_type, description)
                        st.success("Account added.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Account name already exists.")

        st.markdown("Deposit / Withdrawal")
        if accounts_df.empty:
            st.info("Create an account first.")
        else:
            with st.form("cashflow_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                cash_account = c1.selectbox(
                    "Account",
                    options=accounts_df["name"].tolist(),
                    key="cash_account_select",
                )
                flow_type = c2.selectbox("Type", options=["Deposit", "Withdrawal"])
                flow_date = c3.date_input("Date", value=date.today(), key="cashflow_date")
                amount = st.number_input("Amount", min_value=0.01, value=100.0, step=1.0)
                note = st.text_input("Note", placeholder="Funding, payout, transfer, etc.")
                cash_submitted = st.form_submit_button("Save Transfer")
                if cash_submitted:
                    try:
                        account_id = int(
                            accounts_df.loc[accounts_df["name"] == cash_account, "id"].iloc[0]
                        )
                        add_cashflow(
                            conn,
                            user_id=user_id,
                            account_id=account_id,
                            flow_date=str(flow_date),
                            flow_type=flow_type,
                            amount=float(amount),
                            note=note,
                        )
                        st.success(f"{flow_type} saved.")
                        st.rerun()
                    except Exception as exc:
                        report_exception("Save transfer failed", exc)

            if not cashflows_df.empty:
                st.markdown("Recent transfers")
                st.dataframe(
                    cashflows_df[
                        ["id", "flow_date", "account_name", "flow_type", "amount", "note", "created_at"]
                    ].head(20),
                    use_container_width=True,
                    hide_index=True,
                )

        st.markdown("Delete account")
        if accounts_df.empty:
            st.info("No account to delete.")
        else:
            del_col1, del_col2 = st.columns([2, 1])
            delete_account_name = del_col1.selectbox(
                "Select account",
                options=accounts_df["name"].tolist(),
                key="delete_account_select",
            )
            confirm_delete = del_col1.checkbox(
                "I understand this will delete the account, all trades, and all transfers for it.",
                value=False,
            )
            if del_col2.button("Delete Account", type="primary", use_container_width=True):
                if not confirm_delete:
                    st.warning("Please confirm deletion first.")
                elif len(accounts_df) <= 1:
                    st.warning("You must keep at least one account.")
                else:
                    try:
                        account_id = int(
                            accounts_df.loc[accounts_df["name"] == delete_account_name, "id"].iloc[0]
                        )
                        deleted = delete_account(conn, user_id=user_id, account_id=account_id)
                        if deleted:
                            st.success(f"Deleted account '{delete_account_name}'.")
                        else:
                            st.warning("Account could not be deleted.")
                        st.rerun()
                    except Exception as exc:
                        report_exception("Delete account failed", exc)


def init_session_state() -> None:
    if "page" not in st.session_state:
        st.session_state["page"] = "landing"
    if "auth_user_id" not in st.session_state:
        st.session_state["auth_user_id"] = None
    if "auth_username" not in st.session_state:
        st.session_state["auth_username"] = None
    if "landing_loaded" not in st.session_state:
        st.session_state["landing_loaded"] = False
    if "pending_trade_pasted_image_bytes" not in st.session_state:
        st.session_state["pending_trade_pasted_image_bytes"] = None
    if "paste_widget_version" not in st.session_state:
        st.session_state["paste_widget_version"] = 0
    if "pending_transition_animation" not in st.session_state:
        st.session_state["pending_transition_animation"] = None
    if "show_welcome_once" not in st.session_state:
        st.session_state["show_welcome_once"] = False
    if "remember_token" not in st.session_state:
        st.session_state["remember_token"] = None
    if "pending_edit_pasted_image_bytes" not in st.session_state:
        st.session_state["pending_edit_pasted_image_bytes"] = None
    if "edit_paste_widget_version" not in st.session_state:
        st.session_state["edit_paste_widget_version"] = 0
    if "edit_trade_loaded_id" not in st.session_state:
        st.session_state["edit_trade_loaded_id"] = None
    if "debug_mode" not in st.session_state:
        st.session_state["debug_mode"] = DEBUG_DEFAULT
    if "theme_reset_requested" not in st.session_state:
        st.session_state["theme_reset_requested"] = False


def main() -> None:
    st.set_page_config(page_title="Trading Journal", page_icon="📈", layout="wide")
    conn = get_conn()
    init_db(conn)
    init_session_state()
    inject_responsive_css()
    apply_pending_transition()

    if not st.session_state["auth_user_id"] and "rt" in st.query_params:
        raw_token = str(st.query_params.get("rt", "")).strip()
        if raw_token:
            ok, user_id, username = authenticate_with_remember_token(conn, raw_token)
            if ok and user_id is not None and username:
                st.session_state["auth_user_id"] = user_id
                st.session_state["auth_username"] = username
                st.session_state["remember_token"] = raw_token
                st.session_state["page"] = "app"
            else:
                if "rt" in st.query_params:
                    del st.query_params["rt"]

    try:
        if st.session_state["auth_user_id"]:
            st.session_state["page"] = "app"

        page = st.session_state["page"]
        if page == "landing":
            if not st.session_state["landing_loaded"]:
                render_loading_screen()
            else:
                render_landing_page()
        elif page == "login":
            render_login_page(conn)
        elif page == "register":
            render_register_page(conn)
        else:
            if st.session_state.get("show_welcome_once"):
                render_fullscreen_welcome(st.session_state.get("auth_username", "Trader"))
                return
            render_dashboard(conn, int(st.session_state["auth_user_id"]))
    except Exception as exc:
        report_exception("Unexpected app error", exc)


if __name__ == "__main__":
    main()
