"""Mailbox reader and Teams chat extraction, on pypff-shaped fakes."""
from dsar.toolbox.mailbox import reader
from dsar.toolbox.mailviewer import export
from dsar.toolbox.teams import chats, pdf
from tests.pst_fakes import GROUP, ONE_TO_ONE, sample_mailbox


def test_folder_tree_addresses_folders_by_index_so_same_names_differ():
    pff = sample_mailbox()
    tree = reader.folder_tree(pff.get_root_folder())
    inbox = tree["children"][0]["children"][0]
    assert inbox["name"] == "Inbox" and inbox["ix"] == "0.0" and inbox["count"] == 2
    a, b = inbox["children"]
    assert a["name"] == b["name"] == "Archive" and (a["ix"], b["ix"]) == ("0.0.0", "0.0.1")
    assert reader.get_folder(pff, "0.0.1").number_of_sub_messages == 1
    assert reader.get_folder(pff, "0.0.9") is None
    assert reader.get_folder(pff, "x") is None
    assert reader.folder_name_path(pff, "0.0.1") == "Top of Information Store/Inbox/Archive"


def test_message_detail_uses_display_recipients_when_there_are_no_headers():
    pff = sample_mailbox()
    inbox = reader.get_folder(pff, "0.0")
    first = reader.message_detail(inbox, 0)
    assert first["to"] == "Sam Sample <sam@example.test>"
    assert first["sender"] == "Alex Example <alex@example.test>"
    assert first["plain_body"].startswith("Hello Sam")
    assert first["attachments"] == [{"name": "plan.txt", "size": 8}]
    sent = reader.message_detail(inbox, 1)
    assert sent["to"] == "Alex Example" and sent["cc"] == "Jo Third"
    assert "<b>Alex</b>" in sent["html_body"]


def test_list_search_and_attachments():
    pff = sample_mailbox()
    inbox = reader.get_folder(pff, "0.0")
    page = reader.list_messages(inbox, 1, 1)
    assert page["total"] == 2 and [m["subject"] for m in page["messages"]] == ["Quarterly plan"]
    hits = reader.search(pff, "jo third")                     # matched on a display-name recipient
    assert {(h["folder"], h["index"]) for h in hits} >= {("0.0", 1), ("0.0.1", 0)}
    assert reader.attachment_data(inbox, 0, 0) == ("plan.txt", b"the plan")
    eml = reader.message_to_eml(inbox.get_sub_message(0))
    assert b"Subject: Quarterly plan" in eml and b"plan.txt" in eml


def test_excel_export_has_a_row_per_message():
    from io import BytesIO

    from openpyxl import load_workbook
    data, n = export.mailbox_xlsx(sample_mailbox(), "x.pst", "ab" * 32)
    assert n == 9
    ws = load_workbook(BytesIO(data))["Messages"]
    rows = list(ws.values)
    assert rows[0] == tuple(export.COLUMNS)
    assert any(r[6] == "Re: Quarterly plan" and r[3] == "Alex Example" for r in rows[1:])


def test_teams_conversations_participants_and_messages():
    idx = chats.build_index(sample_mailbox())
    assert len(idx) == 6
    convs = {c["thread"]: c for c in chats.conversations(idx)}
    assert len(convs) == 3
    one, group, loose = convs[ONE_TO_ONE], convs[GROUP], convs[""]
    assert (one["kind"], one["count"]) == ("1:1", 3)
    # A group chat whose membership changed is still one conversation, with everyone who took part.
    assert (group["kind"], group["count"]) == ("group", 2)
    assert set(group["participants"]) == {"Alex Example", "Sam Sample", "Jo Third", "New Person"}
    # No thread ID: grouped by the exact set of participants.
    assert (loose["kind"], loose["count"], set(loose["participants"])) == ("1:1", 1, {"Jo Third", "Sam Sample"})
    people = {p["name"]: p for p in chats.participants(idx)}
    assert people["Sam Sample"]["sent"] == 2 and people["Sam Sample"]["partners"] == 2
    key = chats.find_between(idx, "sam sample", "ALEX EXAMPLE")
    assert key == one["key"]
    msgs, meta = chats.messages(sample_mailbox(), idx, key)
    assert [m["text"] for m in msgs] == ["Earlier message", "Morning Alex", "Morning Sam & all"]   # time order
    assert meta["count"] == 3 and meta["first"] < meta["last"]
    assert chats.find_between(idx, "Sam Sample", "Sam Sample") is None


def test_teams_pdf_is_a_pdf():
    idx = chats.build_index(sample_mailbox())
    msgs, meta = chats.messages(sample_mailbox(), idx, chats.find_between(idx, "Sam Sample", "Alex Example"))
    data = pdf.conversation_pdf(msgs, meta, source="x.pst", source_sha256="ab" * 32, reviewer="Test Reviewer")
    assert data.startswith(b"%PDF") and len(data) > 1000
