"""환경 설정. 비밀값은 환경변수 또는 데이터 디렉터리의 secrets.env(개발용 자동 생성)에서 읽는다.

운영(AICC_ENV=production)에서는 비밀값이 없으면 기동하지 않는다.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


@dataclass(frozen=True)
class Settings:
    env: str
    data_dir: Path
    database_url: str
    data_key: bytes  # Fernet 키 (전화번호·원본행·녹음 암호화)
    hmac_key: bytes  # 전화번호 HMAC (연락거부 대조)
    session_hours: int
    web_dist: Path | None
    stt_engine: str
    gemini_api_key: str | None
    gemini_model: str | None

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _generate_dev_secrets(path: Path) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    vals = {
        "AICC_DATA_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "AICC_HMAC_KEY": secrets.token_urlsafe(32),
    }
    path.write_text("# 개발용 자동 생성 비밀값. 저장소에 올리지 마십시오.\n"
                    + "".join(f"{k}={v}\n" for k, v in vals.items()), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return vals


@lru_cache
def get_settings() -> Settings:
    env = os.environ.get("AICC_ENV", "development")
    data_dir = Path(os.environ.get("AICC_DATA_DIR", ".aicc")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    file_vals = _load_env_file(data_dir / "secrets.env")
    data_key = os.environ.get("AICC_DATA_KEY") or file_vals.get("AICC_DATA_KEY")
    hmac_key = os.environ.get("AICC_HMAC_KEY") or file_vals.get("AICC_HMAC_KEY")
    if not data_key or not hmac_key:
        if env == "production":
            raise RuntimeError("운영 환경에는 AICC_DATA_KEY, AICC_HMAC_KEY 가 필요합니다")
        file_vals = _generate_dev_secrets(data_dir / "secrets.env")
        data_key, hmac_key = file_vals["AICC_DATA_KEY"], file_vals["AICC_HMAC_KEY"]
    db = os.environ.get("AICC_DATABASE_URL") or f"sqlite:///{(data_dir / 'aicc.db').as_posix()}"
    web = os.environ.get("AICC_WEB_DIST")
    default_web = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"
    web_dist = Path(web) if web else (default_web if default_web.is_dir() else None)
    return Settings(
        env=env,
        data_dir=data_dir,
        database_url=db,
        data_key=data_key.encode(),
        hmac_key=hmac_key.encode(),
        session_hours=int(os.environ.get("AICC_SESSION_HOURS", "10")),
        web_dist=web_dist,
        stt_engine=os.environ.get("AICC_STT_ENGINE", "none"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
        gemini_model=os.environ.get("AICC_GEMINI_MODEL") or None,
    )
