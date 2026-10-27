"""Client for Carv's gRPC backend (rpc.carv.ai), reconstructed from the Android app (10.0.0)."""

import json
import os
import urllib.request
from pathlib import Path

import grpc
import zstandard

from . import pb

HOST = "rpc.carv.ai:443"
APP_VERSION = "10.0.0"
DATA_VERSION = "14.0.1"  # x-server-data-version reported by the server
STATE_DIR = Path(os.environ.get("CARV_SYNC_HOME", Path.home() / ".config" / "carv-garmin-sync"))
CARV_STATE = STATE_DIR / "carv.json"
CACHE_DIR = Path(os.environ.get("CARV_SYNC_CACHE", Path.home() / ".cache" / "carv-garmin-sync"))


class CarvError(Exception):
    pass


class NotLoggedIn(CarvError):
    pass


class CarvClient:
    def __init__(self, cookie: str | None = None, authorization: str | None = None):
        self.cookie = cookie
        self.authorization = authorization
        self._channel = grpc.secure_channel(
            HOST,
            grpc.ssl_channel_credentials(),
            options=[("grpc.max_receive_message_length", 512 * 1024 * 1024)],
        )

    # --- transport -------------------------------------------------------

    def _metadata(self) -> list[tuple[str, str]]:
        # Same metadata keys the app's RpcInterceptor sets.
        md = [
            ("app", APP_VERSION),
            ("os", "android"),
            ("platform", "android"),
            ("data", DATA_VERSION),
            ("locale", "en-US"),
            ("phone-make", "carv-garmin-sync"),
            ("phone-model", "cli"),
        ]
        if self.cookie:
            md.append(("cookie", self.cookie))
        if self.authorization:
            md.append(("authorization", self.authorization))
        return md

    def call(self, method: str, request: bytes = b"", timeout: float = 120) -> tuple[list[bytes], dict]:
        """Call a method; returns (response messages, response metadata)."""
        rpc = self._channel.unary_stream(
            method, request_serializer=lambda b: b, response_deserializer=lambda b: b
        )
        try:
            it = rpc(request, metadata=self._metadata(), timeout=timeout)
            msgs = list(it)
            meta = {k: v for k, v in list(it.initial_metadata()) + list(it.trailing_metadata())}
        except grpc.RpcError as e:
            raise CarvError(f"{method}: {e.code().name}: {e.details()}") from e
        return msgs, meta

    def call1(self, method: str, request: bytes = b"") -> pb.Msg:
        msgs, _ = self.call(method, request)
        if not msgs:
            raise CarvError(f"{method}: empty response")
        return pb.decode(msgs[0])

    # --- persistence -----------------------------------------------------

    @classmethod
    def load(cls) -> "CarvClient":
        if not CARV_STATE.exists():
            raise NotLoggedIn("niet ingelogd bij Carv; draai `carv-garmin-sync setup`")
        s = json.loads(CARV_STATE.read_text())
        c = cls(cookie=s.get("cookie"), authorization=s.get("authorization"))
        c.email, c.password = s.get("email"), s.get("password")
        return c

    email: str | None = None
    # Only kept when the user opts in, so an unattended service can log in again
    # after the session cookie expires. Stored in a 0600 file.
    password: str | None = None

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {"email": self.email, "password": self.password, "cookie": self.cookie, "authorization": self.authorization}
        fd = os.open(CARV_STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: v for k, v in state.items() if v is not None}, f, indent=2)
        CARV_STATE.chmod(0o600)

    def relogin(self) -> None:
        """Log in again with the stored password (session cookie expired)."""
        if not (self.email and self.password):
            raise NotLoggedIn("Carv-sessie verlopen en geen wachtwoord opgeslagen; draai `carv-garmin-sync setup`")
        self.login(self.email, self.password)
        self.save()

    # --- API -------------------------------------------------------------

    def login(self, email: str, password: str) -> pb.Msg:
        """UserLoginReq {1 email, 2 password} -> UserLoginRes {1 User, ...}.

        User: 1 id, 2 first name, 3 last name, 4 email, 8 session cookie ("sessionid=...")."""
        user = self.call1("/net.Account/UserLogin", pb.encode([(1, email), (2, password)])).msg(1)
        cookie = user.str(8) if user else None
        if not cookie:
            raise CarvError("UserLogin: no session cookie in response")
        self.cookie = cookie
        return user

    def list_sessions(self) -> dict[str, str]:
        """SyncReq {} -> SyncRes {1 {1 repeated {1 session id, 2 signed metrics URL}}}.

        An empty request means "the client has nothing", so the server lists every session."""
        try:
            res = self.call1("/net.SessionApi/Sync")
        except CarvError as e:
            if "UNAUTHENTICATED" not in str(e):
                raise
            self.relogin()
            res = self.call1("/net.SessionApi/Sync")
        listing = res.msg(1)
        return {e.str(1): e.str(2) for e in listing.msgs(1)} if listing else {}

    def download(self, session_id: str, url: str) -> bytes:
        """Fetch a session's metrics file (zstd-compressed proto::data::Session), cached on disk."""
        path = CACHE_DIR / f"{session_id}.{DATA_VERSION}.pb"
        if path.exists():
            return path.read_bytes()
        with urllib.request.urlopen(url, timeout=120) as r:
            raw = r.read()
        data = zstandard.ZstdDecompressor().stream_reader(raw).read()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data
