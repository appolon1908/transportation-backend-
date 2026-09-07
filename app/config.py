from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.release import CANONICAL_MIGRATION_HEAD


class Settings(BaseSettings):
    """Runtime configuration with production-safe defaults.

    Secrets are supplied through the environment or a secrets manager at deploy
    time. No provider credential belongs in source control or database JSON.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = "development"
    app_name: str = "Freight Platform API"
    app_version: str = "0.6.0"
    migration_head: str = CANONICAL_MIGRATION_HEAD
    database_url: str = "postgresql+asyncpg://freight:freight@localhost:5432/freight"
    ingress_database_url: str = ""
    worker_database_url: str = ""

    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    cors_origins: str = "http://localhost:5173"
    enable_api_docs: bool = True
    max_request_body_bytes: int = Field(default=10_485_760, ge=1_024, le=104_857_600)

    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_algorithms: str = "RS256"
    oidc_jwks_url: str = ""
    oidc_allowed_azp: str = ""
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30, le=86_400)
    oidc_jwks_stale_seconds: int = Field(default=900, ge=0, le=86_400)
    oidc_http_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    allow_development_identity_headers: bool = True

    tracking_webhook_secret: str = ""

    capability_live_tender_send: bool = False
    capability_live_dispatch_notification: bool = False
    capability_email_live_send: bool = False
    capability_sms_live_send: bool = False
    capability_accounting_live_export: bool = False
    capability_customer_portal_external_access: bool = False
    capability_carrier_portal_external_access: bool = False
    external_effects_approved: bool = False
    production_change_id: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def resolved_ingress_database_url(self) -> str:
        return self.ingress_database_url or self.database_url

    @property
    def resolved_worker_database_url(self) -> str:
        return self.worker_database_url or self.database_url

    @property
    def allowed_host_list(self) -> list[str]:
        return [value.strip() for value in self.allowed_hosts.split(",") if value.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def oidc_algorithm_list(self) -> list[str]:
        return [value.strip() for value in self.oidc_algorithms.split(",") if value.strip()]

    @property
    def oidc_allowed_azp_list(self) -> list[str]:
        return [value.strip() for value in self.oidc_allowed_azp.split(",") if value.strip()]

    @property
    def resolved_oidc_jwks_url(self) -> str:
        if self.oidc_jwks_url:
            return self.oidc_jwks_url
        return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"

    @staticmethod
    def _database_username(value: str) -> str:
        return urlparse(value).username or ""

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> "Settings":
        allowed_algorithms = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        configured_algorithms = set(self.oidc_algorithm_list)
        if configured_algorithms and not configured_algorithms <= allowed_algorithms:
            unsupported = sorted(configured_algorithms - allowed_algorithms)
            raise ValueError(f"Unsupported OIDC algorithms: {', '.join(unsupported)}")
        if self.migration_head != CANONICAL_MIGRATION_HEAD:
            raise ValueError(
                "MIGRATION_HEAD must match the canonical application schema head "
                f"{CANONICAL_MIGRATION_HEAD}"
            )

        if not self.is_production:
            return self

        missing = [
            name
            for name, value in {
                "OIDC_ISSUER": self.oidc_issuer,
                "OIDC_AUDIENCE": self.oidc_audience,
                "ALLOWED_HOSTS": self.allowed_hosts,
                "DATABASE_URL": self.database_url,
                "INGRESS_DATABASE_URL": self.ingress_database_url,
                "WORKER_DATABASE_URL": self.worker_database_url,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required production settings: {', '.join(missing)}")
        if self.allow_development_identity_headers:
            raise ValueError("ALLOW_DEVELOPMENT_IDENTITY_HEADERS must be false in production")
        if "*" in self.allowed_host_list:
            raise ValueError("Wildcard ALLOWED_HOSTS is forbidden in production")
        if "*" in self.cors_origin_list:
            raise ValueError("Wildcard CORS_ORIGINS is forbidden in production")
        if not self.oidc_algorithm_list:
            raise ValueError("At least one OIDC algorithm is required")
        if urlparse(self.oidc_issuer).scheme != "https":
            raise ValueError("OIDC_ISSUER must use HTTPS in production")

        database_urls = {
            "api": self.database_url,
            "ingress": self.ingress_database_url,
            "worker": self.worker_database_url,
        }
        usernames = {
            purpose: self._database_username(value)
            for purpose, value in database_urls.items()
        }
        if any(not username for username in usernames.values()):
            raise ValueError("Production database URLs must include explicit usernames")
        if len(set(usernames.values())) != len(usernames):
            raise ValueError(
                "DATABASE_URL, INGRESS_DATABASE_URL and WORKER_DATABASE_URL must use distinct database users"
            )

        live_effects = {
            "carrier.live_tender_send": self.capability_live_tender_send,
            "carrier.live_dispatch_notification": self.capability_live_dispatch_notification,
            "email.live_send": self.capability_email_live_send,
            "sms.live_send": self.capability_sms_live_send,
            "accounting.live_export": self.capability_accounting_live_export,
            "customer_portal.external_access": self.capability_customer_portal_external_access,
            "carrier_portal.external_access": self.capability_carrier_portal_external_access,
        }
        enabled = sorted(code for code, active in live_effects.items() if active)
        if enabled and (not self.external_effects_approved or not self.production_change_id):
            raise ValueError(
                "Live external effects require EXTERNAL_EFFECTS_APPROVED=true and PRODUCTION_CHANGE_ID; "
                f"enabled: {', '.join(enabled)}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
