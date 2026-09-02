from __future__ import annotations

import os

from dotenv import load_dotenv

from .core import AutonomyMode, Settings

load_dotenv()


def settings_from_env(*, autonomy_mode: AutonomyMode | None = None) -> Settings:
    """Load deterministic policy controls from .env/environment variables."""
    mode = autonomy_mode or AutonomyMode(os.getenv("SENTINELOPS_AUTONOMY", AutonomyMode.ASSISTED.value).strip().lower())
    return Settings(
        autonomy_mode=mode,
        autonomous_confidence_threshold=float(os.getenv("SENTINELOPS_CONFIDENCE_THRESHOLD", "0.82")),
        max_blast_radius=int(os.getenv("SENTINELOPS_MAX_BLAST_RADIUS", "2")),
        action_budget=int(os.getenv("SENTINELOPS_ACTION_BUDGET", "4")),
    )
