"""Câbles coaxiaux : affaiblissement linéique typique (dB/100 m) en fonction de la fréquence.

Valeurs typiques de fiches constructeurs (à ±10 % selon le fabricant et le vieillissement) ;
le choix « Personnalisé » permet de saisir la valeur exacte d'un câble. Entre deux points,
l'interpolation est linéaire en log-log ; hors de la plage, l'affaiblissement suit √f
(pertes du conducteur dominantes).
"""

import math

CUSTOM = "custom"

# Nom → [(fréquence MHz, affaiblissement dB/100 m), …] par fréquence croissante.
CABLES = {
    "RG-58 C/U": [(10, 4.6), (50, 10.8), (100, 15.6), (200, 23.0), (400, 33.5), (1000, 56.0), (2400, 95.0)],
    "RG-8X (Mini-8)": [(10, 3.6), (50, 8.2), (100, 11.5), (200, 16.4), (400, 23.6), (1000, 39.4), (2400, 64.0)],
    "RG-213/U": [(10, 2.0), (50, 4.6), (100, 6.6), (200, 9.8), (400, 14.8), (1000, 26.2), (2400, 45.0)],
    "H155": [(10, 3.4), (100, 11.2), (145, 13.4), (435, 24.0), (1296, 44.0), (2400, 63.0)],
    "Aircell 7": [(10, 2.1), (100, 6.6), (145, 7.9), (435, 14.1), (1296, 26.1), (2400, 37.9)],
    "Ecoflex 10": [(10, 1.2), (100, 4.0), (145, 4.8), (435, 8.9), (1296, 16.0), (2400, 22.8)],
    "Ecoflex 15": [(10, 0.9), (100, 2.8), (145, 3.4), (435, 6.1), (1296, 11.0), (2400, 15.6)],
    "LMR-400": [(30, 2.2), (50, 2.9), (150, 5.0), (220, 6.1), (450, 8.9), (900, 12.8), (1500, 16.8), (2400, 21.7)],
    "LMR-600": [(30, 1.4), (50, 1.8), (150, 3.2), (220, 3.9), (450, 5.8), (900, 8.3), (1500, 10.9), (2400, 14.2)],
}


def attenuation(name, frequency_mhz):
    """Affaiblissement (dB/100 m) du câble `name` à `frequency_mhz` ; None si inconnu."""
    points = CABLES.get(name)
    if not points or frequency_mhz <= 0:
        return None
    if frequency_mhz <= points[0][0]:
        f0, a0 = points[0]
        return a0 * math.sqrt(frequency_mhz / f0)
    if frequency_mhz >= points[-1][0]:
        f0, a0 = points[-1]
        return a0 * math.sqrt(frequency_mhz / f0)
    for (f0, a0), (f1, a1) in zip(points, points[1:]):
        if f0 <= frequency_mhz <= f1:
            t = math.log(frequency_mhz / f0) / math.log(f1 / f0)
            return math.exp(math.log(a0) + t * (math.log(a1) - math.log(a0)))
    return None


def erp(power_w, gain_dbi, losses_db):
    """(PAR en W, PAR en dBm, PIRE en dBm) : PAR = P × 10^((G_dBi − 2,15 − pertes) / 10)."""
    eirp_dbm = 10 * math.log10(power_w * 1000) + gain_dbi - losses_db
    erp_dbm = eirp_dbm - 2.15
    return 10 ** ((erp_dbm - 30) / 10), erp_dbm, eirp_dbm
