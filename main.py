import os
import smtplib
import requests
import time
import json
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from anthropic import Anthropic
from youtube_transcript_api import YouTubeTranscriptApi

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
                "url":     f"https://youtube.com/watch?v={item['id']['videoId']}"
            })
        return videos
    except Exception as e:
        print(f"  Erro ao buscar canal {channel_id}: {e}")
        return []

# ============================================================
# YOUTUBE: FETCH TRANSCRIPT (new v1.x API syntax)
# ============================================================
def get_transcript(video_id):
    try:
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id, languages=["pt", "en", "es", "pt-BR"])
        text = " ".join([s.text for s in transcript.snippets])
        return text[:MAX_TRANSCRIPT_CHARS]
    except Exception as e:
        print(f"  Transcricao falhou: {e}")
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
        f"Responda APENAS com um JSON valido (sem markdown, sem backticks, sem texto extra):\n"
        f'{{"resumo": "2-3 frases concisas sobre o conteudo.", '
        f'"pontos": ["Ponto 1", "Ponto 2", "Ponto 3"], '
        f'"recomendacao": "IMPERDIVEL ou VALE ASSISTIR ou INFORMATIVO", '
        f'"motivo": "Uma frase explicando a recomendacao."}}'
    )
    try:
        message = client.messages.create(
            model=MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": full_prompt}]
        )
        text = message.content[0].text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  Erro Claude: {e}")
        if "rate_limit" in str(e).lower():
            print("  Rate limit — aguardando 60s...")
            time.sleep(60)
            try:
                message = client.messages.create(
                    model=MODEL, max_tokens=1000,
                    messages=[{"role": "user", "content": full_prompt}]
                )
                text = message.content[0].text.replace("```json", "").replace("```", "").strip()
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
            f"Sem transcricao disponivel. Resuma baseado no titulo e descricao.\n\n"
            f"{prompt}\n\n"
            f"TITULO: {video['title']}\nCANAL: {video['channel']}\nDURACAO: {duration}\n"
            f"DESCRICAO:\n{description}\n\n"
            f"Responda APENAS com um JSON valido (sem markdown, sem backticks, sem texto extra):\n"
            f'{{"resumo": "2-3 frases concisas.", '
            f'"pontos": ["Ponto 1", "Ponto 2", "Ponto 3"], '
            f'"recomendacao": "IMPERDIVEL ou VALE ASSISTIR ou INFORMATIVO", '
            f'"motivo": "Uma frase explicando a recomendacao."}}'
        )
        message = client.messages.create(
            model=MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": full_prompt}]
        )
        text = message.content[0].text.replace("```json", "").replace("```", "").strip()
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
  <p style="text-align:center;color:#ccc;font-size:10px;padding:8px">GitHub Actions + Claude</p>
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
        transcript = get_transcript(video["id"])

        if transcript:
            print(f"  Transcricao: {len(transcript)} chars")
            result = summarize(video, transcript, prompt)
        else:
            print(f"  Sem transcricao — fallback")
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
