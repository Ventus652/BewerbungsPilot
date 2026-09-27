"""Library of CVs already validated and sent (phase 4.3).

Each historical ``generer_documents.py`` is **read, never executed**: its ``make_cv``
function is parsed with ``ast`` and its drawing calls (para, section, item, bullet) are
turned into structured entries. Every sentence therefore comes from a CV Junior already
validated; a new CV may only reorder these blocks.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .records import relative_display

_CALLS = {"para", "section", "item", "bullet"}
_PARA_ARGS = ("text", "size", "leading", "color", "bold", "after", "indent")


class CvEntry(BaseModel):
    kind: str  # para | item | bullet
    text: str
    date: str | None = None
    size: float | None = None
    leading: float | None = None
    color: str | None = None
    bold: bool = False
    after: float | None = None
    line: int


class CvSection(BaseModel):
    title: str
    entries: list[CvEntry] = Field(default_factory=list)


class CvDocument(BaseModel):
    source: str
    sha256: str
    language: str
    colors: dict[str, str] = Field(default_factory=dict)
    header: list[CvEntry] = Field(default_factory=list)
    sections: list[CvSection] = Field(default_factory=list)

    def plain_text(self) -> str:
        parts = [e.text for e in self.header]
        for section in self.sections:
            parts.append(section.title)
            parts += [e.text for e in section.entries]
        return strip_markup(" ".join(parts))


def strip_markup(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("&amp;", "&")


class _Evaluator:
    def __init__(self, constants: dict[str, Any]) -> None:
        self.constants = constants

    def value(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self.constants[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left), self.value(node.right)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            return None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for piece in node.values:
                if isinstance(piece, ast.Constant):
                    parts.append(str(piece.value))
                elif isinstance(piece, ast.FormattedValue):
                    inner = self.value(piece.value)
                    if inner is None:
                        return None
                    parts.append(str(inner))
            return "".join(parts)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "link" and len(node.args) == 2:
            label, url = self.value(node.args[0]), self.value(node.args[1])
            if isinstance(label, str) and isinstance(url, str):
                return f'<link href="{url}">{label}</link>'
        return None


def parse_generator(path: Path, root: Path | None = None) -> CvDocument | None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    constants: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            evaluator = _Evaluator(constants)
            if isinstance(node.value, ast.Tuple) and all(isinstance(t, ast.Name) for t in getattr(node.targets[0], "elts", [])):
                for target, value in zip(node.targets[0].elts, node.value.elts):
                    constants[target.id] = evaluator.value(value)
            elif isinstance(node.targets[0], ast.Name):
                constants[node.targets[0].id] = evaluator.value(node.value)
    make_cv = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_cv"), None)
    if make_cv is None:
        return None
    evaluator = _Evaluator(constants)
    header: list[CvEntry] = []
    sections: list[CvSection] = []
    for statement in make_cv.body:
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name) and statement.value.func.id in _CALLS):
            continue
        call = statement.value
        name = call.func.id
        args = [evaluator.value(a) for a in call.args]
        kwargs = {k.arg: evaluator.value(k.value) for k in call.keywords if k.arg}
        if not args or not isinstance(args[0], str):
            continue
        if name == "section":
            sections.append(CvSection(title=args[0]))
            continue
        entry: dict[str, Any] = {"kind": name, "text": args[0], "line": statement.lineno}
        if name == "item":
            entry["date"] = args[1] if len(args) > 1 and isinstance(args[1], str) else kwargs.get("date")
        elif name == "para":
            for key, val in zip(_PARA_ARGS[1:], args[1:]):
                entry[key] = val
            for key in _PARA_ARGS[1:]:
                if key in kwargs:
                    entry[key] = kwargs[key]
            entry["bold"] = bool(entry.get("bold"))
            entry = {k: v for k, v in entry.items() if v is not None or k in ("text",)}
        target = sections[-1].entries if sections else header
        target.append(CvEntry(**entry))
    if not sections:
        return None
    titles = " ".join(s.title.lower() for s in sections)
    language = "en" if any(w in titles for w in ("skills", "projects", "education", "languages")) else "de"
    colors = {k: v for k, v in constants.items() if k in ("INK", "MUTED", "ACCENT") and isinstance(v, str)}
    return CvDocument(
        source=relative_display(path, root),
        sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        language=language,
        colors=colors,
        header=header,
        sections=sections,
    )


def build_library(root: Path) -> list[CvDocument]:
    documents = []
    for generator in sorted(root.glob("*/generer_documents.py")):
        document = parse_generator(generator, root)
        if document is not None:
            documents.append(document)
    return documents
