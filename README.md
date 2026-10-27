# carv-garmin-sync

Synchroniseert ski-sessies uit [Carv](https://getcarv.com) naar Garmin Connect als
*Resort Skiing*-activiteit: GPS-spoor, hoogte, snelheid, en een lap per afdaling en per lift.

## Installeren als service (Linux, systemd)

Je hebt [uv](https://docs.astral.sh/uv/) nodig. Python 3.13 haalt uv zelf op.

```bash
uv tool install /pad/naar/carvGarminSync
carv-garmin-sync setup
```

`setup` regelt alles in één keer:

1. Je logt in bij Carv. Kies je ervoor het wachtwoord te bewaren, dan kan de service zelf
   opnieuw inloggen als de sessie verloopt.
2. Je logt in bij Garmin Connect. MFA wordt ondersteund, en Garmin vernieuwt de tokens zelf.
3. Je kiest vanaf welke datum er gesynchroniseerd wordt, en of activiteiten die overlappen met
   een bestaande Garmin-activiteit (bijvoorbeeld van je horloge) toch geüpload worden.
4. Er wordt een systemd **user**-timer geïnstalleerd die elke 6 uur `carv-garmin-sync sync`
   draait. Een ander interval kies je met `setup --interval 2h`.

Wil je dat de timer ook draait als je niet bent ingelogd (headless server), zet dan eenmalig
linger aan. `setup` waarschuwt als dat nodig is:

```bash
sudo loginctl enable-linger $USER
```

## Commando's

| Commando | Wat |
|---|---|
| `setup` | alles in één keer (zie hierboven) |
| `sync [--since JJJJ-MM-DD] [--dry-run] [--[no-]allow-overlap]` | nieuwe sessies uploaden; dit draait de service |
| `status` | login-, sync- en service-status |
| `list` | alle Carv-sessies (✓ = verwerkt) |
| `export <sessie-id> -o dag.fit` | één sessie als FIT-bestand |
| `install-service` / `uninstall-service` | alleen de timer (de)installeren |
| `carv-login` / `garmin-login` | alleen opnieuw inloggen |

Logs bekijk je met `journalctl --user -u carv-garmin-sync`. Een mislukte run eindigt met
exitcode 1 en is dan zichtbaar als *failed* in `systemctl --user status carv-garmin-sync`.

## Gedrag

- Sessies waarvoor in Garmin al een activiteit in hetzelfde tijdvak staat, worden overgeslagen en
  gemarkeerd, zodat ze niet bij elke run opnieuw worden bekeken. Dit gebeurt niet als je
  overlappen toestaat.
- Uploads gaan via Garmin's *import*-endpoint, zodat Garmin ze niet doorstuurt naar Strava.
  Carv synchroniseert zelf al naar Strava.
- Alle status staat in `~/.config/carv-garmin-sync/`, met rechten 0600/0700:
  - `carv.json`: Carv-sessiecookie, plus optioneel je wachtwoord
  - `garmin/`: Garmin-tokens
  - `config.json`: je instellingen
  - `synced.json`: wat al verwerkt is
  
  Gedownloade sessies worden gecachet in `~/.cache/carv-garmin-sync/`.

## Hoe het werkt (reverse engineering van Carv 10.0.0, Android)

De app is een dunne Kotlin-schil rond een Rust-library (`libandroid.so`, met symbolen) die met
tonic/prost gRPC praat met `rpc.carv.ai:443`. Er worden geen `.proto`-bestanden meegeleverd. De
berichten zijn gereconstrueerd uit de prost-encoders (disassembly) en uit echte responses.

| Stap | RPC / bron | Bericht |
|---|---|---|
| Inloggen | `/net.Account/UserLogin` | req `{1 email, 2 password}` → res `{1 User{1 id, 2 voornaam, 3 achternaam, 4 email, 8 "sessionid=…"}}` |
| Sessies opvragen | `/net.SessionApi/Sync` | lege req → res `{1 {1 [ {1 sessie-id, 2 signed GCS-URL} ]}}` |
| Sessie downloaden | GCS-URL | `session/<id>/metrics/14.0.1.protobuf.zstd`: zstd-gecomprimeerde `proto::data::Session` |

Verplichte gRPC-metadata: `cookie: sessionid=…` en `data: 14.0.1` (de dataversie, die de server
teruggeeft als `x-server-data-version`). De app stuurt daarnaast `app`, `os`, `platform`,
`locale`, `phone-make` en `phone-model` mee.

Het sessieformaat staat beschreven in [`session.py`](src/carv_garmin_sync/session.py). Liftpunten
hebben geen tijdstempel en geen hoogte. De tijd wordt gelijkmatig over de liftrit verdeeld en de
hoogte wordt geïnterpoleerd tussen het eind van de vorige afdaling en de start van de volgende.

Niet meegenomen: Carv's eigen ski-metrics (Ski:IQ, kantelhoek, enzovoort). Daar heeft Garmin
geen velden voor. Ook de sub-segmenten binnen afdalingen worden niet gebruikt.
