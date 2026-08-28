from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL as SAURL
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.core.config import settings

Base = declarative_base()


def _inject_connect_timeout(url: SAURL, timeout_s: int = 10) -> SAURL:
    """Add connect_timeout to a SQLAlchemy URL object if not already present."""
    if "sqlite" in url.drivername:
        return url
    existing = dict(url.query)
    if "connect_timeout" not in existing:
        existing["connect_timeout"] = str(timeout_s)
        return url.set(query=existing)
    return url


_db_url = _inject_connect_timeout(settings.database_url)
_pool_kwargs: dict = {"pool_pre_ping": True}
_connect_args: dict = {}
if "sqlite" not in str(_db_url):
    _pool_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_use_lifo=True,
        pool_recycle=1800,
    )
    _host = _db_url.host or ""
    _is_localhost = _host in ("localhost", "127.0.0.1", "::1")
    if not _is_localhost:
        _connect_args = {
            "keepalives": 1,
            "keepalives_idle": 10,
            "keepalives_interval": 3,
            "keepalives_count": 3,
        }

engine = create_engine(_db_url, connect_args=_connect_args, **_pool_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Validate DB connectivity once at startup (fail fast)."""
    safe_url = "?"
    try:
        db_url = settings.database_url
        url = db_url if isinstance(db_url, SAURL) else make_url(db_url)
        safe_url = url.render_as_string(hide_password=True)
        print(f"[db] connecting: {safe_url}")
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as e:
        raise RuntimeError(f"Database connection check failed (url={safe_url}): {e}") from e


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            try:
                db.invalidate()
            except Exception:
                pass
