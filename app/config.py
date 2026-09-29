"""Settings from environment variables, with a tiny .env loader (no extra dependency)."""
import os
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv() -> None:
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)  # real env vars (e.g. Railway) win


_load_dotenv()


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing required setting {name}. Add it to .env or Railway variables.")
    return value


def allowed_spaces() -> list[str]:
    return [s.strip() for s in get("ALLOWED_SPACES").split(",") if s.strip()]
