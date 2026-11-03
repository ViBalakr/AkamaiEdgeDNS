import json
from datetime import datetime, timezone

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def normalize_event(event: dict, akamai_host: str) -> dict:
    return {
        "TimeGenerated": _now_iso(),
        "RawData": json.dumps(event, separators=(",", ":"), ensure_ascii=False),
        "AkamaiHost": akamai_host,
    }

def normalize_events(events: list, akamai_host: str) -> list:
    return [normalize_event(e, akamai_host) for e in events]
