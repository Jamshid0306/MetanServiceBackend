"""split price into price_uz_ru_en

Revision ID: d0ddb689d28b
Revises: 7985e0b1cfc1
Create Date: 2025-09-27 22:45:18.569558

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0ddb689d28b'
down_revision: Union[str, Sequence[str], None] = '7985e0b1cfc1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('price_uz', sa.Float(), nullable=False, server_default=sa.text("0")))
    op.add_column('products', sa.Column('price_ru', sa.Float(), nullable=False, server_default=sa.text("0")))
    op.add_column('products', sa.Column('price_en', sa.Float(), nullable=False, server_default=sa.text("0")))

    # Eski 'price' qiymatini uchta ustunga ko‘chirish
    op.execute("UPDATE products SET price_uz = price, price_ru = price, price_en = price")

    op.drop_column('products', 'price')


def downgrade() -> None:
    op.add_column('products', sa.Column('price', sa.Float(), nullable=False, server_default=sa.text("0")))

    # Faqat bitta ustunga qaytarish uchun, masalan price_uz ni ishlatyapmiz
    op.execute("UPDATE products SET price = price_uz")

    op.drop_column('products', 'price_uz')
    op.drop_column('products', 'price_ru')
    op.drop_column('products', 'price_en')



