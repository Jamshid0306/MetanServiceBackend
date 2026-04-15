import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "orders.db"

ORDER_COLUMNS = [
    "id",
    "name",
    "phone",
    "products",
    "total",
    "locale",
    "status",
    "payment_method",
    "click_trans_id",
    "click_paydoc_id",
    "merchant_prepare_id",
    "merchant_confirm_id",
    "click_error",
    "click_error_note",
    "myid_session_id",
    "myid_external_id",
    "myid_job_id",
    "myid_result_code",
    "myid_result_note",
    "myid_profile",
    "ican_credit_id",
    "ican_credit_payload",
    "created_at",
    "updated_at",
]

MIGRATION_COLUMNS: dict[str, str] = {
    "status": "TEXT DEFAULT 'pending'",
    "payment_method": "TEXT DEFAULT 'cash'",
    "click_trans_id": "TEXT",
    "click_paydoc_id": "TEXT",
    "merchant_prepare_id": "INTEGER",
    "merchant_confirm_id": "INTEGER",
    "click_error": "INTEGER",
    "click_error_note": "TEXT",
    "myid_session_id": "TEXT",
    "myid_external_id": "TEXT",
    "myid_job_id": "TEXT",
    "myid_result_code": "INTEGER",
    "myid_result_note": "TEXT",
    "myid_profile": "TEXT",
    "ican_credit_id": "TEXT",
    "ican_credit_payload": "TEXT",
    "updated_at": "TIMESTAMP",
}

MONTHLY_PAYMENT_COLUMNS = [
    "id",
    "order_id",
    "credit_id",
    "phone",
    "amount",
    "status",
    "click_trans_id",
    "click_paydoc_id",
    "merchant_prepare_id",
    "merchant_confirm_id",
    "click_error",
    "click_error_note",
    "ican_response",
    "ican_error_note",
    "created_at",
    "updated_at",
]


def _get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _column_exists(cursor: sqlite3.Cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cursor.fetchall())


def init_db() -> None:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            products TEXT,
            total REAL,
            locale TEXT,
            status TEXT DEFAULT 'pending',
            payment_method TEXT DEFAULT 'cash',
            click_trans_id TEXT,
            click_paydoc_id TEXT,
            merchant_prepare_id INTEGER,
            merchant_confirm_id INTEGER,
            click_error INTEGER,
            click_error_note TEXT,
            myid_session_id TEXT,
            myid_external_id TEXT,
            myid_job_id TEXT,
            myid_result_code INTEGER,
            myid_result_note TEXT,
            myid_profile TEXT,
            ican_credit_id TEXT,
            ican_credit_payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_orders_schema() -> None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        for column_name, definition in MIGRATION_COLUMNS.items():
            if _column_exists(cursor, "orders", column_name):
                continue
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {column_name} {definition}")

        cursor.execute(
            """
            UPDATE orders
            SET payment_method = COALESCE(NULLIF(TRIM(payment_method), ''), 'cash')
            """
        )
        cursor.execute(
            """
            UPDATE orders
            SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
            """
        )
        conn.commit()
    finally:
        conn.close()


def init_monthly_payments_db() -> None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                credit_id TEXT NOT NULL,
                phone TEXT,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                click_trans_id TEXT,
                click_paydoc_id TEXT,
                merchant_prepare_id INTEGER,
                merchant_confirm_id INTEGER,
                click_error INTEGER,
                click_error_note TEXT,
                ican_response TEXT,
                ican_error_note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _serialize_products(products: list[dict[str, Any]] | list[Any]) -> str:
    return json.dumps(products or [], ensure_ascii=False)


def _deserialize_products(raw_value: Any) -> list[Any]:
    if not raw_value:
        return []

    try:
        payload = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []

    return payload if isinstance(payload, list) else []


