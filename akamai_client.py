"""
Collector client for the Akamai Event Viewer / Audit Events API (mocked
locally by mock_akamai.py, or the real Akamai API in production — the
base URL is the only thing that changes).

Handles:
    - EdgeGrid request signing (via edgegrid_auth.sign_request)
    - pagination, following the afterEventId cursor until a short page
      is returned or max_pages_per_run is reached
    - retry with exponential backoff for transient failures (429, 5xx,
      network errors); 401/403 are never retried

This module raises AkamaiAPIError for non-retryable or retry-exhausted
conditions. It never touches checkpoint state — the caller (function_app.py)
is responsible for only advancing the checkpoint after successful Sentinel
ingestion.
"""

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

from edgegrid_auth import EdgeGridCredentials, sign_request

log = logging.getLogger("akamai_client")

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS_CODES = {401, 403}


class AkamaiAPIError(Exception):
    pass


@dataclass
class AkamaiClientConfig:
    base_url: str
    credentials: EdgeGridCredentials
    account_switch_key: Optional[str] = None
    request_timeout_seconds: float = 15.0
    max_retries: int = 4
    initial_backoff_seconds: float = 2.0
    max_pages_per_run: int = 20
    page_size: int = 50


class AkamaiEventViewerClient:
    def __init__(self, config: AkamaiClientConfig, session: Optional[requests.Session] = None):
        self._config = config
        self._session = session or requests.Session()

    def _request_once(self, params: dict) -> requests.Response:
        url = f"{self._config.base_url}/event-viewer-api/v1/events"
        prepared = requests.Request("GET", url, params=params).prepare()
        auth_header = sign_request("GET", prepared.url, self._config.credentials)
        headers = {"Authorization": auth_header, "Accept": "application/json"}
        return self._session.get(
            prepared.url, headers=headers, timeout=self._config.request_timeout_seconds
        )

    def _request_with_retry(self, params: dict) -> dict:
        attempt = 0
        backoff = self._config.initial_backoff_seconds
        last_error: Optional[str] = None

        while attempt <= self._config.max_retries:
            attempt += 1
            try:
                response = self._request_once(params)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"network error: {exc.__class__.__name__}"
                log.warning("Akamai request attempt %d failed (%s)", attempt, last_error)
                if attempt > self._config.max_retries:
                    break
                time.sleep(backoff)
                backoff *= 2
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    raise AkamaiAPIError("Akamai API returned malformed JSON. Checkpoint was not updated.")

            if response.status_code in NON_RETRYABLE_STATUS_CODES:
                raise AkamaiAPIError(
                    f"Akamai API returned HTTP {response.status_code}. Checkpoint was not updated."
                )

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = f"HTTP {response.status_code}"
                log.warning("Akamai request attempt %d failed (%s)", attempt, last_error)
                if attempt > self._config.max_retries:
                    break
                time.sleep(backoff)
                backoff *= 2
                continue

            raise AkamaiAPIError(
                f"Akamai API returned unexpected HTTP {response.status_code}. Checkpoint was not updated."
            )

        raise AkamaiAPIError(
            f"Akamai API request failed after {attempt} attempt(s) ({last_error}). "
            "Checkpoint was not updated."
        )

    def fetch_latest_event_id(self) -> Optional[str]:
        """
        Used only for bootstrap_mode == 'latest': walk forward to the
        final page of the currently-available dataset and return its
        last eventId, WITHOUT returning any event bodies to the caller.
        This lets the collector fast-forward its checkpoint past all
        existing history on first run, then only ingest events that
        arrive after that point - the opposite of bootstrap_mode ==
        'earliest', which ingests the full backlog.
        """
        cursor: Optional[str] = None
        latest: Optional[str] = None
        pages = 0

        while pages < self._config.max_pages_per_run:
            params = {"limit": self._config.page_size}
            if cursor:
                params["afterEventId"] = cursor
            if self._config.account_switch_key:
                params["accountSwitchKey"] = self._config.account_switch_key

            payload = self._request_with_retry(params)
            events = payload.get("events", [])
            pages += 1

            if not events:
                break

            latest = events[-1]["eventId"]
            cursor = latest

            if len(events) < self._config.page_size:
                break  # short page: this is genuinely the last page

        return latest

    def fetch_events_since(self, after_event_id: Optional[str]) -> List[dict]:
        """
        Retrieve all events after `after_event_id` (or from the beginning
        if None), following afterEventId pagination, up to
        config.max_pages_per_run pages. If the page limit is hit while
        more events remain, this returns what it has — the remainder is
        picked up on the next scheduled execution (never silently dropped).
        """
        all_events: List[dict] = []
        cursor = after_event_id
        pages = 0
        hit_page_limit = True

        while pages < self._config.max_pages_per_run:
            params = {"limit": self._config.page_size}
            if cursor:
                params["afterEventId"] = cursor
            if self._config.account_switch_key:
                params["accountSwitchKey"] = self._config.account_switch_key

            payload = self._request_with_retry(params)
            events = payload.get("events", [])
            pages += 1

            if not events:
                hit_page_limit = False
                break

            all_events.extend(events)
            cursor = events[-1]["eventId"]

            if len(events) < self._config.page_size:
                hit_page_limit = False
                break  # short page: no more data available right now

        if hit_page_limit:
            log.warning(
                "Reached max_pages_per_run=%d; more events may remain for the next execution",
                self._config.max_pages_per_run,
            )

        return all_events
