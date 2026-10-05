"""Interface des modèles d'antennes : diagrammes polaires, gestionnaire de bibliothèque,
options d'importation des fichiers texte et calcul de la PAR."""

import math
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QMessageBox, QPushButton, QSplitter,
    QVBoxLayout, QWidget,
)

from . import antennas
from .i18n import N_, tr

RINGS_DB = (0, -3, -10, -20, -30)
RANGE_DB = 40.0


class PatternPlot(QWidget):
    """Diagramme polaire en dB : plan horizontal (lobe vers le haut, sens horaire) ou plan
    vertical (horizon à droite, ciel en haut, sol en bas)."""

    def __init__(self, plane, parent=None):
        super().__init__(parent)
        self.plane = plane
        self.pattern = None
        self.rotation = 0.0          # azimut du lobe (plan horizontal) ou inclinaison (plan vertical)
        self.setMinimumSize(260, 260)

    def set_pattern(self, pattern):
        self.pattern = pattern
        self.update()

    def set_rotation(self, degrees):
        """Oriente le diagramme : azimut du lobe (0 = nord en haut) ou inclinaison vers le sol."""
        self.rotation = float(degrees)
        self.update()

    def _radius(self, db, r_max):
        return r_max * max(0.0, (db + RANGE_DB) / RANGE_DB)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(255, 255, 255))
        title = tr("Plan horizontal (azimut)") if self.plane == "az" else tr("Plan vertical (élévation)")
        font = QFont()
        font.setPixelSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(40, 40, 40))
        painter.drawText(QRectF(0, 2, self.width(), 18), Qt.AlignmentFlag.AlignCenter, title)
        cx, cy = self.width() / 2, (self.height() + 18) / 2
        r_max = min(self.width(), self.height() - 18) / 2 - 22
        small = QFont()
        small.setPixelSize(10)
        painter.setFont(small)
        for db in RINGS_DB:
            r = self._radius(db, r_max)
            painter.setPen(QPen(QColor(200, 200, 200) if db else QColor(150, 150, 150), 1))
            painter.drawEllipse(QPointF(cx, cy), r, r)
            if db == -3 and r_max < 90:
                continue                  # petit diagramme : étiquette collée à celle de 0 dB
            painter.setPen(QColor(130, 130, 130))
            painter.drawText(QPointF(cx + 3, cy - r + 11), f"{db} dB")
        for angle in range(0, 360, 30):
            rad = math.radians(angle)
            painter.setPen(QPen(QColor(225, 225, 225), 1))
            painter.drawLine(QPointF(cx, cy), QPointF(cx + r_max * math.sin(rad), cy - r_max * math.cos(rad)))
            # Plan vertical : à droite l'horizon devant (0°), en haut le zénith, à gauche l'arrière.
            label = f"{angle}°" if self.plane == "az" else f"{90 - angle if angle <= 180 else angle - 270:+d}°"
            lx, ly = cx + (r_max + 12) * math.sin(rad), cy - (r_max + 12) * math.cos(rad)
            painter.setPen(QColor(110, 110, 110))
            painter.drawText(QRectF(lx - 20, ly - 7, 40, 14), Qt.AlignmentFlag.AlignCenter, label)
        if self.plane == "el":
            painter.setPen(QPen(QColor(120, 90, 40), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(cx - r_max, cy), QPointF(cx + r_max, cy))   # horizon
        if not self.pattern:
            return
        path = QPainterPath()
        if self.plane == "az":
            if not self.pattern.get("az"):
                painter.setPen(QColor(120, 120, 120))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("omnidirectionnel\n(aucune donnée)"))
                return
            az = antennas.az_array(self.pattern)
            for i in range(361):
                rad = math.radians(i % 360 + self.rotation)
                r = self._radius(az[i % 360], r_max)
                point = QPointF(cx + r * math.sin(rad), cy - r * math.cos(rad))
                path.moveTo(point) if i == 0 else path.lineTo(point)
        else:
            if not self.pattern.get("el"):
                painter.setPen(QColor(120, 120, 120))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("isotrope\n(aucune donnée)"))
                return
            angles, values = antennas.el_curve(self.pattern)
            fine = np.linspace(angles.min(), angles.max(), 721)
            interp = np.interp(fine, angles, values)
            for i, (elev, db) in enumerate(zip(fine, interp)):
                rad = math.radians(elev - self.rotation)   # 0 = horizon (à droite), +90 = haut
                r = self._radius(db, r_max)
                point = QPointF(cx + r * math.cos(rad), cy - r * math.sin(rad))
                path.moveTo(point) if i == 0 else path.lineTo(point)
        painter.setPen(QPen(QColor(200, 30, 30), 2))
        painter.drawPath(path)


