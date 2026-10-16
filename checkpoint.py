"""
Checkpoint state management using Azure Blob Storage.

The checkpoint blob (akamai-checkpoint/checkpoint.json by default) stores
ONLY:
    last_event_id, last_event_time, updated_at, collector_version

It never stores raw event data (see docs/checkpoint-design.md for why).

A blob lease provides mutual exclusion between concurrent executions.
Azure Blob leases have a maximum FIXED duration of 60 seconds; this
module does NOT assume an execution will finish within that window —
it renews the lease periodically (see LEASE_RENEW_INTERVAL_SECONDS)
for the duration of the `with store.locked() as lock:` block, so long
API collections, large backlogs, or Sentinel throttling cannot cause
the lease to silently expire mid-execution.

Checkpoint-advancement rule (enforced by the caller, function_app.py):
never call lock.write() unless Sentinel ingestion has already succeeded.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobLeaseClient, BlobServiceClient

log = logging.getLogger("checkpoint")

COLLECTOR_VERSION = "1.0.0"
LEASE_DURATION_SECONDS = 60           # Azure Blob's max fixed lease duration
LEASE_RENEW_INTERVAL_SECONDS = 40     # renew well before the 60s expiry


class CheckpointError(Exception):
    pass


@dataclass
class Checkpoint:
    last_event_id: Optional[str] = None
    last_event_time: Optional[str] = None
    updated_at: Optional[str] = None
    collector_version: str = COLLECTOR_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> "Checkpoint":
        obj = json.loads(data)
        return cls(
            last_event_id=obj.get("last_event_id"),
            last_event_time=obj.get("last_event_time"),
            updated_at=obj.get("updated_at"),
            collector_version=obj.get("collector_version", COLLECTOR_VERSION),
        )


class CheckpointStore:
    """
    Wraps a single checkpoint blob with lease-based mutual exclusion.

    Usage:
        store = CheckpointStore(account_url, container, blob, credential)
        with store.locked() as lock:
            checkpoint = lock.read()
            ... collect + normalize + ingest into Sentinel ...
            lock.write(new_checkpoint)   # ONLY after ingestion succeeded
    """

    def __init__(self, account_url: str, container_name: str, blob_name: str, credential):
        self._service = BlobServiceClient(account_url=account_url, credential=credential)
        self._container_name = container_name
        self._blob_name = blob_name
        self._ensure_container()

    def _ensure_container(self) -> None:
        container_client = self._service.get_container_client(self._container_name)
        try:
            container_client.create_container()
        except ResourceExistsError:
            pass

    def _blob_client(self):
        return self._service.get_blob_client(self._container_name, self._blob_name)

    def locked(self) -> "_CheckpointLock":
        return _CheckpointLock(self)


class _CheckpointLock:
    def __init__(self, store: CheckpointStore):
        self._store = store
        self._blob_client = store._blob_client()
        self._lease: Optional[BlobLeaseClient] = None
        self._last_renew = 0.0

    def __enter__(self) -> "_CheckpointLock":
        if not self._blob_client.exists():
            # Lease acquisition requires the blob to exist first.
            try:
                self._blob_client.upload_blob(Checkpoint().to_json(), overwrite=False)
            except ResourceExistsError:
                pass  # created concurrently by another execution; fine

        try:
            self._lease = self._blob_client.acquire_lease(lease_duration=LEASE_DURATION_SECONDS)
        except ResourceExistsError as exc:
            raise CheckpointError(
                "Could not acquire checkpoint lease; another execution may be in progress"
            ) from exc

        self._last_renew = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._lease is not None:
            try:
                self._lease.release()
            except Exception:  # noqa: BLE001 - best-effort release on exit
                log.warning("Failed to release checkpoint lease cleanly")

    def _maybe_renew(self) -> None:
        now = time.monotonic()
        if now - self._last_renew >= LEASE_RENEW_INTERVAL_SECONDS:
            self._lease.renew()
            self._last_renew = now
            log.info("Checkpoint lease renewed")

    def read(self) -> Checkpoint:
        self._maybe_renew()
        try:
            data = self._blob_client.download_blob(lease=self._lease).readall()
        except ResourceNotFoundError:
            return Checkpoint()
        return Checkpoint.from_json(data.decode("utf-8"))

    def write(self, checkpoint: Checkpoint) -> None:
        self._maybe_renew()
        checkpoint.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            self._blob_client.upload_blob(checkpoint.to_json(), overwrite=True, lease=self._lease)
        except Exception as exc:  # noqa: BLE001
            # Per docs/checkpoint-design.md: if this raises AFTER Sentinel
            # ingestion already succeeded, the caller logs it and accepts
            # that the next run may resend the same records (at-least-once
            # delivery), rather than losing events.
            raise CheckpointError(f"Failed to write checkpoint: {exc}") from exc
        log.info("Checkpoint updated to last_event_id=%s", checkpoint.last_event_id)
