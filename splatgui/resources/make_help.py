"""Régénère les pages de l'aide intégrée (splatgui/help/<langue>.html) à partir de la
documentation de SPLAT! (splatgui/help/splat_<langue>.pdf) :

- les titres de section (lignes en majuscules) deviennent des rubriques <h2> ;
- les lignes coupées par la justification sont recomposées en paragraphes, grâce à la position
  de chaque ligne dans la page ;
- les commandes, exemples de fichiers et tableaux sont gardés tels quels (<pre>) ;
- les adresses deviennent des liens, les options (-t, -metric…) sont mises en forme.

Les pages produites peuvent ensuite être retouchées à la main (chaque <h2 id="…"> ouvre une
rubrique du sommaire).

Utilisation, depuis la racine du projet :  python splatgui/resources/make_help.py [fr en es]
"""
import html
import re
import sys
import unicodedata
from pathlib import Path

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtPdf import QPdfDocument

HELP_DIR = Path(__file__).resolve().parents[1] / "help"
TITLES = {"fr": "SPLAT! — Manuel de l’utilisateur", "en": "SPLAT! — User manual",
          "es": "SPLAT! — Manual del usuario"}
# Sigles conservés en majuscules quand les titres passent en minuscules.
ACRONYMS = {"SPLAT", "QTH", "LRP", "ITM", "ITWOM", "LOS", "LDV", "KML", "ERP", "SNR", "SDF", "HD"}
PROPER = {"google": "Google"}
HEADER = re.compile(r"^(SPLAT!\(1\)|KD2BD Software \d)")
COMMAND = re.compile(r"^(splat(-hd)?|srtm2sdf(-hd)?|usgs2sdf|citydecoder|bearing|fontdata|postdownload|convert"
                     r"|wpng|bzip2|gzip|xastir)\s(?!.* — |\w+,)|^[$/~]\S*$")
# Mots collés par l'extraction du texte (espacement serré du PDF d'origine).
FIXES = {"invokesaline-of-sight": "invokes a line-of-sight", "takeafew": "take a few"}
OPTION_LINE = re.compile(r"^\[-|^-[A-Za-z]+\s")


def heading_title(line):
    """« FICHIERS DE DAT A SPLAT (QTH) » → « Fichiers de data SPLAT (QTH) »."""
    line = re.sub(r"\bDAT (A|OS)\b", r"DAT\1", line)        # espace parasite de l'extraction du texte

    def word(match):
        w = match.group(0)
        return w if w in ACRONYMS else PROPER.get(w.lower(), w.lower())
    text = re.sub(r"\w+", word, line)
    return text[:1].upper() + text[1:]


def is_heading(line, previous, following):
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3 or len(line) > 90 or not all(c.isupper() for c in letters):
        return False
    if line.startswith("[") or "=" in line or line.endswith("."):
        return False
    if following and re.fullmatch(r"[-\d.,\s]+", following):
        return False                                # nom du site d'un exemple de fichier .qth
    # Fin de la description d'une option coupée en fin de ligne (« -olditm … del nuevo » / « ITWOM »).
    return not re.match(r"-[A-Za-z]+\s", previous or "")


def clean(text):
    """Césures en fin de ligne (U+FFFE) : supprimées, sauf dans une adresse où c'est un tiret."""
    for wrong, right in FIXES.items():
        text = text.replace(wrong, right)
    return re.sub(r"\S*￾\S*",
                  lambda m: m.group(0).replace("￾", "-" if "/" in m.group(0) else ""), text)


def read_lines(pdf):
    """Lignes du texte (hors en-têtes et pieds de page) : (texte, gauche, droite)."""
    lines = []
    for page in range(pdf.pageCount()):
        text = pdf.getAllText(page).text()
        for m in re.finditer(r"[^\r\n]+", text):
            line = m.group(0).strip()
            if not line or HEADER.match(line):
                continue
            rect = pdf.getSelectionAtIndex(page, m.start(), len(m.group(0))).boundingRectangle()
            lines.append((clean(line), rect.left(), rect.right()))
    return lines


def mode(values):
    counts = {}
    for v in values:
        counts[round(v)] = counts.get(round(v), 0) + 1
    return max(counts, key=counts.get)


