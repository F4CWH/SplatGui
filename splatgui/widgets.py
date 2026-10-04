"""Widgets réutilisables : visionneuse d'images, liste de fichiers, sélecteur de chemin."""

from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLineEdit, QListWidget, QPushButton, QVBoxLayout, QWidget,
)
from .i18n import N_, tr


class ImageView(QGraphicsView):
    """Affiche une image avec zoom à la molette et déplacement à la souris."""

    zoomChanged = pyqtSignal(float)
    contextRequested = pyqtSignal(QPointF, QPoint)   # position dans l'image, position écran

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setScene(self._scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._fit = True

    def load(self, path):
        image = QImage(str(path))
        if image.isNull():
            self.clear()
            return False
        self.set_image(image)
        return True

    def set_image(self, image, refit=True):
        """Affiche une image ; refit=False conserve le zoom et la position (même taille)."""
        same_size = self._item.pixmap().size() == image.size()
        self._item.setPixmap(QPixmap.fromImage(image))
        self._scene.setSceneRect(self._item.boundingRect())
        if refit or not same_size:
            self.fit()

    def clear(self):
        self._item.setPixmap(QPixmap())

    def image_size(self):
        pix = self._item.pixmap()
        return (pix.width(), pix.height()) if not pix.isNull() else (0, 0)

    def fit(self):
        self._fit = True
        if not self._item.pixmap().isNull():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoomChanged.emit(self.transform().m11())

    def actual_size(self):
        self._fit = False
        self.resetTransform()
        self.zoomChanged.emit(1.0)

    def zoom(self, factor):
        self._fit = False
        current = self.transform().m11()
        if 0.02 < current * factor < 40:
            self.scale(factor, factor)
        self.zoomChanged.emit(self.transform().m11())

    def contextMenuEvent(self, event):
        if not self._item.pixmap().isNull():
            self.contextRequested.emit(self.mapToScene(event.pos()), event.globalPos())

    def wheelEvent(self, event):
        self.zoom(1.25 if event.angleDelta().y() > 0 else 0.8)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self.fit()


class PathEdit(QWidget):
    """Champ texte + bouton « … » pour choisir un fichier ou un dossier."""

    changed = pyqtSignal()

    def __init__(self, directory=False, file_filter=N_("Tous les fichiers (*)"), parent=None):
        super().__init__(parent)
        self._directory = directory
        self._filter = file_filter
        self.edit = QLineEdit()
        self.edit.textChanged.connect(self.changed)
        button = QPushButton("…")
        button.setFixedWidth(32)
        button.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        layout.addWidget(button)

    def _browse(self):
        start = self.edit.text() or str(Path.home())
        if self._directory:
            path = QFileDialog.getExistingDirectory(self, tr("Choisir un dossier"), start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, tr("Choisir un fichier"), start, tr(self._filter))
        if path:
            self.edit.setText(str(Path(path)))

    def text(self):
        return self.edit.text()

    def setText(self, text):
        self.edit.setText(text)


class FileList(QWidget):
    """Liste de fichiers (au plus `maximum`) avec boutons Ajouter / Retirer."""

    changed = pyqtSignal()

    def __init__(self, maximum=5, file_filter=N_("Tous les fichiers (*)"), parent=None):
        super().__init__(parent)
        self._max = maximum
        self._filter = file_filter
        self.list = QListWidget()
        self.list.setMaximumHeight(90)
        add = QPushButton(tr("Ajouter…"))
        remove = QPushButton(tr("Retirer"))
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        buttons = QVBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list)
        layout.addLayout(buttons)

    def _add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Ajouter des fichiers"), "", tr(self._filter))
        for path in paths:
            if self.list.count() >= self._max:
                break
            self.list.addItem(str(Path(path)))
        self.changed.emit()

    def _remove(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.changed.emit()

    def files(self):
        return [self.list.item(i).text() for i in range(self.list.count())]

    def setFiles(self, files):
        self.list.clear()
        self.list.addItems(files[: self._max])
