"""Fenêtre principale de SPLAT!Gui."""

import datetime
import html
import math
import re
import os
import sys
from pathlib import Path

import numpy as np
from PyQt6.QtCore import (
    QByteArray, QProcess, QProcessEnvironment, Qt, QThread, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QFontDatabase, QFontMetricsF, QIcon, QImage, QKeySequence, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QPlainTextEdit, QPushButton, QRadioButton,
    QScrollArea, QSlider, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar,
    QVBoxLayout, QWidget,
)

from . import (
    CREDITS, LICENSE_SHORT, SOURCE_URL, __version__, antenna_ui, antennas, basemap, cables, dem, help, hillshade, i18n, layers, layout, linkprofile, livemap, mappicker, prereqs,
    sites, splat, storage, terrain, themes,
)
from .widgets import FileList, ImageView, PathEdit, SliderValue, graduate
from .i18n import N_, tr

QTH_FILTER = N_("Fichiers de site (*.qth);;Tous les fichiers (*)")
LRP_FILTER = N_("Paramètres ITM (*.lrp);;Tous les fichiers (*)")


def folder_setting(settings, key):
    """Dossier d'un réglage (runs_dir, qth_dir, lrp_dir), ou son emplacement par défaut."""
    defaults = {"runs_dir": storage.PROJECT_DIR / "runs", "qth_dir": storage.QTH_DIR, "lrp_dir": storage.LRP_DIR}
    value = (settings or {}).get(key)
    return Path(value) if value else defaults[key]


def data_folder(folder):
    """Dossier par défaut des boîtes d'import / export (créé au besoin)."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(folder)
PROFILE_FILTER = N_("Profil SPLAT!Gui (*.json)")


def format_size(size):
    """Taille lisible : 512 o, 3,2 Mo, 1,25 Go…"""
    for unit, factor, decimals in ((N_("Go"), 1e9, 2), (N_("Mo"), 1e6, 1), (N_("Ko"), 1e3, 1)):
        if size >= factor:
            return f"{size / factor:.{decimals}f} {tr(unit)}"
    return f"{size} {tr('o')}"


def mono_font():
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(9)
    return font


def spin(minimum, maximum, decimals=3, step=1.0, suffix=""):
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    if suffix:
        box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


def scrollable(widget):
    """Onglet défilant verticalement, ajusté à la largeur disponible : les intitulés des
    formulaires passent au-dessus de leur champ quand la place manque."""
    for form in widget.findChildren(QFormLayout):
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return area


# --- Préparation du relief en arrière-plan ------------------------------------

class OutlinedProgressBar(QProgressBar):
    """Barre de progression dont le texte (blanc) est cerné de sombre : lisible sur la partie
    remplie comme sur un fond clair."""

    def __init__(self, parent=None):
        super().__init__(parent)
        super().setTextVisible(False)        # texte natif masqué : dessiné ci-dessous

    def setTextVisible(self, _visible):
        pass

    def label(self):
        """Libellé (format avec %p remplacé par le pourcentage)."""
        text = self.format()
        if "%p" in text:
            span = self.maximum() - self.minimum()
            percent = round((self.value() - self.minimum()) * 100 / span) if span > 0 else 0
            text = text.replace("%p", str(percent))
        return text

    def paintEvent(self, event):
        super().paintEvent(event)
        text = self.label()
        if not text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = self.font()
        font.setBold(True)
        metrics = QFontMetricsF(font)
        x = (self.width() - metrics.horizontalAdvance(text)) / 2
        y = (self.height() + metrics.ascent() - metrics.descent()) / 2
        path = QPainterPath()
        path.addText(x, y, font, text)
        painter.strokePath(path, QPen(QColor(16, 32, 52, 230), 2.6, Qt.PenStyle.SolidLine,
                                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.fillPath(path, QColor(255, 255, 255))
        painter.end()


class TerrainWorker(QThread):
    """Télécharge et convertit les tuiles de relief sans bloquer l'interface."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)   # tuiles traitées, total
    done = pyqtSignal(object)   # résumé (dict) ou None si annulé

    def __init__(self, function, parent=None):
        """`function(log, cancel, progress)` prépare le relief et renvoie un résumé (dict)."""
        super().__init__(parent)
        self._function = function
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            summary = self._function(self.log.emit, lambda: self._cancelled, self.progress.emit)
        except terrain.Cancelled:
            summary = None
        except Exception as exc:  # erreur inattendue : on la journalise et on continue sans relief
            self.log.emit(tr("Erreur pendant la préparation du relief : {exc}\n", exc=exc))
            summary = {"present": [], "converted": [], "unavailable": [], "failed": [str(exc)]}
        self.done.emit(summary)


class TaskWorker(QThread):
    """Exécute une fonction en arrière-plan (chargement GeoJSON, altitudes…)."""

    done = pyqtSignal(object, object, str)   # worker, résultat, message d'erreur

    def __init__(self, key, function, parent=None):
        super().__init__(parent)
        self.key = key
        self._function = function

    def run(self):
        try:
            self.done.emit(self, self._function(), "")
        except Exception as exc:
            self.done.emit(self, None, str(exc))


class PrereqWorker(QThread):
    """Installe des pré-requis (exécutables SPLAT!, DLL) en arrière-plan."""

    log = pyqtSignal(str)
    done = pyqtSignal(object, str)   # résultat (None si annulé), message d'erreur

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self._function = function
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self.done.emit(self._function(self.log.emit, lambda: self._cancelled), "")
        except prereqs.Cancelled:
            self.done.emit(None, "")
        except Exception as exc:
            self.done.emit(None, str(exc) or exc.__class__.__name__)


class BasemapWorker(QThread):
    """Télécharge et reprojette un fond de carte en arrière-plan."""

    progress = pyqtSignal(str)
    done = pyqtSignal(object, object, str)   # worker, QImage ou None, message d'erreur

    def __init__(self, key, source, ref, cache_file, parent=None):
        super().__init__(parent)
        self.key = key
        self._source, self._ref, self._cache_file = source, ref, Path(cache_file)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            image = basemap.build_basemap(self._source, self._ref, self.progress.emit,
                                          cancel=lambda: self._cancelled)
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            # Écriture atomique : l'interface peut lire le cache pendant ce temps.
            tmp = self._cache_file.with_name(self._cache_file.stem + ".tmp.png")
            if image.save(str(tmp), "PNG"):
                os.replace(tmp, self._cache_file)
            self.done.emit(self, image, "")
        except basemap.Cancelled:
            self.done.emit(self, None, "")
        except Exception as exc:
            self.done.emit(self, None, str(exc))


# --- Saisie d'un site ------------------------------------------------------------

class SiteForm(QWidget):
    """Formulaire d'un site unique (utilisé pour le récepteur)."""

    def __init__(self, on_change, parent=None):
        super().__init__(parent)
        self.name = QLineEdit()
        self.lat = QLineEdit()
        self.lon = QLineEdit()
        self.lat.setPlaceholderText(tr("décimal ou DMS, Nord +"))
        self.lon.setPlaceholderText(tr("décimal ou DMS, Est +  /  Ouest −"))
        self.height = spin(0, 100000, 2, 1)
        self.unit = QComboBox()
        self.unit.addItems(["m", "ft"])
        height_row = QHBoxLayout()
        height_row.addWidget(self.height, 1)
        height_row.addWidget(self.unit)
        form = QFormLayout(self)
        form.addRow(tr("Nom"), self.name)
        form.addRow(tr("Latitude"), self.lat)
        form.addRow(tr("Longitude"), self.lon)
        form.addRow(tr("Hauteur antenne (sol)"), height_row)
        for edit in (self.name, self.lat, self.lon):
            edit.textChanged.connect(on_change)
        self.height.valueChanged.connect(on_change)
        self.unit.currentIndexChanged.connect(on_change)

    def set_coordinates(self, lat, lon):
        self.lat.setText(f"{lat:.6f}")
        self.lon.setText(f"{lon:.6f}")

    def set_site(self, site):
        self.name.setText(site["name"])
        self.lat.setText(f"{float(site['lat']):g}")
        self.lon.setText(f"{float(site['lon']):g}")
        self.height.setValue(float(site["height"]))
        self.unit.setCurrentText(site.get("height_unit", "m"))

    def site(self):
        name = self.name.text().strip()
        try:
            lat, lon = splat.parse_angle(self.lat.text()), splat.parse_angle(self.lon.text())
        except ValueError:
            raise splat.ParamError(tr("Coordonnées invalides pour le récepteur « {name} ».", name=name)) from None
        return {"name": name, "lat": lat, "lon": lon,
                "height": self.height.value(), "height_unit": self.unit.currentText()}


class SiteTable(QWidget):
    """Tableau éditable des sites émetteurs."""

    COLUMNS = [N_("Nom"), N_("Latitude"), N_("Longitude"), N_("Hauteur"), N_("Unité"), N_("Antenne"),
               N_("Azimut"), N_("Incl.")]
    NO_ANTENNA = N_("(isotrope)")

    def __init__(self, on_change, on_pick=None, parent=None, on_row=None):
        """`on_row(ligne)` : émetteur courant changé (onglet Antennes)."""
        super().__init__(parent)
        self._on_change = on_change
        self._on_pick = on_pick
        self._on_row = on_row or (lambda _row: None)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in self.COLUMNS])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeaderItem(2).setToolTip(tr("Longitude : Est positif, Ouest négatif"))
        self.table.horizontalHeaderItem(6).setToolTip(tr("Azimut du lobe principal (degrés depuis le nord, sens horaire)"))
        self.table.horizontalHeaderItem(7).setToolTip(tr("Inclinaison mécanique (degrés, positive vers le sol)"))
        self.table.horizontalHeaderItem(5).setToolTip(tr("Modèle d'antenne (diagramme de rayonnement) ; "
                                                      "utilisé par SPLAT! en mode -L et point à point"))
        self._antenna_names = antennas.library_names()
        # Colonnes d'antenne masquées (panneau étroit) : édition dans l'onglet Antennes.
        for col in (5, 6, 7):
            self.table.setColumnHidden(col, True)
        self.table.currentCellChanged.connect(lambda row, *_: self._on_row(row))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Hauteur de 4 lignes (au-delà : défilement) : le tableau n'occupe pas tout l'onglet.
        self.table.setFixedHeight(self.table.horizontalHeader().sizeHint().height()
                                  + 4 * self.table.verticalHeader().defaultSectionSize()
                                  + 2 * self.table.frameWidth())
        self.table.itemChanged.connect(lambda _item: self._on_change())

        buttons = QGridLayout()
        for i, (text, slot) in enumerate((
                (N_("Ajouter"), self.add_empty), (N_("Importer .qth…"), self.import_qth),
                (N_("Exporter .qth…"), self.export_qth), (N_("Monter"), lambda: self.move(-1)),
                (N_("Descendre"), lambda: self.move(1)), (N_("Supprimer"), self.remove))):
            button = QPushButton(tr(text))
            button.clicked.connect(slot)
            buttons.addWidget(button, i // 3, i % 3)
        if on_pick:
            pick = QPushButton(tr("Choisir sur la carte…"))
            pick.setToolTip(tr("Désigner la position de l'émetteur sélectionné sur une carte OSM / IGN "
                            "(un nouvel émetteur est ajouté si aucun n'est sélectionné)"))
            pick.clicked.connect(lambda: on_pick(self.table.currentRow()))
            buttons.addWidget(pick, 2, 0, 1, 3)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

    def fill_antenna_combo(self, combo, wanted):
        """Modèles de la bibliothèque (+ le modèle voulu s'il en est absent), `wanted` sélectionné.
        La liste n'est reconstruite que si elle change : une liste ouverte reste utilisable."""
        items = [(tr(self.NO_ANTENNA), "")] + [(name, name) for name in self._antenna_names]
        if wanted and wanted not in self._antenna_names:
            items.append((tr("{name} (absente)", name=wanted), wanted))
        current = [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]
        combo.blockSignals(True)
        if current != items:
            combo.clear()
            for label, data in items:
                combo.addItem(label, data)
        index = max(0, combo.findData(wanted))
        if combo.currentIndex() != index:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    # ---- Antenne des émetteurs (édition dans l'onglet Antennes) ----

    def names(self):
        return [self.table.item(row, 0).text() if self.table.item(row, 0) else ""
                for row in range(self.table.rowCount())]

    def current_row(self):
        return self.table.currentRow()

    def select_row(self, row):
        if 0 <= row < self.table.rowCount() and row != self.table.currentRow():
            self.table.selectRow(row)

    def has_antenna(self, row):
        """Ligne existante et complète (listes d'antenne créées : pas en cours d'insertion)."""
        return 0 <= row < self.table.rowCount() and all(self.table.cellWidget(row, c) is not None for c in (5, 6, 7))

    def antenna(self, row):
        """(modèle, azimut, inclinaison) de l'émetteur `row`."""
        if not self.has_antenna(row):
            return "", 0.0, 0.0
        return (self.table.cellWidget(row, 5).currentData() or "", self.table.cellWidget(row, 6).value(),
                self.table.cellWidget(row, 7).value())

    def set_antenna(self, row, name, azimuth, tilt):
        if not self.has_antenna(row):
            return
        self.fill_antenna_combo(self.table.cellWidget(row, 5), name)
        for col, value in ((6, azimuth), (7, tilt)):
            cell = self.table.cellWidget(row, col)
            cell.blockSignals(True)
            cell.setValue(value)
            cell.blockSignals(False)
        self._on_change()

    def _add_row(self, site):
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [site["name"], f"{float(site['lat']):g}", f"{float(site['lon']):g}",
                  f"{float(site['height']):g}"]
        for col, value in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(value))
        unit = QComboBox()
        unit.addItems(["m", "ft"])
        unit.setCurrentText(site.get("height_unit", "m"))
        unit.currentIndexChanged.connect(lambda _i: self._on_change())
        self.table.setCellWidget(row, 4, unit)
        antenna = QComboBox()
        self.fill_antenna_combo(antenna, site.get("antenna", ""))
        self.table.setCellWidget(row, 5, antenna)
        for col, key, low, high in ((6, "azimuth", 0.0, 359.9), (7, "tilt", -90.0, 90.0)):
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(1)
            box.setSuffix("°")
            box.setWrapping(col == 6)
            box.setKeyboardTracking(False)
            box.setValue(float(site.get(key, 0.0)))
            self.table.setCellWidget(row, col, box)
        self.table.blockSignals(False)
        if self.table.currentRow() < 0:
            self.table.setCurrentCell(row, 0)
        self._on_row(self.table.currentRow())

    def refresh_antennas(self):
        """Met à jour la liste des modèles proposés (après modification de la bibliothèque)."""
        self._antenna_names = antennas.library_names()
        try:
            current = self.sites()
        except splat.ParamError:
            return
        row = self.table.currentRow()
        self.set_sites(current)
        if 0 <= row < self.table.rowCount():
            self.table.setCurrentCell(row, 0)
        self._on_row(self.table.currentRow())

    def set_coordinates(self, row, lat, lon):
        for col, value in ((1, lat), (2, lon)):
            self.table.item(row, col).setText(f"{value:.6f}")

    def add_empty(self):
        self._add_row(storage.default_site(f"TX{self.table.rowCount() + 1}"))
        self._on_change()

    def _qth_folder(self):
        return folder_setting(getattr(self.window(), "settings", {}), "qth_dir")

    def import_qth(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Importer des sites"), data_folder(self._qth_folder()),
                                                tr(QTH_FILTER))
        for path in paths:
            try:
                self._add_row(splat.read_qth(path))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, tr("Import impossible"), str(exc))
        self._on_change()

    def export_qth(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, tr("Exporter"), tr("Sélectionnez un site."))
            return
        try:
            site = self.sites()[row]
        except splat.ParamError as exc:
            QMessageBox.warning(self, tr("Exporter"), str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Exporter le site"),
            str(Path(data_folder(self._qth_folder())) / (splat.safe_filename(site["name"]) + ".qth")),
            tr(QTH_FILTER))
        if path:
            Path(path).write_text(splat.qth_text(site), encoding="latin-1", errors="replace")

    def move(self, delta):
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        try:
            sites = self.sites()
        except splat.ParamError:
            return
        sites[row], sites[target] = sites[target], sites[row]
        self.set_sites(sites)
        self.table.selectRow(target)
        self._on_change()

    def remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        self._on_row(self.table.currentRow())
        self._on_change()

    def set_sites(self, sites):
        self.table.setRowCount(0)
        for site in sites:
            self._add_row(site)

    def sites(self):
        result = []
        for row in range(self.table.rowCount()):
            text = [(self.table.item(row, c).text().strip() if self.table.item(row, c) else "")
                    for c in range(4)]
            try:
                lat, lon = splat.parse_angle(text[1]), splat.parse_angle(text[2])
                height = float(text[3].replace(",", "."))
            except ValueError:
                raise splat.ParamError(
                    tr("Valeurs invalides pour l'émetteur ligne {row} ({name}).", row=row + 1,
                       name=text[0] or tr("sans nom"))) from None
            result.append({"name": text[0], "lat": lat, "lon": lon, "height": height,
                           "height_unit": self.table.cellWidget(row, 4).currentText(),
                           "antenna": self.table.cellWidget(row, 5).currentData() or "",
                           "azimuth": self.table.cellWidget(row, 6).value(),
                           "tilt": self.table.cellWidget(row, 7).value()})
        return result


