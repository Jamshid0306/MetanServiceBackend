import os
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
                    price_uz VARCHAR NOT NULL,
                    price_ru VARCHAR NOT NULL,
                    price_en VARCHAR NOT NULL,
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
                    price_uz, price_ru, price_en, images
                )
                SELECT
                    id, name_uz, name_ru, name_en,
                    description_uz, description_ru, description_en,
                    characteristic_uz, characteristic_ru, characteristic_en,
                    price_uz, price_ru, price_en, images
                FROM products
                """
            )
            cursor.execute("DROP TABLE products")
            cursor.execute("ALTER TABLE products_new RENAME TO products")

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
