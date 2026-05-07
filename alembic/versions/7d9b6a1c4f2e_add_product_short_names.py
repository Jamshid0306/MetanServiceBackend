"""Add product short names

Revision ID: 7d9b6a1c4f2e
Revises: 319af31829b5
Create Date: 2026-05-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7d9b6a1c4f2e"
down_revision: Union[str, Sequence[str], None] = "319af31829b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("short_name_uz", sa.String(), nullable=True))
    op.add_column("products", sa.Column("short_name_ru", sa.String(), nullable=True))
    op.add_column("products", sa.Column("short_name_en", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "short_name_en")
    op.drop_column("products", "short_name_ru")
    op.drop_column("products", "short_name_uz")
