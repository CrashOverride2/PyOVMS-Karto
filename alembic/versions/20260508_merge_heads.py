"""merge heads 20260227 and 20260507

Revision ID: 20260508
Revises: 20260227, 20260507
Create Date: 2026-05-08 00:00:00.000000

"""
from typing import Sequence, Union

revision: str = '20260508'
down_revision: Union[str, Sequence[str], None] = ('20260227', '20260507')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