# --- Dialogue des réglages ----------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Réglages"))
        self.resize(720, 0)
        self.settings = settings
        self.paths = {}
        form = QFormLayout()
        info = QLabel(
            tr("Dossiers ajoutés au PATH lors de l'exécution (séparés par « ; »). Ils doivent "
            "contenir les DLL requises par chaque version et, pour les graphes, gnuplot.exe.\n"
            "x64 : msys-2.0.dll, msys-stdc++-6.dll, msys-gcc_s-seh-1.dll (MSYS2, C:\\msys64\\usr\\bin) "
            "+ gnuplot (C:\\msys64\\mingw64\\bin).\n"
            "x86 : libstdc++-6.dll, libgcc_s_dw2-1.dll, libbz2-2.dll (MinGW 32 bits).\n"
            "Fichier → Pré-requis… télécharge ces DLL dans deps\\<arch>, ainsi que gnuplot, "
            "et ajoute leurs dossiers ici."))
        info.setWordWrap(True)
        form.addRow(info)
        for arch in splat.ARCHES:
            row = QHBoxLayout()
            edit = QLineEdit(settings["extra_paths"].get(arch, ""))
            add = QPushButton(tr("Ajouter un dossier…"))
            add.clicked.connect(lambda _c, e=edit: self._append_dir(e))
            test = QPushButton(tr("Tester"))
            test.clicked.connect(lambda _c, a=arch, e=edit: self._test(a, e.text()))
            row.addWidget(edit, 1)
            row.addWidget(add)
            row.addWidget(test)
            form.addRow(f"PATH {arch}", row)
            self.paths[arch] = edit
        self.runs_dir = PathEdit(directory=True)
        self.runs_dir.setText(settings["runs_dir"])
        form.addRow(tr("Dossier des résultats"), self.runs_dir)
        self.qth_dir = PathEdit(directory=True)
        self.qth_dir.setText(str(folder_setting(settings, "qth_dir")))
        form.addRow(tr("Dossier des sites (.qth)"), self.qth_dir)
        self.lrp_dir = PathEdit(directory=True)
        self.lrp_dir.setText(str(folder_setting(settings, "lrp_dir")))
        form.addRow(tr("Dossier des paramètres ITM (.lrp)"), self.lrp_dir)
        self.srtm_url = QLineEdit(settings.get("srtm_url") or terrain.DEFAULT_URL)
        self.srtm_url.setToolTip(tr("Variables : {tile} (ex. N48E002), {lat_dir} (ex. N48), {lat}, {lon}. "
                                 "Fichier .hgt, .hgt.gz ou .zip, SRTM 1\" (3601²) ou 3\" (1201², standard seulement)."))
        form.addRow(tr("URL des tuiles SRTM"), self.srtm_url)
        form.addRow(tr("Relief (SRTM / SDF)"), QLabel(str(terrain.TERRAIN_DIR)))
        form.addRow(tr("Paramètres de l'application"), QLabel(str(storage.SETTINGS_FILE)))
        form.addRow(tr("Profils et historique"), QLabel(str(storage.DATA_DIR)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _append_dir(self, edit):
        path = QFileDialog.getExistingDirectory(self, tr("Ajouter un dossier au PATH"))
        if path:
            current = [p for p in edit.text().split(";") if p.strip()]
            edit.setText(";".join(current + [str(Path(path))]))

    def _test(self, arch, extra):
        messages = []
        for variant in splat.VARIANTS:
            exe = splat.executable_path(arch, variant)
            if not exe.exists():
                messages.append(tr("{exe} : absent", exe=exe.name))
                continue
            proc = QProcess(self)
            env = QProcessEnvironment.systemEnvironment()
            if extra.strip():
                env.insert("PATH", extra.strip().rstrip(";") + os.pathsep + env.value("PATH"))
            proc.setProcessEnvironment(env)
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            proc.start(str(exe), [])
            proc.waitForFinished(10000)
            output = bytes(proc.readAll()).decode("latin-1", "replace")
            if "SPLAT!" in output:
                messages.append(f"{exe.name} : OK")
            else:
                why = splat.describe_exit_code(proc.exitCode()) or tr("code {code:#x}", code=proc.exitCode())
                messages.append(tr("{exe} : ÉCHEC – {why}", exe=exe.name, why=why))
        gnuplot = any((Path(p.strip()) / "gnuplot.exe").exists() for p in extra.split(";") if p.strip())
        messages.append("gnuplot.exe : " + (tr("trouvé") if gnuplot else tr("non trouvé dans ces dossiers")))
        QMessageBox.information(self, tr("Test {arch}", arch=arch), "\n".join(messages))

    def values(self):
        return {
            "extra_paths": {arch: edit.text().strip() for arch, edit in self.paths.items()},
            "runs_dir": self.runs_dir.text().strip() or str(storage.PROJECT_DIR / "runs"),
            "qth_dir": self.qth_dir.text().strip() or str(storage.QTH_DIR),
            "lrp_dir": self.lrp_dir.text().strip() or str(storage.LRP_DIR),
            "srtm_url": self.srtm_url.text().strip() or terrain.DEFAULT_URL,
        }


# --- Installation des pré-requis ---------------------------------------------------

class PrereqDialog(QDialog):
    """État et installation des exécutables SPLAT! (archive locale ou URL), des DLL et de gnuplot."""

    ARCHIVE_FILTER = N_("Archives (*.zip *.tar.gz *.tgz *.tar.bz2 *.tar.xz);;Tous les fichiers (*)")

    def __init__(self, settings, parent=None, auto_dlls=(), auto_gnuplot=False):
        """`auto_dlls` : architectures dont les DLL sont téléchargées dès l'ouverture ;
        `auto_gnuplot` : gnuplot aussi."""
        super().__init__(parent)
        self.setWindowTitle(tr("Pré-requis"))
        self.resize(760, 520)
        self.settings = settings
        self.worker = None

        status = QGroupBox(tr("État"))
        grid = QGridLayout(status)
        self.status_labels = {}
        self.dll_buttons = {}
        for row, arch in enumerate(splat.ARCHES):
            grid.addWidget(QLabel(f"<b>{tr(splat.ARCHES[arch])}</b>"), row, 0)
            label = QLabel()
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(label, row, 1)
            button = QPushButton(tr("Télécharger les DLL {arch}", arch=arch))
            button.clicked.connect(lambda _c, a=arch: self._install_dlls(a))
            grid.addWidget(button, row, 2)
            self.status_labels[arch] = label
            self.dll_buttons[arch] = button
        grid.setColumnStretch(1, 1)

        splat_box = QGroupBox(tr("Exécutables SPLAT!"))
        splat_layout = QHBoxLayout(splat_box)
        splat_layout.addWidget(QLabel(tr("Archive (.zip, .tar.gz…) contenant splat.exe / splat-hd.exe :")), 1)
        self.archive_button = QPushButton(tr("Depuis un fichier…"))
        self.archive_button.clicked.connect(self._install_from_file)
        self.url_button = QPushButton(tr("Depuis une URL…"))
        self.url_button.clicked.connect(self._install_from_url)
        splat_layout.addWidget(self.archive_button)
        splat_layout.addWidget(self.url_button)

        gnuplot_box = QGroupBox(tr("gnuplot (facultatif : graphes de SPLAT! -p, -e, -h, -H, -l)"))
        gnuplot_layout = QHBoxLayout(gnuplot_box)
        self.gnuplot_label = QLabel()
        self.gnuplot_label.setWordWrap(True)
        self.gnuplot_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gnuplot_layout.addWidget(self.gnuplot_label, 1)
        self.gnuplot_button = QPushButton(tr("Télécharger gnuplot {version}", version=prereqs.GNUPLOT_VERSION))
        self.gnuplot_button.setToolTip(tr("Distribution Windows 64 bits officielle (archive d'environ 70 Mo), "
                                          "installée dans {folder}", folder=prereqs.GNUPLOT_DIR))
        self.gnuplot_button.clicked.connect(self._install_gnuplot)
        gnuplot_layout.addWidget(self.gnuplot_button)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(mono_font())

        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(tr("Annuler le téléchargement"), QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self.worker and self.worker.cancel())
        self.close_button = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.close_button.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(status)
        layout.addWidget(splat_box)
        layout.addWidget(gnuplot_box)
        layout.addWidget(self.log_view, 1)
        layout.addWidget(buttons)
        self._refresh()
        if auto_dlls or auto_gnuplot:
            QTimer.singleShot(0, lambda: self._install_missing(auto_dlls, auto_gnuplot))

    def _refresh(self):
        self.report = prereqs.check(self.settings["extra_paths"])
        for arch, label in self.status_labels.items():
            state = self.report[arch]
            parts = ["SPLAT! : " + (", ".join(state["splat"]) if state["splat"]
                                    else f"<span style='color:#c0392b'>{tr('absent')}</span>")]
            if prereqs.installed_executables(arch):
                parts.append("DLL : " + (f"<span style='color:#1e8449'>{tr('complètes')}</span>"
                                         if not state["missing"] else
                                         f"<span style='color:#c0392b'>{tr('manquantes :')} "
                                         + ", ".join(state["missing"]) + "</span>"))
            label.setText("<br>".join(parts))
        found = {arch: state["gnuplot"] for arch, state in self.report.items()}
        if all(found.values()):
            paths = sorted({str(path) for path in found.values()})
            self.gnuplot_label.setText(f"<span style='color:#1e8449'>{tr('trouvé')}</span> : " + ", ".join(paths))
        else:
            missing = ", ".join(arch for arch, path in found.items() if not path)
            self.gnuplot_label.setText(f"<span style='color:#c0392b'>{tr('absent')}</span> "
                                       + tr("(PATH {arches})", arches=missing))

    def _set_busy(self, busy):
        for button in (*self.dll_buttons.values(), self.archive_button, self.url_button, self.gnuplot_button,
                       self.close_button):
            button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def _start(self, title, function):
        self.log_view.appendPlainText(f"=== {title}")
        self.worker = PrereqWorker(function, self)
        self.worker.log.connect(self._log)
        self.worker.done.connect(self._finished)
        self._set_busy(True)
        self.worker.start()

    def _log(self, text):
        self.log_view.moveCursor(self.log_view.textCursor().MoveOperation.End)
        self.log_view.insertPlainText(text)
        self.log_view.ensureCursorVisible()

    def _finished(self, result, error):
        self.worker.wait()
        self.worker = None
        self._set_busy(False)
        if error:
            self._log(tr("Échec : {error}\n", error=error))
            QMessageBox.warning(self, tr("Pré-requis"), tr("L'installation a échoué :\n{error}", error=error))
        elif result is None:
            self._log(tr("Annulé.\n"))
        else:
            # Les DLL installées dans deps/<arch> sont ajoutées au PATH d'exécution.
            for arch in splat.ARCHES:
                if any(prereqs.dll_dir(arch).glob("*.dll")):
                    extra = self.settings["extra_paths"].get(arch, "")
                    self.settings["extra_paths"][arch] = prereqs.add_to_path_setting(extra, arch)
                if (prereqs.gnuplot_bin() / "gnuplot.exe").is_file():
                    extra = self.settings["extra_paths"].get(arch, "")
                    self.settings["extra_paths"][arch] = prereqs.add_gnuplot_to_path_setting(extra)
            storage.save_settings(self.settings)
            self._log(tr("Terminé.\n"))
        self._refresh()
        if not error and isinstance(result, dict):
            # Exécutables SPLAT! installés : proposer les DLL qui leur manquent, et gnuplot.
            pending = [arch for arch in result if self.report.get(arch, {}).get("missing")]
            gnuplot = bool(prereqs.gnuplot_missing(self.report))
            missing = ([tr("DLL manquantes pour {arches}.", arches=", ".join(pending))] if pending else []) \
                + ([tr("gnuplot absent.")] if gnuplot else [])
            if missing and QMessageBox.question(
                    self, tr("Pré-requis"), " ".join(missing) + " " + tr("Les télécharger maintenant ?")) \
                    == QMessageBox.StandardButton.Yes:
                self._install_missing(pending, gnuplot)

    def _install_dlls(self, *arches):
        self._start("DLL " + ", ".join(arches),
                    lambda log, cancel: [dll for arch in arches for dll in prereqs.install_dlls(arch, log, cancel)])

    def _install_missing(self, arches, gnuplot):
        """DLL de `arches` puis, si `gnuplot`, gnuplot, en un seul téléchargement suivi."""
        if not gnuplot:
            self._install_dlls(*arches)
        elif not arches:
            self._start("gnuplot " + prereqs.GNUPLOT_VERSION, prereqs.install_gnuplot)
        else:
            self._start("DLL " + ", ".join(arches) + " + gnuplot " + prereqs.GNUPLOT_VERSION,
                        lambda log, cancel: [*(dll for arch in arches for dll in prereqs.install_dlls(arch, log, cancel)),
                                             prereqs.install_gnuplot(log, cancel)])

    def _install_gnuplot(self):
        if prereqs.GNUPLOT_DIR.exists() and QMessageBox.question(
                self, tr("Pré-requis"), tr("gnuplot est déjà installé dans {folder}. Le remplacer ?",
                                           folder=prereqs.GNUPLOT_DIR)) != QMessageBox.StandardButton.Yes:
            return
        self._start("gnuplot " + prereqs.GNUPLOT_VERSION, prereqs.install_gnuplot)

    def _confirm_replace(self):
        existing = [p for arch in splat.ARCHES for p in prereqs.installed_executables(arch)
                    if p.name.lower() in prereqs.SPLAT_NAMES]
        if not existing:
            return True
        return QMessageBox.question(
            self, tr("Pré-requis"), tr("Les exécutables de l'archive remplaceront ceux déjà présents dans bin\\ "
                                "(même nom, même architecture). Continuer ?")) == QMessageBox.StandardButton.Yes

    def _install_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Archive SPLAT!"), "", tr(self.ARCHIVE_FILTER))
        if path and self._confirm_replace():
            self._start(tr("SPLAT! depuis {source}", source=path), lambda log, cancel: prereqs.install_splat(path, log, cancel))

    def _install_from_url(self):
        url, ok = QInputDialog.getText(self, tr("Archive SPLAT!"), tr("URL de l'archive (http ou https) :"))
        url = url.strip()
        if not ok or not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, tr("Pré-requis"), tr("L'URL doit commencer par http:// ou https://."))
            return
        if self._confirm_replace():
            self._start(tr("SPLAT! depuis {source}", source=url), lambda log, cancel: prereqs.install_splat(url, log, cancel))

    def reject(self):
        if self.worker is not None:      # pas de fermeture pendant un téléchargement
            return
        super().reject()


# --- Fenêtre principale ----------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, progress=None):
        """`progress(pourcentage, message)` : suivi de la préparation (écran de démarrage)."""
        super().__init__()
        step = progress or (lambda _value, _text: None)
        step(10, tr("Chargement des réglages et de l'historique…"))
        self.settings = storage.load_settings()
        self.history = storage.load_history()
        self.profile_name = self.settings.get("last_profile") or storage.DEFAULT_PROFILE
        self.saved_params = None      # contenu enregistré du profil courant
        self.process = None
        self.run_dir = None
        self.run_started = None
        self.help_window = None       # aide intégrée, créée à la première ouverture
        self.run_params = None
        self.run_spec = None
        self.terrain_worker = None
        self.terrain_retried = False
        self.site_tiles = []          # tuiles corrigées (sursol) écrites dans le dossier du calcul
        self.pending_result = ""
        self.splat_output = ""
        self._loading = False

        self.setWindowTitle(tr("SPLAT!Gui"))
        step(20, tr("Construction de l'interface…"))
        self._build_toolbar()
        step(30, tr("Panneaux de paramètres et de résultats…"))
        self._build_central()
        step(65, tr("Menus…"))
        self._build_menu()
        self.statusBar().showMessage(tr("Prêt"))
        self.prereq_label = QLabel()
        self.prereq_label.setStyleSheet("color: #b9770e; font-weight: bold;")
        self.prereq_label.setVisible(False)
        self.statusBar().addPermanentWidget(self.prereq_label)

        self._preview_timer = QTimer(self, singleShot=True, interval=150)
        self._preview_timer.timeout.connect(self._refresh_state)

        geometry = self.settings.get("geometry_qt")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
        else:
            self.resize(1500, 920)

        step(75, tr("Chargement du profil…"))
        self._reload_profile_combo()
        if self.profile_name not in storage.list_profiles():
            self.profile_name = storage.DEFAULT_PROFILE
        self.saved_params = storage.load_profile(self.profile_name)
        last = self.settings.get("last_params")
        self.set_params(storage.merge_defaults(last) if last else self.saved_params)
        step(85, tr("Historique des calculs…"))
        self._refresh_history()
        if self.history:
            step(90, tr("Affichage des derniers résultats…"))
            self.history_table.selectRow(0)
            self.show_results(self.history[0].get("run_dir"))

    # ---- Construction -------------------------------------------------------

    def _build_toolbar(self):
        bar = QToolBar("Principal")
        bar.setMovable(False)
        self.addToolBar(bar)

        bar.addWidget(QLabel(" " + tr("Profil :") + " "))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(200)
        self.profile_combo.activated.connect(self._on_profile_selected)
        bar.addWidget(self.profile_combo)
        for text, slot, shortcut in ((N_("Enregistrer"), self.save_profile, QKeySequence.StandardKey.Save),
                                     (N_("Enregistrer sous…"), self.save_profile_as, None),
                                     (N_("Renommer…"), self.rename_profile, None),
                                     (N_("Supprimer"), self.delete_profile, None)):
            action = QAction(tr(text), self)
            action.triggered.connect(slot)
            if shortcut:
                action.setShortcut(shortcut)
            bar.addAction(action)
        delete_button = bar.widgetForAction(action)          # dernier : Supprimer
        bar.addSeparator()

        bar.addWidget(QLabel(" " + tr("Version :") + " "))
        self.arch_combo = QComboBox()
        for key, label in splat.ARCHES.items():
            self.arch_combo.addItem(tr(label), key)
        bar.addWidget(self.arch_combo)
        self.variant_combo = QComboBox()
        for key, label in splat.VARIANTS.items():
            self.variant_combo.addItem(tr(label), key)
        bar.addWidget(self.variant_combo)
        for combo in (self.arch_combo, self.variant_combo):
            combo.currentIndexChanged.connect(self.changed)
        bar.addSeparator()

        self.progress = OutlinedProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setFixedHeight(18)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("")
        self.progress.setTextVisible(True)
        self.progress.setToolTip(tr("Avancement du calcul"))
        self._style_progress()
        bar.addWidget(self.progress)
        self.run_action = QAction("▶ " + tr("Lancer"), self)
        self.run_action.setShortcut("F5")
        self.run_action.triggered.connect(self.run)
        bar.addAction(self.run_action)
        # Bouton « Lancer » au vert du logo (#7fa63a), plus pâle quand il est désactivé.
        run_button = bar.widgetForAction(self.run_action)
        run_button.setStyleSheet(
            "QToolButton { background: #7fa63a; color: #ffffff; font-weight: bold; border: 1px solid #6a8c2f;"
            " border-radius: 4px; padding: 0 10px; margin: 0 3px; }"        # 3 px de plus de chaque côté
            "QToolButton:hover { background: #8db548; }"
            "QToolButton:pressed { background: #6a8c2f; }"
            "QToolButton:disabled { background: #c5d6a6; color: #f3f7ec; border-color: #b3c793; }")
        run_button.setFixedHeight(delete_button.sizeHint().height())     # même hauteur que « Supprimer »
        self.stop_action = QAction("■ " + tr("Arrêter"), self)
        self.stop_action.setEnabled(False)
        self.stop_action.triggered.connect(self.stop)
        bar.addAction(self.stop_action)

    def _build_menu(self):
        menu = self.menuBar()
        file_menu = menu.addMenu(tr("&Fichier"))
        for text, slot in ((N_("Nouveau profil…"), self.new_profile),
                           (N_("Importer un profil…"), self.import_profile),
                           (N_("Exporter le profil…"), self.export_profile),
                           (N_("Réinitialiser les paramètres"), self.reset_params)):
            file_menu.addAction(tr(text), slot)
        file_menu.addSeparator()
        file_menu.addAction(tr("Ouvrir le dossier des résultats"), self.open_runs_dir)
        file_menu.addAction(tr("Modèles d'antennes…"), self.manage_antennas)
        file_menu.addAction(tr("Réglages…"), self.edit_settings)
        file_menu.addAction(tr("Autres données d'entrée…"), self.show_input_dialog)
        file_menu.addAction(tr("Pré-requis (SPLAT!, DLL, gnuplot)…"), self.manage_prereqs)
        file_menu.addSeparator()
        file_menu.addAction(tr("Quitter"), self.close)

        view_menu = menu.addMenu(tr("&Affichage"))
        theme_menu = view_menu.addMenu(tr("Thème"))
        group = QActionGroup(self)
        current = self.settings.get("theme", themes.DEFAULT)
        for key, spec in themes.THEMES.items():
            action = theme_menu.addAction(tr(spec["label"]), lambda k=key: self.set_theme(k))
            action.setCheckable(True)
            action.setChecked(key == current)
            group.addAction(action)
        language_menu = view_menu.addMenu(tr("Langue"))
        group = QActionGroup(self)
        current = self.settings.get("language", "")
        for code, label in (("", tr("Langue du système")), *i18n.LANGUAGES.items()):
            action = language_menu.addAction(label, lambda c=code: self.set_language(c))
            action.setCheckable(True)
            action.setChecked(code == current)
            group.addAction(action)

        help_menu = menu.addMenu(tr("&Aide"))
        help_action = help_menu.addAction(tr("Aide de SPLAT!…"), self.show_help)
        help_action.setShortcut(QKeySequence.StandardKey.HelpContents)
        help_menu.addAction(tr("Rechercher dans l'aide…"), lambda: self.show_help(search=True))
        help_menu.addSeparator()
        help_menu.addAction(tr("Licence (GNU GPL v2)…"), self.show_license)
        help_menu.addAction(tr("À propos"), self.show_about)

    def show_about(self):
        box = QMessageBox(self)
        box.setWindowTitle(tr("À propos"))
        box.setIconPixmap(app_icon().pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<b>SPLAT!Gui {__version__}</b><br>"
            + html.escape(tr("Interface graphique pour SPLAT! 1.4.2 (x64 / x86, standard / HD).")) + "<br><br>"
            + html.escape(tr(CREDITS)) + "<br><br>"
            + html.escape(tr("SPLAT! (John A. Magliacane, KD2BD) et SPLAT!Gui (F4CWH) sont des logiciels libres : "
                             "vous pouvez les redistribuer et/ou les modifier selon les termes de la licence "
                             "publique générale GNU (GNU GPL) publiée par la Free Software Foundation, version 2 "
                             "ou (à votre choix) toute version ultérieure.")) + "<br><br>"
            + html.escape(tr("Ils sont distribués dans l'espoir qu'ils seront utiles, mais SANS AUCUNE GARANTIE, "
                             "sans même la garantie implicite de qualité marchande ou d'adéquation à un usage "
                             "particulier. Voir Aide → Licence (GNU GPL v2).")) + "<br><br>"
            + html.escape(tr("Code source :")) + f' <a href="{SOURCE_URL}">{SOURCE_URL}</a>')
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        for label in box.findChildren(QLabel):
            label.setOpenExternalLinks(True)          # lien ouvert dans le navigateur
        box.exec()

    def show_license(self):
        """Texte complet de la GNU GPL v2 (fichier LICENSE)."""
        path = license_file()
        try:
            text = path.read_text(encoding="utf-8", errors="replace") if path else ""
        except OSError:
            text = ""
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("Licence (GNU GPL v2)"))
        dialog.resize(640, 700)
        layout = QVBoxLayout(dialog)
        intro = QLabel(html.escape(tr("SPLAT! et SPLAT!Gui : GNU GPL version 2 ou (à votre choix) toute version "
                                      "ultérieure.")) + "<br>" + html.escape(tr("Code source :"))
                       + f' <a href="{SOURCE_URL}">{SOURCE_URL}</a>')
        intro.setOpenExternalLinks(True)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        view = QPlainTextEdit(text or tr("Texte de la licence introuvable : https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"))
        view.setReadOnly(True)
        view.setFont(mono_font())
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _build_central(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_params_panel())
        splitter.addWidget(self._build_results_panel())
        # Paramètres : 1/3 de la largeur, résultats : 2/3.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([1000, 2000])
        self.setCentralWidget(splitter)

    def _build_params_panel(self):
        tabs = QTabWidget()
        tabs.addTab(scrollable(self._build_analysis_tab()), tr("Analyse"))
        tabs.addTab(scrollable(self._build_tx_tab()), tr("Émetteurs"))
        tabs.addTab(scrollable(self._build_rx_tab()), tr("Récepteur"))
        tabs.addTab(scrollable(self._build_antenna_tab()), tr("Antennes"))
        tabs.addTab(scrollable(self._build_options_tab()), tr("Options"))
        tabs.addTab(scrollable(self._build_files_tab()), tr("Sorties"))
        for index, tip in enumerate((N_("Mode d'analyse, unités et propagation"), N_("Sites émetteurs (-t) et calcul de la PAR"),
                                     N_("Site récepteur (-r), mode point à point"),
                                     N_("Système antennaire des émetteurs (modèle, orientation)"),
                                     N_("Options de SPLAT!"),
                                     N_("Sorties et fichiers"))):
            tabs.setTabToolTip(index, tr(tip))

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font())
        self.preview.setMaximumHeight(60)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(tabs, 1)
        layout.addWidget(QLabel(tr("Ligne de commande :")))
        layout.addWidget(self.preview)
        return panel

    def _build_analysis_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        mode_box = QGroupBox(tr("Mode d'analyse"))
        mode_layout = QGridLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        for i, (key, label) in enumerate(splat.MODES.items()):
            radio = QRadioButton(tr(label))
            radio.setProperty("mode", key)
            self.mode_group.addButton(radio, i)
            mode_layout.addWidget(radio, i, 0, 1, 2)
        self.mode_group.idToggled.connect(lambda _i, checked: checked and self.changed())
        self.rx_height = spin(0, 100000, 2, 1)
        self.rx_height.valueChanged.connect(self.changed)
        self.metric = QCheckBox(tr("Unités métriques (-metric) : km / m"))
        self.metric.toggled.connect(self.changed)
        rx_label = QLabel(tr("Hauteur de réception (-c / -L) :"))
        rx_label.setToolTip(tr("Hauteur de l'antenne de réception pour la couverture (-c) "
                            "et la perte de trajet (-L)"))
        mode_layout.addWidget(rx_label, 3, 0)
        mode_layout.addWidget(self.rx_height, 3, 1)
        mode_layout.addWidget(self.metric, 4, 0, 1, 2)
        layout.addWidget(mode_box)
        layout.addWidget(self._build_lrp_box())
        layout.addStretch()
        return page

    def _coordinates_note(self):
        note = QLabel(tr("Longitudes : convention usuelle (Est positif, Ouest négatif). "
                      "La conversion vers la convention SPLAT! (Ouest positif) est automatique. "
                      "Les coordonnées acceptent le format décimal ou « degrés minutes secondes »."))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        return note

    def _build_tx_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        tx_box = QGroupBox(tr("Émetteurs (-t)"))
        tx_box.setToolTip(tr("4 émetteurs au plus en couverture (-c), 30 en perte de trajet (-L)"))
        tx_layout = QVBoxLayout(tx_box)
        self.tx_table = SiteTable(self.changed, self._pick_tx, on_row=self._antenna_row_changed)
        tx_layout.addWidget(self.tx_table)
        layout.addWidget(tx_box)
        layout.addWidget(self._coordinates_note())
        layout.addWidget(self._build_erp_box())
        layout.addStretch()
        return page

    def _build_rx_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        self.rx_box = QGroupBox(tr("Récepteur (-r) — mode point à point"))
        rx_layout = QVBoxLayout(self.rx_box)
        self.rx_form = SiteForm(self.changed)
        rx_layout.addWidget(self.rx_form)
        rx_buttons = QHBoxLayout()
        rx_import = QPushButton(tr("Importer .qth…"))
        rx_import.clicked.connect(self._import_rx)
        rx_buttons.addWidget(rx_import)
        rx_pick = QPushButton(tr("Choisir sur la carte…"))
        rx_pick.setToolTip(tr("Désigner la position du récepteur sur une carte OSM / IGN"))
        rx_pick.clicked.connect(self._pick_rx)
        rx_buttons.addWidget(rx_pick)
        rx_buttons.addStretch()
        rx_layout.addLayout(rx_buttons)
        layout.addWidget(self.rx_box)
        layout.addWidget(self._coordinates_note())
        layout.addStretch()
        return page

    def _build_antenna_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        tx_box = QGroupBox(tr("Émetteur"))
        tx_layout = QVBoxLayout(tx_box)
        self.antenna_tx_combo = QComboBox()
        self.antenna_tx_combo.setToolTip(tr("Émetteur dont le système antennaire est affiché (onglet Émetteurs)"))
        self.antenna_tx_combo.currentIndexChanged.connect(self._antenna_tx_selected)
        tx_layout.addWidget(self.antenna_tx_combo)
        self.antenna_empty = QLabel(tr("Aucun émetteur : ajoutez-en un dans l'onglet Émetteurs."))
        self.antenna_empty.setStyleSheet("color: gray;")
        tx_layout.addWidget(self.antenna_empty)
        layout.addWidget(tx_box)

        self.antenna_box = QGroupBox(tr("Antenne"))
        form = QFormLayout(self.antenna_box)
        self.antenna_model = QComboBox()
        self.antenna_model.setToolTip(tr("Modèle d'antenne (diagramme de rayonnement) ; "
                                      "utilisé par SPLAT! en perte de trajet (-L) et en point à point"))
        self.antenna_azimuth = QDoubleSpinBox()
        self.antenna_azimuth.setRange(0, 359.9)
        self.antenna_azimuth.setWrapping(True)
        self.antenna_azimuth.setDecimals(1)
        self.antenna_azimuth.setSuffix("°")
        self.antenna_azimuth.setToolTip(tr("Azimut du lobe principal (degrés depuis le nord, sens horaire)"))
        self.antenna_tilt = QDoubleSpinBox()
        self.antenna_tilt.setRange(-90, 90)
        self.antenna_tilt.setDecimals(1)
        self.antenna_tilt.setSuffix("°")
        self.antenna_tilt.setToolTip(tr("Inclinaison mécanique (degrés, positive vers le sol)"))
        for box in (self.antenna_azimuth, self.antenna_tilt):
            box.setKeyboardTracking(False)
            box.valueChanged.connect(self._store_antenna)
        self.antenna_model.currentIndexChanged.connect(self._store_antenna)
        form.addRow(tr("Modèle"), self.antenna_model)
        form.addRow(tr("Azimut du lobe"), self.antenna_azimuth)
        form.addRow(tr("Inclinaison (vers le bas)"), self.antenna_tilt)
        self.antenna_info = QLabel()
        self.antenna_info.setWordWrap(True)
        self.antenna_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.antenna_info)
        apply_all = QPushButton(tr("Appliquer à tous les émetteurs"))
        apply_all.setToolTip(tr("Même modèle, azimut et inclinaison pour tous les émetteurs"))
        apply_all.clicked.connect(self._antenna_apply_all)
        form.addRow(apply_all)
        layout.addWidget(self.antenna_box)

        plots_box = QGroupBox(tr("Diagrammes orientés"))
        plots_layout = QHBoxLayout(plots_box)
        self.antenna_az_plot = antenna_ui.PatternPlot("az")
        self.antenna_el_plot = antenna_ui.PatternPlot("el")
        for plot in (self.antenna_az_plot, self.antenna_el_plot):
            plot.setMinimumSize(150, 165)
            plot.setMaximumHeight(240)
            plots_layout.addWidget(plot)
        layout.addWidget(plots_box)

        buttons = QHBoxLayout()
        manage = QPushButton(tr("Modèles d'antennes…"))
        manage.setToolTip(tr("Importer et gérer les diagrammes de rayonnement (MSI/Planet, NEC-2/4nec2, "
                          "EZNEC, SPLAT!, texte)"))
        manage.clicked.connect(self.manage_antennas)
        buttons.addWidget(manage)
        buttons.addStretch()
        layout.addLayout(buttons)

        note = QLabel(tr("SPLAT! applique le diagramme en perte de trajet / champ (-L) et en point à point ; "
                      "la couverture en visibilité (-c) l'ignore. La PAR doit inclure le gain de l'antenne."))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        layout.addStretch()
        self._refresh_antenna_tab()
        return page

    def _build_erp_box(self):
        """Calcul de la PAR : puissance, câble coaxial et longueur, pertes additionnelles, gain."""
        box = QGroupBox(tr("Calcul de la PAR"))
        form = QFormLayout(box)
        self.erp_power = spin(0.001, 1e6, 3, 1, " W")
        self.erp_cable = QComboBox()
        self.erp_cable.addItem(tr("(aucun câble)"), "")
        for name in cables.CABLES:
            self.erp_cable.addItem(name, name)
        self.erp_cable.addItem(tr("Personnalisé"), cables.CUSTOM)
        self.erp_cable.setToolTip(tr("Affaiblissement typique des fiches constructeurs, interpolé à la fréquence "
                                     "de l'onglet Analyse ; « Personnalisé » pour saisir la valeur exacte"))
        self.erp_attenuation = spin(0, 1000, 2, 0.1, " dB/100 m")
        self.erp_attenuation.setToolTip(tr("Affaiblissement linéique du câble à la fréquence de calcul"))
        self.erp_length = spin(0, 100000, 1, 1, " m")
        self.erp_extra = spin(0, 100, 2, 0.1, " dB")
        self.erp_extra.setToolTip(tr("Connecteurs, duplexeur, filtre, parafoudre, coupleur…"))
        self.erp_gain = spin(-30, 60, 2, 0.1, " dBi")
        self.erp_gain.setToolTip(tr("Repris du modèle d'antenne lorsqu'il est choisi ; modifiable"))
        self.erp_model_gain = None      # (modèle, gain) de l'émetteur affiché, ou None
        self._erp_gain_source = None    # (émetteur, modèle) dont le gain a été appliqué
        self._erp_keep_gain = False     # profil chargé : garder son gain jusqu'au prochain choix
        self.erp_model_button = QPushButton(tr("Gain du modèle"))
        self.erp_model_button.setToolTip(tr("Rétablir le gain indiqué par le modèle d'antenne"))
        self.erp_model_button.clicked.connect(
            lambda: self.erp_model_gain and self.erp_gain.setValue(round(self.erp_model_gain[1], 2)))
        gain_row = QHBoxLayout()
        gain_row.addWidget(self.erp_gain, 1)
        gain_row.addWidget(self.erp_model_button)
        form.addRow(tr("Puissance de l'émetteur"), self.erp_power)
        form.addRow(tr("Câble coaxial"), self.erp_cable)
        form.addRow(tr("Affaiblissement"), self.erp_attenuation)
        form.addRow(tr("Longueur du câble"), self.erp_length)
        form.addRow(tr("Pertes additionnelles"), self.erp_extra)
        form.addRow(tr("Gain de l'antenne"), gain_row)
        self.erp_result = QLabel()
        self.erp_result.setWordWrap(True)
        self.erp_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.erp_result)
        apply_button = QPushButton(tr("Reporter la PAR dans l'onglet Analyse"))
        apply_button.setToolTip(tr("PAR utilisée par SPLAT! (fichiers .lrp)"))
        apply_button.clicked.connect(self._apply_erp)
        form.addRow(apply_button)
        for box_widget in (self.erp_power, self.erp_attenuation, self.erp_length, self.erp_extra, self.erp_gain):
            box_widget.valueChanged.connect(self._erp_changed)
        self.erp_cable.currentIndexChanged.connect(self._erp_changed)
        return box             # premier calcul : set_params(), au chargement du profil

    def _erp_frequency(self):
        return self.lrp_fields["frequency"].value() if hasattr(self, "lrp_fields") else 0.0

    def _erp_values(self):
        """(fréquence, affaiblissement dB/100 m, pertes câble, pertes totales, PAR W, PAR dBm, PIRE dBm)."""
        frequency = self._erp_frequency()
        cable = self.erp_cable.currentData()
        if cable == cables.CUSTOM:
            attenuation = self.erp_attenuation.value()
        elif cable:
            attenuation = cables.attenuation(cable, frequency) or 0.0
        else:
            attenuation = 0.0
        cable_loss = attenuation * self.erp_length.value() / 100 if cable else 0.0
        total = cable_loss + self.erp_extra.value()
        return (frequency, attenuation, cable_loss, total,
                *cables.erp(self.erp_power.value(), self.erp_gain.value(), total))

    def _erp_changed(self, *_args):
        """Saisie dans le calcul de la PAR : nouveau résultat et paramètres modifiés."""
        self._erp_update()
        self.changed()

    def _erp_update(self):
        """Recalcule et affiche la PAR (sans signaler de modification)."""
        if not hasattr(self, "erp_result"):
            return
        cable = self.erp_cable.currentData()
        frequency, attenuation, cable_loss, total, erp_w, erp_dbm, eirp_dbm = self._erp_values()
        self.erp_attenuation.setEnabled(cable == cables.CUSTOM)
        self.erp_length.setEnabled(bool(cable))
        if cable and cable != cables.CUSTOM:
            self.erp_attenuation.blockSignals(True)
            self.erp_attenuation.setValue(round(attenuation, 2))
            self.erp_attenuation.blockSignals(False)
        gain_note = ""
        if self.erp_model_gain:
            name, model_gain = self.erp_model_gain
            if abs(self.erp_gain.value() - model_gain) < 0.005:
                gain_note = tr("gain du modèle « {name} » : {gain:.2f} dBi", name=name, gain=model_gain)
            else:
                gain_note = tr("gain saisi : {gain:.2f} dBi (modèle « {name} » : {model:.2f} dBi)",
                               gain=self.erp_gain.value(), name=name, model=model_gain)
            gain_note += "<br>"
        self.erp_result.setText(
            gain_note
            + tr("À {frequency:g} MHz (onglet Analyse) : câble {cable:.2f} dB, pertes totales {total:.2f} dB",
                 frequency=frequency, cable=cable_loss, total=total)
            + "<br><b>" + tr("PAR : {erp:.3g} W ({dbm:.2f} dBm)", erp=erp_w, dbm=erp_dbm) + "</b> — "
            + tr("PIRE : {eirp:.2f} dBm", eirp=eirp_dbm))

    def _apply_erp(self):
        erp_w = self._erp_values()[4]
        self.lrp_fields["erp"].setValue(round(erp_w, 3))
        self.lrp_enabled.setChecked(True)
        self.statusBar().showMessage(tr("PAR de {erp:.3f} W reportée dans l'onglet Analyse", erp=erp_w), 6000)

    def _refresh_antenna_tab(self):
        """Liste des émetteurs (noms à jour) et éditeur de l'émetteur courant."""
        if not hasattr(self, "antenna_tx_combo"):
            return
        names = [f"{i + 1} – {name or tr('sans nom')}" for i, name in enumerate(self.tx_table.names())]
        combo = self.antenna_tx_combo
        combo.blockSignals(True)
        if [combo.itemText(i) for i in range(combo.count())] != names:
            combo.clear()
            combo.addItems(names)
        row = self.tx_table.current_row() if names else -1
        if names and row < 0:
            row = 0
        combo.setCurrentIndex(row)
        combo.blockSignals(False)
        self._load_antenna(row)
        if row >= 0 and self.tx_table.current_row() < 0:
            self.tx_table.select_row(row)

    def _antenna_row_changed(self, _row):
        self._refresh_antenna_tab()

    def _antenna_tx_selected(self, index):
        self.tx_table.select_row(index)
        self._load_antenna(index)

    def _load_antenna(self, row):
        valid = self.tx_table.has_antenna(row)
        self.antenna_box.setEnabled(valid)
        self.antenna_tx_combo.setVisible(valid)
        self.antenna_empty.setVisible(not valid)
        name, azimuth, tilt = self.tx_table.antenna(row) if valid else ("", 0.0, 0.0)
        for widget in (self.antenna_model, self.antenna_azimuth, self.antenna_tilt):
            widget.blockSignals(True)
        self.tx_table.fill_antenna_combo(self.antenna_model, name)
        self.antenna_azimuth.setValue(azimuth)
        self.antenna_tilt.setValue(tilt)
        for widget in (self.antenna_model, self.antenna_azimuth, self.antenna_tilt):
            widget.blockSignals(False)
        self._show_antenna_pattern()

    def _store_antenna(self, *_args):
        row = self.antenna_tx_combo.currentIndex()
        self.tx_table.set_antenna(row, self.antenna_model.currentData() or "",
                                  self.antenna_azimuth.value(), self.antenna_tilt.value())
        self._show_antenna_pattern()

    def _show_antenna_pattern(self):
        name = self.antenna_model.currentData() or ""
        pattern = antennas.load(name) if name else None
        for plot, rotation in ((self.antenna_az_plot, self.antenna_azimuth.value()),
                               (self.antenna_el_plot, self.antenna_tilt.value())):
            plot.set_pattern(pattern)
            plot.set_rotation(rotation)
        if pattern:
            self.antenna_info.setText(antennas.describe(pattern))
        elif name:
            self.antenna_info.setText(tr("Modèle « {name} » absent de la bibliothèque : l'émetteur sera isotrope.",
                                         name=name))
        else:
            self.antenna_info.setText(tr("Isotrope : rayonnement identique dans toutes les directions."))
        self._apply_model_gain(name, pattern)

    def _apply_model_gain(self, name, pattern):
        """Choix d'une antenne (modèle ou émetteur affiché différent) : son gain est appliqué au
        calcul de la PAR. Le champ reste modifiable ; une saisie est conservée jusqu'au choix suivant
        (les simples rafraîchissements ne la remplacent pas)."""
        gain = pattern.get("gain_dbi") if pattern else None
        self.erp_model_gain = (name, gain) if gain is not None else None
        self.erp_model_button.setEnabled(gain is not None)
        source = (self.antenna_tx_combo.currentIndex(), name)
        if source != self._erp_gain_source:
            self._erp_gain_source = source
            if self._erp_keep_gain:
                self._erp_keep_gain = False          # gain du profil chargé conservé
            elif gain is not None:
                self.erp_gain.setValue(round(gain, 2))      # nouvelle antenne : modification signalée
        self._erp_update()

    def _antenna_apply_all(self):
        values = (self.antenna_model.currentData() or "", self.antenna_azimuth.value(), self.antenna_tilt.value())
        for row in range(len(self.tx_table.names())):
            self.tx_table.set_antenna(row, *values)

    def _build_lrp_box(self):
        """Paramètres de propagation (fichier .lrp), affichés dans l'onglet Analyse."""
        group = QGroupBox(tr("Propagation (modèle ITM / ITWOM)"))
        layout = QVBoxLayout(group)
        self.lrp_enabled = QCheckBox(tr("Fichier .lrp par émetteur"))
        self.lrp_enabled.setToolTip(tr("Écrit les paramètres ci-dessous dans un fichier .lrp pour chaque "
                                    "émetteur (sinon SPLAT! utilise ses valeurs par défaut)"))
        self.lrp_enabled.toggled.connect(self.changed)
        layout.addWidget(self.lrp_enabled)

        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
        self.ground = QComboBox()
        self.ground.addItem(tr("— Type de sol (préréglage) —"))
        for name, (eps, sigma) in splat.GROUNDS.items():
            self.ground.addItem(f"{tr(name)}  (ε={eps}, σ={sigma})", (eps, sigma))
        self.ground.activated.connect(self._apply_ground)
        form.addRow(tr("Préréglage"), self.ground)
        self.lrp_fields = {
            "dielectric": spin(1, 100, 3, 1),
            "conductivity": spin(0.0001, 100, 4, 0.001, " S/m"),
            "bending": spin(250, 400, 3, 1, " N"),
            "frequency": spin(20, 20000, 3, 1, " MHz"),
            "frac_situations": spin(0.01, 0.99, 2, 0.05),
            "frac_time": spin(0.01, 0.99, 2, 0.05),
            "erp": spin(0, 1e9, 3, 1, " W"),
        }
        self.climate = QComboBox()
        for code, label in splat.CLIMATES.items():
            self.climate.addItem(tr(label), code)
        self.polarization = QComboBox()
        self.polarization.addItem(tr("0 – Horizontale"), 0)
        self.polarization.addItem(tr("1 – Verticale"), 1)
        labels = {
            "dielectric": tr("Constante diélectrique du sol"),
            "conductivity": tr("Conductivité du sol"),
            "bending": tr("Réfractivité atmosphérique"),
            "frequency": tr("Fréquence"),
            "frac_situations": tr("Fraction de situations (lieux)"),
            "frac_time": tr("Fraction de temps"),
            "erp": tr("PAR / ERP (0 = perte de trajet seule)"),
        }
        for key in ("dielectric", "conductivity", "bending", "frequency"):
            form.addRow(labels[key], self.lrp_fields[key])
        form.addRow(tr("Climat radio"), self.climate)
        form.addRow(tr("Polarisation"), self.polarization)
        for key in ("frac_situations", "frac_time", "erp"):
            form.addRow(labels[key], self.lrp_fields[key])
        for field in self.lrp_fields.values():
            field.valueChanged.connect(self.changed)
        self.lrp_fields["frequency"].valueChanged.connect(self._erp_update)
        self.climate.currentIndexChanged.connect(self.changed)
        self.polarization.currentIndexChanged.connect(self.changed)
        self.lrp_enabled.toggled.connect(box.setEnabled)
        layout.addWidget(box)

        buttons = QHBoxLayout()
        load = QPushButton(tr("Importer un .lrp…"))
        load.clicked.connect(self._import_lrp)
        save = QPushButton(tr("Exporter en .lrp…"))
        save.clicked.connect(self._export_lrp)
        buttons.addWidget(load)
        buttons.addWidget(save)
        buttons.addStretch()
        layout.addLayout(buttons)
        return group

    def _build_options_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        values_box = QGroupBox(tr("Options numériques (laisser vide pour la valeur par défaut)"))
        form = QFormLayout(values_box)
        self.value_edits = {}
        for key, label in splat.VALUE_OPTIONS.items():
            edit = QLineEdit()
            edit.setPlaceholderText(tr("défaut"))
            edit.textChanged.connect(self.changed)
            form.addRow(tr(label), edit)
            self.value_edits[key] = edit
        layout.addWidget(values_box)

        flags_box = QGroupBox(tr("Options"))
        flags_layout = QVBoxLayout(flags_box)
        self.flag_checks = {}
        for key, label in splat.FLAG_OPTIONS.items():
            check = QCheckBox(tr(label))
            check.toggled.connect(self.changed)
            flags_layout.addWidget(check)
            self.flag_checks[key] = check
        layout.addWidget(flags_box)

        extra_box = QGroupBox(tr("Arguments supplémentaires (ajoutés tels quels)"))
        extra_layout = QVBoxLayout(extra_box)
        self.extra_args = QLineEdit()
        self.extra_args.textChanged.connect(self.changed)
        extra_layout.addWidget(self.extra_args)
        layout.addWidget(extra_box)
        layout.addStretch()
        return page

    def _build_files_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        out_box = QGroupBox(tr("Sorties"))
        form = QFormLayout(out_box)
        map_row = QHBoxLayout()
        self.map_enabled = QCheckBox(tr("Générer"))
        self.map_name = QLineEdit()
        self.map_enabled.toggled.connect(self.changed)
        self.map_enabled.toggled.connect(self.map_name.setEnabled)
        self.map_name.textChanged.connect(self.changed)
        map_row.addWidget(self.map_enabled)
        map_row.addWidget(self.map_name, 1)
        map_row.addWidget(QLabel(".ppm"))
        form.addRow(tr("Carte topographique (-o)"), map_row)
        aspect_row = QHBoxLayout()
        self.map_aspect_file = QCheckBox(tr("Carte aux bonnes proportions"))
        self.map_aspect_file.setToolTip(
            tr("SPLAT! trace un degré de longitude aussi large qu'un degré de latitude, ce qui étire "
            "la carte horizontalement de 1/cos(latitude). La copie corrigée respecte les distances.\n"
            "PPM : hauteur agrandie sans lissage (couleurs SPLAT! exactes) + calage .geo.\n"
            "PNG : largeur réduite avec lissage + calage .pgw (WGS84).\n"
            "Le format (4:3…) et le flou de la couverture de l'onglet « Cartes et graphes » s'appliquent."))
        self.map_aspect_format = QComboBox()
        self.map_aspect_format.addItem(tr("<carte>_proportions.ppm (+ .geo)"), "ppm")
        self.map_aspect_format.addItem(tr("<carte>_proportions.png (+ .pgw)"), "png")
        self.map_aspect_format.addItem(tr("PPM et PNG"), "ppm+png")
        # Sur la ligne de la case à cocher : la liste peut rétrécir si le panneau est étroit.
        self.map_aspect_format.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.map_aspect_format.setMinimumContentsLength(12)
        self.map_aspect_file.toggled.connect(self.changed)
        self.map_aspect_format.currentIndexChanged.connect(self.changed)
        self.map_enabled.toggled.connect(self.map_aspect_file.setEnabled)
        self.map_enabled.toggled.connect(self.map_aspect_format.setEnabled)
        aspect_row.addWidget(self.map_aspect_file)
        aspect_row.addWidget(self.map_aspect_format, 1)
        form.addRow(tr("Copie corrigée"), aspect_row)
        self.graph_checks = {}
        graphs_widget = QWidget()
        graphs_layout = QGridLayout(graphs_widget)
        graphs_layout.setContentsMargins(0, 0, 0, 0)
        for i, (key, label) in enumerate(splat.GRAPHS.items()):
            check = QCheckBox(tr(label))
            check.toggled.connect(self.changed)
            graphs_layout.addWidget(check, i // 2, i % 2)
            self.graph_checks[key] = check
        form.addRow(tr("Graphes point à point (nécessite gnuplot)"), graphs_widget)
        self.graph_format = QComboBox()
        self.graph_format.addItems(["png", "jpg", "gif", "svg", "ps", "pdf"])
        self.graph_format.currentIndexChanged.connect(self.changed)
        form.addRow(tr("Format des graphes"), self.graph_format)
        self.ano = QLineEdit()
        self.ano.setPlaceholderText(tr("aucun"))
        self.ano.textChanged.connect(self.changed)
        form.addRow(tr("Fichier alphanumérique de sortie (-ano)"), self.ano)
        self.log = QCheckBox(tr("Journal de la commande (-log)"))
        self.log.setToolTip(tr("Enregistre la ligne de commande dans commande.log"))
        self.log.toggled.connect(self.changed)
        form.addRow("", self.log)
        layout.addWidget(out_box)

        relief_box = QGroupBox(tr("Relief (fichiers SDF)"))
        relief = QVBoxLayout(relief_box)
        self.auto_terrain = QCheckBox(tr("Télécharger et préparer le relief manquant"))
        self.auto_terrain.setToolTip(tr("Télécharge les tuiles de relief manquantes (source ci-dessous) "
                                     "et les convertit en fichiers SDF"))
        self.auto_terrain.toggled.connect(self.changed)
        relief.addWidget(self.auto_terrain)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel(tr("Source du relief :")))
        self.relief_source_combo = QComboBox()
        for key, label in dem.SOURCES.items():
            self.relief_source_combo.addItem(tr(label), key)
        self.relief_source_combo.setToolTip(tr(
            "SRTM : relief mondial historique (converti par srtm2sdf).\n"
            "Copernicus GLO-30 : relief mondial plus récent et plus précis ; modèle de surface "
            "(inclut en partie arbres et bâtiments).\n"
            "IGN RGE ALTO : relief de la France au sol nu, le plus précis ; hors de France, "
            "complété par Copernicus."))
        self.relief_source_combo.currentIndexChanged.connect(self.changed)
        source_row.addWidget(self.relief_source_combo, 1)
        relief.addLayout(source_row)
        self.clutter_check = QCheckBox(tr("Ajouter le sursol (occupation du sol ESA WorldCover)"))
        self.clutter_check.setToolTip(tr(
            "Ajoute au relief la hauteur des arbres, du bâti et des arbustes d'après la carte "
            "d'occupation du sol ESA WorldCover (10 m). À l'emplacement des sites, le relief reste "
            "au sol nu : la hauteur d'antenne se compte depuis le sol.\n"
            "Conseillé avec l'IGN (sol nu) ; SRTM et Copernicus incluent déjà une partie du sursol."))
        self.clutter_check.toggled.connect(self.changed)
        relief.addWidget(self.clutter_check)
        clutter_row = QHBoxLayout()
        clutter_row.setContentsMargins(20, 0, 0, 0)
        self.clutter_heights = {}
        for key, label in (("trees", N_("Arbres")), ("built", N_("Bâti")), ("shrubs", N_("Arbustes"))):
            box = spin(0, 100, 1, 1.0, " m")
            box.valueChanged.connect(self.changed)
            clutter_row.addWidget(QLabel(tr(label)))
            clutter_row.addWidget(box)
            self.clutter_heights[key] = box
        clutter_row.addStretch()
        relief.addLayout(clutter_row)
        self.clutter_check.toggled.connect(lambda on: [b.setEnabled(on) for b in self.clutter_heights.values()])
        sdf_row = QVBoxLayout()
        sdf_row.addWidget(QLabel(tr("Dossier SDF (-d) :")))
        self.sdf_dir = PathEdit(directory=True)
        self.sdf_dir.edit.setPlaceholderText(tr("par défaut : terrain/sdf"))
        self.sdf_dir.setToolTip(str(terrain.SDF_DIR))
        self.sdf_dir.changed.connect(self.changed)
        sdf_row.addWidget(self.sdf_dir)
        relief.addLayout(sdf_row)
        note = QLabel(tr("Tuiles SRTM conservées dans terrain/srtm (on peut aussi y déposer "
                      "ses propres fichiers N48E002.hgt, .hgt.gz ou .zip). Les tuiles manquantes "
                      "sont détectées avant l'exécution (sites et portée -R) puis d'après les "
                      "messages de SPLAT!, qui est alors relancé une fois. Sans SDF, SPLAT! "
                      "suppose un terrain au niveau de la mer."))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        relief.addWidget(note)
        open_terrain = QPushButton(tr("Ouvrir le dossier du relief"))
        open_terrain.clicked.connect(self._open_terrain_dir)
        relief.addWidget(open_terrain, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(relief_box)

        disk_box = QGroupBox(tr("Espace disque"))
        disk = QHBoxLayout(disk_box)
        self.disk_usage = QLabel()
        self.disk_usage.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.disk_usage.setWordWrap(True)         # une ligne, sauf si le panneau est trop étroit
        disk.addWidget(self.disk_usage, 1)
        refresh = QPushButton(tr("Actualiser"))
        refresh.clicked.connect(self._update_disk_usage)
        disk.addWidget(refresh)
        self.sdf_dir.changed.connect(self._update_disk_usage)
        layout.addWidget(disk_box)
        layout.addStretch()
        self._update_disk_usage()
        self._build_input_dialog()
        return page

    def _update_disk_usage(self, *_args):
        """Une ligne : taille des tuiles SRTM, des caches Copernicus / IGN / WorldCover (s'ils
        existent) et des fichiers SDF, espace libre du ou des lecteurs. Dossiers et nombres de
        fichiers en infobulle."""
        custom = self.sdf_dir.text().strip()
        # SDF : dossier choisi, sinon celui du SRTM et ceux de chaque configuration de dem.py.
        sdf_dirs = [Path(custom)] if custom else [terrain.SDF_DIR, *sorted(terrain.TERRAIN_DIR.glob("sdf-*"))]
        groups = [("SRTM", [terrain.SRTM_DIR], terrain.SRTM_SUFFIXES, True),
                  ("Copernicus", [dem.CACHE["copernicus"]], (".gz",), False),
                  ("IGN", [dem.CACHE["ign"]], (".gz",), False),
                  ("WorldCover", [dem.CACHE["worldcover"]], (".gz",), False),
                  ("SDF", sdf_dirs, (".sdf",), True)]
        parts, details, folders = [], [], []
        for name, group_dirs, suffixes, always in groups:
            usage = [(folder, *terrain.folder_usage(folder, suffixes)) for folder in group_dirs]
            count, size = sum(u[1] for u in usage), sum(u[2] for u in usage)
            if not (always or count):
                continue
            parts.append(tr("{name} : {size}", name=name, size=format_size(size)))
            details += [tr("{name} : {n} fichier(s) dans {folder}", name=name, n=n, folder=folder)
                        for folder, n, _size in usage if n or always and folder == group_dirs[0]]
            folders += group_dirs
        drives = {}
        for folder in folders:
            space = terrain.disk_space(folder)
            if space:
                drives.setdefault(space[0].upper(), space)
        free = ", ".join(f"{format_size(free)} ({drive.rstrip(chr(92))})" for drive, free, _total in drives.values())
        parts.append(tr("libre : {free}", free=free or tr("inconnu")))
        details += [tr("{drive} : {free} libres sur {total}", drive=drive, free=format_size(free),
                       total=format_size(total)) for drive, free, total in drives.values()]
        self.disk_usage.setText("  ·  ".join(parts))
        self.disk_usage.setToolTip("\n".join(details))

    def _build_input_dialog(self):
        """Fenêtre « Autres données d'entrée » (menu Fichier) : paramètres du profil, conservés
        d'une ouverture à l'autre."""
        self.input_dialog = QDialog(self)
        self.input_dialog.setWindowTitle(tr("Autres données d'entrée"))
        self.input_dialog.resize(560, 0)
        in_box = QGroupBox(tr("Autres données d'entrée"))
        form = QFormLayout(in_box)
        self.city_files = FileList(5)
        self.city_files.changed.connect(self.changed)
        form.addRow(tr("Villes / sites (-s)"), self.city_files)
        self.boundary_files = FileList(5)
        self.boundary_files.changed.connect(self.changed)
        form.addRow(tr("Limites cartographiques (-b)"), self.boundary_files)
        self.udt = PathEdit()
        self.udt.changed.connect(self.changed)
        form.addRow(tr("Terrain utilisateur (-udt)"), self.udt)
        self.ani = PathEdit()
        self.ani.changed.connect(self.changed)
        form.addRow(tr("Fichier alphanumérique d'entrée (-ani)"), self.ani)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.input_dialog.reject)
        layout = QVBoxLayout(self.input_dialog)
        layout.addWidget(in_box)
        layout.addWidget(buttons)

    def show_input_dialog(self):
        self.input_dialog.show()
        self.input_dialog.raise_()
        self.input_dialog.activateWindow()

    def _build_results_panel(self):
        self.results_tabs = QTabWidget()

        # Console
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(mono_font())
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.results_tabs.addTab(self.console, tr("Console"))

        # Rapports
        reports = QWidget()
        rl = QVBoxLayout(reports)
        self.report_combo = QComboBox()
        self.report_combo.currentIndexChanged.connect(self._show_report)
        self.report_view = QPlainTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setFont(mono_font())
        self.report_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        rl.addWidget(self.report_combo)
        rl.addWidget(self.report_view, 1)
        self.results_tabs.addTab(reports, tr("Rapports"))

        # Images
        images = QWidget()
        il = QVBoxLayout(images)
        top = QHBoxLayout()
        self.image_combo = QComboBox()
        self.image_combo.currentIndexChanged.connect(self._show_image)
        top.addWidget(self.image_combo, 1)
        self.image_view = ImageView()
        for text, slot in ((N_("Ajuster"), self.image_view.fit), ("100 %", self.image_view.actual_size),
                           ("+", lambda: self.image_view.zoom(1.25)),
                           ("−", lambda: self.image_view.zoom(0.8)),
                           (N_("Ouvrir"), self._open_current_image)):
            button = QPushButton(tr(text))
            button.clicked.connect(slot)
            top.addWidget(button)
        self.image_info = QLabel()
        self.image_view.zoomChanged.connect(self._update_image_info)
        self.image_view.contextRequested.connect(self._map_context_menu)
        il.addLayout(top)
        il.addWidget(self._build_overlay_bar())
        view_row = QHBoxLayout()
        self.live_map = livemap.LiveMap()
        self.live_map.zoomChanged.connect(lambda _z: self._overlay_timer.start())
        self.live_map.homeRequested.connect(lambda: self._update_live_map(refit=True))
        self.live_map.statusText.connect(lambda text: self.overlay_status.setText(text))
        self.live_map.srtm_url = self.settings.get("srtm_url") or terrain.DEFAULT_URL
        self.live_map.hoveredText.connect(lambda text: self.image_info.setText(
            tr("Carte en ligne — {text}  (glisser : déplacer, molette : zoomer)", text=text)))
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.image_view)
        self.view_stack.addWidget(self.live_map)
        view_row.addWidget(self.view_stack, 1)
        view_row.addWidget(self._build_layers_panel())
        self._display_mode_changed(refresh=False)
        il.addLayout(view_row, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.image_info, 1)
        self.overlay_status = QLabel()
        self.overlay_status.setStyleSheet("color: gray;")
        bottom.addWidget(self.overlay_status)
        il.addLayout(bottom)
        self.results_tabs.addTab(images, tr("Cartes et graphes"))

        # Profil de liaison (point à point), tracé par l'application
        profile_page = QWidget()
        pl = QVBoxLayout(profile_page)
        top = QHBoxLayout()
        self.profile_tx = QComboBox()
        self.profile_tx.setToolTip(tr("Émetteur dont le profil est affiché"))
        self.profile_tx.currentIndexChanged.connect(lambda _i: self._update_profile())
        top.addWidget(self.profile_tx)
        self.profile_summary = QLabel()
        self.profile_summary.setWordWrap(True)
        self.profile_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.profile_summary, 1)
        export = QPushButton(tr("Exporter (PNG)…"))
        export.clicked.connect(self._export_profile)
        top.addWidget(export)
        self.profile_view = linkprofile.ProfileView()
        self.profile_hover = QLabel()
        self.profile_view.hovered.connect(self.profile_hover.setText)
        pl.addLayout(top)
        pl.addWidget(self.profile_view, 1)
        pl.addWidget(self.profile_hover)
        self.profile_tab = self.results_tabs.addTab(profile_page, tr("Profil"))
        self.profile_request = None          # (paramètres, dossier) du calcul point à point affiché
        self.profile_data = []               # profils calculés, un par émetteur
        self.results_tabs.currentChanged.connect(lambda _i: self._update_profile())

        # Fichiers
        files = QWidget()
        fl = QVBoxLayout(files)
        self.files_label = QLabel()
        self.files_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.files_list = QListWidget()
        self.files_list.itemDoubleClicked.connect(
            lambda item: QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole))))
        open_dir = QPushButton(tr("Ouvrir le dossier"))
        open_dir.clicked.connect(lambda: self.run_dir and QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.run_dir))))
        fl.addWidget(self.files_label)
        fl.addWidget(self.files_list, 1)
        fl.addWidget(open_dir, 0, Qt.AlignmentFlag.AlignLeft)
        self.results_tabs.addTab(files, tr("Fichiers"))

        # Historique
        hist = QWidget()
        hl = QVBoxLayout(hist)
        self.history_table = QTableWidget(0, 6)
        self.history_table.setHorizontalHeaderLabels(
            [tr("Date"), tr("Profil"), tr("Version"), tr("Mode"), tr("Émetteurs"), tr("Résultat")])
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.itemDoubleClicked.connect(lambda _i: self._history_show())
        hl.addWidget(self.history_table, 1)
        buttons = QHBoxLayout()
        for text, slot in ((N_("Afficher les résultats"), self._history_show),
                           (N_("Recharger ces paramètres"), self._history_reload),
                           (N_("Ouvrir le dossier"), self._history_open_dir),
                           (N_("Retirer de l'historique"), self._history_remove)):
            button = QPushButton(tr(text))
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        hl.addLayout(buttons)
        self.results_tabs.addTab(hist, tr("Historique"))
        return self.results_tabs

    # ---- Paramètres <-> widgets -----------------------------------------------

    def set_params(self, params):
        self._loading = True
        try:
            self.arch_combo.setCurrentIndex(max(0, self.arch_combo.findData(params["arch"])))
            self.variant_combo.setCurrentIndex(max(0, self.variant_combo.findData(params["variant"])))
            for button in self.mode_group.buttons():
                button.setChecked(button.property("mode") == params["mode"])
            self.rx_height.setValue(float(params["rx_height"]))
            self.metric.setChecked(params["metric"])
            self.tx_table.set_sites(params["tx_sites"])
            self.rx_form.set_site(params["rx_site"])

            lrp = params["lrp"]
            self.lrp_enabled.setChecked(lrp["enabled"])
            for key, field in self.lrp_fields.items():
                field.setValue(float(lrp[key]))
            self.climate.setCurrentIndex(max(0, self.climate.findData(int(lrp["climate"]))))
            self.polarization.setCurrentIndex(int(lrp["polarization"]))
            self.ground.setCurrentIndex(0)

            for key, edit in self.value_edits.items():
                edit.setText(str(params["values"].get(key, "")))
            for key, check in self.flag_checks.items():
                check.setChecked(bool(params["flags"].get(key)))
            self.extra_args.setText(params["extra_args"])

            self.map_enabled.setChecked(params["map_enabled"])
            self.map_name.setEnabled(params["map_enabled"])
            self.map_name.setText(params["map_name"])
            self.map_aspect_file.setChecked(params["map_aspect_file"])
            self.map_aspect_file.setEnabled(params["map_enabled"])
            self.map_aspect_format.setCurrentIndex(max(0, self.map_aspect_format.findData(params["map_aspect_format"])))
            self.map_aspect_format.setEnabled(params["map_enabled"])
            for key, check in self.graph_checks.items():
                check.setChecked(bool(params["graphs"].get(key)))
            self.graph_format.setCurrentText(params["graph_format"])
            self.ano.setText(params["ano"])
            self.log.setChecked(params["log"])
            self.sdf_dir.setText(params["sdf_dir"])
            self.auto_terrain.setChecked(params["auto_terrain"])
            self.relief_source_combo.setCurrentIndex(max(0, self.relief_source_combo.findData(params["relief_source"])))
            self.clutter_check.setChecked(bool(params["clutter"]["enabled"]))
            for key, box in self.clutter_heights.items():
                box.setValue(float(params["clutter"][key]))
                box.setEnabled(bool(params["clutter"]["enabled"]))
            self.city_files.setFiles(params["city_files"])
            self.boundary_files.setFiles(params["boundary_files"])
            self.udt.setText(params["udt"])
            self.ani.setText(params["ani"])
            calc = params["erp_calc"]
            self.erp_power.setValue(float(calc["power"]))
            self.erp_cable.setCurrentIndex(max(0, self.erp_cable.findData(calc["cable"])))
            self.erp_attenuation.setValue(float(calc["attenuation"]))
            self.erp_length.setValue(float(calc["length"]))
            self.erp_extra.setValue(float(calc["extra_loss"]))
            self.erp_gain.setValue(float(calc["gain"]))
            self._erp_gain_source, self._erp_keep_gain = None, True
        finally:
            self._loading = False
        self._erp_update()
        self._refresh_state()

    def get_params(self):
        """Lit les widgets ; lève splat.ParamError si une saisie est invalide."""
        checked = self.mode_group.checkedButton()
        return {
            "arch": self.arch_combo.currentData(),
            "variant": self.variant_combo.currentData(),
            "mode": checked.property("mode") if checked else "p2p",
            "metric": self.metric.isChecked(),
            "rx_height": self.rx_height.value(),
            "tx_sites": self.tx_table.sites(),
            "rx_site": self.rx_form.site(),
            "lrp": {
                "enabled": self.lrp_enabled.isChecked(),
                **{key: field.value() for key, field in self.lrp_fields.items()},
                "climate": self.climate.currentData(),
                "polarization": self.polarization.currentData(),
            },
            "values": {key: edit.text().strip() for key, edit in self.value_edits.items()},
            "flags": {key: check.isChecked() for key, check in self.flag_checks.items()},
            "map_enabled": self.map_enabled.isChecked(),
            "map_name": self.map_name.text().strip(),
            "map_aspect_file": self.map_aspect_file.isChecked(),
            "map_aspect_format": self.map_aspect_format.currentData(),
            "graphs": {key: check.isChecked() for key, check in self.graph_checks.items()},
            "graph_format": self.graph_format.currentText(),
            "sdf_dir": self.sdf_dir.text().strip(),
            "auto_terrain": self.auto_terrain.isChecked(),
            "relief_source": self.relief_source_combo.currentData(),
            "clutter": {"enabled": self.clutter_check.isChecked(),
                        **{key: box.value() for key, box in self.clutter_heights.items()}},
            "city_files": self.city_files.files(),
            "boundary_files": self.boundary_files.files(),
            "udt": self.udt.text().strip(),
            "ani": self.ani.text().strip(),
            "ano": self.ano.text().strip(),
            "log": self.log.isChecked(),
            "extra_args": self.extra_args.text(),
            "erp_calc": {
                "power": self.erp_power.value(),
                "cable": self.erp_cable.currentData(),
                "attenuation": self.erp_attenuation.value(),
                "length": self.erp_length.value(),
                "extra_loss": self.erp_extra.value(),
                "gain": self.erp_gain.value(),
            },
        }

    def changed(self, *_args):
        if not self._loading and hasattr(self, "_preview_timer"):
            self._preview_timer.start()

    def _refresh_state(self):
        self._refresh_antenna_tab()      # noms des émetteurs modifiés dans l'onglet Émetteurs
        try:
            params = self.get_params()
        except splat.ParamError as exc:
            self.preview.setPlainText(tr("(saisie invalide : {exc})", exc=exc))
            self._set_dirty(True)
            return
        self.preview.setPlainText(splat.command_preview(params))
        self.rx_box.setEnabled(params["mode"] == "p2p")
        self.rx_height.setEnabled(params["mode"] != "p2p")
        for check in self.graph_checks.values():
            check.setEnabled(params["mode"] == "p2p")
        exe = splat.executable_path(params["arch"], params["variant"])
        self.run_action.setEnabled(exe.exists() and not self._busy())
        if not exe.exists():
            self.statusBar().showMessage(tr("Exécutable absent : {exe}", exe=exe))
        self._set_dirty(params != self.saved_params)

    def _set_dirty(self, dirty):
        self.dirty = dirty
        self.setWindowTitle(f"SPLAT!Gui — {self.profile_name}{' *' if dirty else ''}")

    # ---- Profils -------------------------------------------------------------

    def _reload_profile_combo(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(storage.list_profiles())
        self.profile_combo.setCurrentText(self.profile_name)
        self.profile_combo.blockSignals(False)

    def _params_or_warn(self):
        try:
            return self.get_params()
        except splat.ParamError as exc:
            QMessageBox.warning(self, tr("Saisie invalide"), str(exc))
            return None

    def _confirm_discard(self):
        """Propose d'enregistrer les modifications. Renvoie False si l'utilisateur annule."""
        if not getattr(self, "dirty", False):
            return True
        answer = QMessageBox.question(
            self, tr("Modifications non enregistrées"),
            tr("Enregistrer les modifications du profil « {name} » ?", name=self.profile_name),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self.save_profile()
        return True

    def _switch_profile(self, name, params=None):
        self.profile_name = name
        self.saved_params = storage.load_profile(name)
        self._reload_profile_combo()
        self.set_params(params or self.saved_params)
        self.settings["last_profile"] = name

    def _on_profile_selected(self, _index):
        name = self.profile_combo.currentText()
        if name == self.profile_name:
            return
        if not self._confirm_discard():
            self.profile_combo.setCurrentText(self.profile_name)
            return
        self._switch_profile(name)
        self.statusBar().showMessage(tr("Profil « {name} » chargé", name=name), 4000)

    def save_profile(self):
        params = self._params_or_warn()
        if params is None:
            return False
        storage.save_profile(self.profile_name, params)
        self.saved_params = params
        self._reload_profile_combo()
        self._refresh_state()
        self.statusBar().showMessage(tr("Profil « {name} » enregistré", name=self.profile_name), 4000)
        return True

    def _ask_name(self, title, default=""):
        name, ok = QInputDialog.getText(self, title, tr("Nom du profil :"), text=default)
        name = name.strip()
        if not ok or not name:
            return None
        if name in storage.list_profiles() and QMessageBox.question(
                self, title, tr("Le profil « {name} » existe déjà. Le remplacer ?", name=name)) \
                != QMessageBox.StandardButton.Yes:
            return None
        return name

    def save_profile_as(self):
        params = self._params_or_warn()
        name = params is not None and self._ask_name(tr("Enregistrer sous"), self.profile_name)
        if not name:
            return
        storage.save_profile(name, params)
        self._switch_profile(name, params)

    def new_profile(self):
        if not self._confirm_discard():
            return
        name = self._ask_name(tr("Nouveau profil"))
        if name:
            storage.save_profile(name, storage.default_params())
            self._switch_profile(name)

    def rename_profile(self):
        old = self.profile_name
        name = self._ask_name(tr("Renommer le profil"), old)
        if not name or name == old:
            return
        params = self._params_or_warn()
        if params is None:
            return
        storage.save_profile(name, params)
        storage.delete_profile(old)
        self._switch_profile(name, params)

    def delete_profile(self):
        name = self.profile_name
        if QMessageBox.question(self, tr("Supprimer"), tr("Supprimer le profil « {name} » ?", name=name)) \
                != QMessageBox.StandardButton.Yes:
            return
        storage.delete_profile(name)
        self._switch_profile(storage.list_profiles()[0])

    def import_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Importer un profil"), "", tr(PROFILE_FILTER))
        if not path:
            return
        try:
            name, params = storage.import_profile(path)
        except ValueError as exc:
            QMessageBox.warning(self, tr("Import impossible"), str(exc))
            return
        name = self._ask_name(tr("Importer un profil"), name)
        if name:
            storage.save_profile(name, params)
            self._switch_profile(name)

    def export_profile(self):
        params = self._params_or_warn()
        if params is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Exporter le profil"), splat.safe_filename(self.profile_name) + ".json", tr(PROFILE_FILTER))
        if path:
            storage.export_profile(path, self.profile_name, params)

    def reset_params(self):
        self.set_params(storage.default_params())

    # ---- Imports divers ---------------------------------------------------

    # ---- Désignation sur carte -------------------------------------------------

    def _site_markers(self, exclude_row=None, exclude_rx=False):
        """Sites déjà saisis, affichés pour repère sur la carte de désignation."""
        markers = []
        try:
            tx_sites = self.tx_table.sites()
        except splat.ParamError:
            tx_sites = []
        names = sites.icon_names([dict(s, role="tx") for s in tx_sites], "auto", None)
        for row, (site, name) in enumerate(zip(tx_sites, names)):
            if row != exclude_row:
                markers.append((site["lat"], site["lon"], sites.icon(name, 64), site["name"]))
        if not exclude_rx:
            try:
                rx = self.rx_form.site()
                markers.append((rx["lat"], rx["lon"], sites.icon("antenne.png", 64), rx["name"]))
            except splat.ParamError:
                pass
        return markers

    def _open_picker(self, title, lat, lon, markers):
        if lat is None:
            lat, lon = (markers[0][0], markers[0][1]) if markers else (46.6, 2.4)
        dialog = mappicker.SitePickerDialog(title, lat, lon, markers, self.settings, self)
        if dialog.exec():
            storage.save_settings(self.settings)
            return dialog.coordinates()
        return None

    def _map_context_menu(self, scene_pos, global_pos):
        """Clic droit sur une carte calée : placer un site au point désigné."""
        ref = self.display_ref
        if ref is None or self.image_combo.currentData() is None:
            return
        dx, dy = ref.pixel_size()
        lon = ref.lon0 + (scene_pos.x() - 0.5) * dx
        lat = ref.lat0 + (scene_pos.y() - 0.5) * dy
        if not (0 <= scene_pos.x() < ref.width and 0 <= scene_pos.y() < ref.height):
            return
        alt = hillshade.elevation_at(lat, lon)
        menu = QMenu(self)
        header = menu.addAction(f"{lat:.6f}, {lon:.6f}" + (tr(" — sol {alt:.0f} m", alt=alt) if alt is not None else ""))
        header.setEnabled(False)
        menu.addSeparator()
        try:
            tx_sites = self.tx_table.sites()
        except splat.ParamError:
            tx_sites = []
        actions = {}
        for row, site in enumerate(tx_sites):
            actions[menu.addAction(tr("Placer l'émetteur « {name} » ici", name=site["name"]))] = ("tx", row)
        actions[menu.addAction(tr("Ajouter un émetteur ici"))] = ("new", None)
        actions[menu.addAction(tr("Placer le récepteur ici"))] = ("rx", None)
        menu.addSeparator()
        copy = menu.addAction(tr("Copier les coordonnées"))
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is copy:
            QApplication.clipboard().setText(f"{lat:.6f}, {lon:.6f}")
            return
        kind, row = actions[chosen]
        if kind == "new":
            self.tx_table.add_empty()
            row = self.tx_table.table.rowCount() - 1
            kind = "tx"
        if kind == "tx":
            self.tx_table.set_coordinates(row, lat, lon)
        else:
            self.rx_form.set_coordinates(lat, lon)
        self.statusBar().showMessage(
            tr("Site placé en {lat:.6f}, {lon:.6f} — relancez le calcul (F5) pour mettre la carte à jour",
               lat=lat, lon=lon), 8000)

    def _pick_tx(self, row):
        if row < 0:
            self.tx_table.add_empty()
            row = self.tx_table.table.rowCount() - 1
            self.tx_table.table.selectRow(row)
        try:
            site = self.tx_table.sites()[row]
            lat, lon = site["lat"], site["lon"]
            if lat == 0 and lon == 0:
                lat = lon = None
        except (splat.ParamError, IndexError):
            site, lat, lon = {"name": f"TX{row + 1}"}, None, None
        result = self._open_picker(tr("Position de l'émetteur « {name} »", name=site["name"]), lat, lon,
                                   self._site_markers(exclude_row=row))
        if result:
            self.tx_table.set_coordinates(row, *result)

    def _pick_rx(self):
        try:
            site = self.rx_form.site()
            lat, lon = site["lat"], site["lon"]
        except splat.ParamError:
            site, lat, lon = {"name": tr("récepteur")}, None, None
        result = self._open_picker(tr("Position du récepteur « {name} »", name=site["name"]), lat, lon,
                                   self._site_markers(exclude_rx=True))
        if result:
            self.rx_form.set_coordinates(*result)

    # ---- Antennes ---------------------------------------------------------

    def manage_antennas(self):
        select = None
        try:
            tx = self.tx_table.sites()
            row = max(0, self.tx_table.table.currentRow())
            select = tx[row]["antenna"] if tx and tx[row]["antenna"] else None
        except (splat.ParamError, IndexError):
            pass
        dialog = antenna_ui.AntennaManager(self, select)
        dialog.exec()
        if dialog.changed:
            self.tx_table.refresh_antennas()
            self.changed()

    def _import_rx(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Importer le récepteur"),
                                              data_folder(folder_setting(self.settings, "qth_dir")),
                                              tr(QTH_FILTER))
        if path:
            try:
                self.rx_form.set_site(splat.read_qth(path))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, tr("Import impossible"), str(exc))

    def _apply_ground(self, index):
        data = self.ground.itemData(index)
        if data:
            self.lrp_fields["dielectric"].setValue(data[0])
            self.lrp_fields["conductivity"].setValue(data[1])

    def _import_lrp(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Importer un .lrp"),
                                              data_folder(folder_setting(self.settings, "lrp_dir")), tr(LRP_FILTER))
        if not path:
            return
        try:
            lrp = splat.read_lrp(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("Import impossible"), str(exc))
            return
        params = self._params_or_warn()
        if params:
            params["lrp"] = lrp
            self.set_params(params)

    def _export_lrp(self):
        params = self._params_or_warn()
        if params is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Exporter en .lrp"),
                                              str(Path(data_folder(folder_setting(self.settings, "lrp_dir")))
                                                  / "splat.lrp"), tr(LRP_FILTER))
        if path:
            Path(path).write_text(splat.lrp_text(params["lrp"]), encoding="ascii")

    # ---- Exécution -------------------------------------------------------

    def _busy(self):
        return self.process is not None or self.terrain_worker is not None

    def run(self):
        if self._busy():
            return
        params = self._params_or_warn()
        if params is None:
            return
        try:
            run_dir, exe, args, env = splat.prepare_run(params, self.settings, self.profile_name)
        except (splat.ParamError, OSError) as exc:
            QMessageBox.warning(self, tr("Lancement impossible"), str(exc))
            return

        self.settings["last_params"] = params
        self.settings["last_profile"] = self.profile_name
        storage.save_settings(self.settings)

        self.run_dir = run_dir
        self.run_params = params
        self.run_spec = (exe, args, env)
        self.terrain_retried = False
        self.site_tiles = []          # tuiles corrigées (sursol) écrites dans le dossier du calcul
        self.run_started = datetime.datetime.now()
        self.history = storage.add_history({
            "date": self.run_started.strftime("%Y-%m-%d %H:%M:%S"),
            "profile": self.profile_name,
            "params": params,
            "run_dir": str(run_dir),
            "command": " ".join([exe.name, *args]),
            "status": N_("en cours"),       # enregistré en français, traduit à l'affichage
        })
        self._refresh_history()

        self.console.clear()
        self._console_write(tr("Dossier : {folder}", folder=run_dir) + f"\n> {splat.command_preview(params)}\n\n")
        self._clear_results()
        self.run_action.setEnabled(False)
        self.stop_action.setEnabled(True)

        if params["auto_terrain"]:
            try:
                tiles = terrain.tiles_for_params(params)
            except (ValueError, KeyError):
                tiles = []
            self._start_terrain(tiles, self._after_initial_terrain)
        else:
            self._start_splat()

    # Relief ------------------------------------------------------------------

    def _start_terrain(self, tiles, callback):
        params = self.run_params
        hd = params["variant"] == "hd"
        if len(tiles) > terrain.MAX_AUTO_TILES:
            self._console_write(tr("Relief : {n} tuiles nécessaires, au-delà de la limite automatique ({max}). "
                                   "Réduisez la portée (-R).\n", n=len(tiles), max=terrain.MAX_AUTO_TILES))
            callback(None)
            return
        sdf_dir = splat.effective_sdf_dir(params)
        url = self.settings.get("srtm_url") or terrain.DEFAULT_URL
        self._console_write(tr("Relief {kind} : {n} tuile(s) ({tiles}) dans {folder}\n",
                               kind="HD" if hd else "standard", n=len(tiles), folder=sdf_dir,
                               tiles=", ".join(terrain.tile_name(*t) for t in tiles) or tr("aucune")))
        if dem.uses_dem(params):
            self._console_write(tr("Source : {source}\n", source=dem.describe(params)))
            source, clutter, run_dir = params["relief_source"], params["clutter"], self.run_dir
            run_sites = params["tx_sites"] + ([params["rx_site"]] if params["mode"] == "p2p" else [])

            def prepare(log, cancel, progress):
                summary = dem.ensure_tiles(tiles, hd, sdf_dir, source, clutter, url, log, cancel, progress)
                # Sursol : tuiles des sites corrigées (sol nu sous les antennes), dans le dossier du calcul.
                self.site_tiles += dem.write_site_tiles(run_dir, run_sites, hd, source, clutter, url, log)
                return summary
        else:
            converters = terrain.converter_candidates(params["arch"], self.settings["extra_paths"], hd)

            def prepare(log, cancel, progress):
                return terrain.ensure_tiles(tiles, hd, sdf_dir, converters, url, log, cancel=cancel, progress=progress)
        self.terrain_worker = TerrainWorker(prepare, self)
        self.terrain_worker.log.connect(self._console_write)
        self.terrain_worker.progress.connect(
            lambda n, total: self._set_progress(n / total if total else None, tr("Relief : {n}/{total} tuile(s)", n=n, total=total)))
        self._set_progress(0.0 if tiles else None, tr("Préparation du relief…"))
        self.terrain_worker.done.connect(lambda summary: self._terrain_done(summary, callback))
        self.terrain_worker.start()
        self.statusBar().showMessage(tr("Préparation du relief…"))

    def _terrain_done(self, summary, callback):
        worker, self.terrain_worker = self.terrain_worker, None
        worker.wait()
        worker.deleteLater()
        if summary is None:
            self._console_write(tr("Préparation du relief interrompue.\n"))
            self._finalize("interrompu")
            return
        parts = [f"{len(summary[k])} {tr(label)}" for k, label in (
            ("present", N_("déjà présente(s)")), ("converted", N_("convertie(s)")),
            ("unavailable", N_("sans données")), ("failed", N_("en échec"))) if summary[k]]
        self._console_write(tr("Relief : {parts}.\n\n", parts=", ".join(parts) or tr("rien à faire")))
        callback(summary)

    def _after_initial_terrain(self, _summary):
        self._start_splat()

    def _after_retry_terrain(self, summary):
        if summary and summary["converted"]:
            self._console_write(tr("Relance de SPLAT! avec le relief complété…\n\n"))
            self._start_splat()
        else:
            self._finalize(self.pending_result)

    # SPLAT! ------------------------------------------------------------------

    def _start_splat(self):
        exe, args, env = self.run_spec
        if self.run_params["variant"] == "hd":
            # Noms de tuiles attendus par splat-hd.exe (voir terrain.hd_alias).
            for folder in (self.run_dir, splat.effective_sdf_dir(self.run_params)):
                for error in terrain.link_hd_aliases(folder) if folder else []:
                    self._console_write(tr("Alias de tuile HD impossible : {error}\n", error=error))
        self.splat_output = ""
        self._progress_label = tr("Relance de SPLAT!") if self.terrain_retried else "SPLAT!"
        self._set_progress(None if self.run_params["mode"] == "p2p" else 0.0,
                           tr("{label} : démarrage…", label=self._progress_label))
        qenv = QProcessEnvironment()
        for key, value in env.items():
            qenv.insert(key, value)
        self.process = QProcess(self)
        self.process.setProcessEnvironment(qenv)
        self.process.setWorkingDirectory(str(self.run_dir))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.process.start(str(exe), args)
        self.process.closeWriteChannel()
        self.statusBar().showMessage(tr("Exécution de {exe} ({arch})…", exe=exe.name, arch=self.run_params["arch"]))

    def stop(self):
        if self.terrain_worker is not None:
            self.terrain_worker.cancel()
        if self.process is not None:
            self.process.kill()

    def _console_write(self, text):
        self.console.moveCursor(self.console.textCursor().MoveOperation.End)
        self.console.insertPlainText(text)
        self.console.ensureCursorVisible()

    def _read_output(self):
        data = bytes(self.process.readAllStandardOutput()).decode("latin-1", "replace")
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        self.splat_output += data
        self._console_write(data)
        self._update_splat_progress()

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._console_write(tr("\nImpossible de démarrer : {error}\n", error=self.process.errorString()))
            self._finished(-1, QProcess.ExitStatus.CrashExit)

    def _finished(self, code, status):
        if self.process is None:
            return
        self._read_output()
        process, self.process = self.process, None
        process.deleteLater()

        problem = splat.describe_exit_code(code)
        if problem:
            arch = self.run_params["arch"]
            result = tr("échec : {problem}", problem=problem)
            self._console_write(tr(
                "\n*** SPLAT! n'a pas pu démarrer : {problem}.\n"
                "Ajoutez le dossier contenant les DLL requises au PATH de la version {arch} "
                "(menu Fichier > Pré-requis…, ou Réglages…, bouton « Tester »).\n", problem=problem, arch=arch))
        elif status == QProcess.ExitStatus.CrashExit:
            result = N_("interrompu") if code in (-1, 0, 1, 62097) else tr("plantage ({code:#x})", code=code)
        elif splat.describe_crash(code):
            result = splat.describe_crash(code)
            self._console_write(tr(
                "\n*** {result}. Les fichiers produits avant l'arrêt sont conservés "
                "(un rapport peut être vide).\n", result=result))
        else:
            result = N_("terminé") if code == 0 else tr("code {code}", code=code)

        # Tuiles signalées absentes par SPLAT! : téléchargement, conversion, puis une relance.
        missing = terrain.missing_from_output(self.splat_output)
        if missing and result != "interrompu" and not problem:
            if self.run_params["auto_terrain"] and not self.terrain_retried:
                self.terrain_retried = True
                self.pending_result = result
                self._console_write(tr("\nSPLAT! a supposé le niveau de la mer pour {n} tuile(s) : "
                                       "recherche du relief manquant.\n", n=len(missing)))
                self._start_terrain(missing, self._after_retry_terrain)
                return
            self._console_write(tr(
                "\nAttention : relief absent pour {tiles} (niveau de la mer supposé).\n",
                tiles=", ".join(terrain.tile_name(*t) for t in missing)))
        self._finalize(result, switch_tab=not problem)

    # Barre de progression ---------------------------------------------------

    QUARTER_RE = re.compile(r"\d+% to\s+\d+% ")
    CHARS_PER_QUARTER = 65   # caractères « .oOo » affichés par SPLAT! pour chaque quart

    def _set_progress(self, fraction, text):
        """fraction : 0..1, ou None pour une progression indéterminée."""
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        # La barre n'affiche que le pourcentage ; l'étape en cours est donnée en infobulle.
        self.progress.setFormat("" if fraction is None else "%p %")
        self.progress.setToolTip(text or tr("Avancement du calcul"))

    def _update_splat_progress(self):
        """Avancement d'après la sortie de SPLAT! : 4 quarts par émetteur, chacun suivi
        d'environ 65 caractères de progression."""
        if self.run_params["mode"] == "p2p":
            return
        output = self.splat_output
        headers = list(self.QUARTER_RE.finditer(output))
        if not headers:
            return
        total = 4 * max(1, len(self.run_params["tx_sites"]))
        tail = output[headers[-1].end():]
        if "\n" in tail:
            done, fraction = len(headers), 0.0
        else:
            done, fraction = len(headers) - 1, min(len(tail) / self.CHARS_PER_QUARTER, 0.99)
        value = (done + fraction) / total
        site = min(len(headers) - 1, total - 1) // 4 + 1
        label = self._progress_label
        if total > 4:
            label += tr(" (émetteur {site}/{total})", site=site, total=total // 4)
        self._set_progress(min(value, 0.995), label)

    def _finalize(self, result, switch_tab=True):
        for path in self.site_tiles:             # copies de tuiles (sursol) : inutiles après le calcul
            Path(path).unlink(missing_ok=True)
            terrain.hd_alias(path).unlink(missing_ok=True)
        self.site_tiles = []
        self._update_disk_usage()                # relief éventuellement téléchargé et converti
        if self.run_params and self.run_params["map_aspect_file"] and self.run_dir:
            formats = self.run_params["map_aspect_format"].split("+")
            run_sites = sites.run_sites(self.run_dir)
            for ppm in sorted(Path(self.run_dir).glob("*.ppm")):
                for written in basemap.write_corrected_map(
                        ppm, formats, self._frame_ratio(), run_sites, self.crop_check.isChecked(),
                        self.frame_width.value(), self.coverage_blur.value() / 2):
                    self._console_write(tr("Carte aux bonnes proportions : {name}\n", name=written.name))
        elapsed = (datetime.datetime.now() - self.run_started).total_seconds()
        self._console_write(tr("\n--- {result} en {elapsed:.1f} s ---\n", result=tr(result), elapsed=elapsed))
        if result == "terminé":
            self._set_progress(1.0, tr("Terminé en {elapsed:.1f} s", elapsed=elapsed))
        else:
            self._set_progress(0.0, result[:1].upper() + result[1:])
        self.history = storage.update_history(str(self.run_dir), status=result,
                                              duration=round(elapsed, 1))
        self._refresh_history()
        self.history_table.selectRow(0)
        self.stop_action.setEnabled(False)
        self._refresh_state()
        self.statusBar().showMessage(f"SPLAT! {tr(result)} ({elapsed:.1f} s)", 8000)
        try:
            (self.run_dir / "console.log").write_text(self.console.toPlainText(), encoding="utf-8")
        except OSError:
            pass
        self.show_results(self.run_dir, switch_tab=switch_tab, load_console=False)

    # ---- Résultats --------------------------------------------------------

    def _clear_results(self):
        self.report_combo.clear()
        self.report_view.clear()
        self.image_combo.clear()
        self.image_view.clear()
        self.image_info.clear()
        self.files_list.clear()

    def show_results(self, run_dir, switch_tab=False, load_console=True):
        self._clear_results()
        if not run_dir or not Path(run_dir).is_dir():
            self.files_label.setText(tr("Dossier de résultats introuvable.") if run_dir else "")
            return
        self.run_dir = Path(run_dir)
        images, texts, others = splat.collect_outputs(run_dir)
        self.files_label.setText(tr("Dossier : {folder}", folder=run_dir))
        for path in texts:
            self.report_combo.addItem(path.name, str(path))
        for path in images:
            self.image_combo.addItem(path.name, str(path))
        for path in images + texts + others:
            size = path.stat().st_size
            item = QListWidgetItem(f"{path.name}    ({size / 1024:.1f} Ko)")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.files_list.addItem(item)
        log = self.run_dir / "console.log"
        if load_console and log.exists():
            self.console.setPlainText(log.read_text(encoding="utf-8", errors="replace"))
        params = self._run_params_for(self.run_dir)
        self.profile_request = (params, self.run_dir) if params and params["mode"] == "p2p" else None
        self.profile_data = []
        self.profile_tx.blockSignals(True)
        self.profile_tx.clear()
        for site in (params["tx_sites"] if self.profile_request else []):
            self.profile_tx.addItem(site["name"])
        self.profile_tx.blockSignals(False)
        self.profile_tx.setVisible(self.profile_tx.count() > 1)
        if switch_tab:
            self.results_tabs.setCurrentIndex(self.profile_tab if self.profile_request
                                              else 2 if images else 1 if texts else 0)
        self._update_profile()

    def _run_params_for(self, run_dir):
        """Paramètres du calcul d'un dossier, d'après l'historique (enregistrés au lancement)."""
        entry = next((e for e in self.history if Path(e.get("run_dir", "")) == Path(run_dir)), None)
        return storage.merge_defaults(entry["params"]) if entry and entry.get("params") else None

    def _update_profile(self):
        """Calcule (une fois) et affiche le profil, quand l'onglet Profil est visible."""
        if self.results_tabs.currentIndex() != self.profile_tab:
            return
        if not self.profile_request:
            self.profile_view.set_profile(None)
            self.profile_summary.setText("")
            return
        params, run_dir = self.profile_request
        if not self.profile_data:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                sdf_dir = splat.effective_sdf_dir(params)
                self.profile_data = [linkprofile.compute(params, tx, sdf_dir=sdf_dir) for tx in params["tx_sites"]]
            except (OSError, ValueError, KeyError) as exc:
                self.profile_data = []
                self.profile_summary.setText(tr("Profil impossible : {exc}", exc=exc))
                return
            finally:
                QApplication.restoreOverrideCursor()
        profile = self.profile_data[max(0, self.profile_tx.currentIndex())] if self.profile_data else None
        self.profile_view.set_profile(profile)
        self.profile_summary.setText(linkprofile.summary(profile) if profile
                                     else tr("Émetteur et récepteur confondus."))

    def _export_profile(self):
        if not self.profile_view.profile:
            return
        default = str(Path(self.run_dir or ".") / "profil_liaison.png")
        path, _ = QFileDialog.getSaveFileName(self, tr("Exporter le profil"), default, "PNG (*.png)")
        if path:
            self.profile_view.image().save(path)

    def _show_report(self, index):
        path = self.report_combo.itemData(index)
        if path:
            try:
                self.report_view.setPlainText(Path(path).read_text(encoding="latin-1"))
            except OSError as exc:
                self.report_view.setPlainText(str(exc))

    def _show_image(self, index):
        path = self.image_combo.itemData(index)
        self.full_image = self.full_ref = None
        self.current_image = self.current_ref = self.current_coverage = None
        if not path:
            self.overlay_bar.setEnabled(False)
            return
        image = QImage(path)
        if image.isNull():
            self.image_view.clear()
            self.image_info.setText(tr("Format non affichable — utilisez « Ouvrir »."))
            self.overlay_bar.setEnabled(False)
            return
        self.full_ref = basemap.georef_for(path)
        self.current_sites = sites.run_sites(Path(path).parent) if self.full_ref else []
        if self.full_ref and self.current_sites and self.hide_marks.isChecked():
            image, _removed = sites.remove_splat_marks(image, self.full_ref, self.current_sites)
        self.full_image = image
        self.overlay_bar.setEnabled(self.full_ref is not None)
        self._set_view_region()
        self._apply_overlay(refit=True)

    def _frame_ratio(self):
        return self.frame_ratio.currentData()

    def _set_view_region(self):
        """Carte entière, recadrée autour du tracé, ou cadrée au format choisi (km réels)."""
        self.current_image, self.current_ref = self.full_image, self.full_ref
        self.current_coverage = None
        self.is_cropped = False
        self.region_tag = ""
        self.frame_km = None
        if self.full_image is None or self.full_ref is None:
            return
        ratio = self._frame_ratio()
        if not ratio and not self.crop_check.isChecked():
            return
        box = basemap.site_content_box(self.full_image, self.full_ref, self.current_sites)
        if not ratio:
            cropped = basemap.crop(self.full_image, self.full_ref, box) if box else None
            if cropped:
                self.current_image, self.current_ref = cropped
                self.is_cropped = True
                self.region_tag = "cadre"
            return
        (left, top, w, h), self.frame_km = basemap.frame_box(
            self.full_ref, ratio, box, self.crop_check.isChecked(), self.frame_width.value())
        # Image directement au format exact, pixels carrés en km ; agrandie pour un fond détaillé.
        scale = max(1, min(8, 1600 // max(int(w), 1)))
        out_w = max(1, round(w * scale))
        self.current_image, self.current_ref = basemap.render_box(
            self.full_image, self.full_ref, left, top, w, h, out_w, round(out_w / ratio))
        self.is_cropped = True
        self.region_tag = f"format{left:.1f}_{top:.1f}_{w:.1f}_{h:.1f}_{out_w}"

    def _frame_changed(self, _value=None, refresh=True):
        # Un cadrage en kilomètres impose les proportions réelles.
        framed = self._frame_ratio() is not None
        self.aspect_check.setEnabled(not framed)
        self.frame_width.setEnabled(framed)
        if refresh:
            self._toggle_crop()

    def _decorate(self, image, source, relief_note):
        """Légende (d'après le contenu de la carte), échelle et nord."""
        if self.current_coverage is None:
            self.current_coverage = basemap.coverage_layer(self.current_image)
        entries = self._legend_entries(self.current_coverage, source, relief_note)
        dx, dy = self.display_ref.pixel_size()
        avoid = [((site["lon"] - self.display_ref.lon0) / dx, (site["lat"] - self.display_ref.lat0) / dy)
                 for site in self.current_sites]
        return layout.decorate(image, self.display_ref, entries, self.current_coverage,
                               self.legend_check.isChecked(), self.scale_check.isChecked(), avoid,
                               self.legend_scale.currentData())

    def _legend_entries(self, coverage, source, relief_note, online=False):
        site_icons = []
        if self.sites_box.isChecked():
            names = sites.icon_names(self.current_sites, self.tx_icon.currentData(), self.rx_icon.currentData())
            site_icons = [(sites.icon(name, 64), site) for name, site in zip(names, self.current_sites)
                          if name and sites.icon(name, 64) is not None]
        checked_layers = [(self._layer_color(n), self.geo_layers[n]["label"]) for n in self._checked_layers()]
        notes = []
        if source and not online:
            notes.append(tr("Fond : ") + tr(basemap.SOURCES[source]["attribution"]))
        if relief_note:
            notes.append(relief_note)
        if checked_layers:
            notes.append(tr("Limites : fichiers GeoJSON"))
        if self.frame_km and not online:
            notes.append(tr("Cadre de {w:.1f} × {h:.1f} km", w=self.frame_km[0], h=self.frame_km[1]))
        if not online:
            notes.append(tr("Calcul : SPLAT! — carte : SPLAT!Gui"))
        return layout.legend_entries(Path(self.image_combo.currentData()).parent, coverage,
                                     self.current_sites, site_icons, checked_layers, notes,
                                     self.coverage_check.isChecked())

    # ---- Carte en ligne ----------------------------------------------------

    def _display_mode_changed(self, _index=None, refresh=True):
        online = self.display_mode.currentData() == "online"
        self.view_stack.setCurrentIndex(1 if online else 0)
        for widget in (self.crop_check, self.aspect_check, self.export_button):
            widget.setVisible(not online)
        for widget in (self.recenter_button, self.browser_button):
            widget.setVisible(online)
        if online:
            self.image_info.setText(tr("Carte en ligne — glisser : déplacer, molette : zoomer"))
        if refresh:
            self.live_key = None
            self._apply_overlay(refit=True)

    def _live_coverage(self):
        key = (self.image_combo.currentData(), self.hide_marks.isChecked(), self.coverage_blur.value())
        if self.live_coverage is None or self.live_coverage[0] != key:
            self.live_coverage = (key, basemap.blur(basemap.coverage_layer(self.full_image),
                                                    self._blur_sigma(self.full_ref)))
        return self.live_coverage[1]

    def _update_live_map(self, refit=False):
        live = self.live_map
        if self.full_image is None or self.full_ref is None:
            live.set_coverage(None, None, 0, False)
            live.markers, live.legend_entries = [], []
            self.overlay_status.setText(tr("Carte en ligne : sélectionnez une carte SPLAT! calée (.ppm)."))
            return
        coverage = self._live_coverage()
        relief = self.relief_source.currentData()
        live.set_layers(self.basemap_combo.currentData(), self.basemap_opacity.value() / 100,
                        relief, self.relief_opacity.value() / 100,
                        (self.sun_azimuth.value(), self.sun_altitude.value(), self.exaggeration.value()))
        live.set_coverage(coverage, self.full_ref, self.coverage_opacity.value() / 100,
                          self.coverage_check.isChecked())
        key = (self.image_combo.currentData(), self.full_ref.width, self.full_ref.height)
        if refit or key != self.live_key:
            self.live_key = key
            live.fit(self._content_ref() or self.full_ref)
        live.markers = []
        if self.sites_box.isChecked():
            names = sites.icon_names(self.current_sites, self.tx_icon.currentData(), self.rx_icon.currentData())
            live.markers = [(site["lat"], site["lon"], sites.icon(name, 64) if name else None, site["name"])
                            for name, site in zip(names, self.current_sites)]
        relief_note = {"ign_estompage": tr("Relief : estompage IGN"), "srtm": tr("Relief : SRTM (NASA)")}.get(relief)
        live.set_vectors(self._live_vectors(), self.layer_opacity.value() / 100)
        live.legend_entries = self._legend_entries(coverage, None, relief_note, online=True)
        live.show_legend, live.show_scale = self.legend_check.isChecked(), self.scale_check.isChecked()
        live.legend_scale = self.legend_scale.currentData()
        live.update()
        if relief == "srtm" and live.zoom < livemap.SRTM_MIN_ZOOM:
            self.overlay_status.setText(tr("Ombrage SRTM affiché à partir du zoom {zoom}", zoom=livemap.SRTM_MIN_ZOOM))

    def _live_vectors(self):
        """Calques GeoJSON cochés pour la carte en ligne (niveau de détail selon le zoom)."""
        live = self.live_map
        lat, _lon = live.center()
        pixel_m = 156543.03 * math.cos(math.radians(lat)) / (2 ** live.zoom)
        vectors, pending = [], []
        for name in self._checked_layers():
            layer = self.geo_layers[name]
            path = layers.choose_level(layer, pixel_m)
            features = self.layer_features.get(path)
            if features is None:
                pending.append(layer["label"])
                self._run_task(("geojson", path), lambda p=path: layers.load(p),
                               lambda result, p=path: self.layer_features.__setitem__(p, result))
                continue
            vectors.append({"key": str(path), "features": features, "color": self._layer_color(name),
                            "label": layer["label"],
                            "width": layer["width"], "font": layer["font"],
                            "labels": self.layer_labels.isChecked()})
        if pending:
            self.overlay_status.setText(tr("Chargement des calques : {names}…", names=", ".join(pending)))
        return vectors

    def _layer_label(self, path):
        for layer in self.geo_layers.values():
            if path in {str(p) for p in layer["levels"].values()}:
                return layer["label"]
        return Path(path).stem

    def _content_ref(self):
        """Emprise géographique de la zone utile (couverture, sites) de la carte entière."""
        box = basemap.site_content_box(self.full_image, self.full_ref, self.current_sites)
        if not box:
            return None
        dx, dy = self.full_ref.pixel_size()
        pad = max(box[2] - box[0], box[3] - box[1]) * 0.08 + 4
        x0, y0, x1, y1 = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
        return basemap.GeoRef(self.full_ref.lon0 + x0 * dx, self.full_ref.lat0 + y0 * dy,
                              self.full_ref.lon0 + x1 * dx, self.full_ref.lat0 + y1 * dy,
                              max(2, int(x1 - x0)), max(2, int(y1 - y0)))

    def _open_in_browser(self):
        if self.full_image is None or self.full_ref is None:
            return
        run_dir = Path(self.image_combo.currentData()).parent
        names = sites.icon_names(self.current_sites, self.tx_icon.currentData(), self.rx_icon.currentData()) \
            if self.sites_box.isChecked() else [None] * len(self.current_sites)
        icon_files = [sites.ICONS_DIR / n if n else None for n in names]
        coverage = self._live_coverage() if self.coverage_check.isChecked() else QImage(1, 1, QImage.Format.Format_ARGB32)
        if not self.coverage_check.isChecked():
            coverage.fill(QColor(0, 0, 0, 0))
        entries = self._legend_entries(coverage, None, None, online=True) if self.legend_check.isChecked() else []
        base = self.basemap_combo.currentData() or "osm"
        ref, relief = self.full_ref, self.relief_source.currentData()
        # Zone de la page : la zone utile élargie d'un tiers, sans dépasser la carte SPLAT!.
        content = self._content_ref() or ref
        c_west, c_south, c_east, c_north = content.bounds()
        f_west, f_south, f_east, f_north = ref.bounds()
        pad_x, pad_y = (c_east - c_west) / 6, (c_north - c_south) / 6
        west, east = max(f_west, c_west - pad_x), min(f_east, c_east + pad_x)
        south, north = max(f_south, c_south - pad_y), min(f_north, c_north + pad_y)
        view_ref = basemap.GeoRef(west, north, east, south, 1400, max(2, round(1400 * (north - south) / (east - west))))
        # Calques : niveau de détail adapté à la page (≈ 1400 px de large), chargé pendant la préparation.
        pixel_m = (east - west) * 111320 * math.cos(math.radians((north + south) / 2)) / 1400
        layer_specs = [(self.geo_layers[n]["label"], self._layer_color(n), self.geo_layers[n]["width"],
                        layers.choose_level(self.geo_layers[n], pixel_m)) for n in self._checked_layers()]
        shade = (self.sun_azimuth.value(), self.sun_altitude.value(), self.exaggeration.value())
        url = self.settings.get("srtm_url") or terrain.DEFAULT_URL
        opacity, title = self.coverage_opacity.value() / 100, f"SPLAT!Gui — {run_dir.name}"
        signals = self.live_map.statusText

        def build():
            # Relief SRTM de la page : tuiles téléchargées si nécessaire (calcul en arrière-plan).
            srtm = livemap.srtm_relief(view_ref, shade, url, signals.emit)
            vectors = [(label, color, width, layers.load(path)) for label, color, width, path in layer_specs]
            return livemap.export_html(run_dir / "web", coverage, ref, self.current_sites, icon_files, entries,
                                       base, relief, opacity, title, vectors, srtm, view_ref)

        def done(page):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(page)))
            self.overlay_status.setText(tr("Page web écrite : {page}", page=page))
            self.browser_button.setEnabled(True)

        self.browser_button.setEnabled(False)
        self.overlay_status.setText(tr("Préparation de la page web (relief SRTM, couverture, calques)…"))
        self._run_task(("web", str(run_dir)), build, done, lambda _e: self.browser_button.setEnabled(True))

    def _toggle_crop(self):
        self._set_view_region()
        self._apply_overlay(refit=True)

    def _overlay_key(self):
        return self.image_combo.currentData(), self.basemap_combo.currentData(), self.region_tag

    # ---- Fonds de carte -----------------------------------------------------

    def _build_overlay_bar(self):
        overlay = self.settings.setdefault("overlay", {})
        self.overlay_bar = QWidget()
        rows = QVBoxLayout(self.overlay_bar)
        rows.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row2 = QHBoxLayout()
        rows.addLayout(row)
        rows.addLayout(row2)
        row.addWidget(QLabel(tr("Affichage :")))
        self.display_mode = QComboBox()
        self.display_mode.addItem(tr("Image composée"), "image")
        self.display_mode.addItem(tr("Carte en ligne"), "online")
        self.display_mode.setToolTip(tr("Image composée : carte figée, exportable (fonctionnement habituel).\n"
                                     "Carte en ligne : carte OSM / IGN interactive chargée en ligne, "
                                     "relief en arrière-plan, couverture SPLAT! superposée."))
        self.display_mode.setCurrentIndex(max(0, self.display_mode.findData(overlay.get("display_mode", "image"))))
        row.addWidget(self.display_mode)
        row.addWidget(QLabel(tr("  Fond de carte :")))
        self.basemap_combo = QComboBox()
        self.basemap_combo.addItem(tr("Aucun (relief seul)"), "")
        for key, source in basemap.basemap_sources().items():
            self.basemap_combo.addItem(tr(source["label"]), key)
        self.basemap_combo.setCurrentIndex(max(0, self.basemap_combo.findData(overlay.get("source", ""))))
        row.addWidget(self.basemap_combo)
        row.addWidget(QLabel(tr("  Opacité du fond :")))
        self.basemap_opacity = QSlider(Qt.Orientation.Horizontal)
        self.basemap_opacity.setRange(0, 100)
        self.basemap_opacity.setMinimumWidth(90)
        self.basemap_opacity.setValue(int(overlay.get("basemap_opacity", 75)))
        self.basemap_opacity.setToolTip(tr("0 % : relief SPLAT! seul — 100 % : fond de carte seul"))
        graduate(self.basemap_opacity, 10)
        row.addWidget(self.basemap_opacity, 1)
        row.addWidget(SliderValue(self.basemap_opacity))
        self.coverage_check = QCheckBox(tr("Couverture radio :"))
        self.coverage_check.setToolTip(tr("Afficher la couverture calculée par SPLAT! "
                                       "(au-dessus de toutes les autres couches)"))
        self.coverage_check.setChecked(bool(overlay.get("coverage_visible", True)))
        row.addWidget(self.coverage_check)
        self.coverage_opacity = QSlider(Qt.Orientation.Horizontal)
        self.coverage_opacity.setRange(0, 100)
        self.coverage_opacity.setMinimumWidth(90)
        self.coverage_opacity.setValue(int(overlay.get("coverage_opacity", 80)))
        self.coverage_opacity.setToolTip(tr("Opacité de la couverture radio (0 % : transparente, 100 % : opaque)"))
        graduate(self.coverage_opacity, 10)
        row.addWidget(self.coverage_opacity, 1)
        row.addWidget(SliderValue(self.coverage_opacity))
        self.crop_check = QCheckBox(tr("Cadrer sur le tracé"))
        self.crop_check.setToolTip(tr("Recadre sur le trajet point à point ou la zone couverte et "
                                   "charge un fond de carte plus détaillé"))
        self.crop_check.setChecked(bool(overlay.get("crop", True)))
        row2.addWidget(self.crop_check)
        self.aspect_check = QCheckBox(tr("Proportions réelles (corriger le rapport hauteur/largeur)"))
        self.aspect_check.setToolTip(
            tr("SPLAT! trace un degré de longitude aussi large qu'un degré de latitude : la carte est "
            "étirée horizontalement de 1/cos(latitude). Cette option la comprime pour que les "
            "distances soient les mêmes dans les deux directions."))
        self.aspect_check.setChecked(bool(overlay.get("aspect", True)))
        row2.addWidget(self.aspect_check)
        self.relief_label = QLabel(tr("Relief :"))
        row2.addWidget(self.relief_label)
        self.relief_source = QComboBox()
        for key, label in livemap.RELIEF_SOURCES.items():
            self.relief_source.addItem(tr(label), key)
        default_relief = "srtm" if overlay.get("shade", True) else ""
        self.relief_source.setCurrentIndex(max(0, self.relief_source.findData(overlay.get("relief_source",
                                                                                         default_relief))))
        self.relief_source.setToolTip(tr("Relief en arrière-plan de la carte en ligne. L'ombrage SRTM utilise "
                                      "les réglages de soleil et d'accentuation du panneau de droite."))
        row2.addWidget(self.relief_source)
        self.relief_opacity = QSlider(Qt.Orientation.Horizontal)
        self.relief_opacity.setRange(0, 100)
        self.relief_opacity.setMinimumWidth(90)
        self.relief_opacity.setValue(int(overlay.get("relief_opacity", 60)))
        self.relief_opacity.setToolTip(tr("Intensité du relief"))
        graduate(self.relief_opacity, 10)
        row2.addWidget(self.relief_opacity, 1)
        row2.addWidget(SliderValue(self.relief_opacity))
        self.blur_label = QLabel(tr("Flou couverture :"))
        row2.addWidget(self.blur_label)
        self.coverage_blur = QSlider(Qt.Orientation.Horizontal)
        self.coverage_blur.setRange(0, 20)
        self.coverage_blur.setMinimumWidth(80)
        self.coverage_blur.setValue(int(overlay.get("coverage_blur", 0)))
        self.coverage_blur.setToolTip(tr("Adoucit les contours de la couverture (0 : aucun flou ; "
                                      "unité : pixel de la carte SPLAT!, ≈ 90 m en standard)"))
        graduate(self.coverage_blur, 2)
        row2.addWidget(self.coverage_blur, 1)
        row2.addWidget(SliderValue(self.coverage_blur, "{:g} px", scale=2))
        self.coverage_blur.valueChanged.connect(lambda v: (self.coverage_blur.setToolTip(
            tr("Flou de la couverture : {v:g} pixel(s) SPLAT!", v=v / 2)), self._overlay_timer.start()))
        self.recenter_button = QPushButton(tr("Recentrer"))
        self.recenter_button.clicked.connect(lambda: self._update_live_map(refit=True))
        row2.addWidget(self.recenter_button)
        self.browser_button = QPushButton(tr("Ouvrir dans le navigateur"))
        self.browser_button.setToolTip(tr("Page web interactive (Leaflet) : fonds OSM / IGN en ligne, "
                                       "estompage du relief, couverture, sites et légende"))
        self.browser_button.clicked.connect(self._open_in_browser)
        row2.addWidget(self.browser_button)
        row2.addStretch()
        self.export_button = QPushButton(tr("Exporter l'image…"))
        self.export_button.clicked.connect(self._export_overlay)
        row2.addWidget(self.export_button)
        self.relief_source.currentIndexChanged.connect(lambda _i: self._overlay_timer.start())
        self.relief_opacity.valueChanged.connect(lambda _v: self._overlay_timer.start())
        self.display_mode.currentIndexChanged.connect(self._display_mode_changed)
        self.live_key = None
        self.live_coverage = None
        self.overlay_bar.setEnabled(False)

        self.full_image = None
        self.full_ref = None
        self.current_image = None
        self.current_ref = None
        self.current_coverage = None
        self.current_composite = None
        self.is_cropped = False
        self.display_ref = None
        self.basemap_cache = {}
        self.basemap_workers = {}
        self._overlay_timer = QTimer(self, singleShot=True, interval=60)
        self._overlay_timer.timeout.connect(lambda: self._apply_overlay(refit=False))
        self.basemap_combo.currentIndexChanged.connect(lambda _i: self._apply_overlay(refit=False))
        self.basemap_opacity.valueChanged.connect(lambda _v: self._overlay_timer.start())
        self.coverage_opacity.valueChanged.connect(lambda _v: self._overlay_timer.start())
        self.coverage_check.toggled.connect(self.coverage_opacity.setEnabled)
        self.coverage_check.toggled.connect(lambda _c: self._overlay_timer.start())
        self.coverage_opacity.setEnabled(self.coverage_check.isChecked())
        self.crop_check.toggled.connect(lambda _c: self._toggle_crop())
        self.aspect_check.toggled.connect(lambda _c: self._apply_overlay(refit=True))
        return self.overlay_bar

    def _overlay_settings_changed(self):
        self.settings["overlay"] = {
            "source": self.basemap_combo.currentData(),
            "basemap_opacity": self.basemap_opacity.value(),
            "coverage_opacity": self.coverage_opacity.value(),
            "coverage_visible": self.coverage_check.isChecked(),
            "display_mode": self.display_mode.currentData(),
            "relief_source": self.relief_source.currentData(),
            "relief_opacity": self.relief_opacity.value(),
            "coverage_blur": self.coverage_blur.value(),
            "crop": self.crop_check.isChecked(),
            "aspect": self.aspect_check.isChecked(),
            "layers": self._checked_layers(),
            "layer_colors": dict(self.layer_colors),
            "labels": self.layer_labels.isChecked(),
            "layer_opacity": self.layer_opacity.value(),
            "sun_azimuth": self.sun_azimuth.value(),
            "sun_altitude": self.sun_altitude.value(),
            "exaggeration": self.exaggeration.value(),
            "site_icons": self.sites_box.isChecked(),
            "tx_icon": self.tx_icon.currentData(),
            "rx_icon": self.rx_icon.currentData(),
            "icon_size": self.icon_size.value(),
            "site_names": self.site_names.isChecked(),
            "hide_marks": self.hide_marks.isChecked(),
            "frame_ratio": self._frame_ratio() or 0,
            "frame_width_km": self.frame_width.value(),
            "legend": self.legend_check.isChecked(),
            "legend_scale": self.legend_scale.currentData(),
            "scale": self.scale_check.isChecked(),
        }

    def _basemap_cache_file(self, source):
        path = Path(self.image_combo.currentData())
        return path.parent / "fonds" / f"{path.stem}_{source}{'_' + self.region_tag if self.region_tag else ''}.png"

    # ---- Calques GeoJSON et ombrage -------------------------------------------

    def _build_layers_panel(self):
        overlay = self.settings.setdefault("overlay", {})
        self.layers_panel = QWidget()
        self.layers_panel.setFixedWidth(252)
        panel = QVBoxLayout(self.layers_panel)
        panel.setContentsMargins(0, 0, 0, 0)

        vectors = QGroupBox(tr("Calques GeoJSON"))
        vl = QVBoxLayout(vectors)
        self.geo_layers = layers.discover()
        self.layer_list = QListWidget()
        checked = set(overlay.get("layers", []))
        self.layer_colors = {k: v for k, v in overlay.get("layer_colors", {}).items()
                             if k in self.geo_layers and QColor(v).isValid()}
        for name, layer in self.geo_layers.items():
            item = QListWidgetItem(tr(layer["label"]))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
            item.setToolTip(tr("Double-clic : choisir la couleur"))
            self.layer_list.addItem(item)
            self._update_layer_item(item)
        if not self.geo_layers:
            self.layer_list.addItem(tr("(aucun fichier dans {folder}/)", folder=layers.GEOJSON_DIR.name))
        self.layer_list.setMaximumHeight(110)
        self.layer_list.itemDoubleClicked.connect(self._choose_layer_color)
        vl.addWidget(self.layer_list)
        color_row = QHBoxLayout()
        color_button = QPushButton(tr("Couleur…"))
        color_button.setToolTip(tr("Couleur du calque sélectionné"))
        color_button.clicked.connect(lambda: self._choose_layer_color(self.layer_list.currentItem()))
        reset_button = QPushButton(tr("Par défaut"))
        reset_button.setToolTip(tr("Rétablir la couleur d'origine du calque sélectionné"))
        reset_button.clicked.connect(self._reset_layer_color)
        color_row.addWidget(color_button)
        color_row.addWidget(reset_button)
        vl.addLayout(color_row)
        self.layer_labels = QCheckBox(tr("Afficher les noms"))
        self.layer_labels.setChecked(bool(overlay.get("labels", True)))
        vl.addWidget(self.layer_labels)
        self.layer_opacity = self._labeled_slider(vl, tr("Opacité"), 0, 100,
                                                  overlay.get("layer_opacity", 90), "{} %", 10)
        panel.addWidget(vectors)

        self.shade_box = QGroupBox(tr("Ombrage SRTM : soleil et relief"))
        sl = QVBoxLayout(self.shade_box)
        self.sun_azimuth = self._labeled_slider(sl, tr("Azimut du soleil"), 0, 359,
                                                overlay.get("sun_azimuth", 315), "{}°", 45, 15)
        self.sun_altitude = self._labeled_slider(sl, tr("Hauteur du soleil"), 5, 85,
                                                 overlay.get("sun_altitude", 45), "{}°", 10, 5)
        self.exaggeration = self._labeled_slider(sl, tr("Accentuation du relief"), 1, 30,
                                                 overlay.get("exaggeration", 3), tr("× {}"), 5)
        note = QLabel(tr("Relief « Ombrage SRTM » (liste Relief, en haut) ; tuiles SRTM téléchargées au besoin."))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        sl.addWidget(note)
        panel.addWidget(self.shade_box)

        self.sites_box = QGroupBox(tr("Icônes des sites"))
        self.sites_box.setCheckable(True)
        self.sites_box.setChecked(bool(overlay.get("site_icons", True)))
        sf = QFormLayout(self.sites_box)
        sites.ensure_variants()
        icon_names = sites.list_icons()
        self.tx_icon = QComboBox()
        self.tx_icon.addItem(tr("Une couleur par émetteur"), "auto")
        self.rx_icon = QComboBox()
        for name in icon_names:
            for combo in (self.tx_icon, self.rx_icon):
                combo.addItem(QIcon(str(sites.ICONS_DIR / name)), Path(name).stem, name)
        self.tx_icon.setCurrentIndex(max(0, self.tx_icon.findData(sites.current_name(overlay.get("tx_icon", "auto")))))
        default_rx = sites.current_name(overlay.get("rx_icon", "antenne.png"))
        self.rx_icon.setCurrentIndex(max(0, self.rx_icon.findData(default_rx)))
        sf.addRow(tr("Émetteurs"), self.tx_icon)
        sf.addRow(tr("Récepteur"), self.rx_icon)
        size_box = QVBoxLayout()
        self.icon_size = self._labeled_slider(size_box, tr("Taille"), 16, 160, overlay.get("icon_size", 48), "{} px", 16, 8)
        sf.addRow(size_box)
        self.site_names = QCheckBox(tr("Noms des sites"))
        self.site_names.setChecked(bool(overlay.get("site_names", True)))
        sf.addRow(self.site_names)
        self.hide_marks = QCheckBox(tr("Masquer les repères de SPLAT!"))
        self.hide_marks.setToolTip(tr("Efface le carré et le nom rouges dessinés par SPLAT! "
                                   "(sans effet sur les cartes où le rouge est une couleur de contour)"))
        self.hide_marks.setChecked(bool(overlay.get("hide_marks", True)))
        sf.addRow(self.hide_marks)
        panel.addWidget(self.sites_box)

        layout_box = QGroupBox(tr("Mise en page"))
        lf = QFormLayout(layout_box)
        self.frame_ratio = QComboBox()
        for label, value in basemap.FRAME_RATIOS:
            self.frame_ratio.addItem(tr(label), value)
        if "frame_ratio" in overlay:
            stored = overlay["frame_ratio"] or None
        else:   # anciens réglages : case « carte carrée »
            stored = 1.0 if overlay.get("square") else basemap.DEFAULT_FRAME_RATIO
        index = next((i for i, (_l, v) in enumerate(basemap.FRAME_RATIOS)
                      if (v is None and stored is None) or (v and stored and abs(v - stored) < 1e-6)), 0)
        self.frame_ratio.setCurrentIndex(index)
        self.frame_ratio.setToolTip(tr("Rapport largeur/hauteur de la carte de sortie, en kilomètres réels. "
                                    "« Libre » conserve l'emprise calculée par SPLAT! (tuiles de 1°)."))
        lf.addRow(tr("Format"), self.frame_ratio)
        self.frame_width = QDoubleSpinBox()
        self.frame_width.setRange(0, 4000)
        self.frame_width.setDecimals(1)
        self.frame_width.setSuffix(" km")
        self.frame_width.setSpecialValueText(tr("automatique"))
        self.frame_width.setKeyboardTracking(False)
        self.frame_width.setToolTip(tr("Largeur du cadre ; « automatique » : la zone utile + 15 % "
                                    "(si « Cadrer sur le tracé ») ou le plus grand cadre possible"))
        self.frame_width.setValue(float(overlay.get("frame_width_km", overlay.get("square_km", 0))))
        lf.addRow(tr("Largeur"), self.frame_width)
        self.legend_check = QCheckBox(tr("Légende"))
        self.legend_check.setChecked(bool(overlay.get("legend", True)))
        self.legend_scale = QComboBox()
        for value in layout.LEGEND_SCALES:
            self.legend_scale.addItem(f"× {value:g}".replace(".", ","), value)
        stored_scale = float(overlay.get("legend_scale", layout.DEFAULT_LEGEND_SCALE))
        self.legend_scale.setCurrentIndex(max(0, self.legend_scale.findData(stored_scale)))
        self.legend_scale.setToolTip(tr("Taille de la légende (texte, pastilles et cadre)"))
        legend_row = QHBoxLayout()
        legend_row.addWidget(self.legend_check)
        legend_row.addStretch()
        legend_row.addWidget(QLabel(tr("Taille")))
        legend_row.addWidget(self.legend_scale)
        lf.addRow(legend_row)
        self.scale_check = QCheckBox(tr("Échelle et nord"))
        self.scale_check.setChecked(bool(overlay.get("scale", True)))
        lf.addRow(self.scale_check)
        panel.addWidget(layout_box)
        panel.addStretch()

        self.layer_features = {}
        self.elevation_cache = {}
        self.shade_cache = {}
        self.vector_cache = None
        self.relief_cache = None
        self.coverage_display_cache = None
        self.tasks = {}
        self.layer_list.itemChanged.connect(lambda _i: self._overlay_timer.start())
        self.layer_labels.toggled.connect(lambda _c: self._overlay_timer.start())
        for slider in (self.layer_opacity, self.sun_azimuth, self.sun_altitude, self.exaggeration):
            slider.valueChanged.connect(lambda _v: self._overlay_timer.start())
        for widget in (self.sites_box, self.site_names):
            widget.toggled.connect(lambda _c: self._overlay_timer.start())
        for combo in (self.tx_icon, self.rx_icon):
            combo.currentIndexChanged.connect(lambda _i: self._overlay_timer.start())
        self.icon_size.valueChanged.connect(lambda _v: self._overlay_timer.start())
        self.hide_marks.toggled.connect(lambda _c: self._show_image(self.image_combo.currentIndex()))
        self.frame_ratio.currentIndexChanged.connect(self._frame_changed)
        self.frame_width.valueChanged.connect(lambda _v: self._frame_ratio() and self._toggle_crop())
        self.legend_check.toggled.connect(self.legend_scale.setEnabled)
        self.legend_scale.setEnabled(self.legend_check.isChecked())
        self.legend_scale.currentIndexChanged.connect(lambda _i: self._overlay_timer.start())
        for widget in (self.legend_check, self.scale_check):
            widget.toggled.connect(lambda _c: self._overlay_timer.start())
        self.frame_km = None
        self.region_tag = ""
        self._frame_changed(refresh=False)
        self.current_sites = []
        self.layers_panel.setEnabled(False)
        scroll = QScrollArea()
        scroll.setWidget(self.layers_panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(270)
        return scroll

    def _labeled_slider(self, layout, title, minimum, maximum, value, fmt, tick, page=None):
        label = QLabel()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        graduate(slider, tick, page)
        slider.valueChanged.connect(lambda v: label.setText(f"{title} : {fmt.format(v)}"))
        slider.setValue(int(value))
        label.setText(f"{title} : {fmt.format(slider.value())}")
        layout.addWidget(label)
        layout.addWidget(slider)
        return slider

    def _layer_color(self, name):
        return self.layer_colors.get(name, self.geo_layers[name]["color"])

    def _update_layer_item(self, item):
        name = item.data(Qt.ItemDataRole.UserRole)
        color = QColor(self._layer_color(name))
        swatch = QPixmap(14, 14)
        swatch.fill(color)
        painter = QPainter(swatch)
        painter.setPen(QColor(0, 0, 0, 120))
        painter.drawRect(0, 0, 13, 13)
        painter.end()
        item.setIcon(QIcon(swatch))
        # Nom du calque dans la couleur de texte du thème ; sa couleur n'est indiquée que par la pastille.
        item.setData(Qt.ItemDataRole.ForegroundRole, None)

    def _choose_layer_color(self, item):
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not name:
            QMessageBox.information(self, tr("Couleur du calque"), tr("Sélectionnez d'abord un calque."))
            return
        color = QColorDialog.getColor(QColor(self._layer_color(name)), self,
                                      tr("Couleur du calque « {name} »", name=item.text()))
        if color.isValid():
            self.layer_colors[name] = color.name()
            self._update_layer_item(item)
            self._overlay_timer.start()

    def _reset_layer_color(self):
        item = self.layer_list.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        if name and self.layer_colors.pop(name, None):
            self._update_layer_item(item)
            self._overlay_timer.start()

    def _checked_layers(self):
        names = []
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            name = item.data(Qt.ItemDataRole.UserRole)
            if name and item.checkState() == Qt.CheckState.Checked:
                names.append(name)
        return names

    def _run_task(self, key, function, on_result, on_error=None):
        """Exécute `function` en arrière-plan (une seule fois par clé) puis réapplique la vue."""
        if key in self.tasks:
            return
        worker = TaskWorker(key, function, self)
        self.tasks[key] = worker

        def finished(w, result, error):
            w.wait()
            w.deleteLater()
            self.tasks.pop(w.key, None)
            if error:
                self.overlay_status.setText(tr("Erreur : {error}", error=error))
                if on_error:
                    on_error(error)
                return
            on_result(result)
            self._overlay_timer.start()

        worker.done.connect(finished)
        worker.start()

    def _elevation(self):
        """(altitudes, tuiles trouvées, tuiles nécessaires) pour la vue courante, ou None
        pendant le calcul (lancé en arrière-plan)."""
        if self.current_ref is None:
            return None
        key = (self.image_combo.currentData(), self.region_tag)
        grid = self.elevation_cache.get(key)
        if grid is None:
            ref = self.current_ref
            url = self.settings.get("srtm_url") or terrain.DEFAULT_URL
            status = self.live_map.statusText

            def compute():
                hillshade.ensure_srtm(hillshade.tiles_for_bounds(*ref.bounds()), url, status.emit)
                return hillshade.elevation_grid(ref)

            self._run_task(("elevation",) + key, compute,
                           lambda result: self.elevation_cache.__setitem__(key, result))
        return grid

    def _relief_layer(self):
        """Relief SPLAT! dont les zones couvertes sont reconstituées (voir basemap.relief_layer)."""
        grid = self._elevation()
        elevation = grid[0] if grid and grid[1] else None
        key = (self.image_combo.currentData(), self.region_tag, elevation is not None,
               self.hide_marks.isChecked())
        if self.relief_cache and self.relief_cache[0] == key:
            return self.relief_cache[1]
        image = basemap.relief_layer(self.current_image, self.current_coverage, elevation)
        self.relief_cache = (key, image)
        return image

    def _blur_sigma(self, ref):
        """Écart type du flou en pixels de `ref` (curseur : demi-pixels de la carte SPLAT!)."""
        value = self.coverage_blur.value() / 2
        if not value or self.full_ref is None or ref is None:
            return 0.0
        return value * abs(self.full_ref.pixel_size()[0]) / abs(ref.pixel_size()[0])

    def _display_coverage(self, size):
        """Calque de couverture transformé comme la carte affichée (proportions, format, flou)."""
        key = (self.image_combo.currentData(), self.region_tag, size.width(), size.height(),
               self.coverage_blur.value())
        if self.coverage_display_cache and self.coverage_display_cache[0] == key:
            return self.coverage_display_cache[1]
        coverage = self.current_coverage
        if coverage.size() != size:
            coverage = coverage.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
        coverage = basemap.blur(coverage, self._blur_sigma(self.display_ref))
        self.coverage_display_cache = (key, coverage)
        return coverage

    def _srtm_estompage(self):
        """Ombrage SRTM de la vue courante (ombre noire semi-transparente), ou None."""
        if self.current_ref is None:
            return None
        key = (self.image_combo.currentData(), self.region_tag)
        grid = self._elevation()
        if grid is None:
            self.overlay_status.setText(tr("Calcul de l'ombrage du relief…"))
            return None
        values, found, needed = grid
        if not found:
            self.overlay_status.setText(tr("Ombrage : aucune tuile SRTM disponible pour cette zone."))
            return None
        params = (self.sun_azimuth.value(), self.sun_altitude.value(), self.exaggeration.value())
        cached = self.shade_cache.get(key)
        if cached and cached[0] == params:
            return cached[1]
        light = hillshade.shade(values, self.current_ref, *params)
        alpha = 255 - livemap.estompage(light, params[1])
        h, w = alpha.shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[..., 3] = alpha
        image = QImage(rgba.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        self.shade_cache = {key: (params, image)}
        return image

    def _draw_vector_layers(self, image, ref):
        """Dessine les calques GeoJSON cochés ; charge en arrière-plan ceux qui manquent."""
        names = self._checked_layers()
        if not names or ref is None:
            return image
        pixel_m = abs(ref.pixel_size()[1]) * layers.METERS_PER_DEG
        cache_key = (self.image_combo.currentData(), self.region_tag, image.width(), image.height(),
                     tuple((n, self._layer_color(n)) for n in names), self.layer_labels.isChecked())
        if self.vector_cache and self.vector_cache[0] == cache_key:
            overlay, pending = self.vector_cache[1], []
        else:
            overlay, pending = self._render_vector_layers(image, ref, names, pixel_m)
            if not pending:
                self.vector_cache = (cache_key, overlay)
        if pending:
            self.overlay_status.setText(tr("Chargement des calques : {names}…", names=", ".join(pending)))
        out = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(out)
        painter.setOpacity(self.layer_opacity.value() / 100)
        painter.drawImage(0, 0, overlay)
        painter.end()
        return out

    def _render_vector_layers(self, image, ref, names, pixel_m):
        overlay = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
        overlay.fill(QColor(0, 0, 0, 0))
        pending = []
        for name in names:
            layer = self.geo_layers[name]
            path = layers.choose_level(layer, pixel_m)
            features = self.layer_features.get(path)
            if features is None:
                pending.append(layer["label"])
                self._run_task(("geojson", path), lambda p=path: layers.load(p),
                               lambda result, p=path: self.layer_features.__setitem__(p, result))
                continue
            layers.draw(overlay, ref, features, self._layer_color(name), layer["width"], layer["font"],
                        self.layer_labels.isChecked())
        return overlay, pending

    def _apply_overlay(self, refit=False):
        self._overlay_settings_changed()
        if self.display_mode.currentData() == "online":
            self.overlay_status.clear()
            self.layers_panel.setEnabled(self.full_ref is not None)
            self._update_live_map(refit)
            return
        if self.current_image is None:
            return
        source = self.basemap_combo.currentData()
        image = self.current_image
        self.overlay_status.clear()
        self.layers_panel.setEnabled(self.current_ref is not None)
        base = self._raster_layer(source)
        notes = []
        relief = self.relief_source.currentData()
        shade = self._srtm_estompage() if relief == "srtm" else None
        ign_relief = self._raster_layer("ign_estompage") if relief == "ign_estompage" else None
        if self.current_coverage is None:
            self.current_coverage = basemap.coverage_layer(image)
        # Ordre : relief SPLAT! (sans la couverture) → fond de carte → ombrage → calques GeoJSON
        # → couverture radio (au-dessus de toutes les couches) → icônes, légende, échelle.
        image = self._relief_layer().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        if base is not None:
            painter.setOpacity(self.basemap_opacity.value() / 100)
            painter.drawImage(0, 0, base)
            notes.append(tr(basemap.SOURCES[source]["attribution"]))
        if shade is not None:              # ombre noire semi-transparente (équivaut à un produit)
            painter.setOpacity(self.relief_opacity.value() / 100)
            painter.drawImage(0, 0, shade)
        if ign_relief is not None:         # estompage IGN (gris) : mélange « produit »
            painter.setOpacity(self.relief_opacity.value() / 100)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            painter.drawImage(0, 0, ign_relief)
            notes.append(tr(basemap.SOURCES["ign_estompage"]["attribution"]))
        painter.end()
        self.display_ref = self.current_ref
        if (self.aspect_check.isChecked() or self._frame_ratio()) and self.current_ref is not None:
            image, self.display_ref = basemap.correct_aspect(image, self.current_ref)
            if self._frame_ratio():
                image, self.display_ref = basemap.force_ratio(image, self.display_ref, self._frame_ratio())
            notes.insert(0, tr("largeur × {factor:.2f}", factor=basemap.aspect_factor(self.current_ref)))
        # Calques vectoriels après la correction : traits et textes ne sont pas déformés.
        image = self._draw_vector_layers(image, self.display_ref)
        if self.coverage_check.isChecked():
            coverage = self._display_coverage(image.size())
            image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            painter.setOpacity(self.coverage_opacity.value() / 100)
            painter.drawImage(0, 0, coverage)
            painter.end()
        if self.sites_box.isChecked() and self.current_sites and self.display_ref is not None:
            image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            sites.draw_sites(image, self.display_ref, self.current_sites, self.tx_icon.currentData(),
                             self.rx_icon.currentData(), self.icon_size.value(), self.site_names.isChecked())
        if self.display_ref is not None and (self.legend_check.isChecked() or self.scale_check.isChecked()):
            image = self._decorate(image, source, {"srtm": tr("Relief : SRTM (NASA)"),
                                                   "ign_estompage": tr("Relief : estompage IGN")}.get(relief))
        if not self.overlay_status.text():
            self.overlay_status.setText(" — ".join(notes))
        self.current_composite = image
        self.image_view.set_image(image, refit=refit)

    @property
    def basemap_worker(self):
        """Un téléchargement de couche raster en cours (ou None)."""
        return next(iter(self.basemap_workers.values()), None)

    def _raster_layer(self, source):
        """Couche raster (fond ou estompage IGN) reprojetée sur la vue courante, ou None
        pendant sa préparation (lancée en arrière-plan, mise en cache dans fonds/)."""
        if not source or self.current_ref is None:
            return None
        key = (self.image_combo.currentData(), source, self.region_tag)
        image = self.basemap_cache.get(key)
        if image is None:
            cache_file = self._basemap_cache_file(source)
            if cache_file.exists():
                image = QImage(str(cache_file))
                if image.size() != self.current_image.size():
                    image = None
            if image is None:
                self._start_basemap(key, source)
            else:
                self.basemap_cache = {k: v for k, v in self.basemap_cache.items() if k[0] == key[0]}
                self.basemap_cache[key] = image
        return image

    def _start_basemap(self, key, source):
        if key in self.basemap_workers:
            return
        worker = BasemapWorker(key, source, self.current_ref, self._basemap_cache_file(source), self)
        worker.progress.connect(self.overlay_status.setText)
        worker.done.connect(self._basemap_done)
        self.basemap_workers[key] = worker
        worker.start()
        self.overlay_status.setText(tr("Préparation : {source}…", source=tr(basemap.SOURCES[source]["label"])))

    def _basemap_done(self, worker, image, error):
        worker.wait()
        worker.deleteLater()
        self.basemap_workers.pop(worker.key, None)
        current = (worker.key[0] == self.image_combo.currentData() and worker.key[2] == self.region_tag)
        if error:
            if current:
                self.overlay_status.setText(tr("{source} indisponible : {error}", source=tr(basemap.SOURCES[worker.key[1]]["label"]),
                                                error=error))
            return
        if image is not None:
            self.basemap_cache[worker.key] = image
            if current:
                self._apply_overlay(refit=False)

    def _export_overlay(self):
        if self.current_composite is None:
            return
        source = Path(self.image_combo.currentData())
        suffix = self.basemap_combo.currentData() or "relief"
        path, chosen = QFileDialog.getSaveFileName(
            self, tr("Exporter l'image"), str(source.with_name(f"{source.stem}_{suffix}.png")),
            tr("Image PNG (*.png);;Image PPM (*.ppm)"))
        if not path:
            return
        as_ppm = path.lower().endswith(".ppm") or ("PPM" in chosen and not path.lower().endswith(".png"))
        if as_ppm and not path.lower().endswith(".ppm"):
            path += ".ppm"
        image = self.current_composite.convertToFormat(QImage.Format.Format_RGB888) if as_ppm \
            else self.current_composite
        if not image.save(path, "PPM" if as_ppm else "PNG"):
            QMessageBox.warning(self, tr("Export"), tr("Impossible d'enregistrer l'image."))
            return
        if self.display_ref is not None:
            if as_ppm:
                basemap.write_geo(path, self.display_ref)
            else:
                basemap.write_world_file(path, self.display_ref)
        self.statusBar().showMessage(
            tr("Image exportée : {path} (+ calage {ref})", path=path, ref=".geo" if as_ppm else ".pgw WGS84"), 6000)

    def _update_image_info(self, zoom):
        width, height = self.image_view.image_size()
        if width:
            self.image_info.setText(tr("{width} × {height} px — zoom {zoom:.0f} %  (molette : zoom, glisser : déplacer)",
                                       width=width, height=height, zoom=zoom * 100))

    def _open_current_image(self):
        path = self.image_combo.currentData()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ---- Historique ------------------------------------------------------

    def _refresh_history(self):
        self.history_table.setRowCount(0)
        for entry in self.history:
            params = storage.merge_defaults(entry.get("params"))
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            values = [
                entry.get("date", ""), entry.get("profile", ""),
                f"{params['arch']} {params['variant']}", tr(splat.MODES.get(params["mode"], "")),
                ", ".join(s["name"] for s in params["tx_sites"]), tr(entry.get("status", "")),
            ]
            for col, value in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(value))
        self.history_table.resizeColumnsToContents()
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

    def _history_entry(self):
        row = self.history_table.currentRow()
        return self.history[row] if 0 <= row < len(self.history) else None

    def _history_show(self):
        entry = self._history_entry()
        if entry:
            self.console.clear()
            self.show_results(entry.get("run_dir"))
            self.results_tabs.setCurrentIndex(2 if self.image_combo.count() else 1)

    def _history_reload(self):
        entry = self._history_entry()
        if not entry:
            return
        profile = entry.get("profile")
        if profile and profile != self.profile_name and profile in storage.list_profiles():
            if not self._confirm_discard():
                return
            self._switch_profile(profile, storage.merge_defaults(entry.get("params")))
        else:
            self.set_params(storage.merge_defaults(entry.get("params")))
        self.statusBar().showMessage(tr("Paramètres du {date} rechargés", date=entry.get("date")), 4000)

    def _history_open_dir(self):
        entry = self._history_entry()
        if entry and Path(entry.get("run_dir", "")).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(entry["run_dir"]))

    def _history_remove(self):
        entry = self._history_entry()
        if not entry:
            return
        run_dir = entry.get("run_dir")
        answer = QMessageBox.question(
            self, tr("Retirer de l'historique"),
            tr("Retirer cette exécution de l'historique ?\n\nOui : supprimer aussi son dossier de résultats\n"
            "Non : conserver le dossier"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:
            return
        if answer == QMessageBox.StandardButton.Yes and run_dir and Path(run_dir).is_dir():
            import shutil
            shutil.rmtree(run_dir, ignore_errors=True)
        self.history = storage.remove_history(run_dir)
        self._refresh_history()

    # ---- Divers -------------------------------------------------------------

    def edit_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings.update(dialog.values())
            storage.save_settings(self.settings)
            self.live_map.srtm_url = self.settings.get("srtm_url") or terrain.DEFAULT_URL

    def _style_progress(self):
        """Barre bleu moyen sur la piste du thème ; texte blanc cerné de sombre, lisible partout."""
        palette = QApplication.palette()
        track = palette.color(palette.ColorRole.Base).name()
        border = palette.color(palette.ColorRole.Mid).name()
        self.progress.setStyleSheet(
            f"QProgressBar {{ border: 1px solid {border}; border-radius: 4px; background: {track};"
            " color: #ffffff; font-weight: bold; text-align: center; }"
            "QProgressBar::chunk { border-radius: 3px;"
            " background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #6ea8ea, stop:1 #3f7fd0); }")

    def set_language(self, code):
        """Langue enregistrée, appliquée au prochain démarrage (proposé tout de suite)."""
        if code == self.settings.get("language", ""):
            return
        self.settings["language"] = code
        storage.save_settings(self.settings)
        # Message dans la langue choisie : c'est celle que l'utilisateur sait lire.
        current = i18n.language()
        i18n.set_language(code)
        title, text = tr("Langue"), tr("La nouvelle langue sera appliquée au prochain démarrage. Redémarrer maintenant ?")
        i18n.set_language(current)
        if QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes and self.close():
            args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
            QProcess.startDetached(sys.executable, args)

    def set_theme(self, name):
        self.settings["theme"] = themes.apply(name)
        self._style_progress()
        storage.save_settings(self.settings)

    def manage_prereqs(self):
        if self._busy():
            QMessageBox.information(self, tr("Pré-requis"), tr("Attendez la fin de l'exécution en cours."))
            return
        PrereqDialog(self.settings, self).exec()
        self._show_prereq_status(prereqs.check(self.settings["extra_paths"]))
        self._refresh_state()

    def _show_prereq_status(self, report):
        """Indicateur permanent de la barre d'état, visible tant qu'une dépendance manque."""
        ok, text = prereqs.summary(report)
        self.prereq_label.setText("⚠ " + tr("{text} — Fichier → Pré-requis…", text=text))
        self.prereq_label.setVisible(not ok)

    def startup_prereqs(self, report):
        """Après l'ouverture : état des dépendances et, au premier démarrage, proposition
        de téléchargement."""
        self._show_prereq_status(report)
        first = not self.settings.get("prereqs_offered")
        if first:
            self.settings["prereqs_offered"] = True
            storage.save_settings(self.settings)
        if prereqs.summary(report)[0] or not first:
            return
        lines = ["• " + tr("DLL {arch} manquantes : {dlls}", arch=arch, dlls=", ".join(state["missing"]))
                 for arch, state in report.items() if state["splat"] and state["missing"]]
        gnuplot = False         # facultatif : le profil de liaison est tracé par l'application
        if not any(state["splat"] for state in report.values()):
            lines.append("• " + tr("Exécutables SPLAT! absents de bin\\x64 et bin\\x86 : ils s'installent depuis "
                                   "une archive (fichier ou URL), puis les DLL nécessaires sont proposées."))
        auto = [arch for arch, state in report.items() if state["splat"] and state["missing"]]
        question = (tr("Télécharger maintenant les dépendances manquantes ?") if auto or gnuplot
                    else tr("Ouvrir la fenêtre des pré-requis ?"))
        if QMessageBox.question(self, tr("Premier démarrage"),
                                tr("Des dépendances de SPLAT! sont absentes :") + "\n\n" + "\n".join(lines)
                                + f"\n\n{question}") == QMessageBox.StandardButton.Yes:
            PrereqDialog(self.settings, self, auto_dlls=auto, auto_gnuplot=gnuplot).exec()
            self._show_prereq_status(prereqs.check(self.settings["extra_paths"]))
            self._refresh_state()

    def open_runs_dir(self):
        path = Path(self.settings["runs_dir"])
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_help(self, search=False):
        """Aide intégrée (documentation SPLAT! avec sommaire, index et recherche), fenêtre unique."""
        if self.help_window is None:
            self.help_window = help.HelpWindow(self)
        self.help_window.show()
        self.help_window.raise_()
        self.help_window.activateWindow()
        if search:
            self.help_window.focus_search()

    def _open_terrain_dir(self):
        params = self._params_or_warn()
        path = Path(splat.effective_sdf_dir(params)) if params else terrain.SDF_DIR
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path == terrain.SDF_DIR else path)))

    def closeEvent(self, event):
        if self._busy():
            if QMessageBox.question(self, tr("Quitter"), tr("Une exécution est en cours. L'arrêter et quitter ?")) \
                    != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self.terrain_worker is not None:
                self.terrain_worker.cancel()
                self.terrain_worker.wait(5000)
            if self.process is not None:
                self.process.kill()
                self.process.waitForFinished(3000)
        for worker in list(self.basemap_workers.values()):
            worker.cancel()
            worker.wait(10000)
        for worker in list(getattr(self, "tasks", {}).values()):
            worker.wait(10000)
        try:
            self.settings["last_params"] = self.get_params()
        except splat.ParamError:
            pass
        self.settings["last_profile"] = self.profile_name
        self._overlay_settings_changed()
        self.settings["geometry_qt"] = bytes(self.saveGeometry().toBase64()).decode()
        storage.save_settings(self.settings)
        event.accept()


