"""Recipients for messages without transport headers (sent items, drafts)."""
from pathlib import Path

from dsar.chunking.pst import _to_raw


class _Entry:
    def __init__(self, entry_type: int, value: str | None):
        self.entry_type = entry_type
        self._value = value

    def get_data_as_string(self):
        if self._value is None:
            raise OSError("corrupt entry")
        return self._value


class _RecordSet:
    def __init__(self, entries):
        self._entries = entries
        self.number_of_entries = len(entries)

    def get_entry(self, i):
        return self._entries[i]


class _Message:
    def __init__(self, headers: str, entries):
        self.identifier = 42
        self.subject = "Interview"
        self.transport_headers = headers
        self.plain_text_body = "Hi Alex, see you Tuesday."
        self.sender_name = "Sam Sender"
        self.client_submit_time = None
        self._record_sets = [_RecordSet(entries)]
        self.number_of_record_sets = 1

    def get_record_set(self, i):
        return self._record_sets[i]


def test_display_recipients_used_when_no_transport_headers():
    msg = _Message("", [
        _Entry(0x0E04, "Alex Example"),
        _Entry(0x0E03, "Pat Person; Chris Colleague"),
        _Entry(0x0E02, "Bea Blind"),
    ])
    raw = _to_raw(Path("/tmp/case.pst"), msg)
    assert raw.to == "Alex Example"
    assert raw.cc == "Pat Person; Chris Colleague"
    assert raw.bcc == "Bea Blind"


def test_transport_headers_win_over_display_recipients():
    msg = _Message("To: alex@example.com\nCc: pat@example.com\n", [
        _Entry(0x0E04, "Alex Example"),
        _Entry(0x0E03, "Pat Person"),
    ])
    raw = _to_raw(Path("/tmp/case.pst"), msg)
    assert raw.to == "alex@example.com"
    assert raw.cc == "pat@example.com"


def test_corrupt_display_entry_is_skipped():
    msg = _Message("", [_Entry(0x0E04, None), _Entry(0x0E03, "Pat Person")])
    raw = _to_raw(Path("/tmp/case.pst"), msg)
    assert raw.to is None
    assert raw.cc == "Pat Person"
