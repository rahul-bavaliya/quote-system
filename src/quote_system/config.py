"""Paths and tunable thresholds. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return type(default)(raw)


@dataclass(frozen=True)
class Settings:
    raw_dir: Path = field(default_factory=lambda: Path(_env("QS_RAW_DIR", str(REPO_ROOT / "data" / "raw"))))
    work_dir: Path = field(default_factory=lambda: Path(_env("QS_WORK_DIR", str(REPO_ROOT / "data" / "work"))))
    config_dir: Path = field(default_factory=lambda: Path(_env("QS_CONFIG_DIR", str(REPO_ROOT / "config"))))
    db_path: Path = field(default_factory=lambda: Path(_env("QS_DB_PATH", str(REPO_ROOT / "data" / "quote.duckdb"))))
    quotes_db_path: Path = field(default_factory=lambda: Path(_env("QS_QUOTES_DB", str(REPO_ROOT / "data" / "quotes.sqlite"))))

    # --- labelling ---
    vote_min_segments: int = _env("QS_VOTE_MIN_SEGMENTS", 3)      # min segments before a 'verify' rule is checked against WO text
    verify_min_share: float = _env("QS_VERIFY_MIN_SHARE", 0.25)   # share of a part's segments whose text supports the component
    discover_min_segments: int = _env("QS_DISCOVER_MIN_SEGMENTS", 15)
    discover_min_share: float = _env("QS_DISCOVER_MIN_SHARE", 0.6)

    # --- statistics ---
    max_hours: float = _env("QS_MAX_HOURS", 400.0)               # segments above this are treated as bad data
    outlier_mad_z: float = _env("QS_OUTLIER_MAD_Z", 3.5)         # robust z-score cut-off on log(hours) per job
    shrink_k: float = _env("QS_SHRINK_K", 8.0)                    # pseudo-count used to blend a level with its parent
    min_level_n: int = _env("QS_MIN_LEVEL_N", 3)                  # min samples for a level to be reported as 'used'
    kit_min_segments: int = _env("QS_KIT_MIN_SEGMENTS", 5)
    kit_attach: float = _env("QS_KIT_ATTACH", 0.6)                # attach rate to be in the default kit
    kit_optional_attach: float = _env("QS_KIT_OPTIONAL_ATTACH", 0.2)
    stats_statuses: str = _env("QS_STATS_STATUSES", "Invoiced")   # comma separated
    stats_exclude_sales_types: str = _env("QS_STATS_EXCLUDE_SALES_TYPES", "Expense")  # comma separated
    min_segments: int = _env("QS_MIN_SEGMENTS", 1000)           # validation: refuse smaller exports
    min_part_lines: int = _env("QS_MIN_PART_LINES", 1000)
    max_drop: float = _env("QS_MAX_DROP", 0.3)                    # refuse a build that loses >30% of segments
    combo_min_n: int = _env("QS_COMBO_MIN_N", 10)                # history needed to quote a combo directly
    similar_min_pool: int = _env("QS_SIMILAR_MIN_POOL", 40)      # segments wanted before loosening the text match
    # machine-class scaling of broad history: backtested, scaling *down* (small machines) helped and scaling
    # up slightly hurt, so the factor is capped at 1.0 by default
    class_factor_min: float = _env("QS_CLASS_FACTOR_MIN", 0.3)
    class_factor_max: float = _env("QS_CLASS_FACTOR_MAX", 1.0)

    # --- service (read at start-up, so tests / containers can set them per process) ---
    api_keys: str = field(default_factory=lambda: os.environ.get("QS_API_KEYS", ""))      # comma separated; empty = auth off
    admin_key: str = field(default_factory=lambda: os.environ.get("QS_ADMIN_KEY", ""))    # required for /admin/*
    cors_origins: str = field(default_factory=lambda: os.environ.get("QS_CORS_ORIGINS", ""))
    log_json: bool = field(default_factory=lambda: os.environ.get("QS_LOG_JSON", "0") in {"1", "true", "yes"})
    log_level: str = field(default_factory=lambda: os.environ.get("QS_LOG_LEVEL", "INFO"))
    version: str = field(default_factory=lambda: os.environ.get("QS_VERSION", "dev"))

    def ensure_dirs(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.quotes_db_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
