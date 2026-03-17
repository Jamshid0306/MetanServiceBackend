import hashlib
import hmac
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "orders.db"
PASSWORD_ITERATIONS = 120_000


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

    return {
        "id": record["id"],
        "name": build_customer_name(record),
        "phone": record["phone"],
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
        SELECT id, first_name, last_name, name, phone, password_hash, created_at, updated_at
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


def save_customer_account(name: str, phone: str, password: str):
    conn = get_connection()
    cursor = conn.cursor()
    existing_record = get_customer_record_by_phone(phone)
    password_hash = hash_password(password)

    if existing_record:
        cursor.execute(
            """
            UPDATE customers
            SET first_name = ?, last_name = ?, name = ?, password_hash = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, "", name, password_hash, existing_record["id"]),
        )
        customer_id = existing_record["id"]
    else:
        cursor.execute(
            """
            INSERT INTO customers (first_name, last_name, name, phone, password_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, "", name, phone, password_hash),
        )
        customer_id = cursor.lastrowid

    conn.commit()
    cursor.execute(
        """
        SELECT id, first_name, last_name, name, phone, password_hash, created_at, updated_at
        FROM customers
        WHERE id = ?
        """,
        (customer_id,),
    )
    record = cursor.fetchone()
    conn.close()
    return serialize_customer(record)


def authenticate_customer(phone: str, password: str):
    record = get_customer_record_by_phone(phone)

    if not record or not verify_password(password, str(record["password_hash"] or "")):
        return None

    return serialize_customer(record)


init_customers_db()
ensure_customer_columns()
