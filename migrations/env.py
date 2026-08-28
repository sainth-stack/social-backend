import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic Config object
config = context.config

# Interpret config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        from app.core.config import normalize_database_url

        return normalize_database_url(url).render_as_string(hide_password=False)

    from app.core.config import settings

    return settings.database_url.render_as_string(hide_password=False)


def get_target_metadata():
    from app.core.database import Base

    # Import every module that defines ORM models so their tables register on
    # Base.metadata before Alembic diffs the schema.
    import app.users.models  # noqa: F401
    import app.workspaces.models  # noqa: F401
    import app.plans.models  # noqa: F401
    import app.social.models  # noqa: F401

    return Base.metadata


target_metadata = get_target_metadata()


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
