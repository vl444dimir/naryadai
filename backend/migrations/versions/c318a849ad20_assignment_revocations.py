"""Deliver removal events to previous assignees without further order access."""
from alembic import op
import sqlalchemy as sa

revision = "c318a849ad20"
down_revision = "79ce6b05dc75"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_event_revocations",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["order_events.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("event_id", "user_id"),
    )
    op.create_index("ix_order_event_revocations_user_id", "order_event_revocations", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_order_event_revocations_user_id", table_name="order_event_revocations")
    op.drop_table("order_event_revocations")
