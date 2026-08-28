"""add logo_url to social_brand_voices

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "social_brand_voices",
        sa.Column("logo_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("social_brand_voices", "logo_url")
