"""SQLAlchemy engine / session factory."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from saas_mvp.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yield a DB session then close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (idempotent)."""
    # import models so their metadata is registered
    # 順序：被依賴的 model 先於依賴它的（ApiKey 先於 ApiKeyUsage）
    from saas_mvp.models import tenant, user, note, usage  # noqa: F401
    from saas_mvp.models import api_key, api_key_usage  # noqa: F401
    Base.metadata.create_all(bind=engine)
