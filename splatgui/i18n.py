"""Gestion multilingue : le français est la langue source, les autres langues sont des
catalogues JSON (splatgui/locales/<code>.json) qui associent chaque texte français à sa
traduction. Un texte absent du catalogue reste en français.

    tr("Prêt")                                   → "Ready" en anglais
    tr("{n} répertoire(s) créé(s)", n=3)         → "3 folder(s) created"

La langue est choisie au démarrage (settings["language"], sinon celle du système) ; un
changement est pris en compte au redémarrage suivant.
"""

import json
from pathlib import Path

from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator

LOCALES_DIR = Path(__file__).parent / "locales"
SOURCE = "fr"
LANGUAGES = {"fr": "Français", "en": "English", "es": "Español"}

_catalog = {}
_language = SOURCE
_qt_translators = []


def system_language():
    code = QLocale.system().name().split("_")[0]
    return code if code in LANGUAGES else "en"


def language():
    return _language


def set_language(code, app=None):
    """Charge le catalogue de `code` ("" = langue du système) et les traductions de Qt
    (boutons standard : Oui, Non, Annuler…). Renvoie la langue retenue."""
    global _catalog, _language
    code = code or system_language()
    if code not in LANGUAGES:
        code = SOURCE
    _language = code
    _catalog = {}
    if code != SOURCE:
        try:
            _catalog = json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _catalog = {}
    if app is not None:
        for translator in _qt_translators:
            app.removeTranslator(translator)
        _qt_translators.clear()
        folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        translator = QTranslator(app)
        if translator.load(f"qtbase_{code}", folder):
            app.installTranslator(translator)
            _qt_translators.append(translator)
    return code


def N_(text):
    """Marque un texte à traduire sans le traduire (constantes définies avant le choix de la
    langue) : il est traduit à l'affichage par tr()."""
    return text


def tr(text, /, **values):
    """Traduction de `text` (texte français), puis remplacement des {champs} par `values`
    (positionnel seul : un champ peut s'appeler « text »)."""
    translated = _catalog.get(text) or text
    return translated.format(**values) if values else translated
