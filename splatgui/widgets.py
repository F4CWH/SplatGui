"""Widgets réutilisables : visionneuse d'images, liste de fichiers, sélecteur de chemin."""

from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QPointF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QPushButton, QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout,
    QWidget,
)
from .i18n import N_, tr


def graduate(slider, tick, page=None):
    """Gradue un curseur : repères tous les `tick`, flèches au pas de 1, Page ↑/↓ d'un repère."""
    slider.setTickPosition(QSlider.TickPosition.TicksBelow)
    slider.setTickInterval(tick)
    slider.setSingleStep(1)
    slider.setPageStep(page or tick)
    return slider


class SliderValue(QLabel):
    """Valeur d'un curseur affichée à côté de lui (largeur fixe : la mise en page ne bouge pas)."""

    def __init__(self, slider, fmt="{} %", scale=1, parent=None):
        super().__init__(parent)
        self._fmt, self._scale = fmt, scale
        widest = max((fmt.format(v / scale if scale != 1 else v) for v in (slider.minimum(), slider.maximum())),
                     key=len)
        self.setMinimumWidth(QFontMetrics(self.font()).horizontalAdvance(widest) + 4)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider.valueChanged.connect(self._show)
        self._show(slider.value())

    def _show(self, value):
        self.setText(self._fmt.format(value / self._scale if self._scale != 1 else value))


class HorizontalScroll(QScrollArea):
    """Barre de commandes qui défile horizontalement quand la place manque, au lieu d'imposer sa
    largeur au panneau qui la contient ; hauteur ajustée au contenu (+ barre de défilement)."""

    def __init__(self, widget, parent=None):
        super().__init__(parent)
        self.setWidget(widget)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._update_height()

    def minimumSizeHint(self):
        return QSize(120, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    def _update_height(self):
        content = self.widget()
        needed = content.minimumSizeHint().width() > self.viewport().width()
        height = content.sizeHint().height() + (self.horizontalScrollBar().sizeHint().height() if needed else 0)
        if self.height() != height:
            self.setFixedHeight(height)


class ZoomControls(QWidget):
    """Boutons + / − / recentrer posés sur une carte (coin haut-droit, replacé par `place`)."""

    zoomIn = pyqtSignal()
    zoomOut = pyqtSignal()
    home = pyqtSignal()

    STYLE = ("QToolButton { background: rgba(255, 255, 255, 225); color: #202020; border: 1px solid #8a8a8a;"
             " border-radius: 4px; font-size: 16px; font-weight: bold; }"
             "QToolButton:hover { background: #ffffff; border-color: #404040; }"
             "QToolButton:pressed { background: #dcdcdc; }"
             "QToolButton:disabled { color: #b0b0b0; }")

    def __init__(self, parent, home_tip=N_("Recentrer")):
        super().__init__(parent)
        self.setStyleSheet(self.STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.buttons = {}
        for key, text, tip, signal in (("in", "+", N_("Zoom avant (+)"), self.zoomIn),
                                       ("out", "−", N_("Zoom arrière (−)"), self.zoomOut),
                                       ("home", "⌖", home_tip, self.home)):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tr(tip))
            button.setFixedSize(30, 30)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoRepeat(key != "home")
            button.clicked.connect(signal)
            layout.addWidget(button)
            self.buttons[key] = button
        self.adjustSize()

    def set_limits(self, can_zoom_in, can_zoom_out):
        self.buttons["in"].setEnabled(can_zoom_in)
        self.buttons["out"].setEnabled(can_zoom_out)

    def place(self, width, margin=10):
        self.move(width - self.width() - margin, margin)
        self.raise_()


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
        self.controls = ZoomControls(self, N_("Ajuster à la fenêtre"))
        self.controls.zoomIn.connect(lambda: self.zoom(1.25, centered=True))
        self.controls.zoomOut.connect(lambda: self.zoom(0.8, centered=True))
        self.controls.home.connect(self.fit)

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

    def zoom(self, factor, centered=False):
        """Zoom autour du point sous la souris (molette) ou du centre de la vue (boutons)."""
        self._fit = False
        current = self.transform().m11()
        if 0.02 < current * factor < 40:
            if centered:
                self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
            self.scale(factor, factor)
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.zoomChanged.emit(self.transform().m11())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(1.25, centered=True)
        elif event.key() == Qt.Key.Key_Minus:
            self.zoom(0.8, centered=True)
        else:
            super().keyPressEvent(event)

    def viewportEvent(self, event):
        # Le viewport change de taille avec la fenêtre et à l'apparition des barres de défilement.
        if event.type() == QEvent.Type.Resize:
            viewport = self.viewport().geometry()
            self.controls.place(viewport.x() + viewport.width())
        return super().viewportEvent(event)

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
