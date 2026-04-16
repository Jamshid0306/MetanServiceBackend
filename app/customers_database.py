import hashlib
import hmac
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "orders.db"
PASSWORD_ITERATIONS = 120_000
REGISTRATION_SESSION_TTL_SECONDS = 10 * 60
LOGIN_SESSION_TTL_SECONDS = 10 * 60


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_customers_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT,
            last_name TEXT,
            name TEXT,
            phone TEXT NOT NULL UNIQUE,
            telegram_id TEXT UNIQUE,
            telegram_username TEXT UNIQUE,
            address TEXT,
            password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_customer_columns():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(customers)")
    existing_columns = {row["name"] for row in cursor.fetchall()}

    if "name" not in existing_columns:
        cursor.execute("ALTER TABLE customers ADD COLUMN name TEXT")

    if "password_hash" not in existing_columns:
        cursor.execute("ALTER TABLE customers ADD COLUMN password_hash TEXT")

    if "telegram_username" not in existing_columns:
        cursor.execute("ALTER TABLE customers ADD COLUMN telegram_username TEXT")

    if "telegram_id" not in existing_columns:
        cursor.execute("ALTER TABLE customers ADD COLUMN telegram_id TEXT")

    if "address" not in existing_columns:
        cursor.execute("ALTER TABLE customers ADD COLUMN address TEXT")

    conn.commit()
    conn.close()


def init_customer_login_sessions_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_login_sessions (
            state TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            phone TEXT,
            telegram_id TEXT,
            telegram_username TEXT,
            telegram_chat_id TEXT,
            last_error TEXT,
            verified_at TIMESTAMP,
            customer_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def init_customer_registration_sessions_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_registration_sessions (
            state TEXT PRIMARY KEY,
            nonce TEXT NOT NULL,
            code_verifier TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_customer_registration_session_columns():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(customer_registration_sessions)")
    existing_columns = {row["name"] for row in cursor.fetchall()}

    if "status" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN status TEXT DEFAULT 'pending'"
        )

    if "phone" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN phone TEXT"
        )

    if "telegram_id" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN telegram_id TEXT"
        )

    if "telegram_username" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN telegram_username TEXT"
        )

    if "telegram_chat_id" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN telegram_chat_id TEXT"
        )

    if "last_error" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN last_error TEXT"
        )

    if "verified_at" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN verified_at TIMESTAMP"
        )

    if "customer_id" not in existing_columns:
        cursor.execute(
            "ALTER TABLE customer_registration_sessions ADD COLUMN customer_id INTEGER"
        )

    conn.commit()
    conn.close()


def build_customer_name(record) -> str:
    if not record:
        return ""

    direct_name = str(record["name"] or "").strip()
    if direct_name:
        return direct_name

    return " ".join(
        part.strip()
        for part in [record["first_name"] or "", record["last_name"] or ""]
        if str(part or "").strip()
    ).strip()


