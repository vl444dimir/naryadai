"""Freeze brigade attribution; legacy NULL stays unknown instead of inferred."""
from alembic import op
import sqlalchemy as sa

revision = "e97b6831a22d"
down_revision = "c318a849ad20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_orders", sa.Column("executor_brigade_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_work_orders_executor_brigade", "work_orders", "brigades", ["executor_brigade_id"], ["id"])
    op.add_column("submissions", sa.Column("actor_brigade_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_submissions_actor_brigade", "submissions", "brigades", ["actor_brigade_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_submissions_actor_brigade", "submissions", type_="foreignkey")
    op.drop_column("submissions", "actor_brigade_id")
    op.drop_constraint("fk_work_orders_executor_brigade", "work_orders", type_="foreignkey")
    op.drop_column("work_orders", "executor_brigade_id")
