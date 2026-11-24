"""Configuration only: importing the API never imports MLX or loads a model."""

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    model: str = "mlx-community/Qwen1.5-0.5B-Chat-4bit"
    mode: str = "managed"
    port: int = 8001
    backend_port: int = 8080
    external_url: str = "http://127.0.0.1:8080"
    mlx_python: str = sys.executable
    idle_seconds: float = 300
    startup_timeout: float = 90
    request_timeout: float = 180
    queue_timeout: float = 60
    max_queue: int = 8
    default_tokens: int = 256
    max_tokens: int = 1024
    max_input_chars: int = 12000
    offline: bool = True
    runtime_dir: Path = ROOT / ".local-ia"

    @property
    def backend_url(self):
        if self.mode == "managed":
            return f"http://127.0.0.1:{self.backend_port}"
        return self.external_url.rstrip("/")

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        values = {}
        for name, field in cls.__dataclass_fields__.items():
            value = os.getenv(f"LOCAL_IA_{name.upper()}")
            if value is not None:
                if field.type is bool:
                    if value.lower() not in {"1", "0", "true", "false"}:
                        raise ValueError(f"LOCAL_IA_{name.upper()} debe ser 1 o 0")
                    values[name] = value.lower() in {"1", "true"}
                else:
                    values[name] = field.type(value)
        settings = cls(**values)
        if settings.mode not in {"managed", "external"}:
            raise ValueError("LOCAL_IA_MODE debe ser managed o external")
        for port in (settings.port, settings.backend_port):
            if not 1 <= port <= 65535:
                raise ValueError("Los puertos deben estar entre 1 y 65535")
        if settings.mode == "managed" and settings.port == settings.backend_port:
            raise ValueError("La API y MLX necesitan puertos distintos")
        for name in ("idle_seconds", "startup_timeout", "request_timeout", "queue_timeout",
                     "default_tokens", "max_tokens", "max_input_chars"):
            if not math.isfinite(getattr(settings, name)) or getattr(settings, name) <= 0:
                raise ValueError(f"LOCAL_IA_{name.upper()} debe ser mayor que cero")
        if settings.max_queue < 0 or settings.default_tokens > settings.max_tokens:
            raise ValueError("Revisa MAX_QUEUE, DEFAULT_TOKENS y MAX_TOKENS")
        if not settings.model.strip():
            raise ValueError("LOCAL_IA_MODEL no puede estar vacío")
        if not settings.external_url.startswith(("http://", "https://")):
            raise ValueError("LOCAL_IA_EXTERNAL_URL debe ser una URL HTTP(S)")
        return settings
