"""All settings in one place. Values come from .env (never committed) with safe defaults."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name, default):
    v = os.environ.get(name)
    return type(default)(v) if v not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    original_backend: Path = ROOT / "vendor/original/backend"
    mongo_backend: Path = ROOT / "apps/mongo-backend"
    state_file: Path = ROOT / "state/pipeline_state.json"
    logs: Path = ROOT / "logs"

    pg_dsn: str = _env("PG_DSN", "postgresql://conduit:conduit@localhost:5432/conduit")
    mongo_uri: str = _env("MONGO_URI", "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true")
    mongo_db: str = _env("MONGO_DB", "conduit")
    mongo_naive_db: str = _env("MONGO_NAIVE_DB", "conduit_naive")
    original_url: str = _env("ORIGINAL_URL", "http://localhost:3001")
    mongo_url: str = _env("MONGO_URL", "http://localhost:3002")
    jwt_key: str = _env("APP_JWT_KEY", "amp_local_dev_jwt_key")

    # LLM (Groq free tier). Free-tier limit observed: 8000 tokens/minute per model.
    llm_provider: str = _env("LLM_PROVIDER", "groq")
    llm_model: str = _env("LLM_MODEL", "openai/gpt-oss-120b")
    llm_review_model: str = _env("LLM_REVIEW_MODEL", "qwen/qwen3.8-27b")
    # used only when a model hits its Groq DAILY limit (each model has its own free quota)
    llm_fallback_models: str = _env("LLM_FALLBACK_MODELS", "qwen/qwen3.8-27b,openai/gpt-oss-20b")
    llm_tpm_limit: int = _env("LLM_TPM_LIMIT", 8000)
    llm_max_tokens_per_run: int = _env("LLM_MAX_TOKENS_PER_RUN", 250000)  # hard cap, all calls summed
    llm_max_spend_usd: float = _env("LLM_MAX_SPEND_USD", 0.0)  # Groq free tier => $0; cap kept for paid providers
    llm_max_repair_iters: int = _env("LLM_MAX_REPAIR_ITERS", 5)
    groq_api_key: str = field(default_factory=lambda: os.environ.get("GROQ_API_KEY", ""), repr=False)

    validation_articles: int = _env("VALIDATION_ARTICLES", 1000)
    bench_articles: int = _env("BENCH_ARTICLES", 100000)


settings = Settings()
