# ClipDad – GTA 6-klippbot

Finner Twitch-klipp som tar av (Kai Cenat og hele GTA VI-kategorien), lager et 9:16-utkast med
undertekster og normalisert lyd, og sender det til Telegram. Varsler når IShowSpeed går live.

## Oppsett (én gang)

1. **Legg inn nøklene:** Settings → Secrets and variables → Actions → New repository secret
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`
   - `TELEGRAM_BOT_TOKEN`
2. **Start boten:** Actions → ClipDad → Run workflow.
3. **Koble gruppene:** Skriv `/start` i hver Telegram-gruppe. Gruppenavnet må inneholde
   *Kai*, *Speed* eller *GTA*. Boten svarer ved neste runde (innen ~10 min).

## Bruk

- Klipp som tar av, kommer automatisk.
- `/klipp LENKE` – lager utkast av et Twitch- eller YouTube-klipp.
- `/klipp LENKE 1:23:40-1:24:20` – lager utkast av en bit av en VOD/video.

## Justere

Alt ligger i `config.json`:

- `min_views` og `min_views_per_min` – hvor viralt et klipp må være før det sendes.
- `game_name` – sett til `Grand Theft Auto V` for å teste før lansering.
- `layout` – `blur` (uskarp bakgrunn) eller `facecam` med `"facecam": [x, y, bredde, høyde]`
  som andeler av bildet, f.eks. `[0.75, 0.0, 0.25, 0.3]` for facecam øverst til høyre.
