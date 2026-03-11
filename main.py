import os
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from anthropic import Anthropic
from youtube_transcript_api import YouTubeTranscriptApi

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUTUBE_API_KEY   = os.environ["YOUTUBE_API_KEY"]
EMAIL_ADDRESS     = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT   = os.environ["EMAIL_RECIPIENT"]

client = Anthropic(api_key=ANTHROPIC_API_KEY)

def get_channel_videos(channel_id):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?key={YOUTUBE_API_KEY}&channelId={channel_id}"
        f"&part=snippet&type=video&order=date&publishedAfter={yesterday}"
    )
    res = requests.get(url).json()
    videos = []
    for item in res.get("items", []):
        videos.append({
            "id":      item["id"]["videoId"],
            "title":   item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
            "url":     f"https://youtube.com/watch?v={item['id']['videoId']}"
        })
    return videos

def get_transcript(video_id):
    try:
        parts = YouTubeTranscriptApi.get_transcript(video_id, languages=["pt", "en"])
        return " ".join([p["text"] for p in parts])
    except Exception:
        return None

def summarize(transcript, prompt):
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"{prompt}\n\nTRANSCRIÇÃO:\n{transcript[:12000]}"
        }]
    )
    return message.content[0].text

def build_html(summaries):
    date_str = datetime.now().strftime("%d/%m/%Y")
    cards = ""
    for s in summaries:
        cards += f"""
        <div style="background:#fff;border-radius:12px;padding:24px;margin-bottom:24px;box-shadow:0 2px 8px rgba(0,0,0,0.08)">
          <p style="color:#888;font-size:13px;margin:0 0 4px">{s['channel']}</p>
          <h2 style="margin:0 0 12px;font-size:18px">
            <a href="{s['url']}" style="color:#1a1a1a;text-decoration:none">{s['title']}</a>
          </h2>
          <div style="color:#333;line-height:1.7;font-size:15px">{s['summary'].replace(chr(10), '<br>')}</div>
        </div>"""
    return f"""
    <html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:32px;max-width:700px;margin:auto">
      <h1 style="color:#1a1a1a;border-bottom:3px solid #ff0000;padding-bottom:12px">
        📺 Resumos de YouTube — {date_str}
      </h1>
      {cards if cards else '<p>Nenhum vídeo novo hoje.</p>'}
    </body></html>"""

def send_email(html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📺 Resumos YouTube — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp-mail.outlook.com", 587) as server:
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())

def main():
    with open("channels.txt") as f:
        channel_ids = [l.strip() for l in f if l.strip()]
    with open("prompt.txt") as f:
        prompt = f.read().strip()

    all_videos = []
    for cid in channel_ids:
        all_videos.extend(get_channel_videos(cid))

    print(f"Vídeos encontrados: {len(all_videos)}")

    summaries = []
    for video in all_videos:
        print(f"Processando: {video['title']}")
        transcript = get_transcript(video["id"])
        if not transcript:
            print(f"  ⚠ Sem transcrição disponível")
            continue
        summary = summarize(transcript, prompt)
        summaries.append({**video, "summary": summary})

    html = build_html(summaries)
    send_email(html)
    print("✅ Email enviado com sucesso!")

if __name__ == "__main__":
    main()