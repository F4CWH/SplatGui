"""Écran de démarrage : couverture SPLAT! sur l'Île-de-France (resources/splash_map.png),
nom, version et barre de progression du chargement.

Le fond est une vraie carte produite par l'application (calcul de champ électrique SPLAT!,
relief SRTM, limites des départements) ; le texte, la version et la progression sont dessinés
au lancement, à la résolution de l'écran.

Plusieurs mises en page sont disponibles (STYLES) ; STYLE choisit celle qui est affichée :
    classique  carte plein cadre, bandeaux sombres en haut et en bas, titre en haut à gauche
               (configuration d'origine, version 1.1.0)
    panneau    carte plein cadre, panneau sombre à gauche (titre, crédits, progression)
    clair      fond clair, carte en vignette arrondie à droite, texte sombre
    radar      sans carte : fond nuit, cercles de portée et balayage dessinés
    cinema     carte avec vignettage, titre centré, barre fine en bas
"""

import math
import time
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor, QConicalGradient, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient,
)
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
NIGHT = QColor(8, 18, 38)                # bleu nuit des bandeaux
STYLE = "classique"

# Zone de progression de chaque style : barre, couleurs du texte et de la piste, barre plate ?
_MARGIN = 12
STYLES = {
    "classique": {"bar": QRectF(_MARGIN, HEIGHT - _MARGIN - BAR_HEIGHT, WIDTH - 2 * _MARGIN, BAR_HEIGHT),
                  "text": QColor(255, 255, 255), "track": QColor(220, 225, 231, 230), "flat": False},
    "panneau": {"bar": QRectF(_MARGIN, HEIGHT - _MARGIN - 6, 160, 6),
                "text": QColor(235, 242, 250), "track": QColor(255, 255, 255, 60), "flat": False},
    "clair": {"bar": QRectF(_MARGIN + 4, HEIGHT - _MARGIN - 6, 170, 6),
              "text": QColor(60, 72, 90), "track": QColor(214, 222, 232), "flat": False},
    "radar": {"bar": QRectF(_MARGIN, HEIGHT - _MARGIN - 6, WIDTH - 2 * _MARGIN, 6),
              "text": QColor(200, 230, 255), "track": QColor(255, 255, 255, 40), "flat": False},
    "cinema": {"bar": QRectF(0, HEIGHT - 3, WIDTH, 3),
               "text": QColor(235, 242, 250), "track": QColor(255, 255, 255, 40), "flat": True},
}


def _map_image(size_w, size_h):
    image = QImage(str(BACKGROUND))
    if image.isNull():                                               # secours : couleur unie
        image = QImage(size_w, size_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(40, 70, 110))
        return image
    return image.scaled(size_w, size_h, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)


def _title(painter, x, y, size=28, color=QColor(255, 255, 255), outline=QColor(8, 18, 38, 200), center=False):
    font = QFont()
    font.setPixelSize(size)
    font.setBold(True)
    path = QPainterPath()
    path.addText(0, 0, font, "SPLAT!Gui")
    if center:
        x -= path.boundingRect().width() / 2
    path.translate(x, y)
    if outline is not None:
        painter.setPen(QPen(outline, 3))
        painter.drawPath(path)
    painter.fillPath(path, color)


def _text(painter, x, y, text, size, color, center=False):
    font = QFont()
    font.setPixelSize(size)
    painter.setFont(font)
    painter.setPen(color)
    if center:
        painter.drawText(QRectF(0, y - size, WIDTH, size * 1.4), Qt.AlignmentFlag.AlignHCenter, text)
    else:
        painter.drawText(QPointF(x, y), text)


def _classique(painter, image):
    top = QLinearGradient(0, 0, 0, 82)
    top.setColorAt(0, QColor(8, 18, 38, 235))
    top.setColorAt(1, QColor(8, 18, 38, 0))
    painter.fillRect(QRectF(0, 0, WIDTH, 82), top)
    bottom = QLinearGradient(0, HEIGHT - 62, 0, HEIGHT)
    bottom.setColorAt(0, QColor(8, 18, 38, 0))
    bottom.setColorAt(1, QColor(8, 18, 38, 235))
    painter.fillRect(QRectF(0, HEIGHT - 62, WIDTH, 62), bottom)
    _title(painter, 14, 34)
    _text(painter, 16, 52, f"Version {__version__}", 12, QColor(200, 230, 255))
    _text(painter, 16, 66, tr(CREDITS), 10, QColor(235, 242, 250))
    _text(painter, 16, 78, tr(LICENSE_SHORT), 9, QColor(200, 214, 230))


