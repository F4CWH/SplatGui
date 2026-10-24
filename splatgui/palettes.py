"""Dégradés de couleurs d'altitude, partagés par le panorama et le schéma du profil."""

import numpy as np
from PyQt6.QtGui import QColor, QLinearGradient

from .i18n import N_

# nom -> (libellé, paliers (0 = plus bas, 1 = plus haut, couleur RGB)) ; "" = relief en gris
PALETTES = {
    "": (N_("Gris (plans)"), ()),
    "hypso": (N_("Hypsométrique"), ((0, (46, 110, 62)), (0.3, (126, 170, 86)), (0.55, (222, 206, 128)),
                                    (0.8, (168, 112, 64)), (1, (245, 242, 236)))),
    "terre": (N_("Terre"), ((0, (62, 92, 48)), (0.5, (150, 136, 84)), (1, (110, 78, 52)))),
    "viridis": (N_("Viridis"), ((0, (68, 1, 84)), (0.25, (59, 82, 139)), (0.5, (33, 145, 140)),
                                (0.75, (94, 201, 98)), (1, (253, 231, 37)))),
    "spectre": (N_("Spectre"), ((0, (48, 18, 59)), (0.25, (40, 150, 240)), (0.5, (60, 230, 120)),
                                (0.75, (250, 180, 40)), (1, (122, 4, 3)))),
}
DEFAULT_PALETTE = "hypso"


def colorize(t, name):
    """Couleurs RGB (tableau t.shape × 3, flottants) du dégradé `name` pour t entre 0 et 1."""
    stops = PALETTES[name][1]
    t = np.clip(t, 0, 1)
    return np.stack([np.interp(t, [s[0] for s in stops], [s[1][c] for s in stops]) for c in range(3)], axis=-1)


def gradient(name, x1, y1, x2, y2):
    """Dégradé Qt du bas (x1, y1) vers le haut (x2, y2) de la plage d'altitudes."""
    out = QLinearGradient(x1, y1, x2, y2)
    for position, rgb in PALETTES[name][1]:
        out.setColorAt(position, QColor(*rgb))
    return out
