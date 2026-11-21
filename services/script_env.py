"""Loads the repository .env for local runs and host-side scripts. Containers have no such file; compose supplies
their variables."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env(path: str | None = None) -> None:
    """Load `path`, by default the repository .env; a relative path starts at the repository root.

    Variables already set in the shell win over the file.
    """
    env_path = Path(path) if path else _REPO_ROOT / ".env"
    if not env_path.is_absolute():
        env_path = _REPO_ROOT / env_path
    load_dotenv(env_path, override=False)
