"""Add product characteristics

Revision ID: c2ee3ac1aa1b
Revises: 
Create Date: 2025-09-02 17:40:03.183788

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2ee3ac1aa1b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column('products', sa.Column('characteristic_uz', sa.String(), nullable=True))
    op.add_column('products', sa.Column('characteristic_ru', sa.String(), nullable=True))
    op.add_column('products', sa.Column('characteristic_en', sa.String(), nullable=True))

def downgrade():
    op.drop_column('products', 'characteristic_uz')
    op.drop_column('products', 'characteristic_ru')
    op.drop_column('products', 'characteristic_en')
