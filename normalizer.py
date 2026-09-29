"""
Normalizes Akamai Event Viewer JSON into the shape expected by the
existing Sentinel custom table AkamaiEdgeDNSEvents_CL.

Input:  one Akamai Event Viewer event (dict), as returned by the mock
        or real Event Viewer API.
Output: one dict ready to hand to the Logs Ingestion API (ingestion.py).

Field mapping:
    TimeGenerated      <- ingestion-time timestamp (datetime)
    EventId            <- eventId                                (string)
    EventTime          <- eventTime                               (datetime)
    EventTypeId        <- eventType.eventTypeId                   (string)
    EventTypeName      <- eventType.eventTypeName                 (string)
    EventDefinitionId  <- eventType.eventDefinition.eventDefinitionId (string)
    EventName          <- eventType.eventDefinition.eventName     (string)
    EventDescription   <- eventType.eventDefinition.eventDescription (string)
    Username           <- username                                (string)
    Impersonator       <- impersonator                            (bool)
    EventData          <- eventData (kept as an array — NOT flattened,
                                      since keys are not guaranteed unique)
    Source             <- constant "Akamai Event Viewer"
    AkamaiHost         <- the Akamai host the event was retrieved from
"""

from datetime import datetime, timezone
from typing import List


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def normalize_event(event: dict, akamai_host: str) -> dict:
    event_type = event.get("eventType", {})
    definition = event_type.get("eventDefinition", {})

    return {
        "TimeGenerated": _now_iso(),
        "EventId": event.get("eventId"),
        "EventTime": event.get("eventTime"),
        "EventTypeId": event_type.get("eventTypeId"),
        "EventTypeName": event_type.get("eventTypeName"),
        "EventDefinitionId": definition.get("eventDefinitionId"),
        "EventName": definition.get("eventName"),
        "EventDescription": definition.get("eventDescription"),
        "Username": event.get("username"),
        "Impersonator": bool(event.get("impersonator", False)),
        "EventData": event.get("eventData", []),  # array, intentionally not flattened
        "Source": "Akamai Event Viewer",
        "AkamaiHost": akamai_host,
    }


def normalize_events(events: List[dict], akamai_host: str) -> List[dict]:
    return [normalize_event(e, akamai_host) for e in events]
