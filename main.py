import os
import smtplib
import requests
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from anthropic import Anthropic
from youtube_transcript_api import YouTubeTranscriptApi

# ============================================================
# CONFIGURAÇÃO
# ============================================================
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUTUBE_API_KEY   = os.environ["YOUTUBE_API_KEY"]
EMAIL_ADDRESS     = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT   = os.environ["EMAIL_RECIPIENT"]

# Usar Haiku para custo menor e rate limits mais altos
MODEL = "claude-haiku-4-5-20251001"
# Transcrição: até 100k caracteres (~2h de podcast)
MAX_TRANSCRIPT_CHARS = 100000
# Janela de busca: 48h para não perder vídeos publicados tarde
LOOKBACK_HOURS = 48

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ============================================================
# BUSCAR VÍDEOS RECENTES DE UM CANAL
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
# BUSCAR TRANSCRIÇÃO (com fallback de idiomas)
# ============================================================
def get_transcript(video_id):
    try:
        parts = YouTubeTranscriptApi.get_transcript(video_id, languages=["pt", "en", "es", "pt-BR"])
        text = " ".join([p["text"] for p in parts])
        return text[:MAX_TRANSCRIPT_CHARS]
    except Exception:
        # Tentar auto-generated
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            for transcript in transcript_list:
                if transcript.is_generated:
                    parts = transcript.fetch()
                    text = " ".join([p["text"] for p in parts])
                    return text[:MAX_TRANSCRIPT_CHARS]
        except Exception:
            pass
        return None

# ============================================================
# RESUMIR COM CLAUDE
# ============================================================
def summarize(video, transcript, prompt):
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": f"{prompt}\n\nTITULO: {video['title']}\nCANAL: {video['channel']}\n\nTRANSCRICAO:\n{transcript}"
            }]
        )
        return message.content[0].text
    except Exception as e:
        print(f"  Erro ao resumir com Claude: {e}")
        # Se rate limit, esperar e tentar de novo
        if "rate_limit" in str(e).lower():
            print("  Aguardando 60s por rate limit...")
            time.sleep(60)
            try:
                message = client.messages.create(
                    model=MODEL,
                    max_tokens=1500,
                    messages=[{
                        "role": "user",
                        "content": f"{prompt}\n\nTITULO: {video['title']}\nCANAL: {video['channel']}\n\nTRANSCRICAO:\n{transcript}"
                    }]
                )
                return message.content[0].text
            except Exception as e2:
                print(f"  Falhou de novo: {e2}")
        return None

# ============================================================
# RESUMIR SEM TRANSCRIÇÃO (baseado em título + descrição)
# ============================================================
def summarize_without_transcript(video, prompt):
    try:
        # Buscar detalhes do vídeo para pegar descrição completa
        url = (
            f"https://www.googleapis.com/youtube/v3/videos"
            f"?key={YOUTUBE_API_KEY}&id={video['id']}"
            f"&part=snippet,contentDetails"
        )
        res = requests.get(url, timeout=10).json()
        description = ""
        duration = ""
        if res.get("items"):
            description = res["items"][0]["snippet"].get("description", "")[:2000]
            duration = res["items"][0].get("contentDetails", {}).get("duration", "")

        message = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": (
                    f"Nao foi possivel obter a transcricao deste video. "
                    f"Faca um resumo baseado no titulo e descricao.\n\n"
                    f"TITULO: {video['title']}\n"
                    f"CANAL: {video['channel']}\n"
                    f"DURACAO: {duration}\n"
                    f"DESCRICAO:\n{description}\n\n"
                    f"Instrucoes adicionais: {prompt}"
                )
            }]
        )
        return message.content[0].text
    except Exception as e:
        print(f"  Erro ao resumir sem transcricao: {e}")
        return None