def serialize_customer(record):
    if not record:
        return None

    phone = str(record["phone"] or "")
    public_phone = phone if phone.isdigit() else ""

    return {
        "id": record["id"],
        "name": build_customer_name(record),
        "phone": public_phone,
        "telegram_id": record["telegram_id"],
        "telegram_username": record["telegram_username"],
        "address": str(record["address"] or "").strip() if "address" in record.keys() else "",
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return f"{salt.hex()}${password_hash.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or "$" not in stored_hash:
        return False

    salt_hex, hash_hex = stored_hash.split("$", 1)

    try:
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
    except ValueError:
        return False

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return hmac.compare_digest(password_hash, expected_hash)


def get_customer_record_by_phone(phone: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE phone = ?
        """,
        (phone,),
    )
    record = cursor.fetchone()
    conn.close()
    return record


def get_customer_by_phone(phone: str):
    return serialize_customer(get_customer_record_by_phone(phone))


def get_customer_record_by_id(customer_id: int | str):
    normalized_customer_id = str(customer_id or "").strip()
    if not normalized_customer_id.isdigit():
        return None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE id = ?
        """,
        (int(normalized_customer_id),),
    )
    record = cursor.fetchone()
    conn.close()
    return record


def get_customer_by_id(customer_id: int | str):
    return serialize_customer(get_customer_record_by_id(customer_id))


def get_all_customers():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        ORDER BY created_at DESC, id DESC
        """
    )
    records = cursor.fetchall()
    conn.close()
    return [serialize_customer(record) for record in records]


def update_customer_telegram_by_phone(
    phone: str,
    telegram_id: str | None = None,
    telegram_username: str | None = None,
):
    existing_record = get_customer_record_by_phone(str(phone or "").strip())
    if not existing_record:
        return None

    normalized_username = normalize_telegram_username(telegram_username or "")
    normalized_telegram_id = str(telegram_id or "").strip()

    if normalized_username:
        existing_telegram_record = get_customer_record_by_telegram_username(normalized_username)
        if existing_telegram_record and existing_telegram_record["id"] != existing_record["id"]:
            raise ValueError("telegram_username_taken")

    if normalized_telegram_id:
        existing_telegram_id_record = get_customer_record_by_telegram_id(normalized_telegram_id)
        if existing_telegram_id_record and existing_telegram_id_record["id"] != existing_record["id"]:
            raise ValueError("telegram_id_taken")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customers
        SET telegram_id = ?, telegram_username = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            normalized_telegram_id or existing_record["telegram_id"],
            normalized_username or existing_record["telegram_username"],
            existing_record["id"],
        ),
    )
    conn.commit()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE id = ?
        """,
        (existing_record["id"],),
    )
    record = cursor.fetchone()
    conn.close()
    return serialize_customer(record)


def delete_customer_by_id(customer_id: int | str):
    record = get_customer_record_by_id(customer_id)
    if not record:
        return None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        DELETE FROM customers
        WHERE id = ?
        """,
        (record["id"],),
    )
    conn.commit()
    conn.close()
    return serialize_customer(record)


def normalize_telegram_username(value: str) -> str:
    username = str(value or "").strip().lstrip("@").lower()
    return username


def build_telegram_placeholder_phone(telegram_username: str) -> str:
    return f"tg:{normalize_telegram_username(telegram_username)}"


def get_customer_record_by_telegram_username(telegram_username: str):
    normalized_username = normalize_telegram_username(telegram_username)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE lower(telegram_username) = ?
        """,
        (normalized_username,),
    )
    record = cursor.fetchone()
    conn.close()
    return record


def get_customer_record_by_telegram_id(telegram_id: str):
    normalized_telegram_id = str(telegram_id or "").strip()
    if not normalized_telegram_id:
        return None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE telegram_id = ?
        """,
        (normalized_telegram_id,),
    )
    record = cursor.fetchone()
    conn.close()
    return record


def save_customer_account(
    name: str,
    phone: str,
    password: str | None = None,
    telegram_username: str | None = None,
    telegram_id: str | None = None,
    password_hash: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    existing_record = get_customer_record_by_phone(phone)
    normalized_username = normalize_telegram_username(telegram_username or "")
    normalized_telegram_id = str(telegram_id or "").strip()

    if normalized_username:
        existing_telegram_record = get_customer_record_by_telegram_username(normalized_username)
        if existing_telegram_record and (
            not existing_record or existing_telegram_record["id"] != existing_record["id"]
        ):
            conn.close()
            raise ValueError("telegram_username_taken")

    if normalized_telegram_id:
        existing_telegram_id_record = get_customer_record_by_telegram_id(normalized_telegram_id)
        if existing_telegram_id_record and (
            not existing_record or existing_telegram_id_record["id"] != existing_record["id"]
        ):
            conn.close()
            raise ValueError("telegram_id_taken")

    next_password_hash = str(password_hash or "").strip()
    if not next_password_hash:
        raw_password = str(password or "").strip()
        if not raw_password:
            conn.close()
            raise ValueError("password_required")
        next_password_hash = hash_password(raw_password)

    if existing_record:
        cursor.execute(
            """
            UPDATE customers
            SET first_name = ?, last_name = ?, name = ?, telegram_id = ?, telegram_username = ?, password_hash = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                name,
                "",
                name,
                normalized_telegram_id or existing_record["telegram_id"],
                normalized_username or existing_record["telegram_username"],
                next_password_hash,
                existing_record["id"],
            ),
        )
        customer_id = existing_record["id"]
    else:
        cursor.execute(
            """
            INSERT INTO customers (first_name, last_name, name, phone, telegram_id, telegram_username, password_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                "",
                name,
                phone,
                normalized_telegram_id or None,
                normalized_username or None,
                next_password_hash,
            ),
        )
        customer_id = cursor.lastrowid

    conn.commit()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE id = ?
        """,
        (customer_id,),
    )
    record = cursor.fetchone()
    conn.close()
    return serialize_customer(record)


