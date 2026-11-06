"""packages/contracts/openapi.json 재생성."""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("AICC_DATA_DIR", tempfile.mkdtemp())
from aicc_api.main import create_app  # noqa: E402

out = Path(__file__).resolve().parents[1] / "packages" / "contracts" / "openapi.json"
out.write_text(json.dumps(create_app().openapi(), ensure_ascii=False, indent=1), encoding="utf-8")
print(out)
