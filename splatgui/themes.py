"""Thèmes de l'interface (style Fusion) : palette du système ou palettes prédéfinies.

Le thème s'applique à chaud à toute l'application ; le choix est enregistré dans
settings["theme"].
"""

import tempfile
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPalette, QPolygonF
from PyQt6.QtWidgets import QApplication
from .i18n import N_, tr

DEFAULT = "system"

# Couleurs de base : fenêtre, champs, champs alternés, texte, boutons, sélection,
# texte sélectionné, liens, texte désactivé, infobulles (fond, texte), schéma (clair/sombre).
THEMES = {
    "system": {"label": N_("Système")},
    "light": {"label": N_("Clair"), "window": "#f0f0f0", "base": "#ffffff", "alternate": "#f5f6f7",
              "text": "#1b1b1b", "button": "#e8e8e8", "highlight": "#3f7fd0", "highlighted": "#ffffff",
              "link": "#1f5fbf", "disabled": "#9a9a9a", "tooltip": "#ffffdc", "tooltip_text": "#1b1b1b",
              "dark": False},
    "dark": {"label": N_("Sombre"), "window": "#2b2b2b", "base": "#1e1e1e", "alternate": "#262626",
             "text": "#e6e6e6", "button": "#3a3a3a", "highlight": "#3f7fd0", "highlighted": "#ffffff",
             "link": "#6ea8ea", "disabled": "#7a7a7a", "tooltip": "#3a3a3a", "tooltip_text": "#e6e6e6",
             "dark": True},
    "midnight": {"label": N_("Bleu nuit"), "window": "#1d2433", "base": "#141a26", "alternate": "#1a2130",
                 "text": "#dce3ee", "button": "#2a3447", "highlight": "#4c8ddb", "highlighted": "#ffffff",
                 "link": "#7fb2f0", "disabled": "#6b7689", "tooltip": "#2a3447", "tooltip_text": "#dce3ee",
                 "dark": True},
    "sepia": {"label": N_("Sépia"), "window": "#efe6d5", "base": "#fbf6ec", "alternate": "#f3ebdc",
              "text": "#3b3024", "button": "#e4d8c2", "highlight": "#9c6b30", "highlighted": "#ffffff",
              "link": "#8a4f12", "disabled": "#a39684", "tooltip": "#fbf6ec", "tooltip_text": "#3b3024",
              "dark": False},
    "contrast": {"label": N_("Contraste élevé"), "window": "#000000", "base": "#000000", "alternate": "#141414",
                 "text": "#ffffff", "button": "#000000", "highlight": "#ffd400", "highlighted": "#000000",
                 "link": "#00e5ff", "disabled": "#8c8c8c", "tooltip": "#000000", "tooltip_text": "#ffffff",
                 "frame": "#ffffff", "dark": True},
}
# Thèmes sombres sans couleur de cadre : contours éclaircis pour rester visibles sur fond sombre.
for _spec in THEMES.values():
    if _spec.get("dark") and "frame" not in _spec:
        _spec["frame"] = QColor(_spec["button"]).lighter(190).name()


def _palette(spec):
    c = {key: QColor(value) for key, value in spec.items() if isinstance(value, str) and value.startswith("#")}
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, key in ((roles.Window, "window"), (roles.WindowText, "text"), (roles.Base, "base"),
                      (roles.AlternateBase, "alternate"), (roles.Text, "text"), (roles.Button, "button"),
                      (roles.ButtonText, "text"), (roles.BrightText, "highlighted"),
                      (roles.Highlight, "highlight"), (roles.HighlightedText, "highlighted"),
                      (roles.Link, "link"), (roles.LinkVisited, "link"), (roles.ToolTipBase, "tooltip"),
                      (roles.ToolTipText, "tooltip_text")):
        palette.setColor(role, c[key])
    placeholder = QColor(c["text"])
    placeholder.setAlpha(128)
    palette.setColor(roles.PlaceholderText, placeholder)
    # Reliefs des cadres et séparateurs : dérivés des boutons (thèmes clairs) ou couleur de cadre.
    if "frame" in c:
        for role in (roles.Light, roles.Midlight, roles.Mid, roles.Dark, roles.Shadow):
            palette.setColor(role, c["frame"])
    else:
        palette.setColor(roles.Light, c["button"].lighter(130))
        palette.setColor(roles.Midlight, c["button"].lighter(115))
        palette.setColor(roles.Mid, c["button"].darker(110))
        palette.setColor(roles.Dark, c["button"].darker(130))
        palette.setColor(roles.Shadow, QColor(0, 0, 0))
    darker = 160 if spec["dark"] else 130
    disabled = QPalette.ColorGroup.Disabled
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(disabled, role, c["disabled"])
    palette.setColor(disabled, roles.Highlight, c["button"].darker(darker))
    palette.setColor(disabled, roles.HighlightedText, c["disabled"])
    return palette


