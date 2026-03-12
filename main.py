import os
import smtplib
import requests
import time
import json
import re
import html as html_lib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from anthropic import Anthropic

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError:
    YouTubeTranscriptApi = None

import yt_dlp

# ============================================================
# CONFIG
# ============================================================
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUTUBE_API_KEY   = os.environ["YOUTUBE_API_KEY"]
EMAIL_ADDRESS     = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT   = os.environ["EMAIL_RECIPIENT"]

MODEL = "claude-haiku-4-5-20251001"
MAX_TRANSCRIPT_CHARS = 100000
LOOKBACK_HOURS = 48
PREFERRED_LANGUAGES = ["pt-BR", "pt", "en", "en-US", "es"]

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ============================================================
# YOUTUBE: FETCH RECENT VIDEOS
# ============================================================
def get_channel_videos(channel_id):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?key={YOUTUBE_API_KEY}&channelId={channel_id}"
        f"&part=snippet&type=video&order=date&publishedAfter={cutoff}"
        f"&maxResults=5"
    )
    try:
        res = requests.get(url, timeout=10).json()
        videos = []
        for item in res.get("items", []):
            videos.append({
                "id":      item["id"]["videoId"],
                "title":   item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "url":     f"https://www.youtube.com/watch?v={item['id']['videoId']}"
            })
        return videos
    except Exception as e:
        print(f"  Erro canal {channel_id}: {e}")
        return []

# ============================================================
# TRANSCRIPT: HELPER FUNCTIONS (from your working app)
# ============================================================
def _clean_text(raw_text):
    text = html_lib.unescape(raw_text or "")
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def _join_segments(segments):
    pieces = []
    for seg in segments:
        text = _clean_text(seg.get("text", ""))
        if text:
            pieces.append(text)
    return " ".join(pieces).strip()

def _choose_caption_tracks(caption_dict):
    if not caption_dict:
        return []
    ordered = []
    seen = set()

    def push(track):
        if not track:
            return
        track_key = track.get("url") or id(track)
        if track_key not in seen:
            ordered.append(track)
            seen.add(track_key)

    for wanted in PREFERRED_LANGUAGES:
        for key, tracks in caption_dict.items():
            if key.lower() == wanted.lower():
                for track in tracks:
                    push(track)

    for wanted in PREFERRED_LANGUAGES:
        for key, tracks in caption_dict.items():
            if key.lower().startswith(wanted.lower().split("-")[0]):
                for track in tracks:
                    push(track)

    for tracks in caption_dict.values():
        for track in tracks:
            push(track)

    return ordered

def _parse_caption_payload(payload, ext_hint):
    ext = (ext_hint or "").lower()
    stripped_payload = payload.lstrip()

    if ext in {"json3", "srv3"} or stripped_payload.startswith("{"):
        data = json.loads(payload)
        events = data.get("events", [])
        pieces = []
        for event in events:
            for seg in event.get("segs", []):
                piece = _clean_text(seg.get("utf8", ""))
                if piece:
                    pieces.append(piece)
        return " ".join(pieces).strip()

    lines = []
    for line in payload.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("WEBVTT"):
            continue
        if "-->" in stripped:
            continue
        if re.fullmatch(r"\d+", stripped):
            continue
        lines.append(_clean_text(stripped))
    return " ".join(lines).strip()

# ============================================================
# TRANSCRIPT: METHOD 1 — youtube-transcript-api
# ============================================================
def _transcript_from_api(video_id):
    if YouTubeTranscriptApi is None:
        return None

    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):
            fetched = api.fetch(video_id, languages=PREFERRED_LANGUAGES)
            if hasattr(fetched, "to_raw_data"):
                raw_segments = fetched.to_raw_data()
            else:
                raw_segments = [
                    {"text": getattr(item, "text", ""), "start": getattr(item, "start", 0), "duration": getattr(item, "duration", 0)}
                    for item in fetched
                ]
            text = _join_segments(raw_segments)
            return text if text else None
        elif hasattr(YouTubeTranscriptApi, "get_transcript"):
            raw_segments = YouTubeTranscriptApi.get_transcript(video_id, languages=PREFERRED_LANGUAGES)
            text = _join_segments(raw_segments)
            return text if text else None
    except Exception as e:
        print(f"    youtube-transcript-api falhou: {e}")
    return None

