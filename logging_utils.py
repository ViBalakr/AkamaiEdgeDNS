"""
Structured logging helpers for the collector.

SECURITY: never pass client_secret, access_token, full Authorization
headers, or full signatures to these helpers or to logging calls
anywhere in this project. See README.md "Security notes".
"""

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@dataclass
class ExecutionMetrics:
    events_received: int = 0
    events_sent: int = 0
    pages_processed: int = 0
    batches_sent: int = 0
    retry_count: int = 0
    failure_count: int = 0
    checkpoint_event_id: str = ""
    api_latency_seconds: float = 0.0
    ingestion_latency_seconds: float = 0.0
    execution_duration_seconds: float = 0.0

    def as_dict(self) -> Dict:
        return self.__dict__.copy()


@contextmanager
def timed_execution(log: logging.Logger, metrics: ExecutionMetrics):
    """
    Wraps one collector execution, logging a start/end marker and the
    final metrics dict. Logs "Execution completed" even on failure paths
    that re-raise, so operational metrics are never silently lost.
    """
    start = time.monotonic()
    log.info("Execution started")
    try:
        yield metrics
    finally:
        metrics.execution_duration_seconds = round(time.monotonic() - start, 3)
        # `extra=` promotes each field into Application Insights'
        # customDimensions (when running in Azure, via the Functions
        # host's default AppInsights logging pipeline), so production
        # dashboards/workbooks can query e.g. customDimensions.events_sent
        # directly instead of regex-parsing this message string. Local
        # console logging is unaffected — `extra` fields simply aren't
        # part of the default `%(message)s` format string used by
        # configure_logging() above. See docs/sentinel-ingestion.md
        # "Operational visibility" and deploy/workbook.json.
        log.info("Execution completed metrics=%s", metrics.as_dict(), extra=metrics.as_dict())
