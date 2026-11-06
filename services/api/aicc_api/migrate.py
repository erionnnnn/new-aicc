"""Alembic 마이그레이션을 코드에서 실행한다(단일 실행 프로그램이 별도 터미널 없이 DB를 준비)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import get_settings

HERE = Path(__file__).resolve().parent


def alembic_config() -> Config:
    cfg = Config(str(HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    return cfg


def upgrade() -> None:
    command.upgrade(alembic_config(), "head")


if __name__ == "__main__":
    upgrade()
    print("DB 마이그레이션 완료")
