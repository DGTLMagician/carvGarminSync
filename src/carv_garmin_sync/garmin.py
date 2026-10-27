"""Garmin Connect upload, via the garminconnect library."""

import datetime as dt
import getpass
from pathlib import Path

from garminconnect import Garmin, GarminConnectConnectionError

from .carv import STATE_DIR

TOKENSTORE = STATE_DIR / "garmin"


class Duplicate(Exception):
    pass


def login_interactive(email: str | None = None) -> None:
    email = email or input("Garmin e-mail: ")
    password = getpass.getpass("Garmin wachtwoord: ")
    TOKENSTORE.mkdir(parents=True, exist_ok=True, mode=0o700)
    g = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA-code: "))
    g.login(str(TOKENSTORE))
    g.client.dump(str(TOKENSTORE))


def client() -> Garmin:
    if not TOKENSTORE.exists():
        raise RuntimeError("niet ingelogd bij Garmin; draai `carv-garmin-sync setup`")
    g = Garmin()
    g.login(str(TOKENSTORE))
    return g


def overlapping(g: Garmin, start: float, end: float) -> list[dict]:
    """Garmin activities whose time range overlaps [start, end] (unix seconds)."""
    day0 = dt.datetime.fromtimestamp(start - 86400).date().isoformat()
    day1 = dt.datetime.fromtimestamp(end + 86400).date().isoformat()
    out = []
    for a in g.get_activities_by_date(day0, day1):
        gmt = a.get("startTimeGMT")
        if not gmt:
            continue
        a_start = dt.datetime.fromisoformat(gmt).replace(tzinfo=dt.timezone.utc).timestamp()
        a_end = a_start + (a.get("elapsedDuration") or a.get("duration") or 0)
        if a_start < end and a_end > start:
            out.append(a)
    return out


def upload(g: Garmin, path: Path) -> int | None:
    """Import a FIT file; returns the Garmin activity id when reported.

    Uses the import endpoint, so Garmin does not re-export it to Strava
    (Carv already syncs there itself)."""
    try:
        res = g.import_activity(str(path))
    except GarminConnectConnectionError as e:
        if "409" in str(e) or "uplicate" in str(e):
            raise Duplicate(str(e)) from e
        raise
    result = res.get("detailedImportResult", res) if isinstance(res, dict) else {}
    failures = result.get("failures") or []
    if failures:
        msgs = " ".join(m.get("content", "") for f in failures for m in f.get("messages", []))
        if "uplicate" in msgs:
            raise Duplicate(msgs)
        raise RuntimeError(f"Garmin weigerde de upload: {msgs or failures}")
    for s in result.get("successes") or []:
        if s.get("internalId"):
            return int(s["internalId"])
    return None