def save_customer_registration_session(
    state: str,
    name: str,
    password_hash: str,
):
    current_timestamp = int(time.time())
    expires_at = current_timestamp + REGISTRATION_SESSION_TTL_SECONDS

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM customer_registration_sessions WHERE expires_at <= ?",
        (current_timestamp,),
    )
    cursor.execute(
        """
        INSERT OR REPLACE INTO customer_registration_sessions (
            state, nonce, code_verifier, redirect_uri, name, password_hash, expires_at, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (state, "", "", "", name, password_hash, expires_at, "pending"),
    )
    conn.commit()
    conn.close()


def save_customer_login_session(state: str):
    current_timestamp = int(time.time())
    expires_at = current_timestamp + LOGIN_SESSION_TTL_SECONDS

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM customer_login_sessions WHERE expires_at <= ?",
        (current_timestamp,),
    )
    cursor.execute(
        """
        INSERT OR REPLACE INTO customer_login_sessions (
            state, expires_at, status
        )
        VALUES (?, ?, ?)
        """,
        (state, expires_at, "pending"),
    )
    conn.commit()
    conn.close()


def get_customer_login_session(state: str):
    normalized_state = str(state or "").strip()
    if not normalized_state:
        return None

    current_timestamp = int(time.time())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM customer_login_sessions WHERE expires_at <= ?",
        (current_timestamp,),
    )
    cursor.execute(
        """
        SELECT state, expires_at, created_at, status, phone, telegram_id, telegram_username,
               telegram_chat_id, last_error, verified_at, customer_id
        FROM customer_login_sessions
        WHERE state = ?
        """,
        (normalized_state,),
    )
    record = cursor.fetchone()
    conn.commit()
    conn.close()
    return record


def get_latest_customer_login_session_by_telegram_id(telegram_id: str):
    normalized_telegram_id = str(telegram_id or "").strip()
    if not normalized_telegram_id:
        return None

    current_timestamp = int(time.time())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM customer_login_sessions WHERE expires_at <= ?",
        (current_timestamp,),
    )
    cursor.execute(
        """
        SELECT state, expires_at, created_at, status, phone, telegram_id, telegram_username,
               telegram_chat_id, last_error, verified_at, customer_id
        FROM customer_login_sessions
        WHERE telegram_id = ? AND status IN ('pending', 'awaiting_contact')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (normalized_telegram_id,),
    )
    record = cursor.fetchone()
    conn.commit()
    conn.close()
    return record


def mark_customer_login_session_awaiting_contact(
    state: str,
    telegram_id: str,
    telegram_username: str | None = None,
    telegram_chat_id: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customer_login_sessions
        SET telegram_id = ?, telegram_username = ?, telegram_chat_id = ?, status = 'awaiting_contact',
            last_error = NULL
        WHERE state = ?
        """,
        (
            str(telegram_id or "").strip() or None,
            normalize_telegram_username(telegram_username or "") or None,
            str(telegram_chat_id or "").strip() or None,
            str(state or "").strip(),
        ),
    )
    conn.commit()
    conn.close()


def mark_customer_login_session_failed(state: str, error_message: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customer_login_sessions
        SET status = 'failed', last_error = ?
        WHERE state = ?
        """,
        (str(error_message or "").strip(), str(state or "").strip()),
    )
    conn.commit()
    conn.close()


