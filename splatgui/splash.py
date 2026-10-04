"""Écran de démarrage : couverture SPLAT! sur l'Île-de-France (resources/splash_map.png),
nom, version et barre de progression du chargement.

Le fond est une vraie carte produite par l'application (calcul de champ électrique SPLAT!,
relief SRTM, limites des départements) ; le texte, la version et la progression sont dessinés
au lancement, à la résolution de l'écran.
"""

import time
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QSplashScreen

from . import CREDITS, LICENSE_SHORT, __version__
from .i18n import tr

BACKGROUND = Path(__file__).parent / "resources" / "splash_map.png"
SIZE_FACTOR = 0.4                        # mise en page : 40 % du fond d'origine (960 × 540)
WIDTH, HEIGHT = round(960 * SIZE_FACTOR), round(540 * SIZE_FACTOR)   # coordonnées de dessin
ZOOM = 1.5                               # agrandissement de l'écran affiché (texte, barre compris)
MIN_DISPLAY_S = 1.5
BAR_HEIGHT = 8
ACCENT = QColor(63, 127, 208)            # bleu moyen (comme la barre de progression du calcul)


def splash_image(scale=1.0):
    """Fond de l'écran (carte, bandeaux, nom et version, sans légende) ; `scale` = échelle de l'écran."""
    scale *= ZOOM
    size_w, size_h = round(WIDTH * scale), round(HEIGHT * scale)
    image = QImage(str(BACKGROUND))
    if image.isNull():                                               # secours : couleur unie
        image = QImage(size_w, size_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(40, 70, 110))
    else:
        image = image.scaled(size_w, size_h, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    painter.scale(scale, scale)                                      # coordonnées WIDTH × HEIGHT
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    top = QLinearGradient(0, 0, 0, 82)
    top.setColorAt(0, QColor(8, 18, 38, 235))
    top.setColorAt(1, QColor(8, 18, 38, 0))
    painter.fillRect(QRectF(0, 0, WIDTH, 82), top)
    bottom = QLinearGradient(0, HEIGHT - 62, 0, HEIGHT)
    bottom.setColorAt(0, QColor(8, 18, 38, 0))
    bottom.setColorAt(1, QColor(8, 18, 38, 235))
    painter.fillRect(QRectF(0, HEIGHT - 62, WIDTH, 62), bottom)

    title = QFont()
    title.setPixelSize(28)
    title.setBold(True)
    path = QPainterPath()
    path.addText(14, 34, title, "Splat!Gui")
    painter.setPen(QPen(QColor(8, 18, 38, 200), 3))
    painter.drawPath(path)
    painter.fillPath(path, QColor(255, 255, 255))
    font = QFont()
    font.setPixelSize(12)
    painter.setFont(font)
    painter.setPen(QColor(200, 230, 255))
    painter.drawText(QPointF(16, 52), f"Version {__version__}")
    font.setPixelSize(10)
    painter.setFont(font)
    painter.setPen(QColor(235, 242, 250))
    painter.drawText(QPointF(16, 66), tr(CREDITS))
    font.setPixelSize(9)
    painter.setFont(font)
    painter.setPen(QColor(200, 214, 230))
    painter.drawText(QPointF(16, 78), tr(LICENSE_SHORT))
    painter.end()
    return image


class Splash(QSplashScreen):
    """Écran de démarrage avec message et barre de progression, affiché au moins
    MIN_DISPLAY_S secondes."""

    def __init__(self):
        screen = QApplication.primaryScreen()
        scale = screen.devicePixelRatio() if screen else 1.0
        pixmap = QPixmap.fromImage(splash_image(scale))         # taille : WIDTH × HEIGHT × ZOOM
        pixmap.setDevicePixelRatio(scale)
        super().__init__(pixmap)
        self.started = time.monotonic()
        self.value = 0.0
        self.text = ""
        self.status = None               # (texte, tout est présent ?) : état des dépendances

    def progress(self, percent, text=None):
        """Avancement (0 à 100) et message ; redessine aussitôt."""
        self.value = max(0.0, min(100.0, float(percent)))
        if text is not None:
            self.text = text
        self.repaint()
        QApplication.processEvents()

    def message(self, text):
        self.progress(self.value, text)

    def set_status(self, text, ok):
        """Ligne d'état des dépendances, affichée jusqu'à la fermeture (verte ou orange)."""
        self.status = (text, ok)
        self.progress(self.value)

    def drawContents(self, painter):
        painter.scale(ZOOM, ZOOM)                                    # mêmes coordonnées que le fond
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = 12
        bar = QRectF(margin, HEIGHT - margin - BAR_HEIGHT, WIDTH - 2 * margin, BAR_HEIGHT)
        font = QFont()
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        label = QRectF(margin, bar.top() - 17, bar.width(), 14)
        painter.drawText(label, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text)
        painter.drawText(label, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{self.value:.0f} %")
        if self.status:
            text, ok = self.status
            painter.setPen(QColor(120, 220, 140) if ok else QColor(255, 180, 70))
            painter.drawText(QRectF(margin, label.top() - 14, bar.width(), 14),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             ("✔ " if ok else "⚠ ") + text)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(220, 225, 231, 230))                  # piste gris clair
        painter.drawRoundedRect(bar, BAR_HEIGHT / 2, BAR_HEIGHT / 2)
        if self.value > 0:
            filled = QRectF(bar.left(), bar.top(), max(BAR_HEIGHT, bar.width() * self.value / 100), BAR_HEIGHT)
            gradient = QLinearGradient(0, bar.top(), 0, bar.bottom())
            gradient.setColorAt(0, QColor(110, 168, 234))
            gradient.setColorAt(1, ACCENT)
            painter.setBrush(gradient)
            painter.drawRoundedRect(filled, BAR_HEIGHT / 2, BAR_HEIGHT / 2)

    def finish_after(self, window):
        self.progress(100, tr("Prêt"))
        while time.monotonic() - self.started < MIN_DISPLAY_S:
            QApplication.processEvents()
            time.sleep(0.02)
        self.finish(window)
