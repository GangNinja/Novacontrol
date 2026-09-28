"""Reading sources: file → text, or an honest reason why not.

The knowledge engine is local-first and dependency-light, so extraction is a
LADDER rather than a requirement:

* plain text, Markdown and code are read directly, with encoding fallbacks and
  a binary sniff (a NUL byte means "this is not text", not "index this noise");
* PDFs are read with whichever extractor this installation actually has —
  ``pypdf``, ``PyPDF2``, ``PyMuPDF``/``fitz``, ``pdfminer.six`` — and, when none
  of those is installed, with a small standard-library reader that decodes
  Flate-compressed content streams and pulls the text-showing operators out.
  That covers the uncompressed and FlateCommon PDFs a local tool meets most
  often; anything else raises :class:`UnsupportedDocument` with the reason, so
  the source is reported as skipped rather than indexed as garbage.

Nothing here shells out, and nothing here installs anything: "where the
existing infrastructure permits" is answered by what is importable, and the
answer is reported instead of assumed.
"""

from __future__ import annotations

import importlib
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from novacontrol.knowledge.models import SourceType, estimate_tokens

#: Suffix → what kind of thing it is. Anything not here is skipped, because
#: indexing a binary blob produces noise that ranks well and answers nothing.
SUFFIX_TYPES: dict[str, SourceType] = {
    ".md": SourceType.MARKDOWN,
    ".markdown": SourceType.MARKDOWN,
    ".mdx": SourceType.MARKDOWN,
    ".rst": SourceType.DOCUMENTATION,
    ".adoc": SourceType.DOCUMENTATION,
    ".txt": SourceType.TEXT,
    ".text": SourceType.TEXT,
    ".log": SourceType.TEXT,
    ".csv": SourceType.TEXT,
    ".ini": SourceType.TEXT,
    ".cfg": SourceType.TEXT,
    ".conf": SourceType.TEXT,
    ".env": SourceType.TEXT,
    ".py": SourceType.CODE,
    ".pyi": SourceType.CODE,
    ".js": SourceType.CODE,
    ".mjs": SourceType.CODE,
    ".cjs": SourceType.CODE,
    ".ts": SourceType.CODE,
    ".tsx": SourceType.CODE,
    ".jsx": SourceType.CODE,
    ".go": SourceType.CODE,
    ".rs": SourceType.CODE,
    ".java": SourceType.CODE,
    ".kt": SourceType.CODE,
    ".cs": SourceType.CODE,
    ".c": SourceType.CODE,
    ".h": SourceType.CODE,
    ".cpp": SourceType.CODE,
    ".hpp": SourceType.CODE,
    ".rb": SourceType.CODE,
    ".php": SourceType.CODE,
    ".swift": SourceType.CODE,
    ".sh": SourceType.CODE,
    ".bash": SourceType.CODE,
    ".ps1": SourceType.CODE,
    ".sql": SourceType.CODE,
    ".toml": SourceType.CODE,
    ".yaml": SourceType.CODE,
    ".yml": SourceType.CODE,
    ".json": SourceType.CODE,
    ".html": SourceType.MARKDOWN,
    ".htm": SourceType.MARKDOWN,
    ".pdf": SourceType.PDF,
}

#: Files whose NAME says "project documentation" regardless of suffix.
DOCUMENTATION_NAMES = frozenset(
    {"readme", "changelog", "contributing", "license", "authors", "notes", "todo"}
)

#: Extensions that are never worth reading.
BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf.zip",
        ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
        ".pyc", ".pyo", ".class", ".jar", ".o", ".a", ".bin", ".db", ".sqlite",
        ".sqlite3", ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".woff",
        ".woff2", ".ttf", ".otf", ".eot", ".whl", ".lock",
    }
)

_MAX_PDF_STREAMS = 256


class UnsupportedDocument(ValueError):
    """This source cannot be read as text — and here is why."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """A source's text, its kind, and anything a reader should know about it."""

    text: str
    source_type: SourceType
    metadata: dict[str, str] = field(default_factory=dict)
    warning: str = ""

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "tokens": self.tokens,
            "bytes": len(self.text.encode("utf-8")),
            "metadata": dict(self.metadata),
            "warning": self.warning,
        }


def source_type_for(path: str | Path, *, explicit: SourceType | None = None) -> SourceType | None:
    """What kind of source this is, or None when it is not indexable."""
    if explicit is not None:
        return explicit
    candidate = Path(path)
    suffix = candidate.suffix.lower()
    if suffix in BINARY_SUFFIXES:
        return None
    if suffix in SUFFIX_TYPES:
        listed = SUFFIX_TYPES[suffix]
        if listed is SourceType.MARKDOWN and candidate.stem.lower() in DOCUMENTATION_NAMES:
            return SourceType.DOCUMENTATION
        return listed
    if candidate.stem.lower() in DOCUMENTATION_NAMES:
        return SourceType.DOCUMENTATION
    return None


def is_probably_binary(data: bytes) -> bool:
    """A NUL byte in the first block is the cheap, reliable binary tell."""
    return b"\x00" in data[:4096]


