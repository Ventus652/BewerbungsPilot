"""PDF rendering and automatic visual checks (phase 4.3).

The layout reproduces the historical ``generer_documents.py`` scripts (A4, Arial, same
sizes and colours). The CV is the validated CV closest to the offer, with its project
blocks reordered; the letter is the checked ``Motivationsschreiben.md``. Every PDF is
verified after rendering: one page, text extractable, required terms present in the right
order, forbidden terms absent, fonts embedded.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from bewerbungspilot.memory.cv_library import CvDocument, CvEntry, CvSection, strip_markup

W, H = A4
INK, MUTED, ACCENT = "#18232F", "#4D5966", "#006F72"
MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

FONT_CANDIDATES = [
    (Path("C:/Windows/Fonts"), ("arial.ttf", "arialbd.ttf", "ariali.ttf")),
    (Path("/usr/share/fonts/truetype/liberation2"), ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf")),
    (Path("/usr/share/fonts/truetype/liberation"), ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Italic.ttf")),
]


class LayoutOverflowError(RuntimeError):
    """The content does not fit on one A4 page."""


class PdfCheck(BaseModel):
    path: str
    ok: bool
    pages: int
    characters: int
    fonts_embedded: bool
    issues: list[str] = Field(default_factory=list)
    preview_png: str | None = None


def register_fonts() -> str:
    if "BP-Sans" in pdfmetrics.getRegisteredFontNames():
        return "BP-Sans"
    for folder, (regular, bold, italic) in FONT_CANDIDATES:
        if (folder / regular).exists() and (folder / bold).exists():
            pdfmetrics.registerFont(TTFont("BP-Sans", str(folder / regular)))
            pdfmetrics.registerFont(TTFont("BP-Sans-Bold", str(folder / bold)))
            pdfmetrics.registerFont(TTFont("BP-Sans-Italic", str(folder / (italic if (folder / italic).exists() else regular))))
            pdfmetrics.registerFontFamily("BP-Sans", normal="BP-Sans", bold="BP-Sans-Bold", italic="BP-Sans-Italic", boldItalic="BP-Sans-Bold")
            return "BP-Sans"
    raise FileNotFoundError("Aucune police Arial/Liberation Sans trouvée")


def _links(text: str, color: str) -> str:
    return text.replace("<link href=", f'<link color="{color}" href=')


# --------------------------------------------------------------------------- CV
PROJECT_KEYS = {
    "quizarena": ("quizarena",),
    "stream club": ("stream club",),
    "data / machine learning": ("machine learning", "datenanalyse", "data analysis"),
    "game development": ("game", "godot"),
}


def choose_base_cv(library: list[CvDocument], language: str, query_terms: set[str]) -> CvDocument:
    candidates = [d for d in library if d.language == language] or library
    if not candidates:
        raise ValueError("Aucun CV validé disponible")

    def words(text: str) -> set[str]:
        return set(re.split(r"[^a-z0-9#+.äöüß]+", strip_markup(text).casefold())) - {""}

    def score(document: CvDocument) -> tuple[int, str]:
        # Headline and summary were tailored to the offer they were written for: weight them.
        header = strip_markup(" ".join(e.text for e in document.header)).casefold()
        body = words(document.plain_text())
        # Substring match on the header catches German compounds ("Webentwicklung").
        in_header = sum(1 for t in query_terms if len(t) >= 3 and t in header)
        return (3 * in_header + len(body & query_terms), document.source)

    return max(candidates, key=score)


def reorder_projects(document: CvDocument, project_order: list[str]) -> CvDocument:
    sections = []
    for section in document.sections:
        if not any(e.kind == "item" and any(k in e.text.casefold() for keys in PROJECT_KEYS.values() for k in keys) for e in section.entries):
            sections.append(section)
            continue
        groups: list[list[CvEntry]] = []
        for entry in section.entries:
            if entry.kind == "item" or not groups:
                groups.append([entry])
            else:
                groups[-1].append(entry)

        def rank(group: list[CvEntry]) -> int:
            title = group[0].text.casefold()
            for index, name in enumerate(project_order):
                if any(k in title for k in PROJECT_KEYS.get(name.casefold(), (name.casefold(),))):
                    return index
            return len(project_order)

        ordered = sorted(groups, key=rank)  # stable: unknown blocks keep their order
        sections.append(CvSection(title=section.title, entries=[e for g in ordered for e in g]))
    return document.model_copy(update={"sections": sections})


def render_cv(document: CvDocument, out: Path, *, title: str, subject: str) -> Path:
    font = register_fonts()
    # Neutral colours for every employer: a colour chosen for one company is never reused for another.
    colors = {"INK": INK, "MUTED": MUTED, "ACCENT": ACCENT}
    c = canvas.Canvas(str(out), pagesize=A4, invariant=1)
    c.setTitle(title)
    c.setAuthor(strip_markup(document.header[0].text) if document.header else "")
    c.setSubject(subject)
    left, right = 43, W - 43
    y = H - 40
    base = ParagraphStyle("body", fontName=font, fontSize=9.5, leading=12.9, textColor=HexColor(colors["INK"]))

    def para(text, size=9.5, leading=12.9, color=None, bold=False, after=3, indent=0):
        nonlocal y
        style = ParagraphStyle("p", parent=base, fontSize=size, leading=leading,
                               textColor=HexColor(color or colors["INK"]), leftIndent=indent)
        p = Paragraph(_links(("<b>" + text + "</b>") if bold else text, colors["ACCENT"]), style)
        _, height = p.wrap(right - left, H)
        p.drawOn(c, left, y - height)
        y -= height + after

    def entry(e: CvEntry) -> None:
        if e.kind == "item":
            if e.date:
                c.setFont(font, 9.1)
                c.setFillColor(HexColor(colors["MUTED"]))
                c.drawRightString(right, y - 11, e.date)
            para(e.text, 10.1, 13.4, bold=True, after=2)
        elif e.kind == "bullet":
            para("• " + e.text, after=2, indent=7)
        else:
            color = e.color if e.color in ("#000000", document.colors.get("INK"), document.colors.get("MUTED")) else colors["ACCENT"]
            if e.color == document.colors.get("MUTED"):
                color = colors["MUTED"]
            elif e.color in (None, document.colors.get("INK")):
                color = None
            para(e.text, e.size or 9.5, e.leading or 12.9, color, e.bold, 3 if e.after is None else e.after)

    for e in document.header:
        entry(e)
    for section in document.sections:
        y -= 8
        para(section.title.upper(), 10.1, 13, "#000000", True, 5)
        for e in section.entries:
            entry(e)
    if y <= 28:
        raise LayoutOverflowError(f"CV trop long : y={y:.1f}")
    c.save()
    return out


# --------------------------------------------------------------------------- letter
class LetterHeader(BaseModel):
    name: str
    city: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None  # only when the user explicitly allows it
    recipient: list[str]
    language: str
    on: date


def parse_letter_markdown(text: str) -> tuple[str, list[str]]:
    lines = text.strip().split("\n")
    subject = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else ""
    body = "\n".join(lines[1:] if subject else lines)
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    return subject, blocks


def render_letter(markdown: str, header: LetterHeader, out: Path, *, title: str) -> Path:
    font = register_fonts()
    subject, blocks = parse_letter_markdown(markdown)
    c = canvas.Canvas(str(out), pagesize=A4, invariant=1)
    c.setTitle(title)
    c.setAuthor(header.name)
    left, right = 54, W - 54
    y = H - 48
    base = ParagraphStyle("letter", fontName=font, fontSize=10.3, leading=14.8, textColor=HexColor(INK))

    def para(text, size=10.3, leading=14.8, color=INK, bold=False, after=10):
        nonlocal y
        style = ParagraphStyle("lp", parent=base, fontSize=size, leading=leading, textColor=HexColor(color))
        safe = text.replace("&", "&amp;")
        p = Paragraph(("<b>" + safe + "</b>") if bold else safe, style)
        _, height = p.wrap(right - left, H)
        p.drawOn(c, left, y - height)
        y -= height + after

    para(header.name, 17, 21, "#000000", True, 2)
    para(header.address or header.city, 9.4, 12.4, MUTED, after=1)
    contact = " · ".join(x for x in (header.email, header.phone) if x)
    if contact:
        para(contact, 9.4, 12.4, MUTED, after=22)
    para("<br/>".join(header.recipient), 9.7, 13.4, after=15)
    months = MONTHS_DE if header.language == "de" else MONTHS_EN
    when = (f"{header.city}, {header.on.day}. {months[header.on.month - 1]} {header.on.year}" if header.language == "de"
            else f"{header.city}, {header.on.day} {months[header.on.month - 1]} {header.on.year}")
    c.setFont(font, 9.4)
    c.setFillColor(HexColor(MUTED))
    c.drawRightString(right, y, when)
    y -= 30
    if subject:
        para(subject, 11.4, 14.7, "#000000", True, 17)
    closing_index = next((i for i, b in enumerate(blocks) if b.startswith(("Mit freundlichen Grüßen", "Kind regards", "Best regards"))), len(blocks))
    for index, block in enumerate(blocks[:closing_index]):
        para(block, after=17 if index == closing_index - 1 else 10)
    if closing_index < len(blocks):
        para(blocks[closing_index], after=23)
        for block in blocks[closing_index + 1:]:
            para(block, bold=True, after=0)
    if y <= 40:
        raise LayoutOverflowError(f"Lettre trop longue : y={y:.1f}")
    c.save()
    return out


# --------------------------------------------------------------------------- checks
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_pdf(path: Path, *, must_contain: list[str], ordered: list[str] | None = None,
               must_not_contain: list[str] | None = None, preview_dir: Path | None = None) -> PdfCheck:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    issues: list[str] = []
    pages = len(reader.pages)
    if pages != 1:
        issues.append(f"{pages} pages au lieu d'une")
    text = " ".join(re.sub(r"\s+", " ", page.extract_text() or "") for page in reader.pages)
    flat = re.sub(r"\s+", " ", text)
    for term in must_contain:
        if term not in flat:
            issues.append(f"terme absent : {term}")
    positions = [flat.find(t) for t in (ordered or []) if t in flat]
    if positions != sorted(positions):
        issues.append(f"ordre incorrect : {ordered}")
    for term in must_not_contain or []:
        if term and re.search(rf"\b{re.escape(term)}\b", flat, re.IGNORECASE):
            issues.append(f"terme interdit présent : {term}")
    embedded = True
    for page in reader.pages:
        fonts = (page.get("/Resources") or {}).get("/Font") or {}
        for font in fonts.values():
            font = font.get_object()
            descriptor = font.get("/FontDescriptor")
            if descriptor is None and "/DescendantFonts" in font:
                descriptor = font["/DescendantFonts"][0].get_object().get("/FontDescriptor")
            if descriptor is not None:
                d = descriptor.get_object()
                if not any(k in d for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                    embedded = False
    if not embedded:
        issues.append("police non intégrée")
    if len(flat) < 300:
        issues.append("texte extrait trop court")
    preview = None
    if preview_dir is not None and shutil.which("pdftoppm"):
        preview_dir.mkdir(parents=True, exist_ok=True)
        stem = preview_dir / path.stem
        subprocess.run(["pdftoppm", "-png", "-r", "70", "-singlefile", str(path), str(stem)], check=False, capture_output=True)
        if stem.with_suffix(".png").exists():
            preview = str(stem.with_suffix(".png"))
    return PdfCheck(path=str(path), ok=not issues, pages=pages, characters=len(flat), fonts_embedded=embedded,
                    issues=issues, preview_png=preview)