# ============================================================
# TRANSCRIPT: METHOD 2 — yt-dlp (fallback that works on cloud)
# ============================================================
def _transcript_from_ytdlp(video_url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if not info:
            return None

        for source_name, caption_dict in (
            ("manual subtitles", info.get("subtitles") or {}),
            ("auto captions", info.get("automatic_captions") or {}),
        ):
            tracks = _choose_caption_tracks(caption_dict)
            for track in tracks:
                caption_url = track.get("url")
                ext_hint = track.get("ext")
                if not caption_url:
                    continue
                try:
                    with urllib.request.urlopen(caption_url, timeout=15) as response:
                        payload = response.read().decode("utf-8", errors="ignore")
                    text = _parse_caption_payload(payload, ext_hint)
                    if text:
                        print(f"    yt-dlp ({source_name}): {len(text)} chars")
                        return text
                except Exception as exc:
                    print(f"    yt-dlp caption download falhou: {exc}")

    except Exception as e:
        print(f"    yt-dlp falhou: {e}")

    return None

# ============================================================
# TRANSCRIPT: COMBINED (try API first, then yt-dlp)
# ============================================================
def get_transcript(video):
    video_id = video["id"]
    video_url = video["url"]

    # Method 1: youtube-transcript-api
    text = _transcript_from_api(video_id)
    if text:
        print(f"    Fonte: youtube-transcript-api ({len(text)} chars)")
        return text[:MAX_TRANSCRIPT_CHARS]

    # Method 2: yt-dlp fallback
    text = _transcript_from_ytdlp(video_url)
    if text:
        return text[:MAX_TRANSCRIPT_CHARS]

    print(f"    Sem transcricao disponivel")
    return None

# ============================================================
# CLAUDE: SUMMARIZE + RECOMMEND
# ============================================================
def summarize(video, transcript, prompt):
    content = f"TRANSCRICAO:\n{transcript}" if transcript else ""
    full_prompt = (
        f"{prompt}\n\n"
        f"TITULO: {video['title']}\n"
        f"CANAL: {video['channel']}\n\n"
        f"{content}\n\n"
        f"Responda APENAS com JSON valido (sem markdown, sem backticks, sem texto extra):\n"
        f'{{"resumo": "2-3 frases concisas.", '
        f'"pontos": ["Ponto 1", "Ponto 2", "Ponto 3"], '
        f'"recomendacao": "IMPERDIVEL ou VALE ASSISTIR ou INFORMATIVO", '
        f'"motivo": "Uma frase curta explicando a recomendacao."}}'
    )
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": full_prompt}]
        )
        text = msg.content[0].text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  Erro Claude: {e}")
        if "rate_limit" in str(e).lower():
            print("  Rate limit — aguardando 60s...")
            time.sleep(60)
            try:
                msg = client.messages.create(
                    model=MODEL, max_tokens=1000,
                    messages=[{"role": "user", "content": full_prompt}]
                )
                text = msg.content[0].text.replace("```json", "").replace("```", "").strip()
                return json.loads(text)
            except Exception:
                pass
        return None

def summarize_without_transcript(video, prompt):
    try:
        url = (
            f"https://www.googleapis.com/youtube/v3/videos"
            f"?key={YOUTUBE_API_KEY}&id={video['id']}"
            f"&part=snippet,contentDetails"
        )
        res = requests.get(url, timeout=10).json()
        description, duration = "", ""
        if res.get("items"):
            description = res["items"][0]["snippet"].get("description", "")[:2000]
            duration = res["items"][0].get("contentDetails", {}).get("duration", "")

        full_prompt = (
            f"Sem transcricao. Resuma baseado no titulo e descricao.\n\n"
            f"{prompt}\n\n"
            f"TITULO: {video['title']}\nCANAL: {video['channel']}\nDURACAO: {duration}\n"
            f"DESCRICAO:\n{description}\n\n"
            f"Responda APENAS com JSON valido (sem markdown, sem backticks, sem texto extra):\n"
            f'{{"resumo": "2-3 frases concisas.", '
            f'"pontos": ["Ponto 1", "Ponto 2", "Ponto 3"], '
            f'"recomendacao": "IMPERDIVEL ou VALE ASSISTIR ou INFORMATIVO", '
            f'"motivo": "Uma frase curta explicando a recomendacao."}}'
        )
        msg = client.messages.create(
            model=MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": full_prompt}]
        )
        text = msg.content[0].text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  Erro fallback: {e}")
        return None