def inline(text):
    """Texte d'un paragraphe : liens, options et nom du programme mis en forme."""
    parts = re.split(r"(https?://\S+?)(?=[\s,;)]|\.(?:\s|$)|$)", text)
    out = []
    for n, part in enumerate(parts):
        if n % 2:
            out.append(f'<a href="{html.escape(part)}">{html.escape(part)}</a>')
            continue
        part = html.escape(part, quote=False)
        part = re.sub(r"(?<![\w/.\-–])(-[A-Za-z]{1,6})\b(?![-’'])", r"<code>\1</code>", part)
        part = part.replace("SPLAT!", "<b>SPLAT!</b>")
        out.append(part)
    return "".join(out)


def slug(title, used):
    ascii_title = unicodedata.normalize("NFKD", title.lower()).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-") or "section"
    name, n = base, 2
    while name in used:
        name, n = f"{base}-{n}", n + 1
    used.add(name)
    return name


def convert(lang):
    pdf = QPdfDocument(None)
    pdf.load(str(HELP_DIR / f"splat_{lang}.pdf"))
    lines = read_lines(pdf)
    right = mode(r for _, _, r in lines if r > 400)                  # marge droite du texte justifié
    body = mode(l for t, l, r in lines if r >= right - 12)          # retrait des paragraphes
    wrapped = lambda r: r >= right - 12

    sections, blocks = [], None       # sections : (titre, blocs) ; bloc : [type, lignes]
    previous = None                   # (texte, gauche, droite) de la ligne précédente
    for n, (text, left, r) in enumerate(lines):
        following = lines[n + 1][0] if n + 1 < len(lines) else None
        if left < body - 15 and is_heading(text, previous and previous[0], following):
            blocks = []
            sections.append((heading_title(text), blocks))
            previous = None
            continue
        if blocks is None:                                          # avant le premier titre
            previous = (text, left, r)
            continue
        code = bool(COMMAND.match(text)) or bool(OPTION_LINE.match(text)) or left > body + 8
        if previous and wrapped(previous[2]) and blocks and blocks[-1][0] == "p" and not COMMAND.match(text):
            blocks[-1][1].append(text)                              # suite d'une ligne justifiée
        elif blocks and blocks[-1][0] == "pre" and blocks[-1][1][-1].count("[") > blocks[-1][1][-1].count("]"):
            blocks[-1][1][-1] += " " + text                         # option du synopsis sur deux lignes
        elif (blocks and blocks[-1][0] == "pre" and previous and previous[2] >= right - 60 and len(text) < 40
              and not code and not text.endswith((".", ":"))):
            blocks[-1][1][-1] += " " + text                         # fin d'une ligne d'exemple trop longue
        elif code:
            if blocks and blocks[-1][0] == "pre":
                blocks[-1][1].append(text)
            else:
                blocks.append(["pre", [text]])
        else:
            blocks.append(["p", [text]])
        previous = (text, left, r)

    used = set()
    out = [f'<!DOCTYPE html>\n<html lang="{lang}">\n<head><meta charset="utf-8"><title>{html.escape(TITLES[lang])}'
           "</title></head>\n<body>\n"
           "<!-- Généré par splatgui/resources/make_help.py à partir de splat_" + lang + ".pdf ; "
           "chaque <h2> ouvre une rubrique du sommaire. -->\n"]
    for title, blocks in sections:
        out.append(f'\n<h2 id="{slug(title, used)}">{html.escape(title)}</h2>\n')
        for kind, block in blocks:
            if kind == "pre" and all(re.fullmatch(r"https?://\S+", line) for line in block):
                out.extend(f"<p>{inline(line)}</p>\n" for line in block)     # adresse seule : lien
            elif kind == "pre":
                out.append("<pre>" + html.escape("\n".join(block), quote=False) + "</pre>\n")
            else:
                text = re.sub(r"\s+", " ", " ".join(block)).strip()
                out.append(f"<p>{inline(text)}</p>\n")
    out.append("</body>\n</html>\n")
    path = HELP_DIR / f"{lang}.html"
    path.write_text("".join(out), encoding="utf-8")
    print(f"{path.name} : {len(sections)} rubriques")


if __name__ == "__main__":
    app = QGuiApplication(sys.argv[:1])
    for code in sys.argv[1:] or ["fr", "en", "es"]:
        convert(code)