def _serialize_json_object(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return json.dumps(value, ensure_ascii=False)


def _deserialize_json_object(raw_value: Any) -> dict[str, Any] | None:
    if not raw_value:
        return None

    try:
        payload = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def _row_to_order(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None

    payload = dict(row)
    payload["products"] = _deserialize_products(payload.get("products"))
    payload["myid_profile"] = _deserialize_json_object(payload.get("myid_profile"))
    payload["ican_credit_payload"] = _deserialize_json_object(payload.get("ican_credit_payload"))
    return payload


def _row_to_monthly_payment(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None

    payload = dict(row)
    payload["ican_response"] = _deserialize_json_object(payload.get("ican_response"))
    return payload


def create_order(
    *,
    name: str,
    phone: str,
    products: list[dict[str, Any]] | list[Any],
    total: float,
    locale: str,
    status: str = "pending",
    payment_method: str = "cash",
    click_trans_id: str | None = None,
    click_paydoc_id: str | None = None,
    merchant_prepare_id: int | None = None,
    merchant_confirm_id: int | None = None,
    click_error: int | None = None,
    click_error_note: str | None = None,
    myid_session_id: str | None = None,
    myid_external_id: str | None = None,
    myid_job_id: str | None = None,
    myid_result_code: int | None = None,
    myid_result_note: str | None = None,
    myid_profile: dict[str, Any] | None = None,
    ican_credit_id: str | None = None,
    ican_credit_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO orders (
                name,
                phone,
                products,
                total,
                locale,
                status,
                payment_method,
                click_trans_id,
                click_paydoc_id,
                merchant_prepare_id,
                merchant_confirm_id,
                click_error,
                click_error_note,
                myid_session_id,
                myid_external_id,
                myid_job_id,
                myid_result_code,
                myid_result_note,
                myid_profile,
                ican_credit_id,
                ican_credit_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                phone,
                _serialize_products(products),
                float(total or 0),
                locale,
                status,
                payment_method,
                click_trans_id,
                click_paydoc_id,
                merchant_prepare_id,
                merchant_confirm_id,
                click_error,
                click_error_note,
                myid_session_id,
                myid_external_id,
                myid_job_id,
                myid_result_code,
                myid_result_note,
                _serialize_json_object(myid_profile),
                ican_credit_id,
                _serialize_json_object(ican_credit_payload),
            ),
        )
        order_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return get_order(order_id) or {}


def save_order(
    name: str,
    phone: str,
    products: list[dict[str, Any]] | list[Any],
    total: float,
    locale: str,
    *,
    status: str = "pending",
    payment_method: str = "cash",
) -> dict[str, Any]:
    return create_order(
        name=name,
        phone=phone,
        products=products,
        total=total,
        locale=locale,
        status=status,
        payment_method=payment_method,
    )


def update_order(order_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed_fields = {
        "name",
        "phone",
        "products",
        "total",
        "locale",
        "status",
        "payment_method",
        "click_trans_id",
        "click_paydoc_id",
        "merchant_prepare_id",
        "merchant_confirm_id",
        "click_error",
        "click_error_note",
        "myid_session_id",
        "myid_external_id",
        "myid_job_id",
        "myid_result_code",
        "myid_result_note",
        "myid_profile",
        "ican_credit_id",
        "ican_credit_payload",
    }

    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed_fields:
            continue
        if key == "products":
            updates[key] = _serialize_products(value or [])
        elif key == "myid_profile":
            updates[key] = _serialize_json_object(value)
        elif key == "ican_credit_payload":
            updates[key] = _serialize_json_object(value)
        else:
            updates[key] = value

    if not updates:
        return get_order(order_id)

    updates["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    assignments = ", ".join(f"{field} = ?" for field in updates.keys())
    values = list(updates.values())
    values.append(order_id)

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE orders SET {assignments} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()

    return get_order(order_id)


def get_order(order_id: int) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {', '.join(ORDER_COLUMNS)} FROM orders WHERE id = ?",
            (order_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    return _row_to_order(row)


def get_order_by_click_trans_id(click_trans_id: str | int) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {', '.join(ORDER_COLUMNS)} FROM orders WHERE click_trans_id = ?",
            (str(click_trans_id),),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    return _row_to_order(row)


def get_order_by_prepare_id(merchant_prepare_id: int) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {', '.join(ORDER_COLUMNS)} FROM orders WHERE merchant_prepare_id = ?",
            (merchant_prepare_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    return _row_to_order(row)


def get_order_by_myid_session_id(myid_session_id: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {', '.join(ORDER_COLUMNS)} FROM orders WHERE myid_session_id = ?",
            (str(myid_session_id).strip(),),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    return _row_to_order(row)


def get_orders() -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {', '.join(ORDER_COLUMNS)}
            FROM orders
            ORDER BY created_at DESC, id DESC
            """
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_order(row) for row in rows if row is not None]


def get_orders_by_phone(phone: str) -> list[dict[str, Any]]:
    normalized_phone = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(normalized_phone) == 9:
        normalized_phone = f"998{normalized_phone}"

    if not normalized_phone:
        return []

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {', '.join(ORDER_COLUMNS)}
            FROM orders
            WHERE phone = ?
            ORDER BY created_at DESC, id DESC
            """,
            (normalized_phone,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_order(row) for row in rows if row is not None]


def create_monthly_payment(
    *,
    order_id: int,
    credit_id: str,
    phone: str,
    amount: float,
    status: str = "pending",
) -> dict[str, Any]:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO monthly_payments (
                order_id,
                credit_id,
                phone,
                amount,
                status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, credit_id, phone, float(amount or 0), status),
        )
        payment_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return get_monthly_payment(payment_id) or {}


def update_monthly_payment(payment_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed_fields = {
        "order_id",
        "credit_id",
        "phone",
        "amount",
        "status",
        "click_trans_id",
        "click_paydoc_id",
        "merchant_prepare_id",
        "merchant_confirm_id",
        "click_error",
        "click_error_note",
        "ican_response",
        "ican_error_note",
    }

    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed_fields:
            continue
        if key == "ican_response":
            updates[key] = _serialize_json_object(value)
        else:
            updates[key] = value

    if not updates:
        return get_monthly_payment(payment_id)

    updates["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    assignments = ", ".join(f"{field} = ?" for field in updates.keys())
    values = list(updates.values())
    values.append(payment_id)

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE monthly_payments SET {assignments} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()

    return get_monthly_payment(payment_id)


def get_monthly_payment(payment_id: int) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {', '.join(MONTHLY_PAYMENT_COLUMNS)} FROM monthly_payments WHERE id = ?",
            (payment_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    return _row_to_monthly_payment(row)


def get_monthly_payments_by_order_id(order_id: int) -> list[dict[str, Any]]:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {', '.join(MONTHLY_PAYMENT_COLUMNS)}
            FROM monthly_payments
            WHERE order_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (order_id,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_monthly_payment(row) for row in rows if row is not None]


def get_monthly_payments_by_phone(phone: str) -> list[dict[str, Any]]:
    normalized_phone = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(normalized_phone) == 9:
        normalized_phone = f"998{normalized_phone}"

    if not normalized_phone:
        return []

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {', '.join(MONTHLY_PAYMENT_COLUMNS)}
            FROM monthly_payments
            WHERE phone = ?
            ORDER BY created_at DESC, id DESC
            """,
            (normalized_phone,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_monthly_payment(row) for row in rows if row is not None]


def get_last_30_days_orders() -> list[dict[str, Any]]:
    thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {', '.join(ORDER_COLUMNS)}
            FROM orders
            WHERE created_at >= ?
            ORDER BY created_at DESC, id DESC
            """,
            (thirty_days_ago,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_order(row) for row in rows if row is not None]


def mark_order_completed(order_id: int) -> dict[str, Any] | None:
    return update_order(order_id, status="completed", click_error=0, click_error_note="")


def mark_order_cancelled(
    order_id: int,
    *,
    click_error: int | None = None,
    click_error_note: str | None = None,
) -> dict[str, Any] | None:
    return update_order(
        order_id,
        status="cancelled",
        click_error=click_error,
        click_error_note=click_error_note,
    )


def delete_order(order_id: int) -> None:
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
    finally:
        conn.close()


init_db()
ensure_orders_schema()
init_monthly_payments_db()
