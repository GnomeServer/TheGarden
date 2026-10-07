from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", hide_input_in_errors=True)

    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    database_host: str = Field(default="postgres", validation_alias="DB_HOST")
    database_port: int = Field(default=5432, validation_alias="DB_PORT")
    database_name: str = Field(default="agent_manager", validation_alias="DB_NAME")
    database_user: str = Field(default="agent_manager", validation_alias="DB_USER")
    database_password: str = Field(default="agent_manager", validation_alias="DB_PASSWORD")
    nats_url: str = Field(default="nats://nats:4222", validation_alias="NATS_URL")
    api_token: str = Field(validation_alias="AGENT_MANAGER_API_TOKEN")
    openwebui_token: str = Field(default="", validation_alias="AGENT_MANAGER_OPENWEBUI_TOKEN")
    session_secret: str = Field(default="", validation_alias="DASHBOARD_SESSION_SECRET")
    session_secure: bool = Field(default=True, validation_alias="DASHBOARD_SESSION_SECURE")
    public_url: str = Field(
        default="https://infra-lab-services.tail494f6d.ts.net",
        validation_alias="DASHBOARD_PUBLIC_URL",
    )
    forgejo_api_url: str = Field(
        default="http://forgejo:3000/api/v1", validation_alias="FORGEJO_API_URL"
    )
    forgejo_public_url: str = Field(
        default="https://infra-lab-services.tail494f6d.ts.net/forgejo",
        validation_alias="FORGEJO_PUBLIC_URL",
    )
    forgejo_token: str = Field(default="", validation_alias="FORGEJO_TOKEN")
    forgejo_webhook_secret: str = Field(
        default="", validation_alias="FORGEJO_WEBHOOK_SECRET"
    )
    forgejo_oauth_client_id: str = Field(
        default="", validation_alias="FORGEJO_OAUTH_CLIENT_ID"
    )
    forgejo_oauth_client_secret: str = Field(
        default="", validation_alias="FORGEJO_OAUTH_CLIENT_SECRET"
    )
    bootstrap_admins: str = Field(default="", validation_alias="DASHBOARD_ADMINS")
    worker_offline_after_seconds: int = Field(
        default=45, ge=15, le=3600, validation_alias="WORKER_OFFLINE_AFTER_SECONDS"
    )
    prometheus_url: str = Field(
        default="http://prometheus:9090", validation_alias="PROMETHEUS_URL"
    )
    prometheus_timeout: float = Field(
        default=5.0, ge=1.0, le=30.0, validation_alias="PROMETHEUS_TIMEOUT"
    )
    grafana_public_url: str = Field(
        default="https://infra-lab-services.tail494f6d.ts.net/grafana",
        validation_alias="GRAFANA_PUBLIC_URL",
    )
    grafana_node_dashboard_uid: str = Field(
        default="server-overview",
        validation_alias="GRAFANA_NODE_DASHBOARD_UID",
    )
    notification_webhook_url: str = Field(
        default="", validation_alias="NOTIFICATION_WEBHOOK_URL"
    )
    notification_webhook_token: str = Field(
        default="", validation_alias="NOTIFICATION_WEBHOOK_TOKEN"
    )
    notification_scan_seconds: int = Field(
        default=60, ge=15, le=3600, validation_alias="NOTIFICATION_SCAN_SECONDS"
    )
    model_base_url: str = Field(
        default="http://litellm:4000/v1", validation_alias="MODEL_BASE_URL"
    )
    model_api_key: str = Field(default="", validation_alias="MODEL_API_KEY")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @model_validator(mode="after")
    def separate_service_credentials(self) -> "Settings":
        if self.openwebui_token and self.openwebui_token == self.api_token:
            raise ValueError("AGENT_MANAGER_OPENWEBUI_TOKEN must differ from the administrator token")
        return self

    @property
    def sqlalchemy_url(self) -> str | URL:
        return self.database_url or URL.create(
            drivername="postgresql+asyncpg",
            username=self.database_user,
            password=self.database_password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )

    @property
    def signing_key(self) -> bytes:
        return (self.session_secret or self.api_token).encode("utf-8")

    @property
    def admin_logins(self) -> set[str]:
        return {item.strip().lower() for item in self.bootstrap_admins.split(",") if item.strip()}

    @property
    def oauth_configured(self) -> bool:
        return bool(self.forgejo_oauth_client_id and self.forgejo_oauth_client_secret)


settings = Settings()
