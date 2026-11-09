import re
from dataclasses import dataclass

from dsar.config import SubjectProfile


@dataclass
class AliasMatcher:
    pattern: re.Pattern[str]

    def matches(self, *texts: str | None) -> bool:
        for t in texts:
            if t and self.pattern.search(t):
                return True
        return False


def build_matcher(subject: SubjectProfile) -> AliasMatcher:
    """
    Case-insensitive regex with word boundaries around every alias.
    Short aliases like "EB" rely on \b on both sides to avoid matching
    inside other words ("webinar", "ebay", etc).
    """
    parts: list[str] = []
    for alias in subject.aliases:
        parts.append(rf"\b{re.escape(alias)}\b")
    for email in subject.emails:
        # Emails don't need word boundaries; re.escape handles the dot.
        parts.append(re.escape(email))
    if not parts:
        raise ValueError("Subject profile has no aliases or emails")
    pattern = re.compile("|".join(parts), flags=re.IGNORECASE)
    return AliasMatcher(pattern=pattern)
