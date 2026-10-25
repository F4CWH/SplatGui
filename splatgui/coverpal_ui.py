"""Fenêtre des palettes de couleurs (Fichier → Palettes de couleurs…) : choix de la palette
des cartes -L, création, modification et suppression des palettes personnelles (couleurs et
seuils)."""

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from . import coverpal
from .i18n import N_, tr

# Grandeur représentée -> (libellé, unité) ; SPLAT! choisit selon la PAR et l'option -dbm.
KINDS = {
    ".dcf": (N_("Puissance reçue (-dbm)"), "dBm"),
    ".scf": (N_("Champ électrique (PAR indiquée)"), "dBµV/m"),
    ".lcf": (N_("Perte de trajet (sans PAR)"), "dB"),
}


def strip_pixmap(palette, size=QSize(160, 16)):
    """Bande des couleurs de la palette, à parts égales."""
    pixmap = QPixmap(size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    colors = palette["colors"]
    width = size.width() / len(colors)
    for i, rgb in enumerate(colors):
        painter.fillRect(QRectF(i * width, 0, width + 1, size.height()), QColor(*rgb))
    painter.setPen(QPen(QColor(0, 0, 0, 90)))
    painter.drawRect(0, 0, size.width() - 1, size.height() - 1)
    painter.end()
    return pixmap


def levels_text(levels):
    return ", ".join(f"{v:g}" for v in levels)


class PaletteBar(QWidget):
    """Aperçu d'une palette : une case par seuil, avec la valeur du seuil dessous."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.palette_ = None
        self.kind = ".dcf"
        self.setMinimumSize(420, 52)

    def set_palette(self, palette):
        self.palette_ = palette
        self.update()

    def set_kind(self, kind):
        self.kind = kind
        self.update()

    def paintEvent(self, _event):
        if not self.palette_ or len(self.palette_["colors"]) < 2:
            return
        painter = QPainter(self)
        entries = coverpal.levels_and_colors(self.palette_, self.kind)
        metrics = QFontMetricsF(self.font())
        bar_height = self.height() - metrics.height() - 6
        width = (self.width() - 1) / len(entries)
        text_color = self.palette().color(self.foregroundRole())
        for i, (level, rgb) in enumerate(entries):
            cell = QRectF(i * width, 0, width, bar_height)
            painter.fillRect(cell, QColor(*rgb))
            painter.setPen(text_color)
            painter.drawText(QRectF(cell.left(), bar_height + 3, width, metrics.height()),
                             Qt.AlignmentFlag.AlignCenter, f"{level:g}")
        painter.setPen(QPen(QColor(0, 0, 0, 90)))
        painter.drawRect(QRectF(0, 0, self.width() - 1, bar_height))
        painter.end()


class PaletteEditor(QDialog):
    """Nom, couleurs (du signal le plus fort au plus faible) et seuils d'une palette personnelle."""

    def __init__(self, name, palette, taken, parent=None):
        """`taken` : noms déjà utilisés (insensible à la casse), interdits pour cette palette."""
        super().__init__(parent)
        self.setWindowTitle(tr("Palette de couleurs"))
        self.resize(600, 640)
        self.taken = {t.lower() for t in taken}

        self.name = QLineEdit(name)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setIconSize(QSize(40, 16))
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())
        self.list.model().rowsMoved.connect(self._update_preview)
        for rgb in palette["colors"]:
            self._add_item(tuple(rgb))

        buttons = QVBoxLayout()
        for text, slot in ((N_("Ajouter…"), self._add), (N_("Modifier…"), self._edit),
                           (N_("Supprimer"), self._remove), (N_("Monter"), lambda: self._move(-1)),
                           (N_("Descendre"), lambda: self._move(1)), (N_("Inverser l'ordre"), self._reverse)):
            button = QPushButton(tr(text))
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        colors_row = QHBoxLayout()
        colors_row.addWidget(self.list, 1)
        colors_row.addLayout(buttons)

        levels_box = QGroupBox(tr("Seuils (vide : ceux de SPLAT!)"))
        levels_form = QFormLayout(levels_box)
        levels_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.level_edits = {}
        for kind, (label, unit) in KINDS.items():
            edit = QLineEdit(levels_text(palette["levels"].get(kind, [])))
            edit.setPlaceholderText(levels_text(coverpal.LEVELS[kind]))
            edit.setToolTip(tr("Entiers séparés par des virgules, triés automatiquement. SPLAT! colore chaque "
                               "point avec le premier seuil atteint ; sous le dernier, le relief reste en gris."))
            edit.textChanged.connect(self._update_preview)
            levels_form.addRow(f"{tr(label)} [{unit}]", edit)
            self.level_edits[kind] = edit
        self.levels_error = QLabel()
        self.levels_error.setStyleSheet("color: #c0392b;")
        self.levels_error.setWordWrap(True)
        levels_form.addRow(self.levels_error)

        self.kind = QComboBox()
        for kind, (label, _unit) in KINDS.items():
            self.kind.addItem(tr(label), kind)
        self.preview = PaletteBar()
        self.count = QLabel()
        self.count.setStyleSheet("color: gray;")
        self.kind.currentIndexChanged.connect(self._update_preview)

        note = QLabel(tr("Couleurs du signal le plus fort (en haut) au plus faible (en bas), réparties sur les "
                         "seuils (interpolées si leur nombre diffère : autant de couleurs que de seuils donne des "
                         "bandes franches). Glisser-déposer pour réordonner, double-clic pour changer une couleur."))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        form = QFormLayout()
        form.addRow(tr("Nom"), self.name)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel(tr("Couleurs")))
        layout.addLayout(colors_row, 1)
        layout.addWidget(note)
        layout.addWidget(levels_box)
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel(tr("Aperçu")))
        preview_row.addWidget(self.kind, 1)
        layout.addLayout(preview_row)
        layout.addWidget(self.preview)
        layout.addWidget(self.count)
        layout.addWidget(box)
        self._update_preview()

    def _add_item(self, rgb, row=None):
        item = QListWidgetItem(f"{rgb[0]}, {rgb[1]}, {rgb[2]}   ({QColor(*rgb).name()})")
        item.setData(Qt.ItemDataRole.UserRole, rgb)
        pixmap = QPixmap(40, 16)
        pixmap.fill(QColor(*rgb))
        item.setIcon(QIcon(pixmap))
        if row is None:
            self.list.addItem(item)
        else:
            self.list.insertItem(row, item)
        return item

    def colors(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def levels(self):
        """{grandeur: seuils} saisis ; lève ValueError (message préfixé de la grandeur)."""
        levels = {}
        for kind, edit in self.level_edits.items():
            try:
                values = coverpal.parse_levels(edit.text(), kind)
            except ValueError as exc:
                raise ValueError(f"{tr(KINDS[kind][0])} : {exc}") from None
            if values:
                levels[kind] = values
        return levels

    def _update_preview(self, *_args):
        try:
            levels = self.levels()
            self.levels_error.clear()
        except ValueError as exc:
            levels = {}
            self.levels_error.setText(str(exc))
        palette = {"colors": self.colors(), "levels": levels}
        kind = self.kind.currentData()
        self.preview.set_kind(kind)
        self.preview.set_palette(palette)
        n_colors, n_levels = len(palette["colors"]), len(coverpal.levels_of(palette, kind))
        self.count.setText(tr("{colors} couleur(s), {levels} seuils", colors=n_colors, levels=n_levels)
                           + ("" if n_colors == n_levels else " — " + tr("couleurs interpolées")))

    def _pick(self, rgb):
        color = QColorDialog.getColor(QColor(*rgb), self, tr("Couleur"))
        return (color.red(), color.green(), color.blue()) if color.isValid() else None

    def _add(self):
        if self.list.count() >= coverpal.MAX_COLORS:
            QMessageBox.information(self, self.windowTitle(),
                                    tr("{n} couleurs au maximum.", n=coverpal.MAX_COLORS))
            return
        row = self.list.currentRow()
        start = self.list.item(row).data(Qt.ItemDataRole.UserRole) if row >= 0 else (0, 160, 0)
        rgb = self._pick(start)
        if rgb:
            item = self._add_item(rgb, row + 1 if row >= 0 else None)
            self.list.setCurrentItem(item)
            self._update_preview()

    def _edit(self):
        row = self.list.currentRow()
        if row < 0:
            return
        rgb = self._pick(self.list.item(row).data(Qt.ItemDataRole.UserRole))
        if rgb:
            self.list.takeItem(row)
            self.list.setCurrentItem(self._add_item(rgb, row))
            self._update_preview()

    def _remove(self):
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
            self._update_preview()

    def _move(self, step):
        row = self.list.currentRow()
        if row < 0 or not 0 <= row + step < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(row + step, item)
        self.list.setCurrentRow(row + step)
        self._update_preview()

    def _reverse(self):
        colors = self.colors()[::-1]
        self.list.clear()
        for rgb in colors:
            self._add_item(rgb)
        self._update_preview()

    def _accept(self):
        name = self.name.text().strip()
        try:
            self.levels()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        if not name:
            QMessageBox.warning(self, self.windowTitle(), tr("Donnez un nom à la palette."))
        elif name.lower() in self.taken:
            QMessageBox.warning(self, self.windowTitle(), tr("Une palette porte déjà ce nom."))
        elif self.list.count() < 2:
            QMessageBox.warning(self, self.windowTitle(), tr("Une palette compte au moins deux couleurs."))
        else:
            self.accept()

    def value(self):
        return {"name": self.name.text().strip(), "colors": [list(rgb) for rgb in self.colors()],
                "levels": self.levels()}


class PaletteDialog(QDialog):
    """Choix de la palette des cartes -L et gestion des palettes personnelles."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Palettes de couleurs"))
        self.resize(640, 460)
        self.user = [dict(p) for p in coverpal.user_palettes(settings)]
        self.current = settings.get("coverage_palette", coverpal.DEFAULT)

        intro = QLabel(tr("Couleurs des contours des cartes de couverture RF (-L) : perte de trajet, "
                          "champ électrique ou puissance reçue. La palette choisie s'applique aux prochains calculs."))
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.setIconSize(QSize(160, 16))
        self.list.currentItemChanged.connect(self._selection_changed)
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())

        buttons = QVBoxLayout()
        self.new_button = QPushButton(tr("Nouvelle…"))
        self.new_button.clicked.connect(self._new)
        self.copy_button = QPushButton(tr("Dupliquer…"))
        self.copy_button.setToolTip(tr("Nouvelle palette personnelle à partir de la palette sélectionnée"))
        self.copy_button.clicked.connect(self._copy)
        self.edit_button = QPushButton(tr("Modifier…"))
        self.edit_button.clicked.connect(self._edit)
        self.delete_button = QPushButton(tr("Supprimer"))
        self.delete_button.clicked.connect(self._delete)
        for button in (self.new_button, self.copy_button, self.edit_button, self.delete_button):
            buttons.addWidget(button)
        buttons.addStretch()
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(buttons)

        self.kind = QComboBox()
        for kind, (label, _unit) in KINDS.items():
            self.kind.addItem(tr(label), kind)
        self.unit = QLabel()
        self.preview = PaletteBar()
        self.kind.currentIndexChanged.connect(self._kind_changed)
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel(tr("Aperçu")))
        preview_row.addWidget(self.kind, 1)
        preview_row.addWidget(self.unit)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(row, 1)
        layout.addLayout(preview_row)
        layout.addWidget(self.preview)
        layout.addWidget(box)
        self._fill(self.current)
        self._kind_changed()

    def _settings(self):
        return {"coverage_palettes": self.user}

    def _fill(self, select):
        self.list.clear()
        for key, label, palette in coverpal.choices(self._settings()):
            item = QListWidgetItem(QIcon(strip_pixmap(palette)), label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            if not key.startswith(coverpal.USER_PREFIX):
                item.setToolTip(tr("Palette fournie (non modifiable : la dupliquer pour en créer une variante)"))
            self.list.addItem(item)
            if key == select:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None:
            self.list.setCurrentRow(0)

    def _key(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else coverpal.DEFAULT

    def _user_index(self, key):
        name = key.removeprefix(coverpal.USER_PREFIX) if key.startswith(coverpal.USER_PREFIX) else None
        return next((i for i, p in enumerate(self.user) if p["name"] == name), None)

    def _selection_changed(self, *_args):
        key = self._key()
        self.preview.set_palette(coverpal.palette_of(key, self._settings()))
        editable = self._user_index(key) is not None
        self.edit_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)

    def _kind_changed(self):
        kind = self.kind.currentData()
        self.preview.set_kind(kind)
        self.unit.setText(KINDS[kind][1])

    def _taken(self, exclude=None):
        names = [label for key, label, _p in coverpal.choices(self._settings())
                 if not key.startswith(coverpal.USER_PREFIX)]
        return names + [p["name"] for i, p in enumerate(self.user) if i != exclude]

    def _open_editor(self, name, palette, index=None):
        editor = PaletteEditor(name, palette, self._taken(index), self)
        if not editor.exec():
            return
        value = editor.value()
        if index is None:
            self.user.append(value)
        else:
            self.user[index] = value
        self._fill(coverpal.USER_PREFIX + value["name"])

    def _new(self):
        self._open_editor("", {"colors": [(0, 150, 60), (240, 220, 40), (200, 30, 30)], "levels": {}})

    def _copy(self):
        key = self._key()
        label = self.list.currentItem().text() if self.list.currentItem() else ""
        self._open_editor(tr("{name} (copie)", name=label), coverpal.palette_of(key, self._settings()))

    def _edit(self):
        index = self._user_index(self._key())
        if index is None:
            return
        palette = self.user[index]
        self._open_editor(palette["name"], coverpal.palette_of(coverpal.USER_PREFIX + palette["name"],
                                                               self._settings()), index)

    def _delete(self):
        index = self._user_index(self._key())
        if index is None:
            return
        if QMessageBox.question(self, self.windowTitle(), tr("Supprimer la palette « {name} » ?",
                                                             name=self.user[index]["name"])) \
                == QMessageBox.StandardButton.Yes:
            del self.user[index]
            self._fill(coverpal.DEFAULT)

    def values(self):
        return {"coverage_palette": self._key(), "coverage_palettes": self.user}
