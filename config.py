"""
Configuration loading and validation.

Azure Functions Core Tools exposes local.settings.json's "Values" object
as process environment variables for the local host, so this module reads
plain os.environ regardless of whether it's running locally or in Azure
(where the equivalent values are Function App application settings, or —
in production — resolved via Key Vault references; see docs/sentinel-
ingestion.md and README.md "Production migration").

Never hardcode secrets in this file.
"""

import os
from dataclasses import dataclass
from typing import Optional


class ConfigurationError(Exception):
    pass


@dataclass
class Settings:
    akamai_base_url: str
    akamai_host: str
    akamai_client_token: str
    akamai_access_token: str
    akamai_client_secret: str
    akamai_account_switch_key: Optional[str]

    azure_storage_account_url: str
    checkpoint_container: str
    checkpoint_blob: str

    dce_endpoint: str
    dcr_immutable_id: str
    dcr_stream_name: str

    max_pages_per_run: int
    batch_size: int
    bootstrap_mode: str


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigurationError(f"Missing required configuration value: {name}")
    return value


def load_settings() -> Settings:
    bootstrap_mode = os.environ.get("BOOTSTRAP_MODE", "earliest")
    if bootstrap_mode not in ("earliest", "latest"):
        raise ConfigurationError(
            f"BOOTSTRAP_MODE must be 'earliest' or 'latest', got: {bootstrap_mode!r}"
        )

    return Settings(
        akamai_base_url=_require("AKAMAI_BASE_URL"),
        akamai_host=_require("AKAMAI_HOST"),
        akamai_client_token=_require("AKAMAI_CLIENT_TOKEN"),
        akamai_access_token=_require("AKAMAI_ACCESS_TOKEN"),
        akamai_client_secret=_require("AKAMAI_CLIENT_SECRET"),
        akamai_account_switch_key=os.environ.get("AKAMAI_ACCOUNT_SWITCH_KEY") or None,
        azure_storage_account_url=_require("AZURE_STORAGE_ACCOUNT_URL"),
        checkpoint_container=os.environ.get("CHECKPOINT_CONTAINER", "akamai-checkpoint"),
        checkpoint_blob=os.environ.get("CHECKPOINT_BLOB", "checkpoint.json"),
        dce_endpoint=_require("DCE_ENDPOINT"),
        dcr_immutable_id=_require("DCR_IMMUTABLE_ID"),
        dcr_stream_name=os.environ.get("DCR_STREAM_NAME", "Custom-AkamaiEdgeDNSEvents_CL"),
        max_pages_per_run=int(os.environ.get("MAX_PAGES_PER_RUN", "20")),
        batch_size=int(os.environ.get("BATCH_SIZE", "500")),
        bootstrap_mode=bootstrap_mode,
    )
