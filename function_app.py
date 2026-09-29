"""
Azure Function (Python v2 programming model) — timer-triggered Akamai
Event Viewer collector.

Schedule: every 5 minutes -> "0 */5 * * * *"

Azure Functions NCRONTAB field order (6 fields):
    {second} {minute} {hour} {day} {month} {day-of-week}

    "0 */5 * * * *"
     |   |  |  |  |  |
     |   |  |  |  |  +-- day-of-week: every day of the week
     |   |  |  |  +----- month: every month
     |   |  |  +-------- day: every day of the month
     |   |  +----------- hour: every hour
     |   +-------------- minute: every 5th minute (0,5,10,...,55)
     +------------------ second: at second 0

    Result: fires at :00, :05, :10, :15 ... of every hour, every day.

WARNING: "0 5 * * * *" means "at minute 5 of every hour" — i.e. ONCE per
hour, at HH:05:00. That is NOT the same as every 5 minutes and is
intentionally NOT used here. This is a common off-by-one-field mistake.

Production configuration (as used below):
    run_on_startup=False
    use_monitor=True

For LOCAL LAB convenience only, SOP.md STEP 23 describes temporarily
setting run_on_startup=True so you don't have to wait for the next
5-minute wall-clock boundary while developing. Do not ship that change
to production — this file's decorator is the production configuration.

Failure semantics (see docs/checkpoint-design.md for full rationale):
    Akamai API failure        -> checkpoint NOT updated, exception re-raised
    Normalization failure     -> checkpoint NOT updated, exception re-raised
    Sentinel ingestion failure-> checkpoint NOT updated, exception re-raised
    Checkpoint write failure
      AFTER successful ingest -> logged; next run may resend records
                                  (at-least-once delivery, never data loss)
"""

import logging
import os

import azure.functions as func
from azure.identity import DefaultAzureCredential

from akamai_client import AkamaiAPIError, AkamaiClientConfig, AkamaiEventViewerClient
from checkpoint import Checkpoint, CheckpointError, CheckpointStore
from config import ConfigurationError, load_settings
from edgegrid_auth import EdgeGridCredentials
from ingestion import IngestionConfig, SentinelIngestionClient, SentinelIngestionError
from logging_utils import ExecutionMetrics, configure_logging, timed_execution
from normalizer import normalize_events

configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("function_app")

app = func.FunctionApp()


@app.function_name(name="AkamaiEventViewerCollector")
@app.timer_trigger(
    schedule="0 */5 * * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def akamai_event_viewer_collector(timer: func.TimerRequest) -> None:
    metrics = ExecutionMetrics()

    try:
        settings = load_settings()
    except ConfigurationError as exc:
        log.error("Configuration error: %s", exc)
        raise

    # LOCAL LAB: DefaultAzureCredential picks up the `az login` session
    # (Azure CLI credential) automatically. PRODUCTION: the Function App's
    # System Assigned Managed Identity is used instead — no code change is
    # required, DefaultAzureCredential selects the right source at runtime.
    # See README.md "Azure authentication: local vs production".
    credential = DefaultAzureCredential()

    checkpoint_store = CheckpointStore(
        account_url=settings.azure_storage_account_url,
        container_name=settings.checkpoint_container,
        blob_name=settings.checkpoint_blob,
        credential=credential,
    )

    akamai_client = AkamaiEventViewerClient(AkamaiClientConfig(
        base_url=settings.akamai_base_url,
        credentials=EdgeGridCredentials(
            client_token=settings.akamai_client_token,
            access_token=settings.akamai_access_token,
            client_secret=settings.akamai_client_secret,
        ),
        account_switch_key=settings.akamai_account_switch_key,
        max_pages_per_run=settings.max_pages_per_run,
    ))

    ingestion_client = SentinelIngestionClient(
        IngestionConfig(
            dce_endpoint=settings.dce_endpoint,
            dcr_immutable_id=settings.dcr_immutable_id,
            dcr_stream_name=settings.dcr_stream_name,
            batch_size=settings.batch_size,
        ),
        credential=credential,
    )

    with timed_execution(log, metrics):
        try:
            with checkpoint_store.locked() as lock:
                checkpoint = lock.read()
                log.info("Checkpoint loaded last_event_id=%s", checkpoint.last_event_id)

                after_event_id = checkpoint.last_event_id
                if after_event_id is None and settings.bootstrap_mode == "latest":
                    # No checkpoint exists and the operator has chosen NOT to
                    # ingest historical backlog: fast-forward the cursor to
                    # the current newest event without sending any event
                    # bodies to Sentinel. See docs/checkpoint-design.md
                    # "Bootstrap modes".
                    log.info("bootstrap_mode=latest: fast-forwarding checkpoint, no backlog will be ingested")
                    latest_id = akamai_client.fetch_latest_event_id()
                    if latest_id is not None:
                        lock.write(Checkpoint(last_event_id=latest_id, last_event_time=None))
                        metrics.checkpoint_event_id = latest_id
                        log.info("Checkpoint fast-forwarded to last_event_id=%s; execution complete", latest_id)
                    else:
                        log.info("No events currently exist upstream; checkpoint left empty")
                    return

                log.info("Akamai request started")
                events = akamai_client.fetch_events_since(after_event_id)
                log.info("Akamai response received: %d events", len(events))
                metrics.events_received = len(events)

                if not events:
                    log.info("No new events; checkpoint unchanged")
                    return

                normalized = normalize_events(events, settings.akamai_host)
                log.info("Events normalized: %d", len(normalized))

                sent = ingestion_client.upload(normalized)
                metrics.events_sent = sent
                metrics.batches_sent = (sent + settings.batch_size - 1) // settings.batch_size
                log.info("Sentinel batch upload complete: %d records", sent)

                new_checkpoint = Checkpoint(
                    last_event_id=events[-1]["eventId"],
                    last_event_time=events[-1]["eventTime"],
                )
                lock.write(new_checkpoint)
                metrics.checkpoint_event_id = new_checkpoint.last_event_id
                log.info("Checkpoint updated")

        except AkamaiAPIError as exc:
            metrics.failure_count += 1
            log.error(str(exc))
            raise
        except SentinelIngestionError as exc:
            metrics.failure_count += 1
            log.error(str(exc))
            raise
        except CheckpointError as exc:
            metrics.failure_count += 1
            log.error("Checkpoint error: %s", exc)
            raise