def _arrow_image(color, up):
    """Petite flèche (triangle) de la couleur donnée, dans le dossier temporaire ; chemin pour url()."""
    path = Path(tempfile.gettempdir()) / "splatgui-theme" / f"{'up' if up else 'down'}-{color.lstrip('#')}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(16, 10, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        points = [QPointF(1, 9), QPointF(15, 9), QPointF(8, 1)] if up else \
                 [QPointF(1, 1), QPointF(15, 1), QPointF(8, 9)]
        painter.drawPolygon(QPolygonF(points))
        painter.end()
        image.save(str(path))
    return path.as_posix()


def _indicator_style(spec):
    """Cases à cocher et boutons radio des thèmes sombres : Fusion trace leur contour d'après
    le fond de la fenêtre, invisible sur fond (presque) noir. Coché = pastille de sélection."""
    frame, base, mark = spec["frame"], spec["base"], spec["highlight"]
    return (
        "QCheckBox::indicator, QRadioButton::indicator, QGroupBox::indicator,"
        " QAbstractItemView::indicator { width: 12px; height: 12px;"
        f" border: 1px solid {frame}; background: {base}; }}"
        "QCheckBox::indicator, QGroupBox::indicator, QAbstractItemView::indicator { border-radius: 2px; }"
        "QRadioButton::indicator { border-radius: 7px; }"
        "QCheckBox::indicator:checked, QGroupBox::indicator:checked, QAbstractItemView::indicator:checked"
        f" {{ background: {mark}; }}"
        "QRadioButton::indicator:checked { background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,"
        f" fx:0.5, fy:0.5, stop:0 {mark}, stop:0.5 {mark}, stop:0.56 {base}, stop:1 {base}); }}"
        f"QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {spec['disabled']}; }}"
    )


def apply(name, app=None):
    """Applique le thème `name` (clé de THEMES ; inconnu = système). Renvoie la clé retenue."""
    app = app or QApplication.instance()
    name = name if name in THEMES else DEFAULT
    spec = THEMES[name]
    hints = app.styleHints()
    if name == "system":
        if hasattr(hints, "unsetColorScheme"):
            hints.unsetColorScheme()
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")
    else:
        if hasattr(hints, "setColorScheme"):     # Qt ≥ 6.8 : barres de titre, boîtes natives
            hints.setColorScheme(Qt.ColorScheme.Dark if spec["dark"] else Qt.ColorScheme.Light)
        app.setPalette(_palette(spec))
        style = _indicator_style(spec) if spec["dark"] else ""
        if spec["base"] == spec["window"]:
            # Champs du même noir que la fenêtre : bordure explicite pour les distinguer.
            frame, text = spec["frame"], spec["text"]
            style += (f"QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit {{ border: 1px solid {frame};"
                      " border-radius: 2px; padding: 1px 2px; }"
                      # Boutons des champs numériques : redessinés (flèches en triangles CSS).
                      "QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { width: 14px;"
                      f" border-left: 1px solid {frame}; background: {spec['button']}; }}"
                      f"QAbstractSpinBox::up-arrow {{ image: url({_arrow_image(text, up=True)}); width: 8px; }}"
                      f"QAbstractSpinBox::down-arrow {{ image: url({_arrow_image(text, up=False)}); width: 8px; }}")
        app.setStyleSheet(style)
    return name