def ensure_directories(settings):
    """Crée les répertoires de travail absents (premier lancement, ou dossier supprimé depuis).
    Renvoie (répertoires créés, messages d'échec)."""
    folders = [storage.PROFILES_DIR, *(folder_setting(settings, key) for key in storage.FOLDER_SETTINGS),
               antennas.LIBRARY_DIR,
               terrain.SRTM_DIR, terrain.SDF_DIR, basemap.TILES_DIR]
    for arch in splat.ARCHES:
        folders += [storage.BIN_DIR / arch / "utils", prereqs.dll_dir(arch), splat.TOOLS_DIR / arch]
    created, failed = [], []
    for folder in folders:
        if folder.is_dir():
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            created.append(folder)
        except OSError as exc:
            failed.append(f"{folder} : {exc.strerror or exc}")
    return created, failed


def license_file():
    """Texte de la GNU GPL v2 : LICENSE à côté de l'exécutable, sinon la copie intégrée à lib/."""
    for folder in (storage.PROJECT_DIR, Path(getattr(sys, "_MEIPASS", storage.PROJECT_DIR))):
        if (folder / "LICENSE").is_file():
            return folder / "LICENSE"
    return None


def app_icon():
    """Icône de l'application : icons/ à côté de l'exécutable, sinon la copie intégrée à lib/."""
    for folder in (storage.PROJECT_DIR, Path(getattr(sys, "_MEIPASS", storage.PROJECT_DIR))):
        path = folder / "icons" / "splat_icon.ico"
        if path.exists():
            return QIcon(str(path))
    return QIcon()


