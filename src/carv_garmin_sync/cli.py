import argparse
import datetime as dt
import getpass
import json
import sys
import tempfile
from pathlib import Path

from . import fit, pb
from .carv import STATE_DIR, CarvClient, CarvError
from .session import SkiSession, parse

SYNCED = STATE_DIR / "synced.json"
CONFIG = STATE_DIR / "config.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _write(path: Path, d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(d, indent=2, sort_keys=True))


def _ask(prompt: str, default: bool) -> bool:
    a = input(f"{prompt} [{'J/n' if default else 'j/N'}] ").strip().lower()
    return default if not a else a.startswith(("j", "y"))


def _sessions(c: CarvClient, since: dt.date | None) -> list[SkiSession]:
    listing = c.list_sessions()
    tty = sys.stderr.isatty()
    out = []
    for i, (sid, url) in enumerate(listing.items(), 1):
        if tty:
            print(f"\rCarv-sessies ophalen: {i}/{len(listing)}", end="", file=sys.stderr, flush=True)
        s = parse(sid, c.download(sid, url))
        if since and dt.datetime.fromtimestamp(s.start).date() < since:
            continue
        out.append(s)
    if tty:
        print(file=sys.stderr)
    return sorted(out, key=lambda s: s.start)


def _describe(s: SkiSession) -> str:
    day = dt.datetime.fromtimestamp(s.start).strftime("%a %d-%m-%Y %H:%M")
    lifts = sorted({g.name for g in s.segments if g.kind == "lift" and g.name})
    where = f" ({', '.join(lifts[:3])}{'…' if len(lifts) > 3 else ''})" if lifts else ""
    return f"{day}  {len(s.runs):2d} afdalingen, {s.turns:4d} bochten{where}"


def _since(args) -> dt.date | None:
    if args.since:
        return args.since
    s = _read(CONFIG).get("since")
    return dt.date.fromisoformat(s) if s else None


# --- commands --------------------------------------------------------------


def _carv_login(email: str | None, store_password: bool | None) -> None:
    email = email or input("Carv e-mail: ")
    password = getpass.getpass("Carv wachtwoord: ")
    c = CarvClient()
    user = c.login(email, password)
    c.email = email
    if store_password is None:
        store_password = _ask(
            "Wachtwoord bewaren (bestand met rechten 0600) zodat de service zelf opnieuw kan inloggen?", True
        )
    c.password = password if store_password else None
    c.save()
    print(f"Ingelogd bij Carv als {user.str(2)} {user.str(3)}.")


def cmd_carv_login(args):
    _carv_login(args.email, args.store_password)


def cmd_garmin_login(args):
    from . import garmin

    garmin.login_interactive(args.email)
    print("Ingelogd bij Garmin Connect.")


def cmd_setup(args):
    from . import garmin, service

    print("== Carv")
    _carv_login(None, None)

    print("\n== Garmin Connect")
    try:
        garmin.client()
        relogin = _ask("Al ingelogd bij Garmin. Opnieuw inloggen?", False)
    except Exception:
        relogin = True
    if relogin:
        garmin.login_interactive()
        print("Ingelogd bij Garmin Connect.")

    print("\n== Synchronisatie")
    cfg = _read(CONFIG)
    while True:
        a = input(f"Sessies synchroniseren vanaf datum (JJJJ-MM-DD, leeg = alles) [{cfg.get('since') or 'alles'}]: ").strip()
        if not a:
            break
        try:
            cfg["since"] = dt.date.fromisoformat(a).isoformat()
            break
        except ValueError:
            print("Ongeldige datum.")
    cfg["allow_overlap"] = _ask(
        "Ook uploaden als Garmin al een activiteit in dat tijdvak heeft (bv. van je horloge)?",
        cfg.get("allow_overlap", False),
    )
    _write(CONFIG, cfg)

    print("\n== Service")
    if _ask(f"systemd-timer installeren (sync elke {args.interval})?", True):
        for note in service.install(args.interval):
            print(note)
        print(f"Timer actief. Logs: journalctl --user -u {service.NAME}")
    if _ask("Nu direct een eerste sync draaien?", False):
        args.since = None
        args.dry_run = False
        args.allow_overlap = None
        cmd_sync(args)


def cmd_list(args):
    synced = _read(SYNCED)
    for s in _sessions(CarvClient.load(), args.since):
        mark = "✓" if s.id in synced else " "
        print(f"{mark} {s.id}  {_describe(s)}")


def cmd_export(args):
    c = CarvClient.load()
    listing = c.list_sessions()
    if args.session not in listing:
        sys.exit(f"Onbekende sessie: {args.session}")
    s = parse(args.session, c.download(args.session, listing[args.session]))
    out = Path(args.out or f"carv-{dt.datetime.fromtimestamp(s.start):%Y%m%d-%H%M}.fit")
    out.write_bytes(fit.build(s))
    print(f"{out}  {_describe(s)}")


