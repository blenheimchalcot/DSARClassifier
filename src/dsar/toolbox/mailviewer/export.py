"""Export every message in a mailbox to an Excel workbook (one row per message)."""
import io

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from dsar.toolbox.mailbox import reader

COLUMNS = ["Folder", "Date", "From", "To", "Cc", "Bcc", "Subject", "Message class", "Attachments", "Body"]
CELL_LIMIT = 32000      # Excel's cell limit is 32,767 characters


def _clean(value) -> str:
    return ILLEGAL_CHARACTERS_RE.sub(" ", str(value or ""))


def mailbox_xlsx(pff, source_label: str = "", sha256: str | None = None) -> tuple[bytes, int]:
    """(workbook bytes, message count). Bodies longer than a cell allows are cut and marked."""
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Messages")
    ws.append(COLUMNS)
    folder_names: dict[str, str] = {}
    n = 0
    for ix, _folder, _i, msg in reader.walk(pff):
        if ix not in folder_names:
            folder_names[ix] = reader.folder_name_path(pff, ix)
        rcpt = reader.recipients(msg)
        body = reader.bodies(msg)
        text = body["plain"] or body["rtf_text"]
        if not text and body["html"]:
            from bs4 import BeautifulSoup
            text = BeautifulSoup(body["html"], "html.parser").get_text("\n").strip()
        if len(text) > CELL_LIMIT:
            text = text[:CELL_LIMIT] + " [cut: body longer than an Excel cell]"
        try:
            subject = reader.safe_str(msg.subject)
        except Exception:
            subject = ""
        try:
            n_att = msg.number_of_attachments
        except Exception:
            n_att = 0
        ws.append([_clean(v) for v in (folder_names[ix], reader.message_date(msg), reader.sender_label(msg),
                                        rcpt["to"], rcpt["cc"], rcpt["bcc"], subject, reader.message_class(msg),
                                        n_att, text)])
        n += 1
    about = wb.create_sheet("About")
    about.append(["Source", _clean(source_label)])
    about.append(["Source SHA-256", sha256 or "(not checked: free-standing mode)"])
    about.append(["Messages", n])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), n