def complete_customer_login_session(
    state: str,
    phone: str,
    telegram_id: str,
    telegram_username: str | None,
    customer_id: int | str | None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customer_login_sessions
        SET status = 'completed', phone = ?, telegram_id = ?, telegram_username = ?, verified_at = CURRENT_TIMESTAMP,
            customer_id = ?, last_error = NULL
        WHERE state = ?
        """,
        (
            str(phone or "").strip() or None,
            str(telegram_id or "").strip() or None,
            normalize_telegram_username(telegram_username or "") or None,
            int(customer_id) if str(customer_id or "").isdigit() else None,
            str(state or "").strip(),
        ),
    )
    conn.commit()
    conn.close()


def get_customer_registration_session(state: str):
    normalized_state = str(state or "").strip()
    if not normalized_state:
        return None

    current_timestamp = int(time.time())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM customer_registration_sessions WHERE expires_at <= ?",
        (current_timestamp,),
    )
    cursor.execute(
        """
        SELECT state, nonce, code_verifier, redirect_uri, name, password_hash, expires_at, created_at,
               status, phone, telegram_id, telegram_username, telegram_chat_id, last_error, verified_at, customer_id
        FROM customer_registration_sessions
        WHERE state = ?
        """,
        (normalized_state,),
    )
    record = cursor.fetchone()
    conn.commit()
    conn.close()
    return record


def delete_customer_registration_session(state: str):
    normalized_state = str(state or "").strip()
    if not normalized_state:
        return

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM customer_registration_sessions WHERE state = ?",
        (normalized_state,),
    )
    conn.commit()
    conn.close()


def get_latest_customer_registration_session_by_telegram_id(telegram_id: str):
    normalized_telegram_id = str(telegram_id or "").strip()
    if not normalized_telegram_id:
        return None

    current_timestamp = int(time.time())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM customer_registration_sessions WHERE expires_at <= ?",
        (current_timestamp,),
    )
    cursor.execute(
        """
        SELECT state, nonce, code_verifier, redirect_uri, name, password_hash, expires_at, created_at,
               status, phone, telegram_id, telegram_username, telegram_chat_id, last_error, verified_at, customer_id
        FROM customer_registration_sessions
        WHERE telegram_id = ? AND status IN ('pending', 'awaiting_contact')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (normalized_telegram_id,),
    )
    record = cursor.fetchone()
    conn.commit()
    conn.close()
    return record


def mark_customer_registration_session_awaiting_contact(
    state: str,
    telegram_id: str,
    telegram_username: str | None = None,
    telegram_chat_id: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customer_registration_sessions
        SET telegram_id = ?, telegram_username = ?, telegram_chat_id = ?, status = 'awaiting_contact',
            last_error = NULL
        WHERE state = ?
        """,
        (
            str(telegram_id or "").strip() or None,
            normalize_telegram_username(telegram_username or "") or None,
            str(telegram_chat_id or "").strip() or None,
            str(state or "").strip(),
        ),
    )
    conn.commit()
    conn.close()


def mark_customer_registration_session_failed(state: str, error_message: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customer_registration_sessions
        SET status = 'failed', last_error = ?
        WHERE state = ?
        """,
        (str(error_message or "").strip(), str(state or "").strip()),
    )
    conn.commit()
    conn.close()


