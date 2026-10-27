"""Build a Garmin FIT activity (resort skiing) from a parsed Carv session."""

import math

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity,
    Event,
    EventType,
    FileType,
    Intensity,
    LapTrigger,
    Manufacturer,
    SessionTrigger,
    Sport,
    SubSport,
)

from .session import Point, Segment, SkiSession

PRODUCT_NAME = "Carv"


def _ms(t: float) -> int:
    return round(t * 1000)


def _dist(a: Point, b: Point) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _stats(seg: Segment) -> dict:
    pts = seg.points
    dist = sum(_dist(a, b) for a, b in zip(pts, pts[1:]))
    alts = [p.alt for p in pts if p.alt is not None]
    ascent = descent = 0.0
    for a, b in zip(alts, alts[1:]):
        if b > a:
            ascent += b - a
        else:
            descent += a - b
    speeds = [p.speed for p in pts if p.speed is not None]
    dur = max(seg.end - seg.start, 0.001)
    return {
        "distance": dist,
        "ascent": ascent,
        "descent": descent,
        "max_speed": max(speeds) if speeds else dist / dur,
        "avg_speed": dist / dur,
        "max_alt": max(alts) if alts else None,
        "min_alt": min(alts) if alts else None,
        "duration": dur,
    }


def build(session: SkiSession) -> bytes:
    segs = session.segments
    if not segs:
        raise ValueError(f"session {session.id} has no GPS segments")
    start, end = segs[0].start, max(s.end for s in segs)

    b = FitFileBuilder(auto_define=True, min_string_size=50)

    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.time_created = _ms(start)
    # Stable serial per Carv session, so a re-upload is recognised as duplicate.
    fid.serial_number = int(session.id.replace("-", "")[:8], 16) or 1
    b.add(fid)

    dev = DeviceInfoMessage()
    dev.timestamp = _ms(start)
    dev.manufacturer = Manufacturer.DEVELOPMENT.value
    dev.product = 0
    dev.product_name = PRODUCT_NAME
    b.add(dev)

    ev = EventMessage()
    ev.timestamp = _ms(start)
    ev.event = Event.TIMER
    ev.event_type = EventType.START
    b.add(ev)

    total_dist = 0.0
    laps = []
    for seg in segs:
        st = _stats(seg)
        prev = None
        for p in seg.points:
            if prev is not None:
                total_dist += _dist(prev, p)
            prev = p
            r = RecordMessage()
            r.timestamp = _ms(p.t)
            r.position_lat = p.lat
            r.position_long = p.lon
            if p.alt is not None:
                r.altitude = p.alt
            if p.speed is not None:
                r.speed = p.speed
            r.distance = total_dist
            b.add(r)

        lap = LapMessage()
        lap.timestamp = _ms(seg.end)
        lap.start_time = _ms(seg.start)
        lap.start_position_lat = seg.points[0].lat
        lap.start_position_long = seg.points[0].lon
        lap.end_position_lat = seg.points[-1].lat
        lap.end_position_long = seg.points[-1].lon
        lap.total_elapsed_time = st["duration"]
        lap.total_timer_time = st["duration"]
        lap.total_distance = st["distance"]
        lap.avg_speed = st["avg_speed"]
        lap.max_speed = st["max_speed"]
        lap.total_ascent = round(st["ascent"])
        lap.total_descent = round(st["descent"])
        if st["max_alt"] is not None:
            lap.max_altitude = st["max_alt"]
            lap.min_altitude = st["min_alt"]
        lap.sport = Sport.ALPINE_SKIING
        lap.sub_sport = SubSport.RESORT
        lap.lap_trigger = LapTrigger.MANUAL
        lap.intensity = Intensity.ACTIVE if seg.kind == "run" else Intensity.REST
        b.add(lap)
        laps.append((seg, st))

    ev = EventMessage()
    ev.timestamp = _ms(end)
    ev.event = Event.TIMER
    ev.event_type = EventType.STOP_ALL
    b.add(ev)

    runs = [st for seg, st in laps if seg.kind == "run"]
    alts = [st["max_alt"] for _, st in laps if st["max_alt"] is not None]
    timer = sum(st["duration"] for _, st in laps)

    ses = SessionMessage()
    ses.timestamp = _ms(end)
    ses.start_time = _ms(start)
    ses.start_position_lat = segs[0].points[0].lat
    ses.start_position_long = segs[0].points[0].lon
    ses.total_elapsed_time = end - start
    ses.total_timer_time = timer
    ses.total_distance = total_dist
    ses.total_ascent = round(sum(st["ascent"] for _, st in laps))
    ses.total_descent = round(sum(st["descent"] for st in runs))
    ses.max_speed = max(st["max_speed"] for st in runs) if runs else None
    ses.avg_speed = total_dist / timer if timer else None
    if alts:
        ses.max_altitude = max(alts)
        ses.min_altitude = min(st["min_alt"] for _, st in laps if st["min_alt"] is not None)
    ses.sport = Sport.ALPINE_SKIING
    ses.sub_sport = SubSport.RESORT
    ses.first_lap_index = 0
    ses.num_laps = len(laps)
    ses.trigger = SessionTrigger.ACTIVITY_END
    ses.event = Event.SESSION
    ses.event_type = EventType.STOP
    b.add(ses)

    act = ActivityMessage()
    act.timestamp = _ms(end)
    act.total_timer_time = timer
    act.num_sessions = 1
    act.type = Activity.MANUAL
    act.event = Event.ACTIVITY
    act.event_type = EventType.STOP
    b.add(act)

    return b.build().to_bytes()
