"""Configuración leída de .env vía Pydantic Settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── DB ──────────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db"
    )
    database_url_sync: str = Field(
        default="postgresql://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # ── Anthropic ───────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="")
    claude_model_principal: str = "claude-sonnet-4-6"
    claude_model_intent: str = "claude-haiku-4-5-20251001"
    claude_max_tokens_output: int = 1024
    claude_max_conversaciones_por_hora: int = 300

    # ── whapi ───────────────────────────────────────────────────────────────
    whapi_base_url: str = "https://gate.whapi.cloud"
    whapi_token: str = Field(default="")
    whapi_numero_bot: str = "+573026041584"
    whapi_webhook_secret: str = Field(default="")
    whapi_webhook_url: str = "https://asistente.dtgrowthpartners.com/webhook"

    # ── Shopify ─────────────────────────────────────────────────────────────
    shopify_api_base_url: str = "https://innova.dtgrowthpartners.com/api"
    shopify_api_key: str = "a3f1b2c4-d5e6-7890-abcd-ef1234567890"
    catalogo_html_url: str = "https://innovacionfashion.co/pages/catalogo-de-whatsapp"
    catalogo_publico_url: str = "https://innovacionfashion.co/products.json"
    catalogo_html_sync_interval_horas: int = 12

    # ── Teléfonos ───────────────────────────────────────────────────────────
    asistente_phone_produccion: str = "+573243798269"
    fabio_phone: str = "+573019836645"
    dueno_phone_blocked: str = "+573206811130"

    # ── Bot ─────────────────────────────────────────────────────────────────
    bot_host: str = "127.0.0.1"
    bot_port: int = 8003
    bot_log_level: str = "INFO"
    bot_log_file: str = "/home/asistente/logs/asistente.log"
    bot_env: str = "development"

    rate_limit_mensajes_por_minuto_cliente: int = 10
    rate_limit_outbound_por_hora: int = 15

    # ── Humanización (anti-detección WhatsApp spam) ────────────────────────
    humanization_delay_min_s: float = 4.0          # delay mínimo antes de enviar
    humanization_delay_max_s: float = 25.0         # delay máximo
    humanization_delay_por_caracter_s: float = 0.04  # ~25 cps de "tipeo"
    humanization_typing_indicator: bool = True
    horario_inicio_hora: int = 8                   # 8AM Bogotá
    horario_fin_hora: int = 22                     # 10PM Bogotá
    feature_humanizacion: bool = True              # toggle global

    # ── Admin panel ────────────────────────────────────────────────────────
    admin_user: str = "admin"
    admin_password: str = "cambiame_en_env"        # set en .env
    admin_session_secret: str = "set_un_string_random_de_32_chars"

    # ── Paths ───────────────────────────────────────────────────────────────
    data_dir: str = "/home/asistente/data"
    catalogo_dir: str = "/home/asistente/data/catalogo"
    pdfs_dir: str = "/home/asistente/data/pdfs"
    bancos_dir: str = "/home/asistente/data/mediosdepago"
    ubicaciones_dir: str = "/home/asistente/data/ubicaciones"
    prompts_dir: str = "/home/asistente/data/prompts"
    voice_module_python: str = "/home/asistente/data/voice_notes_module/speech_env/bin/python"
    voice_module_script: str = "/home/asistente/data/voice_notes_module/audio_processor.py"
    voice_timeout_segundos: int = 60

    # ── Feature flags ───────────────────────────────────────────────────────
    feature_seguimiento_auto: bool = False
    feature_analytics: bool = False
    feature_human_takeover: bool = True
    feature_audio_transcripcion: bool = True
    feature_catalogo_html: bool = True

    tz: str = "America/Bogota"

    @property
    def is_production(self) -> bool:
        return self.bot_env.lower() == "production"

    @property
    def prompts_path(self) -> Path:
        return Path(self.prompts_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
