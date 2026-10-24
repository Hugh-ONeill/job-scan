"""Resume PDF -> text with mtime-based cache."""
from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from .config import DATA_DIR

# pypdf's default extraction turns the resume into a column of single words
# with blank lines between them, and reads pdfTeX's kerning offsets after a
# capital as spaces ("F astAPI", "T eacher"). Layout mode keeps glyph runs
# intact; the whitespace it pads with is collapsed back into prose below so
# the matcher isn't fed (and billed for) a wall of spaces.
_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n\s*\n+")
_NL = re.compile(r"\s*\n\s*")
_BAR = re.compile(r"\s*\|\s*")       # layout mode drops the spaces around "|"
_BULLET = re.compile(r"·(?=\S)")
_HYPH = re.compile(r"(?<=[a-z])- (?=[a-z])")  # "user- defined" from a line-end hyphen


CACHE = DATA_DIR / "resume_cache.txt"
MTIME = DATA_DIR / "resume_cache.mtime"


def load_resume_text(resume_path: Path) -> str:
    if not resume_path.exists():
        raise FileNotFoundError(f"resume not found: {resume_path}")
    mtime = str(int(resume_path.stat().st_mtime))
    if CACHE.exists() and MTIME.exists() and MTIME.read_text().strip() == mtime:
        return CACHE.read_text()
    text = _extract(resume_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(text)
    MTIME.write_text(mtime)
    return text


def _normalize(text: str) -> str:
    """Collapse pypdf's one-word-per-line output into readable prose."""
    text = _WS.sub(" ", text)        # runs of spaces/tabs -> single space
    text = _BLANKS.sub("\n", text)   # blank-line runs -> single newline
    text = _NL.sub(" ", text)        # join remaining single-word line breaks
    text = _BAR.sub(" | ", text)
    text = _BULLET.sub("· ", text)
    text = _HYPH.sub("-", text)
    return text.strip()


def _extract(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        raw = "\n".join(
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages
        )
        return _normalize(raw)
    # Fallback for .txt / .md
    return path.read_text()
