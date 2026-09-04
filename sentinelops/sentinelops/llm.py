from __future__ import annotations

import json
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from dotenv import load_dotenv

load_dotenv()


class LLMError(RuntimeError):
    """Raised when the configured model backend cannot produce a usable result."""


@dataclass(slots=True)
class LLMConfig:
    backend: str = "auto"
    model: str = "gpt-5-mini"
    fallback_to_deterministic: bool = True
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> LLMConfig:
        backend = os.getenv("SENTINELOPS_AGENT_BACKEND", "auto").strip().lower()
        if backend not in {"auto", "openai", "deterministic"}:
            raise ValueError(
                "SENTINELOPS_AGENT_BACKEND must be auto, openai, or deterministic"
            )
        fallback = os.getenv(
            "SENTINELOPS_LLM_FALLBACK", "true"
        ).strip().lower() not in {"0", "false", "no", "off"}
        return cls(
            backend=backend,
            model=os.getenv("SENTINELOPS_MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
            fallback_to_deterministic=fallback,
            timeout_seconds=float(os.getenv("SENTINELOPS_LLM_TIMEOUT_SECONDS", "30")),
        )


@dataclass(slots=True)
class LLMResult:
    data: dict[str, Any]
    model: str
    response_id: str
    latency_ms: float


class OpenAIReasoner:
    """Thin Responses API adapter. The model proposes reasoning; policy remains deterministic."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
        self._client = client
        self.configuration_error = ""

        if self.config.backend == "deterministic":
            return
        if not self.api_key and client is None:
            if self.config.backend == "openai":
                self.configuration_error = (
                    "OPENAI_API_KEY is required when SENTINELOPS_AGENT_BACKEND=openai"
                )
            return
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                self.configuration_error = (
                    "Install the LLM extra with: pip install -e '.[llm]'"
                )
                if not self.config.fallback_to_deterministic:
                    raise LLMError(self.configuration_error) from exc
                return
            self._client = OpenAI(
                api_key=self.api_key, timeout=self.config.timeout_seconds, max_retries=2
            )

    @property
    def enabled(self) -> bool:
        return self.config.backend != "deterministic" and self._client is not None

    def status(self) -> dict[str, Any]:
        active_backend = "openai" if self.enabled else "deterministic"
        return {
            "requested_backend": self.config.backend,
            "active_backend": active_backend,
            "model": self.config.model if self.enabled else None,
            "fallback_to_deterministic": self.config.fallback_to_deterministic,
            "configured": not bool(self.configuration_error),
            "configuration_error": self.configuration_error or None,
        }

    def generate_json(
        self,
        *,
        schema_name: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> LLMResult:
        if not self.enabled:
            raise LLMError(
                self.configuration_error or "OpenAI reasoning backend is not enabled"
            )

        start = perf_counter()
        try:
            response = self._client.responses.create(
                model=self.config.model,
                instructions=instructions,
                input=json.dumps(payload, sort_keys=True, default=str),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    }
                },
            )
            raw = response.output_text
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("structured model output was not an object")
            return LLMResult(
                data=data,
                model=self.config.model,
                response_id=getattr(response, "id", ""),
                latency_ms=(perf_counter() - start) * 1000,
            )
        except Exception as exc:
            raise LLMError(f"OpenAI Responses API call failed: {exc}") from exc
