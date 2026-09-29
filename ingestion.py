"""
Sends normalized events to Microsoft Sentinel via the Azure Monitor Logs
Ingestion API (real DCE + real DCR — these already exist and are NOT
created by this project), in configurable batches, with retry handling.

Callers MUST treat any SentinelIngestionError as "checkpoint must not
advance" (see checkpoint.py / function_app.py).
"""

import logging
import time
from dataclasses import dataclass
from typing import List

from azure.core.exceptions import HttpResponseError
from azure.monitor.ingestion import LogsIngestionClient

log = logging.getLogger("ingestion")

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class SentinelIngestionError(Exception):
    pass


@dataclass
class IngestionConfig:
    dce_endpoint: str
    dcr_immutable_id: str
    dcr_stream_name: str
    batch_size: int = 500
    max_retries: int = 4
    initial_backoff_seconds: float = 2.0


class SentinelIngestionClient:
    def __init__(self, config: IngestionConfig, credential):
        self._config = config
        self._client = LogsIngestionClient(endpoint=config.dce_endpoint, credential=credential)

    def upload(self, records: List[dict]) -> int:
        """
        Uploads `records` in batches of config.batch_size. Raises
        SentinelIngestionError on failure — callers MUST NOT advance the
        checkpoint if this raises. Returns the number of records
        successfully uploaded (only reached if ALL batches succeeded).
        """
        sent = 0
        for start in range(0, len(records), self._config.batch_size):
            batch = records[start:start + self._config.batch_size]
            self._upload_batch_with_retry(batch)
            sent += len(batch)
        return sent

    def _upload_batch_with_retry(self, batch: List[dict]) -> None:
        attempt = 0
        backoff = self._config.initial_backoff_seconds

        while True:
            attempt += 1
            try:
                self._client.upload(
                    rule_id=self._config.dcr_immutable_id,
                    stream_name=self._config.dcr_stream_name,
                    logs=batch,
                )
                log.info("Uploaded batch of %d records to Sentinel", len(batch))
                return
            except HttpResponseError as exc:
                status = getattr(exc, "status_code", None)
                if status in RETRYABLE_STATUS_CODES and attempt <= self._config.max_retries:
                    log.warning("Sentinel ingestion attempt %d failed (HTTP %s); retrying", attempt, status)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise SentinelIngestionError(
                    f"Sentinel ingestion failed (HTTP {status}) after {attempt} attempt(s). "
                    "Checkpoint was not updated."
                ) from exc
            except Exception as exc:  # noqa: BLE001 - network errors etc.
                if attempt <= self._config.max_retries:
                    log.warning("Sentinel ingestion attempt %d failed (%s); retrying", attempt, exc)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise SentinelIngestionError(
                    f"Sentinel ingestion failed after {attempt} attempt(s): {exc}. "
                    "Checkpoint was not updated."
                ) from exc