def read_source(path: str | Path, *, source_type: SourceType | None = None) -> ExtractedText:
    """Read one source into text, or raise :class:`UnsupportedDocument`."""
    candidate = Path(path)
    kind = source_type_for(candidate, explicit=source_type)
    if kind is None:
        raise UnsupportedDocument(f"unsupported file type: {candidate.suffix or 'no suffix'}")
    if kind is SourceType.PDF:
        return read_pdf(candidate)
    data = candidate.read_bytes()
    if is_probably_binary(data):
        raise UnsupportedDocument("the file looks binary, so it has no text to index")
    text = decode_text(data)
    return ExtractedText(
        text=text,
        source_type=kind,
        metadata={"filename": candidate.name, "lines": str(text.count("\n") + 1)},
    )


def decode_text(data: bytes) -> str:
    """Decode bytes with the encodings a local file actually arrives in."""
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def read_pdf(path: str | Path) -> ExtractedText:
    """Read a PDF with the best extractor this installation has."""
    candidate = Path(path)
    data = candidate.read_bytes()
    text, reader = _pdf_text(data)
    if not text.strip():
        raise UnsupportedDocument(
            f"no text could be extracted from {candidate.name} "
            f"(tried {reader}); it may be a scan without a text layer"
        )
    return ExtractedText(
        text=text,
        source_type=SourceType.PDF,
        metadata={
            "filename": candidate.name,
            "pages": str(data.count(b"/Type /Page") or 1),
            "extractor": reader,
        },
    )


def _pdf_text(data: bytes) -> tuple[str, str]:
    """The first extractor that yields text, and its name."""
    for module_name, reader in (
        ("pypdf", _read_with_pypdf),
        ("PyPDF2", _read_with_pypdf),
        ("fitz", _read_with_fitz),
        ("pdfminer.high_level", _read_with_pdfminer),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - an absent optional dependency is not an error
            continue
        try:
            extracted = reader(module, data)
        except Exception:  # noqa: BLE001 - a broken extractor falls through to the next
            continue
        if extracted and extracted.strip():
            return extracted, module_name
    return _read_with_stdlib(data), "stdlib"


def _read_with_pypdf(module: Any, data: bytes) -> str:
    import io

    reader = module.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _read_with_fitz(module: Any, data: bytes) -> str:
    document = module.open(stream=data, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in document)
    finally:
        document.close()


def _read_with_pdfminer(module: Any, data: bytes) -> str:
    import io

    return str(module.extract_text(io.BytesIO(data)) or "")


_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
#: ``(text) Tj`` and ``[(a) -20 (b)] TJ`` — the two operators that carry text.
_TEXT_OPERATOR_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")
_ARRAY_TEXT_RE = re.compile(rb"\[(?:\\.|[^\]])*\]")


def _read_with_stdlib(data: bytes) -> str:
    """A small standard-library PDF text reader (Flate streams, Tj/TJ operators).

    Deliberately modest: this is the extractor that exists when nothing else is
    installed, so it handles the common cases (uncompressed and
    Flate-compressed content streams, literal strings in text-showing
    operators) and returns what it found — the caller decides whether that is
    enough, and says so when it is not.
    """
    pieces: list[str] = []
    for index, match in enumerate(_STREAM_RE.finditer(data)):
        if index >= _MAX_PDF_STREAMS:
            break
        raw = match.group(1)
        content = _inflate(raw)
        if b"Tj" not in content and b"TJ" not in content:
            continue
        pieces.extend(_strings_from_content(content))
    return "\n".join(pieces)


def _inflate(raw: bytes) -> bytes:
    stripped = raw.strip(b"\r\n")
    try:
        return zlib.decompress(stripped)
    except zlib.error:
        return stripped


def _strings_from_content(content: bytes) -> list[str]:
    line = ""
    collected: list[str] = []
    for segment in re.split(rb"ET|BT", content):
        line = ""
        for array_match in _ARRAY_TEXT_RE.finditer(segment):
            inner = b" ".join(_TEXT_OPERATOR_RE.findall(array_match.group(0)))
            line += _unescape_pdf_string(inner)
        for text_match in _TEXT_OPERATOR_RE.finditer(segment):
            line += _unescape_pdf_string(text_match.group(0))
        if line.strip():
            collected.append(line.strip())
    return collected


def _unescape_pdf_string(token: bytes) -> str:
    raw = token
    if raw.startswith(b"(") and raw.endswith(b")"):
        raw = raw[1:-1]
    text = raw.decode("latin-1", errors="replace")
    replacements = {
        r"\(": "(",
        r"\)": ")",
        r"\\": "\\",
        r"\n": "\n",
        r"\r": "",
        r"\t": "\t",
    }
    for escaped, plain in replacements.items():
        text = text.replace(escaped, plain)
    return text


__all__ = [
    "BINARY_SUFFIXES",
    "DOCUMENTATION_NAMES",
    "SUFFIX_TYPES",
    "ExtractedText",
    "UnsupportedDocument",
    "decode_text",
    "is_probably_binary",
    "read_pdf",
    "read_source",
    "source_type_for",
]
