import os
import json
import sqlite3
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "products.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _column_exists(cursor: sqlite3.Cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cursor.fetchall())


def _migrate_to_products_only_schema() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")

        if _table_exists(cursor, "products") and (
            _column_exists(cursor, "products", "category_id")
            or _column_exists(cursor, "products", "brand_id")
        ):
            cursor.execute(
                """
                CREATE TABLE products_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name_uz VARCHAR NOT NULL,
                    name_ru VARCHAR NOT NULL,
                    name_en VARCHAR NOT NULL,
                    description_uz TEXT,
                    description_ru TEXT,
                    description_en TEXT,
                    characteristic_uz TEXT,
                    characteristic_ru TEXT,
                    characteristic_en TEXT,
                    default_price VARCHAR,
                    price_uz VARCHAR NOT NULL,
                    price_ru VARCHAR NOT NULL,
                    price_en VARCHAR NOT NULL,
                    credit_enabled INTEGER NOT NULL DEFAULT 0,
                    credit_months INTEGER,
                    credit_percent INTEGER,
                    credit_6m_percent INTEGER,
                    credit_plans TEXT,
                    config_options TEXT,
                    images VARCHAR
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO products_new (
                    id, name_uz, name_ru, name_en,
                    description_uz, description_ru, description_en,
                    characteristic_uz, characteristic_ru, characteristic_en,
                    default_price,
                    price_uz, price_ru, price_en,
                    credit_enabled, credit_months, credit_percent, credit_6m_percent, credit_plans,
                    config_options, images
                )
                SELECT
                    id, name_uz, name_ru, name_en,
                    description_uz, description_ru, description_en,
                    characteristic_uz, characteristic_ru, characteristic_en,
                    NULL,
                    price_uz, price_ru, price_en,
                    0, NULL, NULL, NULL, NULL, NULL, images
                FROM products
                """
            )
            cursor.execute("DROP TABLE products")
            cursor.execute("ALTER TABLE products_new RENAME TO products")

        if _table_exists(cursor, "products") and not _column_exists(cursor, "products", "credit_enabled"):
            cursor.execute("ALTER TABLE products ADD COLUMN credit_enabled INTEGER NOT NULL DEFAULT 0")

        if _table_exists(cursor, "products") and not _column_exists(cursor, "products", "default_price"):
            cursor.execute("ALTER TABLE products ADD COLUMN default_price VARCHAR")

        if _table_exists(cursor, "products") and not _column_exists(cursor, "products", "credit_months"):
            cursor.execute("ALTER TABLE products ADD COLUMN credit_months INTEGER")

        if _table_exists(cursor, "products") and not _column_exists(cursor, "products", "credit_percent"):
            cursor.execute("ALTER TABLE products ADD COLUMN credit_percent INTEGER")

        if _table_exists(cursor, "products") and not _column_exists(cursor, "products", "credit_6m_percent"):
            cursor.execute("ALTER TABLE products ADD COLUMN credit_6m_percent INTEGER")

        if _table_exists(cursor, "products") and not _column_exists(cursor, "products", "credit_plans"):
            cursor.execute("ALTER TABLE products ADD COLUMN credit_plans TEXT")

        if _table_exists(cursor, "products") and not _column_exists(cursor, "products", "config_options"):
            cursor.execute("ALTER TABLE products ADD COLUMN config_options TEXT")

        if _table_exists(cursor, "products"):
            cursor.execute(
                """
                UPDATE products
                SET credit_months = COALESCE(credit_months, CASE WHEN credit_6m_percent IS NOT NULL THEN 6 END)
                WHERE credit_months IS NULL
                """
            )
            cursor.execute(
                """
                UPDATE products
                SET credit_percent = COALESCE(credit_percent, credit_6m_percent)
                WHERE credit_percent IS NULL
                """
            )

            cursor.execute(
                """
                SELECT id, credit_months, credit_percent, credit_6m_percent, credit_plans
                FROM products
                """
            )
            rows = cursor.fetchall()

            for product_id, credit_months, credit_percent, credit_6m_percent, credit_plans in rows:
                if str(credit_plans or "").strip():
                    continue

                plans = []
                seen_months = set()

                if credit_months is not None and credit_percent is not None:
                    month_value = int(credit_months)
                    plans.append(
                        {
                            "months": month_value,
                            "percent": int(credit_percent),
                        }
                    )
                    seen_months.add(month_value)

                if credit_6m_percent is not None and 6 not in seen_months:
                    plans.append(
                        {
                            "months": 6,
                            "percent": int(credit_6m_percent),
                        }
                    )

                if not plans:
                    continue

                plans.sort(key=lambda item: item["months"])
                cursor.execute(
                    "UPDATE products SET credit_plans = ? WHERE id = ?",
                    (json.dumps(plans, ensure_ascii=False), product_id),
                )

        if _table_exists(cursor, "categories"):
            cursor.execute("DROP TABLE categories")

        if _table_exists(cursor, "brands"):
            cursor.execute("DROP TABLE brands")

        if _table_exists(cursor, "about_blocks"):
            cursor.execute("DROP TABLE about_blocks")

        cursor.execute("PRAGMA foreign_keys=ON")
        conn.commit()
    finally:
        conn.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    _migrate_to_products_only_schema()
    from . import models  # bu import barcha modellarni yuklaydi

    Base.metadata.create_all(bind=engine)