def cmd_sync(args):
    from . import garmin

    allow_overlap = args.allow_overlap if args.allow_overlap is not None else _read(CONFIG).get("allow_overlap", False)
    synced = _read(SYNCED)
    todo = [s for s in _sessions(CarvClient.load(), _since(args)) if s.id not in synced and s.segments]
    if not todo:
        print("Niets te synchroniseren.")
        return
    g = None if args.dry_run else garmin.client()
    failed = 0
    for s in todo:
        label = _describe(s)
        if args.dry_run:
            print(f"[dry-run] zou uploaden: {label}")
            continue
        try:
            if not allow_overlap:
                clash = garmin.overlapping(g, s.segments[0].start, s.segments[-1].end)
                if clash:
                    names = ", ".join(a.get("activityName") or str(a.get("activityId")) for a in clash)
                    print(f"overgeslagen (overlapt met Garmin-activiteit: {names}): {label}")
                    synced[s.id] = {"skipped": "overlap", "at": dt.datetime.now().isoformat(timespec="seconds")}
                    _write(SYNCED, synced)
                    continue
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"carv-{s.id}.fit"
                path.write_bytes(fit.build(s))
                try:
                    activity_id = garmin.upload(g, path)
                except garmin.Duplicate:
                    print(f"al aanwezig in Garmin: {label}")
                    activity_id = None
        except Exception as e:  # keep going; one bad session must not block the rest
            failed += 1
            print(f"FOUT bij {label}: {e}", file=sys.stderr)
            continue
        synced[s.id] = {"garmin_activity_id": activity_id, "at": dt.datetime.now().isoformat(timespec="seconds")}
        _write(SYNCED, synced)
        print(f"geüpload: {label}" + (f"  → https://connect.garmin.com/modern/activity/{activity_id}" if activity_id else ""))
    if failed:
        sys.exit(f"{failed} sessie(s) mislukt")


def cmd_status(args):
    from . import garmin, service

    try:
        c = CarvClient.load()
        print(f"Carv:    ingelogd als {c.email}" + (" (wachtwoord bewaard)" if c.password else ""))
    except CarvError as e:
        print(f"Carv:    {e}")
    print(f"Garmin:  {'tokens aanwezig' if garmin.TOKENSTORE.exists() else 'niet ingelogd'}")
    cfg = _read(CONFIG)
    synced = _read(SYNCED)
    uploaded = sum(1 for v in synced.values() if "skipped" not in v)
    print(f"Sync:    vanaf {cfg.get('since') or 'begin'}, {uploaded} geüpload, {len(synced) - uploaded} overgeslagen")
    print(f"\n{service.status()}")


def cmd_install_service(args):
    from . import service

    for note in service.install(args.interval):
        print(note)
    print(f"Timer geïnstalleerd (elke {args.interval}). Logs: journalctl --user -u {service.NAME}")


def cmd_uninstall_service(args):
    from . import service

    service.uninstall()
    print("Service verwijderd.")


def cmd_raw(args):
    c = CarvClient.load()
    req = bytes.fromhex(args.hex) if args.hex else b""
    msgs, meta = c.call(args.method, req)
    print(f"# {len(msgs)} message(s), meta keys: {sorted(meta)}")
    for m in msgs:
        print(pb.dump(m, max_items=args.items))


def main(argv=None):
    p = argparse.ArgumentParser(prog="carv-garmin-sync", description="Synchroniseer Carv-skisessies naar Garmin Connect")
    sub = p.add_subparsers(required=True, metavar="commando")
    date = dt.date.fromisoformat

    s = sub.add_parser("setup", help="Alles in één keer: inloggen, instellingen en de service installeren")
    s.add_argument("--interval", default="6h", help="hoe vaak de service synchroniseert (systemd-tijd, standaard 6h)")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("sync", help="Upload nieuwe Carv-sessies naar Garmin Connect (dit draait de service)")
    s.add_argument("--since", type=date, help="alleen sessies vanaf deze datum (JJJJ-MM-DD)")
    s.add_argument("--dry-run", action="store_true", help="niets uploaden, alleen tonen")
    s.add_argument("--allow-overlap", action=argparse.BooleanOptionalAction, default=None,
                   help="ook uploaden als er al een Garmin-activiteit in dat tijdvak is")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("status", help="Toon login-, sync- en service-status")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("list", help="Toon Carv-sessies (✓ = verwerkt)")
    s.add_argument("--since", type=date)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("export", help="Schrijf één sessie naar een FIT-bestand")
    s.add_argument("session", help="Carv-sessie-id (zie `list`)")
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("install-service", help="Installeer alleen de systemd-timer")
    s.add_argument("--interval", default="6h")
    s.set_defaults(fn=cmd_install_service)

    s = sub.add_parser("uninstall-service", help="Verwijder de systemd-timer")
    s.set_defaults(fn=cmd_uninstall_service)

    s = sub.add_parser("carv-login", help="Alleen opnieuw inloggen bij Carv")
    s.add_argument("--email")
    s.add_argument("--store-password", action=argparse.BooleanOptionalAction, default=None)
    s.set_defaults(fn=cmd_carv_login)

    s = sub.add_parser("garmin-login", help="Alleen opnieuw inloggen bij Garmin Connect")
    s.add_argument("--email")
    s.set_defaults(fn=cmd_garmin_login)

    s = sub.add_parser("raw", help="(debug) roep een Carv gRPC-methode aan en dump de respons")
    s.add_argument("method")
    s.add_argument("hex", nargs="?")
    s.add_argument("--items", type=int, default=6)
    s.set_defaults(fn=cmd_raw)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (CarvError, RuntimeError) as e:
        sys.exit(f"Fout: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
