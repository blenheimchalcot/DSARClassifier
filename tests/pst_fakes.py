"""pypff-shaped fakes for toolbox tests (no real PST needed). Synthetic data only."""
from datetime import datetime


class Entry:
    def __init__(self, entry_type, value):
        self.entry_type = entry_type
        self.data_as_string = value

    def get_data_as_string(self):
        return self.data_as_string


class RecordSet:
    def __init__(self, props: dict):
        self._entries = [Entry(k, v) for k, v in props.items()]

    @property
    def number_of_entries(self):
        return len(self._entries)

    def get_entry(self, i):
        return self._entries[i]


class _Records:
    props: dict = {}

    @property
    def number_of_record_sets(self):
        return 1

    def get_record_set(self, i):
        return RecordSet(self.props)


class Attachment(_Records):
    def __init__(self, name, data):
        self.props = {0x3707: name}
        self._data = data
        self.size = len(data)
        self.number_of_sub_items = 0

    def seek_offset(self, n):
        pass

    def read_buffer(self, n):
        return self._data[:n]


class Message(_Records):
    def __init__(self, subject="", sender="", when=None, headers=None, html=None, plain=None, props=None,
                 attachments=None):
        self.subject = subject
        self.sender_name = sender
        self.delivery_time = when
        self.client_submit_time = None
        self.transport_headers = headers
        self.html_body = html.encode() if isinstance(html, str) else html
        self.plain_text_body = plain.encode() if isinstance(plain, str) else plain
        self.rtf_body = None
        self.props = props or {}
        self._atts = attachments or []

    @property
    def number_of_attachments(self):
        return len(self._atts)

    def get_attachment(self, i):
        return self._atts[i]


class Folder:
    def __init__(self, name, subs=None, msgs=None):
        self.name = name
        self._subs = subs or []
        self._msgs = msgs or []

    @property
    def number_of_sub_folders(self):
        return len(self._subs)

    def get_sub_folder(self, i):
        return self._subs[i]

    @property
    def number_of_sub_messages(self):
        return len(self._msgs)

    def get_sub_message(self, i):
        return self._msgs[i]


class File:
    def __init__(self, root):
        self._root = root
        self.closed = False

    def get_root_folder(self):
        return self._root

    def close(self):
        self.closed = True


TEAMS = "IPM.SkypeTeams.Message"


ONE_TO_ONE = "19:aaaa_bbbb@unq.gbl.spaces"
GROUP = "19:0123abcd@thread.v2"


def teams_msg(sender, to, when, text, thread=None):
    props = {0x001A: TEAMS, 0x0E04: to}
    if thread:
        props[0x366D] = thread
    return Message(subject="", sender=sender, when=when, html=f"<html><body><p>{text}</p></body></html>",
                   props=props)


def sample_mailbox() -> File:
    """Root > Top > [Inbox (2 emails, two folders named 'Archive'), Teams (6 chat messages)].

    Teams: a 1:1 thread (3 messages, out of time order), a group thread whose
    membership changed (2), and one message with no thread ID (grouped by participants).
    """
    d = lambda h: datetime(2026, 3, 2, h, 0, 0)                                    # noqa: E731
    inbox = Folder("Inbox", msgs=[
        Message("Quarterly plan", "Alex Example", d(9),
                headers="From: Alex Example <alex@example.test>\r\nTo: Sam Sample <sam@example.test>\r\n",
                plain="Hello Sam,\nplease see the plan.", props={0x001A: "IPM.Note"},
                attachments=[Attachment("plan.txt", b"the plan")]),
        # A sent item: no transport headers, recipients only in the display properties.
        Message("Re: Quarterly plan", "Sam Sample", d(10), headers=None, html="<p>Thanks <b>Alex</b></p>",
                props={0x001A: "IPM.Note", 0x0E04: "Alex Example", 0x0E03: "Jo Third"}),
    ], subs=[Folder("Archive"), Folder("Archive", msgs=[Message("Old", "Jo Third", d(8), plain="old")])])
    teams = Folder("TeamsMessagesData", msgs=[
        teams_msg("Sam Sample", "Alex Example", d(11), "Morning Alex", ONE_TO_ONE),
        teams_msg("Alex Example", "Sam Sample", d(12), "Morning Sam &amp; all", ONE_TO_ONE),
        teams_msg("Alex Example", "Sam Sample; Jo Third", d(13), "Group hello", GROUP),
        teams_msg("Sam Sample", "Alex Example", d(10), "Earlier message", ONE_TO_ONE),
        teams_msg("Jo Third", "Sam Sample; Alex Example; New Person", d(14), "Welcome New", GROUP),
        teams_msg("Jo Third", "Sam Sample", d(15), "No thread id here"),
    ])
    return File(Folder("", subs=[Folder("Top of Information Store", subs=[inbox, teams])]))
