# UI Stylesheet and theme constants

MAIN_STYLE = """
QMainWindow {
    background-color: #f8fafc;
}
QGroupBox {
    font-weight: bold;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    margin-top: 8px;
    padding-top: 14px;
    background-color: #ffffff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #1e293b;
}
QTableWidget {
    border: 1px solid #e2e8f0;
    gridline-color: #f1f5f9;
    selection-background-color: #e0f2fe;
    selection-color: #0f172a;
    background-color: #ffffff;
}
QHeaderView::section {
    background-color: #f1f5f9;
    padding: 4px;
    border: none;
    border-bottom: 1px solid #cbd5e1;
    font-weight: bold;
    color: #475569;
}
QLineEdit, QComboBox, QTextEdit {
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 4px 6px;
    background: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
    border: 1px solid #3b82f6;
}
QPushButton {
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 5px 12px;
    background-color: #f8fafc;
}
QPushButton:hover {
    background-color: #e2e8f0;
}
QPushButton:pressed {
    background-color: #cbd5e1;
}
QSplitter::handle {
    background-color: #cbd5e1;
}
QSplitter::handle:horizontal {
    width: 6px;
    border-left: 1px solid #94a3b8;
    border-right: 1px solid #94a3b8;
}
QSplitter::handle:horizontal:hover {
    background-color: #2563eb;
    border-color: #1d4ed8;
}
QSplitter::handle:vertical {
    height: 6px;
    border-top: 1px solid #94a3b8;
    border-bottom: 1px solid #94a3b8;
}
QSplitter::handle:vertical:hover {
    background-color: #2563eb;
    border-color: #1d4ed8;
}
"""

WS_BADGE_STYLE = """
QPushButton {
    background-color: #faf5ff;
    color: #7e22ce;
    border: 1px solid #e9d5ff;
    font-weight: bold;
    border-radius: 4px;
    padding: 4px 8px;
}
QPushButton:hover {
    background-color: #f3e8ff;
}
QPushButton:checked {
    background-color: #7e22ce;
    color: #ffffff;
    border: 1px solid #6b21a8;
}
"""

USEFUL_FILTER_BTN_STYLE = """
QPushButton {
    background-color: #eff6ff;
    color: #1d4ed8;
    border: 1px solid #bfdbfe;
    font-weight: bold;
    border-radius: 4px;
    padding: 4px 10px;
}
QPushButton:hover {
    background-color: #dbeafe;
}
QPushButton:checked {
    background-color: #2563eb;
    color: #ffffff;
    border: 1px solid #1d4ed8;
}
"""