class GenericImportDialog(QDialog):
    """Options pour un fichier texte / CSV à deux colonnes."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Importer « {name} »", name=Path(path).name))
        form = QFormLayout(self)
        form.addRow(QLabel(tr("Fichier texte à deux colonnes (angle, valeur) : précisez son contenu.")))
        self.plane = QComboBox()
        self.plane.addItem(tr("Plan horizontal (azimut)"), "az")
        self.plane.addItem(tr("Plan vertical (élévation)"), "el")
        self.values = QComboBox()
        for label, key in ((N_("Automatique"), "auto"), (N_("Gain en dB / dBi"), "db"),
                           (N_("Atténuation en dB (positive)"), "att"), (N_("Champ relatif linéaire (0 à 1)"), "lin")):
            self.values.addItem(tr(label), key)
        self.clockwise = QComboBox()
        self.clockwise.addItem(tr("Sens horaire (vu de dessus)"), True)
        self.clockwise.addItem(tr("Sens antihoraire (convention NEC / EZNEC)"), False)
        self.elevation = QComboBox()
        self.elevation.addItem(tr("Élévation : positive vers le ciel"), True)
        self.elevation.addItem(tr("Inclinaison : positive vers le sol"), False)
        form.addRow(tr("Plan"), self.plane)
        form.addRow(tr("Valeurs"), self.values)
        form.addRow(tr("Angles d'azimut"), self.clockwise)
        form.addRow(tr("Angles verticaux"), self.elevation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def options(self):
        return {"plane": self.plane.currentData(), "values": self.values.currentData(),
                "clockwise": self.clockwise.currentData(), "elevation_up": self.elevation.currentData()}


def read_patterns(parent, path):
    """Importe un fichier (avec questions si nécessaire) ; renvoie la liste des diagrammes."""
    options = None
    text = antennas._read_text(path)
    if antennas.detect_format(path, text) == "generic":
        dialog = GenericImportDialog(path, parent)
        if not dialog.exec():
            return []
        options = dialog.options()
    try:
        _fmt, patterns = antennas.import_file(path, options)
    except (antennas.PatternError, OSError, ValueError) as exc:
        QMessageBox.warning(parent, tr("Importation impossible"), f"{Path(path).name} : {exc}")
        return []
    if len(patterns) > 1:
        labels = [p["name"] for p in patterns]
        choice, ok = QInputDialog.getItem(parent, tr("Plusieurs diagrammes"),
                                          tr("Le fichier contient plusieurs diagrammes (fréquences) :"),
                                          ["Tous"] + labels, 0, False)
        if not ok:
            return []
        if choice != "Tous":
            patterns = [patterns[labels.index(choice)]]
    return patterns


class AntennaManager(QDialog):
    """Bibliothèque des modèles d'antennes (dossier antennas/)."""

    def __init__(self, parent=None, select=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Modèles d'antennes"))
        self.resize(1000, 640)
        self.changed = False
        self.list = QListWidget()
        self.list.currentTextChanged.connect(self._show)
        self.az_plot = PatternPlot("az")
        self.el_plot = PatternPlot("el")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        buttons = QVBoxLayout()
        for text, slot, tip in (
                (N_("Importer…"), self._import,
                 N_("MSI / Planet, sortie NEC-2 (4nec2, nec2c), EZNEC FF Tab, SPLAT! .az/.el, texte / CSV")),
                (N_("Compléter avec un fichier…"), self._complete,
                 N_("Ajoute le plan manquant (ex. diagramme d'élévation EZNEC) à l'antenne sélectionnée")),
                (N_("Inverser le sens de l'azimut"), self._mirror,
                 N_("À utiliser si le diagramme horizontal apparaît en miroir")),
                (N_("Renommer…"), self._rename, ""),
                (N_("Dupliquer"), self._duplicate, ""),
                (N_("Supprimer"), self._delete, ""),
                (N_("Exporter en .az / .el…"), self._export, N_("Fichiers SPLAT! (rotation 0°, sans inclinaison)"))):
            button = QPushButton(tr(text))
            button.setToolTip(tr(tip) if tip else "")
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.list, 1)
        left_layout.addLayout(buttons)

        plots = QHBoxLayout()
        plots.addWidget(self.az_plot)
        plots.addWidget(self.el_plot)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addLayout(plots, 1)
        right_layout.addWidget(self.info)
        note = QLabel(tr("SPLAT! applique les diagrammes d'antenne aux calculs de perte de trajet et de champ "
                      "(mode -L) et au point à point ; la couverture en visibilité (-c) les ignore. "
                      "La PAR (onglet Analyse) doit inclure le gain de l'antenne."))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        right_layout.addWidget(note)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([280, 720])
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addWidget(close)
        self._refresh(select)

    def _refresh(self, select=None):
        current = select or (self.list.currentItem().text() if self.list.currentItem() else None)
        self.list.clear()
        self.list.addItems(antennas.library_names())
        matches = self.list.findItems(current, Qt.MatchFlag.MatchExactly) if current else []
        self.list.setCurrentItem(matches[0] if matches else self.list.item(0))
        if self.list.count() == 0:
            self._show("")

    def _current(self):
        item = self.list.currentItem()
        return antennas.load(item.text()) if item else None

    def _show(self, name):
        pattern = antennas.load(name) if name else None
        self.az_plot.set_pattern(pattern)
        self.el_plot.set_pattern(pattern)
        if pattern:
            text = f"<b>{pattern['name']}</b><br>{antennas.describe(pattern)}"
            if pattern.get("source_file"):
                text += "<br>" + tr("Source : {file}", file=pattern["source_file"])
            if pattern.get("comment"):
                text += f"<br>{pattern['comment']}"
            self.info.setText(text)
        else:
            self.info.setText(tr("Aucune antenne : importez un diagramme de rayonnement."))

    def _unique(self, name):
        existing = set(antennas.library_names())
        candidate, n = name, 2
        while candidate in existing:
            candidate, n = f"{name} ({n})", n + 1
        return candidate

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Importer des diagrammes"), "", tr(antennas.FILE_FILTER))
        last = None
        for path in paths:
            for pattern in read_patterns(self, path):
                pattern["name"] = self._unique(pattern["name"])
                antennas.save(pattern, path)
                last = pattern["name"]
                self.changed = True
        if last:
            self._refresh(last)

    def _complete(self):
        pattern = self._current()
        if pattern is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("Compléter avec un fichier"), "", tr(antennas.FILE_FILTER))
        if not path:
            return
        others = read_patterns(self, path)
        if others:
            antennas.save(antennas.merge(pattern, others[0]), path)
            self.changed = True
            self._refresh(pattern["name"])

    def _mirror(self):
        pattern = self._current()
        if pattern and pattern.get("az"):
            antennas.save(antennas.mirror_az(pattern))
            self.changed = True
            self._show(pattern["name"])

    def _rename(self):
        pattern = self._current()
        if pattern is None:
            return
        name, ok = QInputDialog.getText(self, tr("Renommer"), tr("Nouveau nom :"), text=pattern["name"])
        name = name.strip()
        if ok and name and name != pattern["name"]:
            antennas.rename(pattern["name"], self._unique(name))
            self.changed = True
            self._refresh(name)

    def _duplicate(self):
        pattern = self._current()
        if pattern:
            pattern["name"] = self._unique(pattern["name"])
            antennas.save(pattern)
            self.changed = True
            self._refresh(pattern["name"])

    def _delete(self):
        pattern = self._current()
        if pattern and QMessageBox.question(self, tr("Supprimer"), tr("Supprimer l'antenne « {name} » ?", name=pattern["name"])) \
                == QMessageBox.StandardButton.Yes:
            antennas.delete(pattern["name"])
            self.changed = True
            self._refresh()

    def _export(self):
        pattern = self._current()
        if pattern is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Exporter en .az / .el"), pattern["name"] + ".az",
                                              tr("SPLAT! (*.az)"))
        if path:
            az, el = antennas.write_splat_files(pattern, Path(path).with_suffix(""))
            QMessageBox.information(self, tr("Exporter"), tr("Fichiers écrits :") + f"\n{az}\n{el}")
