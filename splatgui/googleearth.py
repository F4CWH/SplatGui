"""Export Google Earth : archive .kmz (ou .kml + dossier d'images) de la carte SPLAT!.

Les cartes SPLAT! sont en « plate carrée » (lon/lat linéaires) : la couverture se pose telle
quelle en GroundOverlay, sans reprojection. Sites en Placemark (icônes de l'application),
calques GeoJSON en lignes, légende en ScreenOverlay.
"""

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
from PyQt6.QtGui import QColor, QImage, QPainter

from . import layout
from .i18n import tr

LEGEND_PX = 14


def _png(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def _kml_color(color, alpha=255):
    """Couleur KML : aabbggrr."""
    c = QColor(color)
    return f"{alpha:02x}{c.blue():02x}{c.green():02x}{c.red():02x}"


def legend_image(entries, px=LEGEND_PX):
    width, height = layout.legend_size(entries, px)
    image = QImage(int(width) + 2, int(height) + 2, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    layout.draw_legend(painter, entries, QRectF(0.5, 0.5, width, height), px)
    painter.end()
    return image


def _vector_folder(label, color, width, features, bounds):
    west, south, east, north = bounds
    style = f"<Style><LineStyle><color>{_kml_color(color)}</color><width>{width:g}</width></LineStyle></Style>"
    marks = []
    for name, kind, arrays, (x0, y0, x1, y1), _center, _area in features:
        if x1 < west or x0 > east or y1 < south or y0 > north or kind not in ("poly", "line"):
            continue
        lines = "".join("<LineString><tessellate>1</tessellate><coordinates>"
                        + " ".join(f"{float(x):.5f},{float(y):.5f}" for x, y in arr)
                        + "</coordinates></LineString>" for arr in arrays if len(arr) > 1)
        if lines:
            marks.append(f"<Placemark><name>{escape(name or '')}</name>{style}"
                         f"<MultiGeometry>{lines}</MultiGeometry></Placemark>")
    return f"<Folder><name>{escape(label)}</name>{''.join(marks)}</Folder>"


def export(path, coverage, ref, sites, icon_files, entries, coverage_opacity, title, vectors=(), view_ref=None):
    """Écrit `path` (.kmz, ou .kml avec ses images dans <nom>_fichiers/). `vectors` :
    [(libellé, couleur, épaisseur, entités)], limités à l'emprise `view_ref`. Renvoie le chemin."""
    path = Path(path)
    files = {}                                   # chemin relatif -> octets
    west, south, east, north = ref.bounds()
    files["couverture.png"] = _png(coverage)
    alpha = max(0, min(255, round(coverage_opacity * 255)))

    styles, marks = [], []
    for index, (site, icon) in enumerate(zip(sites, icon_files)):
        style_id = f"site{index}"
        if icon and Path(icon).exists():
            name = f"icones/{Path(icon).name}"
            files[name] = Path(icon).read_bytes()
            styles.append(f'<Style id="{style_id}"><IconStyle><scale>1.3</scale><Icon><href>{name}</href></Icon>'
                          f'<hotSpot x="0.5" y="0" xunits="fraction" yunits="fraction"/></IconStyle></Style>')
        else:
            styles.append(f'<Style id="{style_id}"/>')
        unit = site.get("height_unit", "m")
        height = site.get("height")
        role = tr("Récepteur") if site.get("role") == "rx" else tr("Émetteur")
        description = f"{role} — {site['lat']:.6f}, {site['lon']:.6f}"
        if height is not None:
            description += " — " + tr("antenne à {height:g} {unit}", height=height, unit=unit)
        marks.append(f"<Placemark><name>{escape(site['name'])}</name>"
                     f"<description>{escape(description)}</description><styleUrl>#{style_id}</styleUrl>"
                     f"<Point><coordinates>{site['lon']:.6f},{site['lat']:.6f},0</coordinates></Point></Placemark>")

    legend = ""
    if entries:
        files["legende.png"] = _png(legend_image(entries))
        legend = (f"<ScreenOverlay><name>{escape(tr('Légende'))}</name><Icon><href>legende.png</href></Icon>"
                  '<overlayXY x="0" y="0" xunits="fraction" yunits="fraction"/>'
                  '<screenXY x="10" y="30" xunits="pixels" yunits="pixels"/>'
                  '<size x="0" y="0" xunits="pixels" yunits="pixels"/></ScreenOverlay>')

    bounds = (view_ref or ref).bounds()
    folders = "".join(_vector_folder(label, color, width, features, bounds)
                      for label, color, width, features in vectors)
    v_west, v_south, v_east, v_north = bounds
    span_km = max(v_east - v_west, v_north - v_south) * 111.32
    doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
<name>{escape(title)}</name>
<LookAt><longitude>{(v_west + v_east) / 2:.6f}</longitude><latitude>{(v_south + v_north) / 2:.6f}</latitude>
<range>{span_km * 1500:.0f}</range><tilt>0</tilt><heading>0</heading></LookAt>
{''.join(styles)}
<GroundOverlay><name>{escape(tr('Couverture radio (SPLAT!)'))}</name><color>{alpha:02x}ffffff</color>
<Icon><href>couverture.png</href></Icon>
<LatLonBox><north>{north:.8f}</north><south>{south:.8f}</south><east>{east:.8f}</east><west>{west:.8f}</west></LatLonBox>
</GroundOverlay>
{folders}
<Folder><name>{escape(tr('Sites'))}</name>{''.join(marks)}</Folder>
{legend}
</Document>
</kml>
"""
    if path.suffix.lower() == ".kml":
        folder = path.with_name(path.stem + "_fichiers")
        for name, data in files.items():
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        prefix = folder.name + "/"
        for name in files:
            doc = doc.replace(f"<href>{name}</href>", f"<href>{prefix}{name}</href>")
        path.write_text(doc, encoding="utf-8")
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("doc.kml", doc)      # premier fichier : document principal
            for name, data in files.items():
                archive.writestr(name, data)
        path.write_bytes(buffer.getvalue())
    return path
