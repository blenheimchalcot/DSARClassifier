"""
Shared mailbox access for the toolbox: PST (pypff) and MBOX (stdlib) behind one interface.

  reader   folders, message lists, message detail, attachments, search
  mbox     MBOX adapters shaped like pypff objects, so every reader function works on both
  store    which mailboxes may be opened (the case manifest, or any path when free-standing),
           hash checks before opening, and one lock per open file (pypff is not thread-safe)

Ported from the stand-alone PSTViewer project. Everything runs locally.
"""
