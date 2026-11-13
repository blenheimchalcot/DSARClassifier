"""Render a Teams conversation as a chat-transcript PDF (reportlab, local only)."""
import html
import io
from datetime import datetime, timezone

from dsar import __version__
from dsar.toolbox.teams.chats import norm

_TINTS = [("#EEF3F8", "#1F4E79"), ("#FBF1E6", "#7A3E00"), ("#EEF6EE", "#2E6B30"),
          ("#F5EEF8", "#6A2C82"), ("#EEF7F7", "#1F6B70"), ("#F7F3E8", "#6B5A1F")]


def _ts(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


def _day(iso: str) -> str:
    if not iso:
        return "Undated"
    try:
        return datetime.fromisoformat(iso).strftime("%A %d %B %Y")
    except ValueError:
        return iso[:10]


def conversation_pdf(messages: list[dict], meta: dict, *, source: str, source_sha256: str | None,
                     reviewer: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    people = meta.get("participants") or []
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], fontSize=17, spaceAfter=4)
    sub = ParagraphStyle("S", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#555555"),
                         spaceAfter=2, leading=11)
    day = ParagraphStyle("D", parent=styles["Normal"], fontSize=9, alignment=1, textColor=colors.HexColor("#888888"),
                         spaceBefore=10, spaceAfter=6)

    tints = {}
    for n, p in enumerate(people):
        tints[norm(p)] = _TINTS[n % len(_TINTS)]

    def bubble(bg):
        return ParagraphStyle("B" + bg, parent=styles["Normal"], fontSize=10, leading=14,
                              backColor=colors.HexColor(bg), borderPadding=(6, 8, 6, 8),
                              leftIndent=2, rightIndent=2, spaceBefore=2, spaceAfter=6)

    e = html.escape
    buf = io.BytesIO()
    names = " &amp; ".join(e(p) for p in people) or "(no participants)"
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"Teams conversation: {', '.join(people)}",
                            author=reviewer, creator=f"dsar {__version__}")
    kind = {"1:1": "1:1 chat", "group": "Group or meeting chat", "channel": "Channel"}.get(meta.get("kind"), "Chat")
    story = [Paragraph("Teams conversation", title),
             Paragraph(f"<b>{kind}</b> · {len(people)} participant(s): {names}", sub),
             Paragraph(f"{meta.get('count', 0)} messages · {_day(meta.get('first', ''))} to {_day(meta.get('last', ''))}", sub),
             Paragraph(f"Teams thread ID: {e(meta['thread'])}" if meta.get("thread") else
                       "No Teams thread ID: messages grouped by their exact set of participants", sub),
             Paragraph(f"Source: {e(source)}" + (f"<br/>Source SHA-256: {source_sha256}" if source_sha256 else
                                                 "<br/>Source not hash-checked (free-standing mode)"), sub),
             Paragraph(f"Produced {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by {e(reviewer)} "
                       f"with dsar {__version__}, on this machine. Every message in the conversation is shown, "
                       "as stored, in time order.", sub),
             Spacer(1, 4), HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cccccc")), Spacer(1, 6)]
    current = None
    for m in messages:
        d = _day(m["date"])
        if d != current:
            current = d
            story.append(Paragraph(f"— {e(d)} —", day))
        bg, ink = tints.get(norm(m["sender"]), ("#F2F2F2", "#333333"))
        head = (f"<font color='{ink}'><b>{e(m['sender'])}</b></font> &nbsp; "
                f"<font size=8 color='#999999'>{e(_ts(m['date']))}</font>")
        text = m["text"] or "(no text: may be an attachment, image, call or reaction)"
        body = "<br/>".join(e(line) for line in text.split("\n"))
        story.append(Paragraph(f"{head}<br/>{body}", bubble(bg)))
    if not messages:
        story.append(Paragraph("No messages.", styles["Normal"]))
    doc.build(story)
    return buf.getvalue()
