"""Parse a Carv session file (proto::data::Session, data version 14.0.1).

Field numbers were reconstructed from real session files; only what is needed
for a GPS activity is decoded:

Session
  1  data version (string)
  3  header: 1 start (unix s, double), 2 end (unix s, double)
  7  repeated segment, a oneof wrapper:
       1 run, 2 lift, 7 sub-segment of a run (ignored)
  35 repeated turn

Segment (run or lift)
  1  header: 1 start, 2 end (unix s, double)
  2  lift track: repeated {1 lat, 2 lon} (doubles, no timestamps)
  5  lift info: 2 name
  17 run track: repeated {1 lat, 2 lon (double), 3 speed m/s, 4 accuracy m,
                          5 altitude m, 6 seconds since segment start (floats)}
"""

from dataclasses import dataclass, field

from . import pb


@dataclass
class Point:
    t: float  # unix seconds
    lat: float
    lon: float
    alt: float | None = None
    speed: float | None = None


@dataclass
class Segment:
    kind: str  # "run" or "lift"
    start: float
    end: float
    name: str | None = None
    points: list[Point] = field(default_factory=list)


@dataclass
class SkiSession:
    id: str
    data_version: str
    start: float
    end: float
    segments: list[Segment]
    turns: int

    @property
    def runs(self) -> list[Segment]:
        return [s for s in self.segments if s.kind == "run"]


def parse(session_id: str, raw: bytes) -> SkiSession:
    m = pb.decode(raw)
    hdr = m.msg(3)
    segments = []
    for wrapper in m.msgs(7):
        if 1 in wrapper.fields:
            segments.append(_run(wrapper.msg(1)))
        elif 2 in wrapper.fields:
            segments.append(_lift(wrapper.msg(2)))
    segments.sort(key=lambda s: s.start)
    _drop_overlap(segments)
    segments = [s for s in segments if s.points]
    _fill_lift_altitude(segments)
    return SkiSession(
        id=session_id,
        data_version=m.str(1, ""),
        start=hdr.double(1),
        end=hdr.double(2),
        segments=segments,
        turns=len(m.all(35)),
    )


def _run(s: pb.Msg) -> Segment:
    h = s.msg(1)
    start, end = h.double(1), h.double(2)
    pts = []
    for p in s.msgs(17):
        lat, lon = p.double(1), p.double(2)
        if lat is None or lon is None:
            continue
        pts.append(Point(t=start + (p.float(6) or 0.0), lat=lat, lon=lon, alt=p.float(5), speed=p.float(3)))
    pts.sort(key=lambda p: p.t)
    return Segment("run", start, end, None, pts)


def _lift(s: pb.Msg) -> Segment:
    h = s.msg(1)
    start, end = h.double(1), h.double(2)
    coords = [(p.double(1), p.double(2)) for p in s.msgs(2)]
    coords = [c for c in coords if None not in c]
    info = s.msg(5)
    # Lift points carry no timestamps: spread them evenly over the ride.
    n = len(coords)
    step = (end - start) / (n - 1) if n > 1 else 0.0
    pts = [Point(t=start + i * step, lat=lat, lon=lon) for i, (lat, lon) in enumerate(coords)]
    return Segment("lift", start, end, info.str(2) if info else None, pts)


def _drop_overlap(segments: list[Segment]) -> None:
    """Consecutive segments can overlap by a few seconds; FIT needs increasing timestamps."""
    last = float("-inf")
    for seg in segments:
        seg.points = [p for p in seg.points if p.t > last]
        if seg.points:
            seg.start, seg.end = seg.points[0].t, seg.points[-1].t
            last = seg.end


def _fill_lift_altitude(segments: list[Segment]) -> None:
    """Lifts have no altitude; interpolate from the bottom of the previous run
    to the top of the next one."""
    for i, seg in enumerate(segments):
        if seg.kind != "lift":
            continue
        below = next((s.points[-1].alt for s in reversed(segments[:i]) if s.kind == "run"), None)
        above = next((s.points[0].alt for s in segments[i + 1 :] if s.kind == "run"), None)
        if below is None or above is None:
            continue
        n = len(seg.points)
        for j, p in enumerate(seg.points):
            p.alt = below + (above - below) * (j / (n - 1) if n > 1 else 1.0)
