"""
Toolbox: stand-alone tools used alongside the pipeline during a case.

Each tool lives in its own sub-package and runs on this machine only, against a
case folder (never the code folder). All of them are served by the local web
app (`dsar serve`, dsar.webapp) under /toolbox/<key>/; see docs/TOOLBOX.md.

  mailviewer  browse a PST or MBOX: folders, messages, attachments, search, Excel export
  teams       Teams chats in a PST: conversations, participants, transcript to PDF
  redaction   mark up documents for release and export redacted copies (also `dsar markup`)

Shared code: dsar.toolbox.mailbox (PST / MBOX reading, which mailboxes may be opened).
"""
import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    key: str            # URL segment: /toolbox/<key>/
    name: str
    description: str
    module: str         # module with a `bp` Flask blueprint

    @property
    def blueprint_name(self) -> str:
        return f"tool_{self.key}"

    @property
    def endpoint(self) -> str:
        return f"{self.blueprint_name}.index"

    def blueprint(self):
        return importlib.import_module(self.module).bp


TOOLS = [
    Tool("mail", "Mail viewer", "Browse a PST or MBOX: folders, messages, attachments and search. "
         "Save messages and attachments into the case, or export a mailbox to Excel.",
         "dsar.toolbox.mailviewer.web"),
    Tool("teams", "Teams viewer", "Teams chats in a PST: every conversation and participant, read in the "
         "browser, and saved as a PDF transcript.", "dsar.toolbox.teams.web"),
    Tool("redaction", "Redaction", "Decide, passage by passage, what is released and what is redacted, "
         "and export a redacted copy with a redaction log.", "dsar.toolbox.redaction.web"),
]
