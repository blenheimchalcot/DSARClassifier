from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SubjectProfile:
    name: str
    aliases: list[str]
    emails: list[str]


@dataclass
class ModelSettings:
    default: str
    max_tokens: int
    temperature: float
    concurrency: int
    # Digest the model was validated with (`ollama list`, 12+ hex chars). `dsar doctor`
    # fails if the installed model differs, so a silent model update can't change results.
    expected_digest: str | None = None


@dataclass
class ModelCheckSettings:
    # Thresholds for `dsar model-check`; `dsar classify` needs a passing check
    # for the current model digest, prompt and settings.
    min_accuracy: float = 0.8          # share of golden examples scored in their band
    min_format_rate: float = 1.0       # share of responses that parse
    min_chunks_per_minute: float = 2.0 # below this is a WARN (plan the run), not a FAIL
    sample_size: int = 6               # chunks timed for the throughput estimate


@dataclass
class QASettings:
    # Thresholds for `dsar qa classify`.
    max_unparseable_rate: float = 0.02   # responses that are neither a score nor a special return
    rescore_sample: int = 10             # classifications re-scored to measure consistency
    min_rescore_agreement: float = 0.8   # share of re-scores within ±1 of the original
    sample_per_band: int = 10            # per band (0-4, 5-6, 7-10, special) for the human QA workbook


@dataclass
class ChunkingSettings:
    max_chars: int
    overlap_chars: int


@dataclass
class ProvenanceRule:
    # Glob relative to the case input/ dir (fnmatch: `*` also crosses `/`).
    match: str
    label: str


@dataclass
class ClassifyScope:
    # A chunk is classified if (its provenance label is in include_labels OR its
    # path matches include_paths) AND its path matches no exclude_paths.
    # Paths are globs relative to the case input/ dir. Empty scope = everything.
    include_labels: list[str] = field(default_factory=list)
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)

    @property
    def is_unrestricted(self) -> bool:
        return not (self.include_labels or self.include_paths or self.exclude_paths)


@dataclass
class Config:
    subject: SubjectProfile
    model: ModelSettings
    chunking: ChunkingSettings
    provenance: list[ProvenanceRule] = field(default_factory=list)
    classify_scope: ClassifyScope = field(default_factory=ClassifyScope)
    model_check: ModelCheckSettings = field(default_factory=ModelCheckSettings)
    qa: QASettings = field(default_factory=QASettings)


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    return Config(
        subject=SubjectProfile(**raw["subject"]),
        model=ModelSettings(**raw["model"]),
        chunking=ChunkingSettings(**raw["chunking"]),
        provenance=[ProvenanceRule(**r) for r in raw.get("provenance") or []],
        classify_scope=ClassifyScope(**(raw.get("classify_scope") or {})),
        model_check=ModelCheckSettings(**(raw.get("model_check") or {})),
        qa=QASettings(**(raw.get("qa") or {})),
    )
