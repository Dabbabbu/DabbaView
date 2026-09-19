# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""docs/*.md → PDF (Qt QTextDocument: Markdown 표·이미지 지원, 외부 도구 불필요)

사용: python docs/build_pdf.py            (docs/manual.pdf, docs/analysis_guide.pdf 생성)
"""
import os
import re
import sys

from PyQt5.QtCore import QMarginsF, QSizeF, QUrl
from PyQt5.QtGui import QFont, QImage, QPageLayout, QPageSize, QTextDocument
from PyQt5.QtPrintSupport import QPrinter
from PyQt5.QtWidgets import QApplication

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = """
body { font-size: 10pt; }
h1 { font-size: 20pt; color: #1f4e79; }
h2 { font-size: 15pt; color: #1f4e79; margin-top: 18px; }
h3 { font-size: 12pt; color: #2e6da4; }
table { border-collapse: collapse; }
td, th { border: 1px solid #999; padding: 3px; }
th { background-color: #dde6f0; }
code, pre { font-family: Menlo, Consolas, monospace; font-size: 8.5pt; background-color: #f2f2f2; }
blockquote { color: #555; }
"""
PAGE_W_PX = 680      # 본문 폭 (이미지 최대 폭)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _flatten_image_tables(text):
    """이미지를 나란히 놓은 표 → 제목 + 이미지를 차례로 (Qt Markdown은 표 칸 안 이미지를 못 그림)"""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if (lines[i].startswith("|") and i + 2 < len(lines) and re.match(r"^\|[\s|:-]+\|$", lines[i + 1])
                and "![" in lines[i + 2]):
            header = _cells(lines[i])
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                for title, cell in zip(header, _cells(lines[j])):
                    if cell:
                        out += [f"**{title}**", "", cell, ""]
                j += 1
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _code_blocks(text):
    """``` 코드 블록 → 줄마다 강제 줄바꿈한 인라인 코드 (Qt는 코드 블록 줄바꿈을 잃음)"""
    def repl(m):
        rows = [ln for ln in m.group(1).split("\n") if ln.strip()]
        return "\n".join(f"`{ln}`  " for ln in rows) + "\n"
    return re.sub(r"```[a-z]*\n(.*?)```", repl, text, flags=re.S)


def convert(md_path, pdf_path):
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"\]\(#[^)]*\)", "]()", text)          # 문서 안 링크는 PDF에서 뺌
    text = _flatten_image_tables(text)
    text = _code_blocks(text)
    doc = QTextDocument()
    doc.setDefaultFont(QFont("Apple SD Gothic Neo" if sys.platform == "darwin" else "Malgun Gothic", 10))
    doc.setMarkdown(text)
    html = doc.toHtml()
    base = os.path.dirname(md_path)

    def fix_img(m):
        src = m.group(1)
        path = os.path.join(base, src)
        img = QImage(path)
        cells = 1
        # 표 안 이미지는 칸 수에 맞춰 작게
        width = min(img.width(), PAGE_W_PX) if not img.isNull() else PAGE_W_PX
        return f'<img src="{QUrl.fromLocalFile(path).toString()}" width="{int(width / cells)}" />'
    html = re.sub(r'<img src="([^"]+)"[^>]*/?>', fix_img, html)
    # 표 안에 든 이미지는 칸 폭에 맞게 줄임
    html = re.sub(r"(<td[^>]*>(?:(?!</td>).)*?)<img ([^>]*?)width=\"(\d+)\"",
                  lambda m: f'{m.group(1)}<img {m.group(2)}width="{min(int(m.group(3)), 320)}"', html,
                  flags=re.S)
    doc.setDefaultStyleSheet(CSS)
    doc.setHtml(html)
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(pdf_path)
    printer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                                      QMarginsF(15, 15, 15, 15), QPageLayout.Millimeter))
    doc.setPageSize(QSizeF(printer.pageRect(QPrinter.Point).size()))
    doc.setTextWidth(printer.pageRect(QPrinter.Point).width())
    doc.print_(printer)
    return pdf_path


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    for name in ("manual", "analysis_guide"):
        out = convert(os.path.join(HERE, name + ".md"), os.path.join(HERE, name + ".pdf"))
        print(out, os.path.getsize(out))
    del app


if __name__ == "__main__":
    main()
