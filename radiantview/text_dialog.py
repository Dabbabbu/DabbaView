"""
텍스트 주석 입력 대화상자 (내용, 글꼴 크기, 색상)
"""
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QLineEdit, QSpinBox, QPushButton, QColorDialog,
                             QDialogButtonBox, QButtonGroup)
from PyQt5.QtGui import QColor

PRESET_COLORS = ["#ffff00", "#ffffff", "#00ff80", "#00c8ff", "#ff5050", "#ff8c00"]


class TextAnnotationDialog(QDialog):

    def __init__(self, parent=None, text="", size=14, color="#ffff00"):
        super().__init__(parent)
        self.setWindowTitle("Text Annotation")
        self._color = QColor(color)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._text = QLineEdit(text)
        self._text.setPlaceholderText("메모 내용")
        form.addRow("Text:", self._text)

        self._size = QSpinBox()
        self._size.setRange(8, 72)
        self._size.setValue(size)
        self._size.setSuffix(" pt")
        form.addRow("Size:", self._size)

        color_row = QHBoxLayout()
        self._swatches = QButtonGroup(self)
        for hex_color in PRESET_COLORS:
            btn = QPushButton()
            btn.setFixedSize(24, 24)
            btn.setCheckable(True)
            btn.setStyleSheet(f"QPushButton {{ background: {hex_color}; border: 1px solid #555; }}"
                              "QPushButton:checked { border: 3px solid #007acc; }")
            btn.setChecked(QColor(hex_color) == self._color)
            btn.clicked.connect(lambda _, c=hex_color: self._set_color(QColor(c)))
            self._swatches.addButton(btn)
            color_row.addWidget(btn)
        more = QPushButton("More…")
        more.clicked.connect(self._pick_color)
        color_row.addWidget(more)
        color_row.addStretch()
        form.addRow("Color:", color_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_color(self, color):
        self._color = color

    def _pick_color(self):
        color = QColorDialog.getColor(self._color, self, "Text Color")
        if color.isValid():
            self._color = color
            for btn in self._swatches.buttons():
                btn.setChecked(False)

    def values(self):
        return self._text.text().strip(), self._size.value(), self._color.name()

    @staticmethod
    def get_annotation(parent=None, text="", size=14, color="#ffff00"):
        """(text, size, color) 또는 None (취소/빈 문자열)"""
        dialog = TextAnnotationDialog(parent, text, size, color)
        if dialog.exec_() != QDialog.Accepted:
            return None
        values = dialog.values()
        return values if values[0] else None
