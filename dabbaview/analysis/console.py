# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
Python 콘솔 (3D Slicer Python Interactor에 해당) - 하단 도크, F3

네임스페이스:
  app   현재 시리즈·배열·마스크에 접근하고 결과를 새 시리즈로 추가하는 API (help(app))
  np, ndi(scipy.ndimage), plt(matplotlib.pyplot), skimage (있으면)
plt 그림은 실행이 끝나면 오른쪽 그래프 창에 표시된다.
"""
import ast
import builtins
import contextlib
import io
import keyword
import traceback

import numpy as np
from PyQt5.QtCore import QRegularExpression, Qt
from PyQt5.QtGui import (QColor, QFont, QFontDatabase, QKeySequence, QPixmap,
                         QSyntaxHighlighter, QTextCharFormat)
from PyQt5.QtWidgets import (QDockWidget, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                             QPlainTextEdit, QPushButton, QScrollArea, QShortcut, QSplitter,
                             QVBoxLayout, QWidget)

WELCOME = """# DabbaView Python 콘솔 - Ctrl+Enter(⌘+Enter) 실행, 선택 영역만 실행도 가능
# app.current_array  현재 시리즈 볼륨 (슬라이스, 행, 열) float32
# app.current_image  현재 슬라이스 2D,  app.mask  AI 세그멘테이션 마스크
# app.add_series(배열, "이름")  결과를 새 시리즈로 추가 (원본 보존)
# 예:
vol = app.current_array
print(vol.shape, vol.dtype, vol.min(), vol.max())
plt.hist(app.current_image.ravel(), bins=100)
plt.title("current slice histogram")
"""


class PythonHighlighter(QSyntaxHighlighter):
    """간단한 Python 구문 강조"""

    def __init__(self, document):
        super().__init__(document)

        def fmt(color, bold=False, italic=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            f.setFontItalic(italic)
            return f
        self._rules = []
        kw = "|".join(keyword.kwlist)
        self._rules.append((QRegularExpression(rf"\b({kw})\b"), fmt("#c586c0", bold=True)))
        names = "|".join(n for n in dir(builtins) if not n.startswith("_"))
        self._rules.append((QRegularExpression(rf"\b({names})\b"), fmt("#4ec9b0")))
        self._rules.append((QRegularExpression(r"\b(app|np|ndi|plt|skimage)\b"), fmt("#9cdcfe", bold=True)))
        self._rules.append((QRegularExpression(r"\b[0-9]+(\.[0-9]*)?([eE][-+]?[0-9]+)?\b"), fmt("#b5cea8")))
        self._rules.append((QRegularExpression(r"\bdef\s+(\w+)|\bclass\s+(\w+)"), fmt("#dcdcaa")))
        self._string = fmt("#ce9178")
        self._comment = fmt("#6a9955", italic=True)
        self._string_re = QRegularExpression(r"(\"[^\"\\\\]*(\\\\.[^\"\\\\]*)*\"|'[^'\\\\]*(\\\\.[^'\\\\]*)*')")
        self._comment_re = QRegularExpression(r"#[^\n]*")

    def highlightBlock(self, text):
        for regex, f in self._rules:
            it = regex.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)
        it = self._string_re.globalMatch(text)
        strings = []
        while it.hasNext():
            m = it.next()
            strings.append((m.capturedStart(), m.capturedEnd()))
            self.setFormat(m.capturedStart(), m.capturedLength(), self._string)
        it = self._comment_re.globalMatch(text)
        while it.hasNext():
            m = it.next()
            start = m.capturedStart()
            if not any(a <= start < b for a, b in strings):
                self.setFormat(start, len(text) - start, self._comment)
                break


class ConsoleAPI:
    """콘솔의 `app` 객체 - 현재 보고 있는 시리즈에 접근"""

    def __init__(self, main_window):
        self._main = main_window
        self._volume_cache = (None, None)

    def __repr__(self):
        s = self.current_series
        return f"<DabbaView app: {s.description if s else '시리즈 없음'}>"

    @property
    def viewport(self):
        return self._main._target_viewport()

    @property
    def current_series(self):
        return self.viewport.series

    @property
    def series_list(self):
        return self._main._loader.get_series_list()

    @property
    def current_slice(self):
        """현재 슬라이스 번호 (0부터)"""
        return self.viewport.current_slice

    @property
    def current_image(self):
        """현재 슬라이스 2D 배열 (Rescale 적용 값)"""
        s = self.current_series
        return None if s is None else np.asarray(s.get_pixel_array(self.current_slice))

    @property
    def current_volume(self):
        """ai.volume.Volume (array, spacing, affine_lps)"""
        from ..ai.volume import load_volume
        s = self.current_series
        if s is None:
            return None
        uid, vol = self._volume_cache
        if uid != s.series_uid:
            vol = load_volume(s)
            self._volume_cache = (s.series_uid, vol)
        return vol

    @property
    def current_array(self):
        """현재 시리즈 전체 (슬라이스, 행, 열) float32"""
        vol = self.current_volume
        return None if vol is None else vol.array

    @property
    def spacing(self):
        """(슬라이스, 행, 열) 간격 mm"""
        vol = self.current_volume
        return None if vol is None else vol.spacing

    @property
    def window(self):
        return self.viewport.window_level

    @property
    def mask(self):
        """AI 세그멘테이션 마스크 (슬라이스, 행, 열) uint8 또는 None"""
        s = self.current_series
        if s is None:
            return None
        case = self._main._seg.case(s)
        return None if not case.editable or case.is_empty() else case.mask

    @property
    def landmarks(self):
        return [dict(p) for p in self._main._landmarks]

    def add_series(self, array, name="Result", like=None, select=True):
        """배열 (슬라이스, 행, 열) 또는 2D → 새 시리즈 (like 시리즈와 같은 위치·간격)"""
        from . import derived_series
        like = like or self.current_series
        if like is None:
            raise ValueError("기준 시리즈가 없습니다.")
        array = np.asarray(array, dtype=np.float32)
        if array.ndim == 2:
            array = array[None]
        series = derived_series(array, like, name, "console")
        self._main.add_derived_series(series, select=select)
        return series

    def set_mask(self, mask, series=None):
        """AI 마스크로 표시 (라벨 번호 배열)"""
        series = series or self.current_series
        self._main._apply_mask(series, np.asarray(mask, dtype=np.uint8))

    def go_to_slice(self, index):
        self.viewport.go_to_slice(int(index))

    def set_window(self, center, width):
        self.viewport.set_window(center, width, user=True)

    def open(self, path):
        """파일/폴더 불러오기 (DICOM, NIfTI 등)"""
        self._main.load_paths([path])


class PythonConsoleDock(QDockWidget):
    def __init__(self, main_window):
        super().__init__("Python Console (F3)", main_window)
        self.setObjectName("PythonConsoleDock")
        self.main = main_window
        self.api = ConsoleAPI(main_window)
        self.namespace = self._make_namespace()
        self._script_path = None

        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSize(12)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        buttons = QHBoxLayout()
        for text, tip, slot in (
                ("▶ 실행", "전체 실행 (Ctrl+Enter). 선택한 부분이 있으면 그 부분만", self.run),
                ("열기", "스크립트(.py) 불러오기", self.open_script),
                ("저장", "스크립트 저장", self.save_script),
                ("매크로로 저장", "Analysis 탭 매크로 목록에 추가 (원클릭 실행)", self.save_as_macro),
                ("출력 지우기", "", lambda: self.output.clear()),
                ("변수 초기화", "콘솔 변수를 모두 지움", self.reset_namespace)):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch()
        layout.addLayout(buttons)

        splitter = QSplitter(Qt.Horizontal)
        left = QSplitter(Qt.Vertical)
        self.editor = QPlainTextEdit()
        self.editor.setFont(mono)
        self.editor.setTabStopDistance(4 * self.editor.fontMetrics().horizontalAdvance(" "))
        self.editor.setPlainText(WELCOME)
        self._highlighter = PythonHighlighter(self.editor.document())
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(mono)
        self.output.setStyleSheet("background: #111; color: #ddd;")
        left.addWidget(self.editor)
        left.addWidget(self.output)
        left.setSizes([220, 140])
        splitter.addWidget(left)
        self.plot_label = QLabel("plt 그래프가 여기 표시됩니다")
        self.plot_label.setAlignment(Qt.AlignCenter)
        self.plot_label.setStyleSheet("color: #777;")
        plot_scroll = QScrollArea()
        plot_scroll.setWidgetResizable(True)
        plot_scroll.setWidget(self.plot_label)
        splitter.addWidget(plot_scroll)
        splitter.setSizes([640, 360])
        layout.addWidget(splitter, 1)
        self.setWidget(body)

        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(seq), self.editor)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(self.run)

    def _make_namespace(self):
        from scipy import ndimage
        from . import configure_matplotlib
        configure_matplotlib()
        from matplotlib import pyplot as plt
        plt.switch_backend("Agg")   # 창을 띄우지 않고 실행 후 그래프 창에 그림
        ns = {"app": self.api, "np": np, "ndi": ndimage, "plt": plt,
              "__name__": "__console__"}
        try:
            import skimage
            ns["skimage"] = skimage
        except ImportError:
            pass
        return ns

    def reset_namespace(self):
        self.namespace = self._make_namespace()
        self._write("# 변수를 초기화했습니다.\n")

    def _write(self, text, error=False):
        if not text:
            return
        self.output.moveCursor(self.output.textCursor().End)
        if error:
            self.output.appendHtml(f"<span style='color:#ff6b6b; white-space:pre'>"
                                   f"{_escape(text.rstrip())}</span>")
        else:
            self.output.insertPlainText(text if text.endswith("\n") else text + "\n")
        self.output.ensureCursorVisible()

    def run(self, code=None):
        if not isinstance(code, str):
            cursor = self.editor.textCursor()
            code = cursor.selectedText().replace(" ", "\n") if cursor.hasSelection() \
                else self.editor.toPlainText()
        self.execute(code)

    def execute(self, code, label=None):
        """코드 실행 → 출력·그래프 표시. 성공하면 True"""
        if not code.strip():
            return True
        first = label or code.strip().splitlines()[0][:60]
        self._write(f">>> {first}{' ...' if not label and len(code.strip().splitlines()) > 1 else ''}")
        out = io.StringIO()
        ok = True
        try:
            tree = ast.parse(code, "<console>", "exec")
            last = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last = ast.Expression(tree.body.pop().value)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(tree, "<console>", "exec"), self.namespace)
                if last is not None:
                    value = eval(compile(last, "<console>", "eval"), self.namespace)
                    if value is not None:
                        print(repr(value))
        except Exception:  # noqa: BLE001 - 사용자 코드 오류는 콘솔에 표시
            ok = False
            self._write(out.getvalue())
            out = io.StringIO()
            tb = traceback.format_exc().split("\n")
            # 콘솔 내부 프레임 제외
            self._write("\n".join(line for line in tb if "console.py" not in line), error=True)
        self._write(out.getvalue())
        self._show_plots()
        return ok

    def _show_plots(self):
        plt = self.namespace.get("plt")
        if plt is None or not plt.get_fignums():
            return
        fig = plt.figure(plt.get_fignums()[-1])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close("all")
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        self.plot_label.setPixmap(pix)
        self.plot_label.setStyleSheet("")

    # ─── 파일 ───

    def open_script(self):
        path, _ = QFileDialog.getOpenFileName(self, "스크립트 열기", self._script_path or "",
                                              "Python (*.py);;All Files (*)")
        if path:
            with open(path, encoding="utf-8") as f:
                self.editor.setPlainText(f.read())
            self._script_path = path

    def save_script(self):
        path, _ = QFileDialog.getSaveFileName(self, "스크립트 저장",
                                              self._script_path or "script.py", "Python (*.py)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.editor.toPlainText())
            self._script_path = path
            self.main.statusBar().showMessage(f"저장: {path}", 4000)

    def save_as_macro(self):
        name, ok = QInputDialog.getText(self, "매크로로 저장", "매크로 이름:")
        if ok and name.strip():
            self.main._macros.save(name.strip(), self.editor.toPlainText())
            self.main.statusBar().showMessage(f"매크로 '{name.strip()}' 저장", 4000)

    def load_code(self, code, path=None):
        self.editor.setPlainText(code)
        self._script_path = path
        self.show()
        self.raise_()


def _escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