def main():
    if os.name == "nt":
        # Identifiant propre : la barre des tâches affiche l'icône de l'application, pas celle de python.exe.
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("F4CWH.SplatGui")
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("SPLAT!Gui")
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())
    settings = storage.load_settings()
    storage.apply_install_paths(settings)
    i18n.set_language(settings.get("language", ""), app)
    themes.apply(settings.get("theme", themes.DEFAULT), app)
    from .splash import Splash
    splash = Splash()
    splash.show()
    splash.progress(2, tr("Création des répertoires…"))
    created, failed = ensure_directories(settings)
    if created:
        splash.message(tr("{n} répertoire(s) créé(s)", n=len(created)))
    splash.progress(3, tr("Vérification des dépendances…"))
    report = prereqs.check(settings["extra_paths"])
    ok, text = prereqs.summary(report)
    splash.set_status(text, ok)
    splash.progress(5, tr("Démarrage…"))
    window = MainWindow(splash.progress)
    splash.progress(96, tr("Ouverture de la fenêtre…"))
    window.show()
    splash.finish_after(window)
    if failed:
        QTimer.singleShot(0, lambda: QMessageBox.warning(
            window, tr("Répertoires"), tr("Impossible de créer ces répertoires :") + "\n\n" + "\n".join(failed)))
    QTimer.singleShot(0, lambda: window.startup_prereqs(report))
    return app.exec()
