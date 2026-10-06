"""Aide intégrée : pages d'aide tirées de la documentation de SPLAT! (splatgui/help/<langue>.html,
produites par splatgui/resources/make_help.py à partir des PDF français, anglais et espagnol),
avec sommaire, index et recherche plein texte (sans tenir compte des accents ni de la casse).

Sommaire : les rubriques <h2>. Index : options de la ligne de commande (avec leur description),
rubriques, types de fichiers et sigles cités dans le texte.
"""

import re
import unicodedata
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QKeySequence, QPalette, QShortcut, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QSplitter, QTabWidget, QTextBrowser,
    QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import i18n
from .i18n import tr

HELP_DIR = Path(__file__).parent / "help"
TARGET = Qt.ItemDataRole.UserRole          # (position dans le texte, longueur) d'une entrée
EXTENSIONS = (".qth", ".lrp", ".sdf", ".az", ".el", ".ant", ".scf", ".dcf", ".lcf", ".ppm", ".kml", ".geo",
              ".dat", ".udt", ".txt")
IGNORED_ACRONYMS = {"SPLAT", "GNU", "ASCII", "RF", "II", "III"}


def page_path(lang):
    path = HELP_DIR / f"{lang}.html"
    return path if path.exists() else None


def pdf_path(lang):
    path = HELP_DIR / f"splat_{lang}.pdf"
    return path if path.exists() else None


def fold(text):
    """Minuscules sans accents, caractère pour caractère : les positions restent celles du texte."""
    return "".join(unicodedata.normalize("NFD", c)[0].casefold()[:1] or c for c in text)


