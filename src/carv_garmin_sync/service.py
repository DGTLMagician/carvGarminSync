"""Install the sync as a systemd user service with a timer (Linux)."""

import getpass
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "carv-garmin-sync"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
SERVICE = UNIT_DIR / f"{NAME}.service"
TIMER = UNIT_DIR / f"{NAME}.timer"


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], check=check, text=True, capture_output=True)


def install(interval: str = "6h") -> list[str]:
    """Write and enable the units. Returns notes for the user."""
    if not shutil.which("systemctl"):
        raise RuntimeError("systemctl niet gevonden; de service werkt alleen op Linux met systemd")
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    # Run with the interpreter of this installation, so the unit keeps working
    # regardless of PATH (uv tool / venv / pipx).
    SERVICE.write_text(f"""[Unit]
Description=Synchroniseer Carv-skisessies naar Garmin Connect
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart={sys.executable} -m carv_garmin_sync sync
Nice=10
""")
    TIMER.write_text(f"""[Unit]
Description=Periodieke Carv → Garmin sync

[Timer]
OnBootSec=5min
OnUnitActiveSec={interval}
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
""")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{NAME}.timer")

    notes = []
    linger = subprocess.run(
        ["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"],
        text=True, capture_output=True,
    ).stdout.strip()
    if linger != "yes":
        notes.append(
            "Let op: zonder 'linger' draait de timer alleen zolang je bent ingelogd. "
            f"Zet het aan met:  sudo loginctl enable-linger {getpass.getuser()}"
        )
    return notes


def uninstall() -> None:
    _systemctl("disable", "--now", f"{NAME}.timer", check=False)
    for f in (TIMER, SERVICE):
        f.unlink(missing_ok=True)
    _systemctl("daemon-reload", check=False)


def status() -> str:
    if not TIMER.exists():
        return "service niet geïnstalleerd"
    t = _systemctl("list-timers", f"{NAME}.timer", "--no-pager", check=False).stdout.strip()
    s = _systemctl("status", f"{NAME}.service", "--no-pager", "-n", "5", check=False).stdout.strip()
    return f"{t}\n\n{s}"
