"""Runtime settings, read from environment / .env. See CONTRACTS.md."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    polytone_model: str = "claude-sonnet-5-5"
    polytone_llm: str = ""  # "", "anthropic", "mock"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "polytone-local"

    polytone_api_port: int = 8765
    polytone_data_dir: Path = REPO_ROOT / "data"
    polytone_watch_dir: Path = REPO_ROOT / "data" / "inbox"

    gmail_credentials_path: Path = REPO_ROOT / "credentials.json"
    gmail_token_path: Path = REPO_ROOT / "data" / "gmail_token.json"

    wispr_api_key: str = ""
    wispr_ws_url: str = ""

    polytone_embeddings: str = "qwen"  # "qwen" | "none"

    @property
    def llm_backend(self) -> str:
        if self.polytone_llm in ("mock", "anthropic"):
            return self.polytone_llm
        return "anthropic" if self.anthropic_api_key else "mock"

    @property
    def sqlite_path(self) -> Path:
        return self.polytone_data_dir / "polytone.sqlite3"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.polytone_data_dir.mkdir(parents=True, exist_ok=True)
    return s
