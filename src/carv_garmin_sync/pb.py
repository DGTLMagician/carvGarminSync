"""Minimal protobuf wire-format encoder/decoder.

Carv ships no .proto files, so messages are built and read by field number.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


def _varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def encode(fields: list[tuple[int, object]]) -> bytes:
    """Encode (field_number, value) pairs. str/bytes -> length-delimited,
    bool/int -> varint, float -> double, list -> nested message."""
    out = bytearray()
    for num, val in fields:
        if val is None:
            continue
        if isinstance(val, bool) or isinstance(val, int):
            out += _varint(num << 3) + _varint(int(val))
        elif isinstance(val, float):
            out += _varint(num << 3 | 1) + struct.pack("<d", val)
        elif isinstance(val, str):
            b = val.encode()
            out += _varint(num << 3 | 2) + _varint(len(b)) + b
        elif isinstance(val, (bytes, bytearray)):
            out += _varint(num << 3 | 2) + _varint(len(val)) + bytes(val)
        elif isinstance(val, list):
            b = encode(val)
            out += _varint(num << 3 | 2) + _varint(len(b)) + b
        else:
            raise TypeError(f"cannot encode {type(val)} for field {num}")
    return bytes(out)


@dataclass
class Msg:
    """Decoded message: field number -> list of raw values.

    varint -> int, fixed64 -> bytes(8), fixed32 -> bytes(4), length-delimited -> bytes.
    Interpretation (string, nested message, double, ...) is up to the caller.
    """

    fields: dict[int, list] = field(default_factory=dict)

    def all(self, n: int) -> list:
        return self.fields.get(n, [])

    def get(self, n: int, default=None):
        v = self.fields.get(n)
        return v[-1] if v else default

    def str(self, n: int, default: str | None = None) -> str | None:
        v = self.get(n)
        return v.decode() if isinstance(v, (bytes, bytearray)) else default

    def int(self, n: int, default: int | None = None) -> int | None:
        v = self.get(n)
        return v if isinstance(v, int) else default

    def sint(self, n: int, default: int | None = None) -> int | None:
        v = self.int(n)
        return default if v is None else (v - (1 << 64) if v >= 1 << 63 else v)

    def double(self, n: int, default: float | None = None) -> float | None:
        v = self.get(n)
        return struct.unpack("<d", v)[0] if isinstance(v, bytes) and len(v) == 8 else default

    def float(self, n: int, default: float | None = None) -> float | None:
        v = self.get(n)
        return struct.unpack("<f", v)[0] if isinstance(v, bytes) and len(v) == 4 else default

    def msg(self, n: int) -> "Msg | None":
        v = self.get(n)
        return decode(v) if isinstance(v, (bytes, bytearray)) else None

    def msgs(self, n: int) -> list["Msg"]:
        return [decode(v) for v in self.all(n)]

    def packed_doubles(self, n: int) -> list[float]:
        out = []
        for v in self.all(n):
            if isinstance(v, bytes):
                out += [x[0] for x in struct.iter_unpack("<d", v)]
        return out

    def packed_floats(self, n: int) -> list[float]:
        out = []
        for v in self.all(n):
            if isinstance(v, bytes):
                out += [x[0] for x in struct.iter_unpack("<f", v)]
        return out


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    shift = n = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def decode(b: bytes) -> Msg:
    m = Msg()
    i = 0
    while i < len(b):
        key, i = _read_varint(b, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ValueError("field number 0")
        if wt == 0:
            v, i = _read_varint(b, i)
        elif wt == 1:
            v, i = bytes(b[i : i + 8]), i + 8
        elif wt == 2:
            ln, i = _read_varint(b, i)
            if i + ln > len(b):
                raise ValueError("truncated length-delimited field")
            v, i = bytes(b[i : i + ln]), i + ln
        elif wt == 5:
            v, i = bytes(b[i : i + 4]), i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        m.fields.setdefault(num, []).append(v)
    return m


def dump(b: bytes, indent: int = 0, max_depth: int = 8, max_items: int = 6) -> str:
    """Schemaless pretty-printer, for reverse engineering responses."""
    pad = "  " * indent
    lines = []
    try:
        m = decode(b)
    except ValueError:
        return pad + repr(b[:80])
    for num, vals in sorted(m.fields.items()):
        shown = vals[:max_items]
        for v in shown:
            if isinstance(v, int):
                lines.append(f"{pad}{num}: {v}")
            elif len(v) == 8 and not _looks_msg(v):
                lines.append(f"{pad}{num}: f64={struct.unpack('<d', v)[0]!r} i64={struct.unpack('<q', v)[0]}")
            elif len(v) == 4 and not _looks_msg(v):
                lines.append(f"{pad}{num}: f32={struct.unpack('<f', v)[0]!r} i32={struct.unpack('<i', v)[0]}")
            elif _printable(v):
                lines.append(f"{pad}{num}: {v.decode()!r}")
            elif indent < max_depth and _looks_msg(v):
                lines.append(f"{pad}{num}: {{  # {len(v)} bytes")
                lines.append(dump(v, indent + 1, max_depth, max_items))
                lines.append(f"{pad}}}")
            else:
                lines.append(f"{pad}{num}: bytes[{len(v)}] {v[:32].hex()}")
        if len(vals) > max_items:
            lines.append(f"{pad}{num}: ... ({len(vals)} total)")
    return "\n".join(lines)


def _printable(v: bytes) -> bool:
    if not v:
        return True
    try:
        s = v.decode()
    except UnicodeDecodeError:
        return False
    return all(c.isprintable() or c in "\n\t" for c in s)


def _looks_msg(v: bytes) -> bool:
    try:
        return bool(decode(v).fields)
    except ValueError:
        return False
