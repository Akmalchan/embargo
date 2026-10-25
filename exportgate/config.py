"""All settings in one place. Override any of them with environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(_env("EG_DATA", str(ROOT / "data")))
    state_dir: Path = Path(_env("EG_STATE", str(ROOT / "data" / "state")))

    # Reader backend: "auto" (text layer first, vision model only when needed),
    # "vlm" (always the vision model), "text" (text layer only, no model), or
    # "replay" (reuse readings saved by earlier live runs; the stage safety net, labelled in the log).
    reader: str = _env("EG_READER", "auto")
    # Any OpenAI-compatible server on this machine: vLLM on the GB10, Ollama on a laptop.
    llm_base_url: str = _env("EG_LLM_URL", "http://127.0.0.1:8000/v1")
    llm_model: str = _env("EG_LLM_MODEL", "nvidia/Qwen3.6-35B-A3B-NVFP4")
    llm_timeout: float = float(_env("EG_LLM_TIMEOUT", "180"))
    llm_concurrency: int = int(_env("EG_LLM_CONCURRENCY", "16"))
    tile_px: int = int(_env("EG_TILE", "1024"))
    tile_overlap: float = float(_env("EG_TILE_OVERLAP", "0.15"))
    render_dpi: int = int(_env("EG_DPI", "150"))

    # Sweep
    sweep_workers: int = int(_env("EG_SWEEP_WORKERS", str(max(4, (os.cpu_count() or 8)))))
    digest_time: str = _env("EG_DIGEST_TIME", "07:00")

    # Slack (Socket Mode). Tokens come from the environment only, never from files in the repo.
    slack_bot_token: str = field(default_factory=lambda: os.environ.get("SLACK_BOT_TOKEN", ""))
    slack_app_token: str = field(default_factory=lambda: os.environ.get("SLACK_APP_TOKEN", ""))
    slack_channel: str = _env("EG_SLACK_CHANNEL", "embargo")

    @property
    def company_dir(self) -> Path:
        return self.data_dir / "company"

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "exportgate.db"


SETTINGS = Settings()
SETTINGS.state_dir.mkdir(parents=True, exist_ok=True)
