from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.event import listens_for

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """SQLite 并发稳健性：WAL 允许读写并发；busy_timeout 让写锁竞争时等待而非立刻报错。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema(bind=engine):
    """建表并对旧版本数据库做增量列迁移（幂等）。

    历史数据库没有 pending_crisis 列；create_all 不会修改已存在的表，
    因此这里按表结构检测后手动 ALTER 补齐，保证旧档案可以继续使用。
    """
    Base.metadata.create_all(bind=bind)
    inspector = inspect(bind)
    if "game_sessions" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("game_sessions")}
    if "pending_crisis" not in columns:
        with bind.begin() as conn:
            # SQLite 中 JSON 列以 TEXT 存储，旧库补列时显式用 TEXT 以匹配
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN pending_crisis TEXT")
            )