def complete_customer_registration_session(
    state: str,
    phone: str,
    telegram_id: str,
    telegram_username: str | None,
    customer_id: int | str | None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customer_registration_sessions
        SET status = 'completed', phone = ?, telegram_id = ?, telegram_username = ?, verified_at = CURRENT_TIMESTAMP,
            customer_id = ?, last_error = NULL
        WHERE state = ?
        """,
        (
            str(phone or "").strip() or None,
            str(telegram_id or "").strip() or None,
            normalize_telegram_username(telegram_username or "") or None,
            int(customer_id) if str(customer_id or "").isdigit() else None,
            str(state or "").strip(),
        ),
    )
    conn.commit()
    conn.close()


def authenticate_customer(identifier: str, password: str):
    normalized_identifier = str(identifier or "").strip()
    record = get_customer_record_by_phone(normalized_identifier)

    if not record:
        record = get_customer_record_by_telegram_id(normalized_identifier)

    if not record:
        record = get_customer_record_by_telegram_username(normalized_identifier)

    if not record or not verify_password(password, str(record["password_hash"] or "")):
        return None

    return serialize_customer(record)


def update_customer_password_by_phone(phone: str, password: str):
    normalized_phone = str(phone or "").strip()
    existing_record = get_customer_record_by_phone(normalized_phone)
    if not existing_record:
      return None

    conn = get_connection()
    cursor = conn.cursor()
    password_hash = hash_password(password)
    cursor.execute(
        """
        UPDATE customers
        SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (password_hash, existing_record["id"]),
    )
    conn.commit()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE id = ?
        """,
        (existing_record["id"],),
    )
    record = cursor.fetchone()
    conn.close()
    return serialize_customer(record)


def update_customer_address_by_phone(phone: str, address: str):
    normalized_phone = str(phone or "").strip()
    existing_record = get_customer_record_by_phone(normalized_phone)
    if not existing_record:
        return None

    normalized_address = str(address or "").strip()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE customers
        SET address = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (normalized_address or None, existing_record["id"]),
    )
    conn.commit()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE id = ?
        """,
        (existing_record["id"],),
    )
    record = cursor.fetchone()
    conn.close()
    return serialize_customer(record)


def save_or_update_customer_from_telegram(
    telegram_id: str,
    first_name: str,
    last_name: str,
    telegram_username: str | None = None,
):
    normalized_telegram_id = str(telegram_id or "").strip()
    normalized_username = normalize_telegram_username(telegram_username or "")
    display_name = " ".join(
        part.strip()
        for part in [str(first_name or "").strip(), str(last_name or "").strip()]
        if part.strip()
    ).strip()

    if not normalized_telegram_id:
        raise ValueError("telegram_id_required")

    if not display_name and normalized_username:
        display_name = normalized_username

    if not display_name:
        display_name = f"Telegram {normalized_telegram_id}"

    existing_record = get_customer_record_by_telegram_id(normalized_telegram_id)
    existing_username_record = (
        get_customer_record_by_telegram_username(normalized_username)
        if normalized_username
        else None
    )

    if (
        existing_record
        and existing_username_record
        and existing_record["id"] != existing_username_record["id"]
    ):
        raise ValueError("telegram_identity_conflict")

    matched_record = existing_record or existing_username_record
    placeholder_phone = build_telegram_placeholder_phone(
        normalized_username or normalized_telegram_id
    )

    conn = get_connection()
    cursor = conn.cursor()

    if matched_record:
        cursor.execute(
            """
            UPDATE customers
            SET first_name = ?, last_name = ?, name = ?, phone = ?, telegram_id = ?, telegram_username = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                str(first_name or "").strip() or matched_record["first_name"] or "",
                str(last_name or "").strip() or matched_record["last_name"] or "",
                display_name,
                matched_record["phone"] or placeholder_phone,
                normalized_telegram_id,
                normalized_username or matched_record["telegram_username"],
                matched_record["id"],
            ),
        )
        customer_id = matched_record["id"]
    else:
        cursor.execute(
            """
            INSERT INTO customers (first_name, last_name, name, phone, telegram_id, telegram_username, password_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(first_name or "").strip(),
                str(last_name or "").strip(),
                display_name,
                placeholder_phone,
                normalized_telegram_id,
                normalized_username or None,
                None,
            ),
        )
        customer_id = cursor.lastrowid

    conn.commit()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, telegram_id, telegram_username, password_hash, address, created_at, updated_at
        FROM customers
        WHERE id = ?
        """,
        (customer_id,),
    )
    record = cursor.fetchone()
    conn.close()
    return serialize_customer(record)


init_customers_db()
ensure_customer_columns()
init_customer_login_sessions_db()
init_customer_registration_sessions_db()
ensure_customer_registration_session_columns()
