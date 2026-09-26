"""Deterministic Markdown-to-DOCX/PDF export without external dependencies.

The source Markdown is never rewritten.  Exporters only consume the text and
produce controlled, application-owned artifacts.  They never execute code or
follow links found in the document.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
import unicodedata
from typing import Iterable
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .artifact_store import ControlledArtifactStore, StoredArtifact


@dataclass(frozen=True)
class ExportedDocument:
    extension: str
    artifact: StoredArtifact | None
    error: str | None = None


def _inline_text(value: str) -> str:
    value = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r"\1 (\2)", value)
    value = re.sub(r"[`*_~]", "", value)
    return value.strip()


def _markdown_blocks(markdown: str) -> Iterable[tuple[str, object]]:
    lines = markdown.splitlines()
    index = 0
    paragraph: list[str] = []
    code: list[str] | None = None

    def flush_paragraph() -> tuple[str, object] | None:
        if not paragraph:
            return None
        value = " ".join(line.strip() for line in paragraph).strip()
        paragraph.clear()
        return ("paragraph", value) if value else None

    while index < len(lines):
        line = lines[index]
        if code is not None:
            if line.strip().startswith("```"):
                yield ("code", "\n".join(code))
                code = None
            else:
                code.append(line)
            index += 1
            continue
        if line.strip().startswith("```"):
            item = flush_paragraph()
            if item:
                yield item
            code = []
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            item = flush_paragraph()
            if item:
                yield item
            yield ("heading", (len(heading.group(1)), _inline_text(heading.group(2))))
            index += 1
            continue
        if re.match(r"^\s*\|.*\|\s*$", line) and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1]):
            item = flush_paragraph()
            if item:
                yield item
            rows: list[list[str]] = []
            index += 2
            first = line
            for row in (first, *lines[index:]):
                if not re.match(r"^\s*\|.*\|\s*$", row):
                    break
                rows.append([_inline_text(cell) for cell in row.strip().strip("|").split("|")])
                index += 1
            yield ("table", rows)
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if bullet or ordered:
            item = flush_paragraph()
            if item:
                yield item
            yield ("list_bullet" if bullet else "list_number", _inline_text((bullet or ordered).group(1)))
            index += 1
            continue
        if not line.strip():
            item = flush_paragraph()
            if item:
                yield item
        else:
            paragraph.append(line)
        index += 1
    if code is not None:
        yield ("code", "\n".join(code))
    item = flush_paragraph()
    if item:
        yield item


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
register_namespace("w", W)


def _w(tag: str) -> str:
    return "{" + W + "}" + tag


def _run(parent: Element, text: str, *, bold: bool = False) -> None:
    run = SubElement(parent, _w("r"))
    if bold:
        SubElement(run, _w("rPr"))
        run.find(_w("rPr")).append(Element(_w("b")))
    for offset, part in enumerate(str(text).split("\n")):
        if offset:
            SubElement(run, _w("br"))
        node = SubElement(run, _w("t"))
        if part[:1].isspace() or part[-1:].isspace():
            node.set("{" + XML_NS + "}space", "preserve")
        node.text = part


def _paragraph(body: Element, text: str, style: str | None = None, *, bold: bool = False) -> None:
    paragraph = SubElement(body, _w("p"))
    if style:
        props = SubElement(paragraph, _w("pPr"))
        style_node = SubElement(props, _w("pStyle"))
        style_node.set(_w("val"), style)
    _run(paragraph, text, bold=bold)


def _table(body: Element, rows: list[list[str]]) -> None:
    table = SubElement(body, _w("tbl"))
    props = SubElement(table, _w("tblPr"))
    borders = SubElement(props, _w("tblBorders"))
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = SubElement(borders, _w("" + edge))
        node.set(_w("val"), "single")
        node.set(_w("sz"), "4")
    for row in rows:
        tr = SubElement(table, _w("tr"))
        for cell in row:
            tc = SubElement(tr, _w("tc"))
            _paragraph(tc, cell)


def markdown_to_docx(markdown: str) -> bytes:
    document = Element(_w("document"))
    body = SubElement(document, _w("body"))
    for kind, value in _markdown_blocks(markdown):
        if kind == "heading":
            level, text = value  # type: ignore[misc]
            _paragraph(body, text, f"Heading{min(int(level), 6)}", bold=True)
        elif kind == "code":
            _paragraph(body, value, "Code")
        elif kind == "table":
            _table(body, value)  # type: ignore[arg-type]
        elif kind == "list_bullet":
            _paragraph(body, "• " + str(value), "ListBullet")
        elif kind == "list_number":
            _paragraph(body, "1. " + str(value), "ListNumber")
        else:
            _paragraph(body, str(value))
    SubElement(body, _w("sectPr"))

    styles = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/></w:style>
<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/></w:style>
<w:style w:type="paragraph" w:styleId="ListNumber"><w:name w:val="List Number"/></w:style></w:styles>'''.encode()
    document_xml = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + tostring(document, encoding="utf-8")
    content_types = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'''.encode()
    rels = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    document_rels = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    output = __import__("io").BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in (("[Content_Types].xml", content_types), ("_rels/.rels", rels), ("word/document.xml", document_xml), ("word/styles.xml", styles), ("word/_rels/document.xml.rels", document_rels)):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


def _pdf_escape(text: str) -> bytes:
    plain = unicodedata.normalize("NFKD", text).encode("latin-1", "replace").decode("latin-1")
    return plain.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1", "replace")


def markdown_to_pdf(markdown: str) -> bytes:
    lines: list[str] = []
    for kind, value in _markdown_blocks(markdown):
        if kind == "heading":
            _, text = value  # type: ignore[misc]
            lines.append(str(text).upper())
        elif kind == "table":
            for row in value:  # type: ignore[union-attr]
                lines.append(" | ".join(row))
        elif kind == "code":
            lines.extend("    " + line for line in str(value).splitlines())
        elif kind == "list_bullet":
            lines.append("- " + str(value))
        elif kind == "list_number":
            lines.append("1. " + str(value))
        else:
            lines.append(str(value))
    wrapped: list[str] = []
    for line in lines:
        while len(line) > 105:
            split = line.rfind(" ", 0, 105)
            split = split if split > 20 else 105
            wrapped.append(line[:split])
            line = line[split:].lstrip()
        wrapped.append(line)
    pages = [wrapped[offset:offset + 52] for offset in range(0, max(len(wrapped), 1), 52)]
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_ids = [4 + index * 2 for index in range(len(pages))]
    objects.append(("<< /Type /Pages /Kids [" + " ".join(f"{item} 0 R" for item in page_ids) + "] /Count " + str(len(page_ids)) + " >>").encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_id, page_lines in zip(page_ids, pages):
        content_lines = [b"BT", b"/F1 9 Tf", b"36 806 Td"]
        for index, line in enumerate(page_lines):
            if index:
                content_lines.append(b"0 -14 Td")
            content_lines.append(b"(" + _pdf_escape(line) + b") Tj")
        content_lines.append(b"ET")
        stream = b"\n".join(content_lines)
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>").encode())
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def export_requested_formats(store: ControlledArtifactStore, analysis_id: str, markdown: str, requested_formats: tuple[str, ...] | list[str]) -> dict[str, ExportedDocument]:
    """Store Markdown plus requested office formats without overwriting."""
    formats = set(requested_formats) | {"markdown"}
    output: dict[str, ExportedDocument] = {}
    try:
        output["markdown"] = ExportedDocument("markdown", store.write_markdown(analysis_id, markdown))
    except Exception as exc:
        output["markdown"] = ExportedDocument("markdown", None, type(exc).__name__ + ":" + str(exc)[:180])
        return output
    for extension, builder in (("docx", markdown_to_docx), ("pdf", markdown_to_pdf)):
        if extension not in formats:
            output[extension] = ExportedDocument(extension, None, "not_requested")
            continue
        try:
            output[extension] = ExportedDocument(extension, store.write_bytes(analysis_id, extension, builder(markdown)))
        except Exception as exc:
            output[extension] = ExportedDocument(extension, None, type(exc).__name__ + ":" + str(exc)[:180])
    return output


__all__ = ["ExportedDocument", "export_requested_formats", "markdown_to_docx", "markdown_to_pdf"]
