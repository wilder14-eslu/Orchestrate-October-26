"""Image evidence (application screenshots) -> text -> row-level document records.

Extraction is cache-first: code/.cache/text/<sha256>.json. On a miss, Tesseract
runs three passes (psm 6 on the original, psm 6 on a 2x grayscale upscale, psm 3
on the original) and the union of their text is cached. Upscaling uses Pillow
or ImageMagick when available; the cache records which passes ran.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from ..models import ManifestEntry, ParseWarning
from ..text_cache import TextCache, sha256_file
from .pdf_text import document_records

OCR_CONFIDENCE = 0.5
EXTRACTOR = "tesseract:psm6+psm6_x2gray+psm3"


def _tesseract(binary: str, image: Path, psm: int) -> Optional[str]:
    proc = subprocess.run([binary, str(image), "-", "--psm", str(psm)], capture_output=True, text=True, timeout=300)
    return proc.stdout if proc.returncode == 0 else None


def _upscaled(image: Path, workdir: Path) -> Optional[Path]:
    target = workdir / "x2gray.png"
    try:
        from PIL import Image  # optional, only needed to build the cache
        with Image.open(image) as im:
            im.convert("L").resize((im.width * 2, im.height * 2), Image.LANCZOS).save(target)
        return target
    except Exception:
        pass
    convert = shutil.which("magick") or shutil.which("convert")
    if convert:
        proc = subprocess.run([convert, str(image), "-resize", "200%", "-colorspace", "Gray", str(target)], capture_output=True)
        if proc.returncode == 0:
            return target
    return None


def extract(path: Path, cache: TextCache, warnings: List[ParseWarning], entry: ManifestEntry) -> Optional[str]:
    digest = sha256_file(path)
    cached = cache.get("text", digest)
    if cached:
        return cached["text"]
    binary = shutil.which("tesseract")
    if not binary:
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", "no cached text and tesseract is not installed"))
        return None
    passes = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, image, psm in (("psm6", path, 6), ("psm6_x2gray", _upscaled(path, Path(tmp)), 6), ("psm3", path, 3)):
            if image is None:
                continue
            text = _tesseract(binary, image, psm)
            if text is not None:
                passes.append({"pass": name, "text": text})
    if not passes:
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", "tesseract produced no text"))
        return None
    version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.splitlines()[:1]
    text = "\n".join(p["text"] for p in passes)
    cache.put("text", digest, {"file_name": path.name, "extractor": EXTRACTOR, "tool_version": " ".join(version),
                               "passes": [p["pass"] for p in passes], "text": text})
    return text


def parse(entry: ManifestEntry, path: str, warnings: List[ParseWarning], ocr: bool, cache: Optional[TextCache] = None,
          aliases: Optional[dict] = None):
    if not ocr:
        warnings.append(ParseWarning(entry.file_id, entry.file_path, "-", "image not read (--no-ocr)"))
        return [], [], []
    text = extract(Path(path), cache or TextCache(), warnings, entry)
    if not text:
        return [], [], []
    observations, mentions = document_records(entry, text, "IMAGE", "ocr", OCR_CONFIDENCE, aliases)
    return observations, [], mentions
