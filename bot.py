#!/usr/bin/env python3
"""ClipDad: finner GTA 6-klipp som tar av, lager et 9:16-utkast og sender det til Telegram.

Kjøres hvert 10. minutt av GitHub Actions (se .github/workflows/bot.yml).
Hemmeligheter kommer fra miljøvariabler: TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TELEGRAM_BOT_TOKEN.
Valgfritt: CHAT_KAI, CHAT_SPEED, CHAT_GTA (ellers lærer boten chat-ID fra gruppenavnet).
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_PATH = Path(os.environ.get("STATE_FILE", ROOT / "state" / "state.json"))
TG_MAX_BYTES = 49 * 1024 * 1024
W, H = 1080, 1920


def log(*a):
    print(*a, flush=True)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- state

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:  # korrupt fil: start på nytt
            log("Kunne ikke lese state:", e)
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method, files=None, timeout=120, **params):
        r = requests.post(f"{self.base}/{method}", data=params, files=files, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"ok": False, "description": r.text[:200]}
        if not data.get("ok"):
            log(f"Telegram {method} feilet:", data.get("description"))
        return data

    def text(self, chat, msg):
        return self.call("sendMessage", chat_id=chat, text=msg, parse_mode="HTML",
                         disable_web_page_preview="true")

    def video(self, chat, path, caption):
        with open(path, "rb") as f:
            return self.call("sendVideo", files={"video": f}, chat_id=chat, caption=caption,
                             parse_mode="HTML", supports_streaming="true", timeout=300)

    def document(self, chat, path, caption=""):
        with open(path, "rb") as f:
            return self.call("sendDocument", files={"document": f}, chat_id=chat,
                             caption=caption, parse_mode="HTML", timeout=300)


def chat_for(channel, state):
    env = os.environ.get(f"CHAT_{channel.upper()}", "").strip()
    return env or state.get("chats", {}).get(channel)


def learn_chat(chat, state):
    """Kobler en Telegram-gruppe til en kanal ut fra gruppenavnet (f.eks. 'Kai Cenat clips' -> kai)."""
    title = (chat.get("title") or "").lower()
    for name, ch in CFG["channels"].items():
        if any(k in title for k in ch.get("title_keywords", [])):
            chats = state.setdefault("chats", {})
            if chats.get(name) != chat["id"]:
                chats[name] = chat["id"]
                log(f"Lærte chat for {name}: {chat.get('title')} ({chat['id']})")
            return name
    return None


HELP = (
    "<b>ClipDad</b> er koblet til kanalen <b>{ch}</b>.\n\n"
    "Klipp som tar av, kommer hit automatisk.\n\n"
    "Lag et klipp selv:\n"
    "<code>/klipp LENKE</code> – et Twitch- eller YouTube-klipp\n"
    "<code>/klipp LENKE 1:23:40-1:24:20</code> – en bit av en VOD/video\n\n"
    "Boten sjekker hvert ~10. minutt, så svar kan ta litt tid."
)


def handle_commands(tg, state):
    """Leser nye Telegram-meldinger og returnerer en liste med /klipp-jobber."""
    jobs = []
    params = {"allowed_updates": json.dumps(["message", "my_chat_member"])}
    if state.get("tg_offset"):
        params["offset"] = state["tg_offset"]
    data = tg.call("getUpdates", **params)
    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        member = upd.get("my_chat_member") or {}
        chat = msg.get("chat") or member.get("chat")
        if not chat:
            continue
        if msg.get("migrate_to_chat_id"):  # gruppe ble til supergruppe
            chat = dict(chat, id=msg["migrate_to_chat_id"])
        channel = learn_chat(chat, state)
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if channel is None:
            tg.text(chat["id"], "Jeg vet ikke hvilken kanal denne gruppen hører til. "
                                "Gi gruppen et navn som inneholder Kai, Speed eller GTA.")
            continue
        if cmd in ("/start", "/hjelp", "/help"):
            tg.text(chat["id"], HELP.format(ch=channel))
        elif cmd == "/klipp":
            parts = rest.split()
            if not parts:
                tg.text(chat["id"], "Bruk: <code>/klipp LENKE</code> eller "
                                    "<code>/klipp LENKE 1:23:40-1:24:20</code>")
                continue
            jobs.append({"channel": channel, "chat": chat["id"], "url": parts[0],
                         "range": parts[1] if len(parts) > 1 else None})
    return jobs


# ---------------------------------------------------------------- twitch

class Twitch:
    def __init__(self, cid, secret):
        r = requests.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"},
            timeout=30)
        r.raise_for_status()
        self.h = {"Client-Id": cid, "Authorization": f"Bearer {r.json()['access_token']}"}

    def get(self, path, **params):
        r = requests.get(f"https://api.twitch.tv/helix/{path}", headers=self.h,
                         params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def game_id(self, name):
        data = self.get("games", name=name)["data"]
        if data:
            return data[0]["id"]
        found = self.get("search/categories", query=name, first=5)["data"]
        for g in found:
            if g["name"].lower() == name.lower():
                return g["id"]
        return None

    def user_ids(self, logins):
        if not logins:
            return {}
        data = self.get("users", login=logins)["data"]
        return {u["login"].lower(): u["id"] for u in data}

    def clips(self, started_at, pages=2, **params):
        out, cursor = [], None
        for _ in range(pages):
            q = dict(params, started_at=started_at, first=100)
            if cursor:
                q["after"] = cursor
            data = self.get("clips", **q)
            out += data["data"]
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return out


def pick_viral(clips, ch_cfg, state, now):
    """Regner ut visninger per minutt siden forrige sjekk og plukker klipp som tar av."""
    seen = state.setdefault("clips", {})
    exclude = {x.lower() for x in ch_cfg.get("exclude_logins", [])}
    picked = []
    for c in clips:
        if c["broadcaster_name"].lower() in exclude:
            continue
        prev = seen.get(c["id"])
        if prev:
            minutes = max((now.timestamp() - prev["t"]) / 60, 1)
            rate = (c["view_count"] - prev["views"]) / minutes
        else:
            age = max((now - parse_ts(c["created_at"])).total_seconds() / 60, 1)
            rate = c["view_count"] / age
        seen[c["id"]] = {"views": c["view_count"], "t": now.timestamp(),
                         "sent": prev.get("sent", False) if prev else False,
                         "video": c.get("video_id") or "", "offset": c.get("vod_offset"),
                         "b": c["broadcaster_id"]}
        if seen[c["id"]]["sent"]:
            continue
        if c["view_count"] >= ch_cfg["min_views"] and rate >= ch_cfg["min_views_per_min"]:
            picked.append((rate, c))
    picked.sort(key=lambda x: -x[0])
    return picked


def same_moment_already_sent(c, state):
    """Mange klipper samme øyeblikk. Hopp over hvis vi har sendt et klipp innen 60 s av samme VOD."""
    if not c.get("video_id") or c.get("vod_offset") is None:
        return False
    for other in state.get("clips", {}).values():
        if (other.get("sent") and other.get("video") == c["video_id"]
                and other.get("offset") is not None
                and abs(other["offset"] - c["vod_offset"]) < 60):
            return True
    return False


def prune(state, now):
    cutoff = now.timestamp() - 48 * 3600
    state["clips"] = {k: v for k, v in state.get("clips", {}).items() if v["t"] > cutoff}


# ---------------------------------------------------------------- video

def run(cmd, timeout=600):
    log("$", " ".join(str(c) for c in cmd)[:300])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} feilet: {p.stderr[-1500:]}")
    return p.stdout


def download(url, out_dir, section=None):
    tmpl = str(Path(out_dir) / "raw.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--no-progress", "-f",
           "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
           "-o", tmpl, url]
    if section:
        cmd[1:1] = ["--download-sections", f"*{section}", "--force-keyframes-at-cuts"]
    run(cmd, timeout=900)
    files = sorted(Path(out_dir).glob("raw.*"))
    if not files:
        raise RuntimeError("Fant ingen nedlastet fil")
    return files[0]


def probe_size(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height", "-of", "json", str(path)])
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_whisper = None


def transcribe(path):
    """Returnerer ord med tidskoder. Hopper over teksting hvis whisper ikke er installert."""
    global _whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper mangler, hopper over undertekster")
        return []
    if _whisper is None:
        _whisper = WhisperModel(CFG.get("whisper_model", "small"), device="cpu",
                                compute_type="int8")
    segments, _ = _whisper.transcribe(str(path), word_timestamps=True, beam_size=1,
                                      vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            if w.word.strip():
                words.append((w.start, w.end, w.word.strip()))
    return words


def write_ass(words, path, y_pos):
    lines = []
    i = 0
    while i < len(words):  # 1–3 ord per linje, bryter på pause
        group = [words[i]]
        i += 1
        while i < len(words) and len(group) < 3 and words[i][0] - group[-1][1] < 0.35:
            group.append(words[i])
            i += 1
        start, end = group[0][0], max(group[-1][1], group[0][0] + 0.3)
        text = " ".join(w[2] for w in group).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,"
                     f"{{\\pos({W // 2},{y_pos})}}{text}")
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 0\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Cap,DejaVu Sans,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,7,2,5,40,40,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def make_vertical(src, out, layout="blur", facecam=None, captions=True):
    """9:16-utkast: uskarp bakgrunn eller facecam øverst, undertekster og lyd til ca. -14 LUFS."""
    work = Path(out).parent
    sw, sh = probe_size(src)
    if layout == "facecam" and facecam:
        fx, fy, fw, fh = facecam  # andeler av bildet, f.eks. [0.75, 0.0, 0.25, 0.3]
        top_h, bot_h = 640, H - 640
        gw = min(sw, int(sh * W / bot_h))
        vf = (f"[0:v]crop={int(fw * sw)}:{int(fh * sh)}:{int(fx * sw)}:{int(fy * sh)},"
              f"scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h}[cam];"
              f"[0:v]crop={gw}:{sh}:{(sw - gw) // 2}:0,scale={W}:{bot_h}[game];"
              f"[cam][game]vstack[v0]")
        cap_y = top_h + 160
    else:
        fg_h = int(round(sh * W / sw / 2) * 2)
        vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=24:2,eq=brightness=-0.12[bg];"
              f"[0:v]scale={W}:{fg_h}[fg];"
              f"[bg][fg]overlay=0:(H-h)/2[v0]")
        cap_y = min(H // 2 + fg_h // 2 + 170, H - 300)
    final = "[v0]"
    if captions:
        try:
            words = transcribe(src)
        except Exception as e:
            log("Teksting feilet:", e)
            words = []
        if words:
            ass = work / "subs.ass"
            write_ass(words, ass, cap_y)
            vf += f";[v0]ass={ass.as_posix()}[v1]"
            final = "[v1]"
    base = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-filter_complex", vf,
            "-map", final, "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"]
    for crf in (21, 26, 30):
        run(base + ["-crf", str(crf), str(out)], timeout=900)
        if Path(out).stat().st_size <= TG_MAX_BYTES:
            return out
    raise RuntimeError("Videoen ble for stor for Telegram")


def produce_and_send(tg, chat, url, caption, layout, section=None):
    with tempfile.TemporaryDirectory() as d:
        raw = download(url, d, section)
        out = Path(d) / "utkast.mp4"
        ch_layout = layout.get("layout", "blur") if isinstance(layout, dict) else layout
        facecam = layout.get("facecam") if isinstance(layout, dict) else None
        make_vertical(raw, out, ch_layout, facecam)
        res = tg.video(chat, out, caption)
        if not res.get("ok"):
            return False
        if CFG.get("send_raw_clip") and raw.stat().st_size <= TG_MAX_BYTES:
            tg.document(chat, raw, "Originalklipp (urørt)")
        return True


# ---------------------------------------------------------------- jobs

def twitch_scan(tg, state, budget):
    tw = Twitch(os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"])
    now = now_utc()
    since = (now - dt.timedelta(hours=CFG["lookback_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    game = tw.game_id(CFG["game_name"])
    log("Spill:", CFG["game_name"], "->", game)
    candidates = []
    for name, ch in CFG["channels"].items():
        if "min_views" not in ch:
            continue
        clips = []
        if ch.get("whole_category"):
            if not game:
                log("Fant ikke kategorien, hopper over", name)
                continue
            clips = tw.clips(since, game_id=game)
        for login, uid in tw.user_ids(ch.get("twitch_logins", [])).items():
            got = tw.clips(since, pages=1, broadcaster_id=uid)
            if game:  # bare GTA-klipp fra streameren
                got = [c for c in got if c.get("game_id") == game]
            clips += got
        for rate, c in pick_viral(clips, ch, state, now):
            candidates.append((rate, name, c))
    candidates.sort(key=lambda x: -x[0])
    for rate, name, c in candidates:
        if budget[0] <= 0:
            break
        chat = chat_for(name, state)
        if not chat:
            log(f"Ingen Telegram-gruppe for {name} ennå – skriv /start i gruppen")
            continue
        if same_moment_already_sent(c, state):
            state["clips"][c["id"]]["sent"] = True
            continue
        cap = (f"🔥 <b>{html.escape(c['broadcaster_name'])}</b>\n"
               f"{html.escape(c['title'])}\n"
               f"👁 {c['view_count']:,} visninger · +{rate:,.0f}/min\n"
               f"{c['url']}").replace(",", " ")
        try:
            ok = produce_and_send(tg, chat, c["url"], cap, CFG["channels"][name])
        except Exception as e:
            log("Klarte ikke å lage klipp:", e)
            tg.text(chat, f"{cap}\n\n(Klarte ikke å lage utkast, her er lenken.)")
            ok = True
        state["clips"][c["id"]]["sent"] = ok
        budget[0] -= 1
        save_state(state)
    prune(state, now)


def speed_check(tg, state):
    ch = CFG["channels"].get("speed", {})
    url, chat = ch.get("youtube_live_url"), chat_for("speed", state)
    if not url or not chat:
        return
    try:
        out = run(["yt-dlp", "--skip-download", "--no-warnings", "-J", url], timeout=90)
        info = json.loads(out)
    except Exception as e:
        log("Live-sjekk feilet (normalt hvis han ikke er live):", str(e)[:200])
        return
    if info.get("is_live") and info.get("id") != state.get("speed_live_id"):
        state["speed_live_id"] = info["id"]
        tg.text(chat, (
            f"🔴 <b>IShowSpeed er live!</b>\n{html.escape(info.get('title', ''))}\n"
            f"https://youtu.be/{info['id']}\n\n"
            "Ser du noe bra: lag et klipp i YouTube-appen (saks-ikonet ✂️), "
            "og send <code>/klipp LENKE</code> her."))


def run_jobs(tg, jobs, budget):
    for job in jobs:
        if budget[0] <= 0:
            tg.text(job["chat"], "Mange klipp i kø, tar dette ved neste runde.")
            continue
        tg.text(job["chat"], "⏳ Lager klipp…")
        cap = f"✂️ Ditt klipp\n{job['url']}" + (f" ({job['range']})" if job["range"] else "")
        try:
            produce_and_send(tg, job["chat"], job["url"], html.escape(cap),
                             CFG["channels"][job["channel"]], job["range"])
        except Exception as e:
            log("Jobb feilet:", e)
            tg.text(job["chat"], "❌ Klarte ikke å lage klippet. Sjekk lenken og tidspunktene.\n"
                                 f"<code>{html.escape(str(e)[-300:])}</code>")
        budget[0] -= 1


def main():
    state = load_state()
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    budget = [CFG.get("max_sends_per_run", 4)]
    jobs = handle_commands(tg, state)
    save_state(state)
    run_jobs(tg, jobs, budget)
    try:
        twitch_scan(tg, state, budget)
    except Exception as e:
        log("Twitch-sjekk feilet:", e)
    speed_check(tg, state)
    save_state(state)
    log("Ferdig.")


if __name__ == "__main__":
    main()
