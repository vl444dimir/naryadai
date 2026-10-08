from alembic import context
from sqlalchemy import engine_from_config, pool
from app.config import get_settings
from app.models import Base
from app.db import UTCDateTime

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


if context.is_offline_mode():
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_item=render_item, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