class HelpWindow(QWidget):
    """Fenêtre d'aide non modale (une seule instance, gardée par la fenêtre principale)."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(tr("Aide de SPLAT!"))
        self.resize(1100, 800)
        self.lang = None
        self.plain = self.folded = ""
        self.hits = []              # positions des résultats de la recherche courante
        self.hit_length = 0

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)

        # Barre d'outils
        self.language = QComboBox()
        for code, label in i18n.LANGUAGES.items():
            if page_path(code):
                self.language.addItem(label, code)
        self.language.setToolTip(tr("Langue de la documentation"))
        zoom_out, zoom_in = self._tool("−", tr("Zoom arrière")), self._tool("+", tr("Zoom avant"))
        zoom_out.clicked.connect(lambda: self.browser.zoomOut(1))
        zoom_in.clicked.connect(lambda: self.browser.zoomIn(1))
        self.open_pdf = self._tool("⧉ " + tr("Ouvrir le PDF"), tr("Ouvrir la documentation d'origine (PDF)"))
        self.open_pdf.clicked.connect(self._open_pdf)
        bar = QHBoxLayout()
        bar.addWidget(QLabel(tr("Langue :")))
        bar.addWidget(self.language)
        bar.addSpacing(12)
        bar.addWidget(zoom_out)
        bar.addWidget(zoom_in)
        bar.addStretch(1)
        bar.addWidget(self.open_pdf)

        # Panneau latéral : sommaire, index, recherche
        self.contents = QTreeWidget()
        self.contents.setHeaderHidden(True)
        self.contents.setRootIsDecorated(False)
        self.contents.itemClicked.connect(self._go_item)
        self.contents.itemActivated.connect(self._go_item)

        self.index_filter = QLineEdit()
        self.index_filter.setPlaceholderText(tr("Filtrer l'index…"))
        self.index_filter.setClearButtonEnabled(True)
        self.index_filter.textChanged.connect(self._filter_index)
        self.index_filter.returnPressed.connect(self._go_first_index)
        self.index_list = QListWidget()
        self.index_list.setWordWrap(True)
        self.index_list.itemClicked.connect(self._go_item)
        self.index_list.itemActivated.connect(self._go_item)
        index_page = QWidget()
        box = QVBoxLayout(index_page)
        box.setContentsMargins(0, 4, 0, 0)
        box.addWidget(self.index_filter)
        box.addWidget(self.index_list, 1)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(tr("Rechercher dans l'aide…"))
        self.search_edit.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=250)
        self._search_timer.timeout.connect(self._search)
        self.search_edit.textChanged.connect(lambda _: self._search_timer.start())
        self.search_edit.returnPressed.connect(lambda: self._step(1))
        previous_hit = self._tool("▲", tr("Résultat précédent (Maj+F3)"))
        next_hit = self._tool("▼", tr("Résultat suivant (F3)"))
        previous_hit.clicked.connect(lambda: self._step(-1))
        next_hit.clicked.connect(lambda: self._step(1))
        self.search_count = QLabel()
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setAlternatingRowColors(True)
        self.results.currentRowChanged.connect(self._show_hit)
        search_page = QWidget()
        box = QVBoxLayout(search_page)
        box.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.search_edit, 1)
        row.addWidget(previous_hit)
        row.addWidget(next_hit)
        box.addLayout(row)
        box.addWidget(self.search_count)
        box.addWidget(self.results, 1)

        self.side = QTabWidget()
        self.side.addTab(self.contents, tr("Sommaire"))
        self.side.addTab(index_page, tr("Index"))
        self.side.addTab(search_page, tr("Recherche"))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.side)
        splitter.addWidget(self.browser)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 770])
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(splitter, 1)

        QShortcut(QKeySequence.StandardKey.Find, self, self.focus_search)
        QShortcut(QKeySequence.StandardKey.FindNext, self, lambda: self._step(1))
        QShortcut(QKeySequence.StandardKey.FindPrevious, self, lambda: self._step(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)
        self.language.currentIndexChanged.connect(lambda _: self.set_language(self.language.currentData()))

        start = max(0, self.language.findData(i18n.language()))
        if start == self.language.currentIndex():      # pas de signal si l'index ne change pas
            self.set_language(self.language.currentData())
        else:
            self.language.setCurrentIndex(start)

    @staticmethod
    def _tool(text, tip):
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setAutoRaise(True)
        return button

    def _style_sheet(self):
        palette = self.palette()
        code_bg = palette.color(QPalette.ColorRole.AlternateBase).name()
        link = palette.color(QPalette.ColorRole.Link).name()
        return (f"h2 {{ margin-top: 18px; margin-bottom: 6px; }}"
                f"p {{ margin-top: 4px; margin-bottom: 6px; line-height: 125%; }}"
                f"pre {{ background-color: {code_bg}; margin: 4px 0 8px 18px; padding: 6px; }}"
                f"code {{ font-weight: 600; }}"
                f"a {{ color: {link}; }}")

    # --- Contenu ---------------------------------------------------------------------------

    def set_language(self, lang):
        if not lang:
            self.browser.setPlainText(tr("Documentation introuvable."))
            return
        self.lang = lang
        self.open_pdf.setEnabled(pdf_path(lang) is not None)
        self.browser.document().setDefaultStyleSheet(self._style_sheet())
        self.browser.setHtml(page_path(lang).read_text(encoding="utf-8"))
        self.plain = self.browser.document().toPlainText()
        self.folded = fold(self.plain)

        headings = self._headings()
        self.contents.clear()
        for title, position in headings:
            item = QTreeWidgetItem([title])
            item.setData(0, TARGET, (position, 0))
            item.setToolTip(0, title)
            self.contents.addTopLevelItem(item)
        self.index_list.clear()
        for label, detail, position, length in self._index(headings):
            item = QListWidgetItem(f"{label}  —  {detail}" if detail else label)
            item.setData(TARGET, (position, length))
            self.index_list.addItem(item)
        self._filter_index(self.index_filter.text())
        self._search()
        self.browser.moveCursor(QTextCursor.MoveOperation.Start)

    def _headings(self):
        """(titre, position) des rubriques : blocs de titre de niveau 2."""
        found = []
        block = self.browser.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel() == 2 and block.text().strip():
                found.append((block.text().strip(), block.position()))
            block = block.next()
        return found

    def _index(self, headings):
        """Entrées de l'index : (libellé, détail, position, longueur), triées."""
        entries, seen = [], set()
        # Options : première ligne « -t description » (liste des options du programme).
        for m in re.finditer(r"^(-[A-Za-z]+)[ \t]+(\S.*)$", self.plain, re.MULTILINE):
            option, detail = m.group(1), m.group(2).strip()
            if option not in seen and not detail.endswith(":"):
                seen.add(option)
                entries.append((option, detail, m.start(), len(option)))
        entries += [(title, "", position, 0) for title, position in headings]
        # Types de fichiers : rubrique qui leur est consacrée (« … (QTH) »), sinon première mention.
        body_start = headings[2][1] if len(headings) > 2 else 0          # après le synopsis
        for ext in EXTENSIONS:
            heading = next((h for h in headings if f"({ext[1:].upper()})" in h[0]), None)
            m = re.search(re.escape(ext) + r"\b", self.folded[body_start:])
            if heading:
                entries.append((ext, tr("fichier"), heading[1], 0))
            elif m:
                entries.append((ext, tr("fichier"), body_start + m.start(), len(ext)))
        # Sigles cités au moins deux fois (SRTM, ITWOM, ERP…) : première mention dans le texte.
        counts = {}
        for m in re.finditer(r"\b[A-Z][A-Z0-9]{2,6}\b", self.plain[body_start:]):
            counts.setdefault(m.group(0), []).append(body_start + m.start())
        for word, positions in counts.items():
            if len(positions) >= 2 and word not in IGNORED_ACRONYMS:
                entries.append((word, "", positions[0], len(word)))
        entries.sort(key=lambda e: (fold(e[0].lstrip("-.")), e[0]))
        return entries

    def _go_to(self, position, length=0):
        """Amène `position` en haut de la vue et sélectionne `length` caractères."""
        cursor = QTextCursor(self.browser.document())
        cursor.setPosition(min(len(self.plain), position))
        bar = self.browser.verticalScrollBar()
        bar.setValue(bar.value() + self.browser.cursorRect(cursor).top() - 12)
        if length:
            cursor.setPosition(min(len(self.plain), position + length), QTextCursor.MoveMode.KeepAnchor)
        self.browser.setTextCursor(cursor)

    def _open_pdf(self):
        path = pdf_path(self.lang)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # --- Sommaire et index -----------------------------------------------------------------

    def _go_item(self, item, *_):
        target = item.data(0, TARGET) if isinstance(item, QTreeWidgetItem) else item.data(TARGET)
        if target:
            self._go_to(*target)

    def _filter_index(self, text):
        needle = fold(text.strip())
        for row in range(self.index_list.count()):
            item = self.index_list.item(row)
            item.setHidden(bool(needle) and needle not in fold(item.text()))

    def _go_first_index(self):
        for row in range(self.index_list.count()):
            item = self.index_list.item(row)
            if not item.isHidden():
                self.index_list.setCurrentItem(item)
                self._go_item(item)
                return

    # --- Recherche -------------------------------------------------------------------------

    def focus_search(self, text=None):
        self.side.setCurrentIndex(2)
        if text:
            self.search_edit.setText(text)
            self._search()
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _section_of(self, position):
        title = ""
        for row in range(self.contents.topLevelItemCount()):
            item = self.contents.topLevelItem(row)
            if item.data(0, TARGET)[0] > position:
                break
            title = item.text(0)
        return title

    def _search(self):
        term = fold(self.search_edit.text().strip())
        self.results.blockSignals(True)
        self.results.clear()
        self.results.blockSignals(False)
        self.hits = [m.start() for m in re.finditer(re.escape(term), self.folded)] if term else []
        self.hit_length = len(term)
        mark = QTextCharFormat()
        mark.setBackground(QColor("#ffe066"))
        mark.setForeground(QColor("#000000"))
        selections = []
        for position in self.hits:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(self.browser.document())
            selection.cursor.setPosition(position)
            selection.cursor.setPosition(position + self.hit_length, QTextCursor.MoveMode.KeepAnchor)
            selection.format = mark
            selections.append(selection)
            end = position + self.hit_length
            context = (self.plain[max(0, position - 45):position] + "«" + self.plain[position:end] + "»"
                       + self.plain[end:end + 60])
            self.results.addItem(f"{self._section_of(position)}\n…{' '.join(context.split())}…")
        self.browser.setExtraSelections(selections)
        self.search_count.setText("" if not term else tr("{n} résultat(s)", n=len(self.hits)) if self.hits
                                  else tr("Aucun résultat."))

    def _show_hit(self, row):
        if 0 <= row < len(self.hits):
            self._go_to(self.hits[row], self.hit_length)

    def _step(self, delta):
        if self._search_timer.isActive():
            self._search_timer.stop()
            self._search()
        if not self.hits:
            return
        self.side.setCurrentIndex(2)
        row = self.results.currentRow()
        row = (row + delta) % len(self.hits) if row >= 0 else (0 if delta > 0 else len(self.hits) - 1)
        self.results.setCurrentRow(row)