def _panneau(painter, image):
    panel = QLinearGradient(0, 0, 230, 0)
    panel.setColorAt(0, QColor(8, 18, 38, 240))
    panel.setColorAt(0.72, QColor(8, 18, 38, 225))
    panel.setColorAt(1, QColor(8, 18, 38, 0))
    painter.fillRect(QRectF(0, 0, 230, HEIGHT), panel)
    painter.fillRect(QRectF(_MARGIN, 18, 3, 40), ACCENT)                  # filet d'accent
    _title(painter, _MARGIN + 9, 44, 24, outline=None)
    _text(painter, _MARGIN + 10, 58, f"Version {__version__}", 11, QColor(160, 200, 245))
    font = QFont()
    font.setPixelSize(9)
    painter.setFont(font)
    painter.setPen(QColor(215, 226, 240))
    painter.drawText(QRectF(_MARGIN, 72, 175, 30), Qt.TextFlag.TextWordWrap, tr(CREDITS))
    _text(painter, _MARGIN, 96, tr(LICENSE_SHORT), 8, QColor(160, 176, 196))


def _clair(painter, image):
    painter.fillRect(QRectF(0, 0, WIDTH, HEIGHT), QColor(246, 248, 251))
    card = QRectF(200, 14, WIDTH - 214, HEIGHT - 28)
    clip = QPainterPath()
    clip.addRoundedRect(card, 10, 10)
    painter.save()
    painter.setClipPath(clip)
    source = QRectF(image.width() * 0.2, 0, image.width() * 0.6, image.height())   # centre de la carte
    painter.drawImage(card, image, source)
    painter.restore()
    painter.setPen(QPen(QColor(205, 214, 226), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(clip)
    _title(painter, 16, 46, 26, color=QColor(20, 40, 72), outline=None)
    painter.fillRect(QRectF(17, 54, 34, 3), ACCENT)
    _text(painter, 17, 72, f"Version {__version__}", 11, ACCENT)
    font = QFont()
    font.setPixelSize(9)
    painter.setFont(font)
    painter.setPen(QColor(70, 82, 100))
    painter.drawText(QRectF(17, 82, 170, 40), Qt.TextFlag.TextWordWrap, tr(CREDITS))
    _text(painter, 17, 106, tr(LICENSE_SHORT), 8, QColor(120, 132, 150))


def _radar(painter, image):
    centre = QPointF(WIDTH * 0.74, HEIGHT * 0.48)
    background = QRadialGradient(centre, WIDTH * 0.8)
    background.setColorAt(0, QColor(18, 46, 84))
    background.setColorAt(1, QColor(6, 12, 26))
    painter.fillRect(QRectF(0, 0, WIDTH, HEIGHT), background)
    sweep = QConicalGradient(centre, 60)                               # balayage
    sweep.setColorAt(0, QColor(120, 220, 140, 120))
    sweep.setColorAt(0.18, QColor(120, 220, 140, 0))
    sweep.setColorAt(1, QColor(120, 220, 140, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(sweep)
    painter.drawEllipse(centre, 120, 120)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for n, radius in enumerate((24, 48, 72, 96, 120)):                # cercles de portée
        painter.setPen(QPen(QColor(110, 168, 234, 150 - n * 22), 1))
        painter.drawEllipse(centre, radius, radius)
    painter.setPen(QPen(QColor(110, 168, 234, 60), 1))
    for angle in range(0, 180, 30):
        a = math.radians(angle)
        dx, dy = 120 * math.cos(a), 120 * math.sin(a)
        painter.drawLine(QPointF(centre.x() - dx, centre.y() - dy), QPointF(centre.x() + dx, centre.y() + dy))
    glow = QRadialGradient(centre, 8)
    glow.setColorAt(0, QColor(255, 210, 90))
    glow.setColorAt(1, QColor(255, 140, 40, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(glow)
    painter.drawEllipse(centre, 8, 8)
    _title(painter, 16, 48, 28, outline=None)
    _text(painter, 18, 66, f"Version {__version__}", 12, QColor(120, 220, 140))
    _text(painter, 18, 84, tr(CREDITS), 9, QColor(215, 226, 240))
    _text(painter, 18, 97, tr(LICENSE_SHORT), 8, QColor(150, 170, 196))


def _cinema(painter, image):
    vignette = QRadialGradient(QPointF(WIDTH / 2, HEIGHT / 2), WIDTH * 0.62)
    vignette.setColorAt(0, QColor(8, 18, 38, 40))
    vignette.setColorAt(0.65, QColor(8, 18, 38, 150))
    vignette.setColorAt(1, QColor(8, 18, 38, 245))
    painter.fillRect(QRectF(0, 0, WIDTH, HEIGHT), vignette)
    _title(painter, WIDTH / 2, 96, 36, center=True)
    _text(painter, 0, 116, f"Version {__version__}", 12, QColor(200, 230, 255), center=True)
    _text(painter, 0, 134, tr(CREDITS) + "  ·  " + tr(LICENSE_SHORT), 8, QColor(200, 214, 230), center=True)


_PAINTERS = {"classique": _classique, "panneau": _panneau, "clair": _clair, "radar": _radar, "cinema": _cinema}


def splash_image(scale=1.0, style=None):
    """Fond de l'écran (carte, bandeaux, nom et version, sans progression) ; `scale` = échelle de l'écran."""
    style = style or STYLE
    scale *= ZOOM
    size_w, size_h = round(WIDTH * scale), round(HEIGHT * scale)
    carte = _map_image(size_w, size_h).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if style in ("clair", "radar"):              # fond dessiné : la carte est en vignette, ou absente
        image = QImage(size_w, size_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
    else:
        image = carte.copy()
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.scale(scale, scale)                                      # coordonnées WIDTH × HEIGHT
    _PAINTERS[style](painter, carte)
    painter.end()
    return image


class Splash(QSplashScreen):
    """Écran de démarrage avec message et barre de progression, affiché au moins
    MIN_DISPLAY_S secondes."""

    def __init__(self, style=None):
        self.style = style if style in STYLES else STYLE
        screen = QApplication.primaryScreen()
        scale = screen.devicePixelRatio() if screen else 1.0
        pixmap = QPixmap.fromImage(splash_image(scale, self.style))   # taille : WIDTH × HEIGHT × ZOOM
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
        layout = STYLES[self.style]
        painter.scale(ZOOM, ZOOM)                                    # mêmes coordonnées que le fond
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = layout["bar"]
        font = QFont()
        font.setPixelSize(10 if bar.width() > 200 else 9)
        painter.setFont(font)
        painter.setPen(layout["text"])
        if layout["flat"]:                       # barre plate collée au bord : texte centré au-dessus
            label = QRectF(0, bar.top() - 20, WIDTH, 14)
            painter.drawText(label, Qt.AlignmentFlag.AlignCenter, f"{self.text}  {self.value:.0f} %")
        else:
            label = QRectF(bar.left(), bar.top() - 17, bar.width(), 14)
            painter.drawText(label, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text)
            painter.drawText(label, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{self.value:.0f} %")
        if self.status:
            text, ok = self.status
            dark_text = layout["text"].lightness() < 128
            painter.setPen(QColor(30, 140, 70) if ok and dark_text else QColor(200, 110, 0) if dark_text
                           else QColor(120, 220, 140) if ok else QColor(255, 180, 70))
            flags = Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignHCenter if layout["flat"]
                                                     else Qt.AlignmentFlag.AlignLeft)
            painter.drawText(QRectF(label.left(), label.top() - 14, label.width(), 14), flags,
                             ("✔ " if ok else "⚠ ") + text)
        painter.setPen(Qt.PenStyle.NoPen)
        radius = 0 if layout["flat"] else bar.height() / 2
        painter.setBrush(layout["track"])
        painter.drawRoundedRect(bar, radius, radius)
        if self.value > 0:
            filled = QRectF(bar.left(), bar.top(), max(bar.height(), bar.width() * self.value / 100), bar.height())
            gradient = QLinearGradient(0, bar.top(), 0, bar.bottom())
            gradient.setColorAt(0, QColor(110, 168, 234))
            gradient.setColorAt(1, ACCENT)
            painter.setBrush(gradient)
            painter.drawRoundedRect(filled, radius, radius)

    def finish_after(self, window):
        self.progress(100, tr("Prêt"))
        while time.monotonic() - self.started < MIN_DISPLAY_S:
            QApplication.processEvents()
            time.sleep(0.02)
        self.finish(window)