# ============================================================
# MONTAR EMAIL HTML
# ============================================================
def build_html(summaries, skipped_videos):
    date_str = datetime.now().strftime("%d/%m/%Y")

    # Separar por categoria baseado no canal
    cards = ""
    for s in summaries:
        # Badge de classificação
        rating = s.get("rating", "")
        badge_color = "#FFF8E1" if "IMPERDIVEL" in rating.upper() else "#E8F5E9" if "VALE" in rating.upper() else "#F5F5F5"
        badge_html = f'<span style="background:{badge_color};padding:3px 10px;border-radius:12px;font-size:12px;display:inline-block;margin-bottom:8px">{rating}</span>' if rating else ""
        
        # Indicador se usou transcrição ou não
        source_badge = ""
        if s.get("no_transcript"):
            source_badge = '<span style="background:#FFF3E0;padding:2px 8px;border-radius:8px;font-size:11px;color:#E65100;margin-left:8px">sem transcricao</span>'

        cards += f"""
        <div style="background:#fff;border-radius:12px;padding:24px;margin-bottom:24px;box-shadow:0 2px 8px rgba(0,0,0,0.08)">
          <p style="color:#888;font-size:13px;margin:0 0 4px">{s['channel']}{source_badge}</p>
          {badge_html}
          <h2 style="margin:0 0 12px;font-size:18px">
            <a href="{s['url']}" style="color:#1565C0;text-decoration:none">{s['title']}</a>
          </h2>
          <div style="color:#333;line-height:1.7;font-size:15px">{s['summary'].replace(chr(10), '<br>')}</div>
        </div>"""

    # Seção de vídeos sem transcrição que foram pulados completamente
    skipped_html = ""
    if skipped_videos:
        skipped_items = "".join([
            f'<li style="margin:4px 0"><a href="{v["url"]}" style="color:#1565C0">{v["title"]}</a> ({v["channel"]})</li>'
            for v in skipped_videos
        ])
        skipped_html = f"""
        <div style="background:#fff;border-radius:12px;padding:20px;margin-bottom:24px;border-left:4px solid #FFB300">
          <h3 style="color:#F57F17;margin:0 0 8px;font-size:15px">Videos encontrados mas sem resumo disponivel:</h3>
          <ul style="margin:0;padding-left:20px;font-size:14px">{skipped_items}</ul>
        </div>"""

    # Contagem
    total = len(summaries) + len(skipped_videos)
    resumidos = len(summaries)

    content = cards + skipped_html if (cards or skipped_html) else '<p style="color:#666;font-size:16px">Nenhum video novo encontrado nas ultimas 48 horas.</p>'

    return f"""
    <html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f5f5f5;padding:32px;max-width:700px;margin:auto">
      <div style="background:#1565C0;padding:24px 28px;border-radius:12px 12px 0 0">
        <h1 style="color:#fff;margin:0;font-size:24px">Resumos YouTube</h1>
        <p style="color:#bbdefb;margin:6px 0 0;font-size:14px">{date_str} &middot; {resumidos} resumos de {total} videos encontrados</p>
      </div>
      <div style="background:#f5f5f5;padding:24px 0">
        {content}
      </div>
      <div style="text-align:center;padding:16px;color:#999;font-size:12px">
        Gerado automaticamente via GitHub Actions + Claude
      </div>
    </body></html>"""

# ============================================================
# ENVIAR EMAIL
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

    # Buscar vídeos de todos os canais
    all_videos = []
    for cid in channel_ids:
        videos = get_channel_videos(cid)
        all_videos.extend(videos)
        print(f"Canal {cid}: {len(videos)} videos")

    print(f"\nTotal de videos encontrados: {len(all_videos)}")

    if not all_videos:
        print("Nenhum video encontrado. Enviando email informativo.")
        html = build_html([], [])
        send_email(html)
        print("Email enviado!")
        return

    summaries = []
    skipped = []

    for i, video in enumerate(all_videos):
        print(f"\n[{i+1}/{len(all_videos)}] {video['title']}")

        # Tentar buscar transcrição
        transcript = get_transcript(video["id"])

        if transcript:
            print(f"  Transcricao: {len(transcript)} caracteres")
            summary = summarize(video, transcript, prompt)
            if summary:
                summaries.append({**video, "summary": summary, "rating": "", "no_transcript": False})
            else:
                skipped.append(video)
        else:
            print(f"  Sem transcricao — tentando resumir via titulo/descricao...")
            summary = summarize_without_transcript(video, prompt)
            if summary:
                summaries.append({**video, "summary": summary, "rating": "", "no_transcript": True})
            else:
                skipped.append(video)

        # Pausa entre requests para rate limits
        if i < len(all_videos) - 1:
            time.sleep(5)

    print(f"\nResumos gerados: {len(summaries)}")
    print(f"Videos sem resumo: {len(skipped)}")

    html = build_html(summaries, skipped)
    send_email(html)
    print("Email enviado com sucesso!")

if __name__ == "__main__":
    main()
