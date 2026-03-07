import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).resolve().parent.parent / "orders.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        phone TEXT,
        products TEXT,
        total REAL,
        locale TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()


def save_order(name: str, phone: str, products: list, total: float, locale: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO orders (name, phone, products, total, locale) VALUES (?, ?, ?, ?, ?)",
        (name, phone, json.dumps(products, ensure_ascii=False), total, locale)
    )
    conn.commit()
    conn.close()


def get_orders():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, phone, products, total, locale, status, created_at FROM orders ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    orders = []
    for r in rows:
        orders.append({
            "id": r[0],
            "name": r[1],
            "phone": r[2],
            "products": json.loads(r[3]),
            "total": r[4],
            "locale": r[5],
            "status": r[6],
            "created_at": r[7],
        })
    return orders


def get_last_30_days_orders():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "SELECT id, name, phone, products, total, locale, status, created_at FROM orders WHERE created_at >= ? ORDER BY created_at DESC",
        (thirty_days_ago,)
    )
    rows = cursor.fetchall()
    conn.close()
    orders = []
    for r in rows:
        orders.append({
            "id": r[0],
            "name": r[1],
            "phone": r[2],
            "products": json.loads(r[3]),
            "total": r[4],
            "locale": r[5],
            "status": r[6],
            "created_at": r[7],
        })
    return orders


def mark_order_completed(order_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()


def delete_order(order_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()


def ensure_status_column():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'pending'")
        conn.commit()
    except sqlite3.OperationalError:
        # agar ustun allaqachon mavjud bo‘lsa, xato chiqadi – uni e’tiborsiz qoldiramiz
        pass
    finally:
        conn.close()

init_db()
ensure_status_column()

