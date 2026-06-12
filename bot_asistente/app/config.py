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

    # ── Dario (proxy local que enruta vía suscripción Claude Max) ───────────
    # provider: "fallback" = Dario primario + API directa de fallback (recomendado)
    #           "dario"    = solo Dario
    #           "direct"   = solo API directa de Anthropic
    claude_provider: str = "fallback"
    dario_base_url: str = "http://127.0.0.1:3456"
    dario_api_key: str = "dario"

    # ── whapi (identidad principal) ─────────────────────────────────────────
    whapi_base_url: str = "https://gate.whapi.cloud"
    whapi_token: str = Field(default="")
    whapi_numero_bot: str = "+573007189383"
    whapi_webhook_secret: str = Field(default="")
    whapi_webhook_url: str = "https://david.dtgrowthpartners.com/webhook"

    # ── API EXTERNA (POST /api/externo/enviar) ──────────────────────────────
    # Shared secret para sistemas terceros que necesiten disparar mensajes
    # vía whapi sin tener el token directo. Caso típico: el monitor de
    # cuentas Meta avisa al grupo "Pautas-estrategias equilibrio" cuando hay
    # un vencimiento.
    # ⚠️ Generar con: openssl rand -hex 32 (NUNCA commitear)
    api_externo_key: str = Field(default="")

    # ── WEBHOOKS SALIENTES (bot → plataforma admin externa) ─────────────────
    # Si configurás `panel_admin_webhook_url`, el bot le pega POST a esa URL
    # cuando ocurren eventos relevantes (estado cambiado, cita agendada,
    # alerta abierta). El header `X-Bot-Source: dairo-bot` + auth opcional
    # con `panel_admin_webhook_secret` permiten que la plataforma valide
    # que el ping vino de este bot.
    panel_admin_webhook_url: str = Field(default="")
    panel_admin_webhook_secret: str = Field(default="")

    # ── Identidad principal (qué persona habla por el canal principal) ─────
    # Default: Dairo Traslaviña (CEO), política estricta (silencio a NULL/personal).
    # Para cambiar a otra persona, sobreescribir desde .env.
    identidad_principal_nombre: str = "Dairo Traslaviña"
    identidad_principal_persona_file: str = "dairo-identidad.md"
    identidad_principal_estricta: bool = True

    # ── whapi — identidad SECUNDARIA opcional (segundo canal) ──────────────
    # Si en el futuro hay un segundo número en paralelo, se conecta
    # acá y queda servido en /webhook/secundaria.
    whapi_token_dairo: str = Field(default="")
    whapi_numero_dairo: str = ""

    # Grupo WhatsApp donde se notifican pedidos confirmados. Vacío = deshabilitado.
    grupo_pedidos_confirmados_id: str = "120363425539154194@g.us"

    # ── DT-OS (backend operativo de DTGP) ───────────────────────────────────
    # API que el bot consulta para tareas, finanzas, terceros, CRM, clientes,
    # cuentas de cobro y registro en Google Sheets. Se REUSA tal cual (no se
    # reconstruye). El header de auth es `x-api-key: <dtos_api_key>`.
    dtos_base_url: str = "https://os.dtgrowthpartners.com/api/webhook/bot"
    dtos_api_key: str = Field(default="")  # ROTAR el viejo (dt-bot-secret-key-2024) y poner aquí vía .env

    # ── MetaSuite (Meta Ads) ─────────────────────────────────────────────────
    metasuite_base_url: str = "https://metasuite.dtgrowthpartners.com/api"
    metasuite_token: str = Field(default="")  # opcional según deploy (algunos endpoints no lo piden)

    # ── Lead Radar (scraping/scoring de prospectos) ──────────────────────────
    leadradar_base_url: str = "https://buscar.dtgrowthpartners.com"

    # ── Cal.com (agendamiento de citas con prospectos) ───────────────────────
    calcom_base_url: str = "https://api.cal.com/v2"
    calcom_api_key: str = Field(default="")
    calcom_event_type_id: str = ""   # event type de la cita de diagnóstico/reunión
    calcom_webhook_secret: str = Field(default="")  # firma del webhook entrante de Cal.com

    # ── OpenAI Whisper (transcripción de notas de voz entrantes) ────────────
    # Se invoca en el webhook ANTES de routear al flow: muta `msg.texto` con
    # la transcripción para que prospecto/equipo/cliente WL lo reciban como
    # texto normal. Falla silencioso si no hay key o si OpenAI responde error.
    openai_api_key: str = Field(default="")
    whisper_model: str = "whisper-1"  # whisper-1 | gpt-4o-mini-transcribe | gpt-4o-transcribe
    whisper_idioma: str = "es"
    feature_transcribir_audio: bool = True

    # ── Fish Audio (voz del bot: TTS para responder + ASR para transcribir) ─
    fish_audio_api_key: str = ""
    fish_audio_base_url: str = "https://api.fish.audio/v1"
    fish_audio_reference_id: str = "a0d3c07d5bc34713b967b0019c893695"  # voz colombiana
    fish_audio_tts_model: str = "s2-pro"
    feature_transcripcion_voz: bool = True
    # Cuando un prospecto manda una nota de voz, responderle también con voz (TTS).
    feature_responder_voz: bool = True

    # Backend STT genérico alternativo (si no se usa Fish Audio ASR). Whisper-like.
    voz_api_url: str = ""
    voz_api_key: str = ""
    voz_idioma: str = "es"

    # ── Google Sheets (service account — finanzas DTGP) ──────────────────────
    # Path al JSON del service account (fuera del repo). ROTAR la llave que
    # estaba expuesta en el workspace anterior/google_sheets_config.json.
    google_sheets_credentials_path: str = ""

    # ── Shopify (LEGACY retail — se elimina en Fase 4 junto con tools/modelos) ─
    shopify_api_base_url: str = "https://innova.dtgrowthpartners.com/api"
    shopify_api_key: str = "a3f1b2c4-d5e6-7890-abcd-ef1234567890"
    catalogo_html_url: str = "https://innovacionfashion.co/pages/catalogo-de-whatsapp"
    catalogo_publico_url: str = "https://innovacionfashion.co/products.json"
    catalogo_html_sync_interval_horas: int = 12

    # ── Teléfonos / equipo DTGP ──────────────────────────────────────────────
    asistente_phone_produccion: str = "+573243798269"  # número whapi del asistente (set en .env)
    dairo_phone: str = "+573007189383"   # CEO DTGP — prioridad
    stiven_phone: str = "+573026444564"  # dueño técnico
    edgardo_phone: str = "+573116123189"
    jhonathan_phone: str = "+573005033093"
    # LEGACY retail (se reemplaza por el directorio de equipo DTGP en BD):
    fabio_phone: str = "+573019836645"
    dueno_phone_blocked: str = "+573206811130"

    # Grupo de WhatsApp del equipo DTGP — recibe notificaciones del bot
    # (citas agendadas, alertas, etc.). Formato: '<group_id>@g.us'.
    # Vacío = no notifica a grupo.
    equipo_dtgp_group_id: str = "120363420390814303@g.us"

    # ── Bot ─────────────────────────────────────────────────────────────────
    bot_host: str = "127.0.0.1"
    bot_port: int = 8003
    bot_log_level: str = "INFO"
    bot_log_file: str = "/home/asistente/logs/asistente.log"
    bot_env: str = "development"

    rate_limit_mensajes_por_minuto_cliente: int = 10
    rate_limit_outbound_por_hora: int = 15

    # ── Humanización (anti-detección WhatsApp spam) ────────────────────────
    # Política dueño 2026-05-16: Meta detecta bots por velocidad de respuesta.
    # Si llegan 200 conversaciones y el bot responde 40 de un golpe en <30s
    # → bloqueo de cuenta probable. Objetivo: 1-2 minutos por respuesta para
    # que parezca asesora humana escribiendo. Mensaje muy largo puede llegar a 3 min.
    humanization_delay_min_s: float = 60.0         # 1 min mínimo
    humanization_delay_max_s: float = 180.0        # 3 min máximo
    humanization_delay_por_caracter_s: float = 0.05  # +50ms por carácter
    humanization_typing_indicator: bool = True
    horario_inicio_hora: int = 8                   # 8AM Bogotá
    horario_fin_hora: int = 22                     # 10PM Bogotá
    feature_humanizacion: bool = True              # toggle global
    feature_responder_24_7: bool = True            # si True, ignora ventana horaria (responde a cualquier hora)

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