# ============================================================
# BUILD HTML EMAIL
# ============================================================
def build_html(summaries, skipped):
    date_str = datetime.now().strftime("%d/%m/%Y")
    total = len(summaries) + len(skipped)

    order = {"IMPERDIVEL": 0, "VALE ASSISTIR": 1, "INFORMATIVO": 2}
    summaries.sort(key=lambda s: order.get(s.get("recomendacao", "INFORMATIVO"), 2))

    cards = ""
    for s in summaries:
        rec = s.get("recomendacao", "INFORMATIVO")
        motivo = s.get("motivo", "")
        resumo = s.get("resumo", "")
        pontos = s.get("pontos", [])
        has_transcript = s.get("has_transcript", True)

        if "IMPERDIVEL" in rec.upper():
            badge_bg, badge_border, badge_text = "#FFF8E1", "#FFB300", "#F57F17"
            badge_label = "IMPERDIVEL"
        elif "VALE" in rec.upper():
            badge_bg, badge_border, badge_text = "#E8F5E9", "#66BB6A", "#2E7D32"
            badge_label = "VALE ASSISTIR"
        else:
            badge_bg, badge_border, badge_text = "#F5F5F5", "#BDBDBD", "#616161"
            badge_label = "INFORMATIVO"

        source_tag = ""
        if not has_transcript:
            source_tag = ' <span style="background:#FFF3E0;padding:1px 6px;border-radius:4px;font-size:10px;color:#E65100">via descricao</span>'

        points_html = ""
        if pontos:
            points_html = "".join([f"<li style='color:#555;font-size:13px;margin:2px 0'>{p}</li>" for p in pontos[:4]])
            points_html = f"<ul style='margin:6px 0 0;padding-left:16px'>{points_html}</ul>"

        cards += f"""<div style="background:#fff;border-radius:8px;padding:16px;margin-bottom:12px;border-left:4px solid {badge_border}">
  <div style="margin-bottom:4px"><span style="color:#999;font-size:11px">{s['channel']}{source_tag}</span>
    <span style="float:right;background:{badge_bg};color:{badge_text};padding:2px 8px;border-radius:8px;font-size:10px;font-weight:bold">{badge_label}</span></div>
  <a href="{s['url']}" style="color:#1565C0;font-size:15px;font-weight:600;text-decoration:none">{s['title']}</a>
  <p style="color:#333;font-size:13px;line-height:1.5;margin:6px 0 2px">{resumo}</p>
  {points_html}
  <p style="color:#888;font-size:11px;font-style:italic;margin:4px 0 0">{motivo}</p>
</div>"""

    skipped_html = ""
    if skipped:
        items = "".join([
            f'<li style="margin:2px 0;font-size:12px"><a href="{v["url"]}" style="color:#1565C0">{v["title"]}</a> <span style="color:#bbb">({v["channel"]})</span></li>'
            for v in skipped
        ])
        skipped_html = f"""<div style="background:#FAFAFA;border-radius:8px;padding:12px 16px;margin-bottom:12px">
  <p style="color:#bbb;font-size:11px;margin:0 0 4px;font-weight:600">Sem resumo:</p>
  <ul style="margin:0;padding-left:16px">{items}</ul>
</div>"""

    no_content = '<p style="color:#888;font-size:14px;text-align:center;padding:24px 0">Nenhum video novo nas ultimas 48h.</p>'
    content = (cards + skipped_html) if (cards or skipped_html) else no_content

    return f"""<html><body style="margin:0;padding:0;background:#ECECEC">
<div style="max-width:600px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <div style="background:#1565C0;padding:16px 20px;border-radius:8px 8px 0 0">
    <h1 style="color:#fff;margin:0;font-size:18px;font-weight:600">Resumos YouTube</h1>
    <p style="color:#BBDEFB;margin:2px 0 0;font-size:12px">{date_str} | {len(summaries)} resumos de {total} videos</p>
  </div>
  <div style="padding:12px">{content}</div>
  <p style="text-align:center;color:#ccc;font-size:10px;padding:8px">GitHub Actions + Claude + yt-dlp</p>
</div></body></html>"""

# ============================================================
# SEND EMAIL
# ============================================================
def send_email(html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Resumos YouTube - {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())

# ============================================================
# MAIN
# ============================================================
def main():
    with open("channels.txt") as f:
        channel_ids = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    with open("prompt.txt") as f:
        prompt = f.read().strip()

    all_videos = []
    for cid in channel_ids:
        videos = get_channel_videos(cid)
        all_videos.extend(videos)
        print(f"Canal {cid}: {len(videos)} videos")

    print(f"\nTotal: {len(all_videos)} videos")

    if not all_videos:
        html = build_html([], [])
        send_email(html)
        print("Nenhum video — email enviado.")
        return

    summaries, skipped = [], []

    for i, video in enumerate(all_videos):
        print(f"\n[{i+1}/{len(all_videos)}] {video['title']}")
        transcript = get_transcript(video)

        if transcript:
            result = summarize(video, transcript, prompt)
        else:
            result = summarize_without_transcript(video, prompt)

        if result:
            summaries.append({
                **video,
                "resumo": result.get("resumo", ""),
                "pontos": result.get("pontos", []),
                "recomendacao": result.get("recomendacao", "INFORMATIVO"),
                "motivo": result.get("motivo", ""),
                "has_transcript": transcript is not None
            })
        else:
            skipped.append(video)

        if i < len(all_videos) - 1:
            time.sleep(5)

    print(f"\nResumos: {len(summaries)} | Sem resumo: {len(skipped)}")
    html = build_html(summaries, skipped)
    send_email(html)
    print("Email enviado!")

if __name__ == "__main__":
    main()
