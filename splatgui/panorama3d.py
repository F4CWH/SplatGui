"""Panorama en 3D (Qt Quick 3D, rendu Direct3D 11 sous Windows) : relief maillé autour du site,
vu depuis le haut de l'antenne, avec la couverture radio de SPLAT! plaquée sur le relief.

Maillage : grille rectiligne centrée sur le site, de demi-côté égal au rayon du panorama, plus
fine près du site (pas d'une vingtaine de mètres) qu'au bord (deux cents mètres environ pour
60 km). Les altitudes sont comptées depuis l'œil et abaissées de d² / (2 k R) (courbure terrestre
et réfraction, comme le panorama 2D) ; l'exagération du relief est une simple échelle verticale du
modèle, l'œil étant à l'origine.

Repère de la scène : x vers l'est, y vers le haut, z vers le sud (la caméra de Qt regarde vers -z,
donc vers le nord à l'azimut 0). Caméra au sommet de l'antenne.

Texture plaquée : dégradé d'altitude ou carte (tuiles en cache), couverture radio par-dessus.

PyQt6 plante quand QML instancie lui-même une sous-classe Python de QQuick3DGeometry : la
géométrie est créée en Python puis affectée au modèle de la scène.
"""

import atexit
import math
import shutil
import tempfile
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QByteArray, QObject, QPointF, QRectF, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPalette, QPen, QPolygonF,
                         QVector3D)
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from . import basemap
from .i18n import tr
from .linkprofile import EARTH_RADIUS_M, units
from .palettes import colorize
from .panorama import CARDINALS, MAX_SIGNS, SKY, _mix, ground_bilinear, population

MESH_SIZE = 769            # sommets par côté de la grille
MESH_LINEAR = 0.12         # part linéaire de la répartition des sommets (le reste croît comme u²)
TEXTURE_SIZE = 3072        # côté de la texture plaquée (pixels)
TEXTURE_LINEAR = 0.35      # part linéaire de la répartition des pixels de la texture (plus fins près du site)
MAX_FOV = 120.0            # champ de vision horizontal maximal de la caméra (degrés)
MIN_FOV = 15.0
NEUTRAL = (196, 200, 186)  # relief sans dégradé d'altitude
COVERAGE_OPACITY = 0.8
DRAPE_NEAR = 0.15          # abords du site couverts par la carte plaquée détaillée (part du rayon)

QML = """
import QtQuick
import QtQuick3D

Item {
    id: root
    focus: true
    property real yaw: 0              // azimut au centre de la vue (degrés)
    property real pitch: 0            // angle de vue (degrés, + = vers le haut)
    property real fov: 90             // champ de vision horizontal (degrés)
    property real minFov: 15
    property real maxFov: 120
    property real extent: 50000       // demi-côté du relief maillé (m)
    property real exaggeration: 1
    property url textureSource: ""
    property bool hasTerrain: false
    property real hazeDensity: 0
    property color skyTop: "#5c92d4"
    property color skyHorizon: "#d6e6f5"

    function changed() { bridge.view(yaw, pitch, fov) }
    function clampPitch(p) { return Math.max(-60, Math.min(60, p)) }
    function turn(dyaw, dpitch) {
        yaw = ((yaw + dyaw) % 360 + 360) % 360
        pitch = clampPitch(pitch + dpitch)
        changed()
    }
    function zoom(factor) {
        fov = Math.max(minFov, Math.min(maxFov, fov * factor))
        changed()
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: root.skyTop }
            GradientStop { position: 1.0; color: root.skyHorizon }
        }
    }

    View3D {
        id: view
        anchors.fill: parent
        camera: camera
        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Transparent
            antialiasingMode: SceneEnvironment.MSAA
            antialiasingQuality: SceneEnvironment.High
            fog: Fog {
                enabled: root.hazeDensity > 0
                color: root.skyHorizon
                density: root.hazeDensity
                depthEnabled: true
                depthNear: root.extent * 0.02
                depthFar: root.extent
                depthCurve: 0.8
            }
        }
        // Caméra au sommet de l'antenne (origine de la scène)
        PerspectiveCamera {
            id: camera
            position: Qt.vector3d(0, 0, 0)
            eulerRotation: Qt.vector3d(root.pitch, -root.yaw, 0)
            fieldOfView: root.fov
            fieldOfViewOrientation: PerspectiveCamera.Horizontal
            clipNear: 2
            clipFar: root.extent * 1.6
        }
        // Soleil au nord-ouest, comme l'ombrage des cartes, et lumière d'appoint zénithale
        DirectionalLight { eulerRotation: Qt.vector3d(-40, 45, 0); brightness: 1.0 }
        DirectionalLight { eulerRotation: Qt.vector3d(-90, 0, 0); brightness: 0.45 }
        Model {
            objectName: "terrain"
            visible: root.hasTerrain
            pickable: true
            scale: Qt.vector3d(1, root.exaggeration, 1)
            materials: PrincipledMaterial {
                baseColorMap: Texture {
                    source: root.textureSource
                    generateMipmaps: true
                    mipFilter: Texture.Linear
                }
                roughness: 1.0
                metalness: 0.0
                specularAmount: 0.0
                cullMode: Material.NoCulling
            }
        }
    }

    DragHandler {
        id: drag
        target: null
        property real yaw0
        property real pitch0
        onActiveChanged: if (active) { yaw0 = root.yaw; pitch0 = root.pitch; root.forceActiveFocus() }
        onTranslationChanged: {
            var k = root.fov / Math.max(1, root.width)
            root.yaw = ((yaw0 - translation.x * k) % 360 + 360) % 360
            root.pitch = root.clampPitch(pitch0 + translation.y * k)
            root.changed()
        }
    }
    WheelHandler {
        acceptedModifiers: Qt.NoModifier
        onWheel: (event) => root.zoom(Math.pow(0.9, event.angleDelta.y / 120))
    }
    WheelHandler {
        acceptedModifiers: Qt.ShiftModifier
        onWheel: (event) => root.turn(0, event.angleDelta.y / 120 * 2)
    }
    HoverHandler {
        id: hover
        onPointChanged: pickTimer.restart()
        onHoveredChanged: if (!hovered) bridge.hover(0, 0, 0, false)
    }
    Timer {
        id: pickTimer
        interval: 40
        onTriggered: {
            var hit = view.pick(hover.point.position.x, hover.point.position.y)
            if (hit.objectHit)
                bridge.hover(hit.scenePosition.x, hit.scenePosition.y, hit.scenePosition.z, true)
            else
                bridge.hover(0, 0, 0, false)
        }
    }
    Keys.onPressed: (event) => {
        var step = root.fov / 12
        if (event.key === Qt.Key_Left) root.turn(-step, 0)
        else if (event.key === Qt.Key_Right) root.turn(step, 0)
        else if (event.key === Qt.Key_PageUp || event.key === Qt.Key_Up) root.turn(0, 2)
        else if (event.key === Qt.Key_PageDown || event.key === Qt.Key_Down) root.turn(0, -2)
        else if (event.key === Qt.Key_Plus || event.key === Qt.Key_Equal) root.zoom(0.9)
        else if (event.key === Qt.Key_Minus) root.zoom(1 / 0.9)
        else if (event.key === Qt.Key_Home) bridge.home()
        else return
        event.accepted = true
    }
}
"""

_work_dir = None


def _work():
    """Dossier temporaire de la session (scène QML, textures), supprimé à la sortie."""
    global _work_dir
    if _work_dir is None:
        _work_dir = Path(tempfile.mkdtemp(prefix="splatgui3d_"))
        atexit.register(shutil.rmtree, _work_dir, True)
    return _work_dir


# --- Maillage et texture (calculs en arrière-plan) -------------------------------------------

def _stretch(half, u, linear):
    """Coordonnée (m) le long d'un axe pour u de 0 à 1 (de -half à +half), resserrée au centre :
    part `linear` constante, le reste croissant comme u². Sert aux sommets du maillage
    (MESH_LINEAR) et aux pixels de la texture (TEXTURE_LINEAR, plus fins près du site qu'au bord)."""
    s = 2.0 * np.asarray(u, dtype=float) - 1.0
    return half * s * (linear + (1 - linear) * np.abs(s))


def _unstretch(half, x, linear):
    """Inverse de _stretch : u (0 à 1) de la coordonnée x (m)."""
    a = np.abs(np.asarray(x, dtype=float)) / half
    s = (-linear + np.sqrt(linear ** 2 + 4 * (1 - linear) * a)) / (2 * (1 - linear))
    return (np.sign(x) * s + 1.0) / 2.0


def _axis(half, size=MESH_SIZE):
    """Coordonnées (m) des sommets le long d'un axe, de -half à +half, resserrées au centre."""
    return _stretch(half, np.linspace(0.0, 1.0, size), MESH_LINEAR)


def _lat_lon(lat, lon, east, north):
    """Position (degrés) des points à (east, north) mètres du site (projection locale)."""
    return (lat + north / 110540.0,
            lon + east / (111320.0 * max(math.cos(math.radians(lat)), 0.01)))


def build_mesh(site, site_ground, sdf_dirs, hd, radius_km, height_m, k):
    """Maillage du relief autour du site : sommets (position, normale, coordonnées de texture)
    et triangles prêts pour la carte graphique, plus les altitudes de la grille pour la texture."""
    lat, lon = float(site["lat"]), float(site["lon"])
    half = radius_km * 1000.0
    axis = _axis(half)
    east, north = np.meshgrid(axis, axis[::-1])           # rangée 0 au nord
    lats, lons = _lat_lon(lat, lon, east, north)
    ground = ground_bilinear(lats, lons, sdf_dirs, hd, missing=np.nan)
    if np.isnan(ground).all():
        raise ValueError(tr("aucun relief SDF autour du site"))
    ground = np.where(np.isnan(ground), np.nanmin(ground), ground)
    eye = site_ground + height_m
    drop = (east ** 2 + north ** 2) / (2 * k * EARTH_RADIUS_M)
    y = ground - eye - drop
    # Normales (repère x est, y haut, z sud) d'après les pentes de la grille non uniforme
    dy_dnorth, dy_deast = np.gradient(y, axis[::-1], axis)
    normals = np.stack([-dy_deast, np.ones_like(y), dy_dnorth], axis=-1)
    normals /= np.linalg.norm(normals, axis=-1, keepdims=True)
    rank = _unstretch(half, axis, TEXTURE_LINEAR)
    uv = np.stack(np.meshgrid(rank, rank[::-1]), axis=-1)  # v croît vers le nord
    vertices = np.concatenate([np.stack([east, y, -north], axis=-1), normals, uv], axis=-1)
    n = axis.size
    i = np.arange(n * n, dtype=np.uint32).reshape(n, n)
    a, b, c, d = i[:-1, :-1], i[:-1, 1:], i[1:, :-1], i[1:, 1:]
    indices = np.stack([a, c, b, b, c, d], axis=-1).reshape(-1)
    return {"vertices": vertices.astype(np.float32).reshape(-1, 8), "indices": indices,
            "bounds": ((-half, float(y.min()), -half), (half, float(y.max()), half)),
            "axis": axis, "ground": ground, "half": half, "lat": lat, "lon": lon,
            "eye": eye, "k": k}


def _resample(mesh, size):
    """Altitudes de la grille du maillage rééchantillonnées aux pixels de la texture (size × size,
    rangée 0 au nord, répartis comme les sommets), par interpolation bilinéaire le long des deux
    axes ; et coordonnées (m) des pixels le long d'un axe."""
    axis, ground, half = mesh["axis"], mesh["ground"], mesh["half"]
    regular = _stretch(half, (np.arange(size) + 0.5) / size, TEXTURE_LINEAR)
    col = np.interp(regular, axis, np.arange(axis.size))
    row = axis.size - 1 - np.interp(regular[::-1], axis, np.arange(axis.size))   # rangée 0 au nord
    c0 = np.clip(np.floor(col).astype(int), 0, axis.size - 2)
    r0 = np.clip(np.floor(row).astype(int), 0, axis.size - 2)
    fc, fr = (col - c0)[None, :], (row - r0)[:, None]
    g = ground
    return (g[r0][:, c0] * (1 - fr) * (1 - fc) + g[r0][:, c0 + 1] * (1 - fr) * fc
            + g[r0 + 1][:, c0] * fr * (1 - fc) + g[r0 + 1][:, c0 + 1] * fr * fc), regular


def _sample(layer, ref, lats, lons):
    """Pixels (B, G, R, A) d'une image calée aux positions données, et masque des positions
    couvertes par l'image."""
    arr = basemap._rgba_view(layer.convertToFormat(QImage.Format.Format_ARGB32))
    dx, dy = ref.pixel_size()
    cols = np.rint((lons - ref.lon0) / dx).astype(int)
    rows = np.rint((lats - ref.lat0) / dy).astype(int)
    valid = (cols >= 0) & (cols < ref.width) & (rows >= 0) & (rows < ref.height)
    return arr[np.clip(rows, 0, ref.height - 1), np.clip(cols, 0, ref.width - 1)], valid


def drape_for(mesh, source_key, size=TEXTURE_SIZE):
    """[(carte `source_key`, GeoRef)] : toute l'emprise du maillage, puis les abords du site
    (DRAPE_NEAR du rayon) plus détaillés, pour la texture plus fine près du site ; tuiles
    téléchargées et mises en cache."""
    layers = []
    for part in (1.0, DRAPE_NEAR):
        half = mesh["half"] * part
        lat0, lon0 = _lat_lon(mesh["lat"], mesh["lon"], -half, half)
        lat1, lon1 = _lat_lon(mesh["lat"], mesh["lon"], half, -half)
        ref = basemap.GeoRef(lon0, lat0, lon1, lat1, size, size)
        layers.append((basemap.build_basemap(source_key, ref), ref))
    return layers


def build_texture(mesh, palette, coverage=None, drape=None, size=TEXTURE_SIZE):
    """Texture RGBA (size × size, rangée 0 au nord) : carte plaquée, sinon dégradé d'altitude
    (ou teinte neutre), et couverture radio par-dessus. `coverage` : (calque de couverture QImage,
    GeoRef) ou None ; `drape` : [(carte QImage, GeoRef)] de drape_for
    (la plus détaillée en dernier), ou None."""
    ground, regular = _resample(mesh, size)
    east, north = np.meshgrid(regular, regular[::-1])
    lats, lons = _lat_lon(mesh["lat"], mesh["lon"], east, north)
    if drape:
        rgb = np.zeros(lats.shape + (3,), dtype=np.float32)
        for layer, ref in drape:
            px, valid = _sample(layer, ref, lats, lons)
            rgb[valid] = px[valid][:, 2::-1]
    elif palette:
        inside = np.hypot(*np.meshgrid(regular, regular)) <= mesh["half"]
        low, high = float(ground[inside].min()), float(ground[inside].max())
        rgb = colorize((ground - low) / max(high - low, 1.0), palette)
    else:
        rgb = np.broadcast_to(np.array(NEUTRAL, dtype=float), ground.shape + (3,)).copy()
    if coverage is not None:
        px, valid = _sample(*coverage, lats, lons)
        alpha = np.where(valid, px[..., 3] / 255.0 * COVERAGE_OPACITY, 0.0)[..., None]
        rgb = rgb * (1 - alpha) + px[..., 2::-1].astype(float) * alpha
    out = np.empty((size, size, 4), dtype=np.uint8)
    out[..., :3] = np.clip(rgb, 0, 255)
    out[..., 3] = 255
    return out


# --- Vue ---------------------------------------------------------------------------------------

class _Bridge(QObject):
    """Appels de la scène QML vers Python."""
    viewChanged = pyqtSignal(float, float, float)
    hovered = pyqtSignal(float, float, float, bool)
    homeRequested = pyqtSignal()

    @pyqtSlot()
    def home(self):
        self.homeRequested.emit()

    @pyqtSlot(float, float, float)
    def view(self, yaw, pitch, fov):
        self.viewChanged.emit(yaw, pitch, fov)

    @pyqtSlot(float, float, float, bool)
    def hover(self, x, y, z, hit):
        self.hovered.emit(x, y, z, hit)


class Panorama3DView(QWidget):
    """Panorama en 3D. Lève RuntimeError si Qt Quick 3D n'est pas utilisable ; `failed` est émis
    si le rendu échoue ensuite (carte graphique)."""

    viewChanged = pyqtSignal(float, float)   # azimut au centre, champ de vision
    hovered = pyqtSignal(str)
    cursorMoved = pyqtSignal(object)         # azimut survolé (degrés) ou None : curseur de la carte
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtQuick3D import QQuick3DGeometry
        from PyQt6.QtQuickWidgets import QQuickWidget
        self._geometry_class = QQuick3DGeometry
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.quick = QQuickWidget(self)
        self.quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.quick.setMinimumHeight(240)
        self.bridge = _Bridge(self)
        self.bridge.viewChanged.connect(lambda yaw, _pitch, fov: self.viewChanged.emit(yaw, fov))
        self.labels = _Labels(self)
        self.bridge.viewChanged.connect(lambda *_args: self.labels.update())
        self.bridge.hovered.connect(self._hover)
        self.bridge.homeRequested.connect(self.home)
        self.quick.rootContext().setContextProperty("bridge", self.bridge)
        self.quick.sceneGraphError.connect(lambda _error, message: self.failed.emit(message))
        qml = _work() / "panorama3d.qml"
        qml.write_text(QML, encoding="utf-8")
        self.quick.setSource(QUrl.fromLocalFile(str(qml)))
        if self.quick.status() != QQuickWidget.Status.Ready:
            raise RuntimeError("; ".join(e.toString() for e in self.quick.errors()) or "Qt Quick 3D")
        layout.addWidget(self.quick)
        self.root = self.quick.rootObject()
        model = self.root.findChild(QObject, "terrain")
        self.geometry3d = QQuick3DGeometry()
        self.geometry3d.setParent(model)
        model.setProperty("geometry", self.geometry3d)
        self.labels.raise_()
        self.mesh = None
        self.metric = True
        self._texture_files = []
        self._texture_count = 0
        self.set_theme(self.palette().color(QPalette.ColorRole.Window).lightness() < 128)

    # Réglages ---------------------------------------------------------------------------------

    def set_theme(self, dark):
        top, horizon = SKY[1 if dark else 0]
        self.root.setProperty("skyTop", top)
        self.root.setProperty("skyHorizon", horizon)

    def set_mesh(self, mesh, metric=True):
        """Affiche un maillage de build_mesh (None : vue vide)."""
        self.mesh, self.metric = mesh, metric
        g = self.geometry3d
        g.clear()
        if mesh is None:
            self.root.setProperty("hasTerrain", False)
            g.update()
            return
        A = self._geometry_class.Attribute
        g.setVertexData(QByteArray(mesh["vertices"].tobytes()))
        g.setIndexData(QByteArray(mesh["indices"].tobytes()))
        g.setStride(32)
        g.setPrimitiveType(self._geometry_class.PrimitiveType.Triangles)
        g.addAttribute(A.Semantic.PositionSemantic, 0, A.ComponentType.F32Type)
        g.addAttribute(A.Semantic.NormalSemantic, 12, A.ComponentType.F32Type)
        g.addAttribute(A.Semantic.TexCoord0Semantic, 24, A.ComponentType.F32Type)
        g.addAttribute(A.Semantic.IndexSemantic, 0, A.ComponentType.U32Type)
        low, high = mesh["bounds"]
        g.setBounds(QVector3D(*low), QVector3D(*high))
        g.update()
        self.root.setProperty("extent", mesh["half"])
        self.root.setProperty("hasTerrain", True)
        self.labels.update()

    def set_texture(self, rgba):
        """Texture plaquée (tableau RGBA, rangée 0 au nord)."""
        h, w = rgba.shape[:2]
        image = QImage(np.ascontiguousarray(rgba).data, w, h, 4 * w, QImage.Format.Format_RGBA8888)
        self._texture_count += 1
        path = _work() / f"texture_{self._texture_count}.png"
        image.save(str(path))
        self.root.setProperty("textureSource", QUrl.fromLocalFile(str(path)))
        self._texture_files.append(path)
        while len(self._texture_files) > 2:               # la précédente peut être encore en chargement
            self._texture_files.pop(0).unlink(missing_ok=True)

    def set_exaggeration(self, factor):
        self.root.setProperty("exaggeration", float(factor or 1.0))
        self.labels.update()

    def set_places_source(self, view2d):
        """Panorama 2D dont les localités et leurs réglages (affichage, seuil d'habitants) sont
        repris sur la vue 3D."""
        self.labels.source = view2d
        self.labels.update()

    def set_haze(self, strength):
        """Brume : part de la couleur de l'horizon au bord du rayon (0 = aucune)."""
        self.root.setProperty("hazeDensity", float(strength))

    def home(self):
        """Vue de départ (touche Origine) : horizontale, champ le plus large."""
        self.root.setProperty("pitch", 0.0)
        self.root.setProperty("fov", MAX_FOV)
        self.labels.update()
        yaw, fov, pitch = self.view()
        self.bridge.view(yaw, pitch, fov)

    def set_view(self, yaw=None, fov=None, pitch=None):
        if yaw is not None:
            self.root.setProperty("yaw", float(yaw) % 360)
        if fov is not None:
            self.root.setProperty("fov", min(MAX_FOV, max(MIN_FOV, float(fov))))
        if pitch is not None:
            self.root.setProperty("pitch", float(pitch))
        self.labels.update()

    def view(self):
        """(azimut au centre, champ de vision, angle de vue)."""
        return (float(self.root.property("yaw")), float(self.root.property("fov")),
                float(self.root.property("pitch")))

    def image(self):
        """Vue rendue avec les noms des localités (export PNG)."""
        image = self.quick.grabFramebuffer()
        image.setDevicePixelRatio(image.width() / max(1, self.quick.width()))   # rendu en pixels physiques
        painter = QPainter(image)
        self.labels.render(painter, flags=QWidget.RenderFlag.DrawChildren)
        painter.end()
        return image

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.labels.setGeometry(self.quick.geometry())

    def projector(self):
        """Fonction project(azimut, distance, hauteur) -> point à l'écran (pixels logiques) ou
        None derrière la caméra, pour un point à `distance` m du site et `hauteur` m sous ou sur
        l'œil (sans exagération, appliquée ici)."""
        yaw, fov, pitch = self.view()
        exaggeration = float(self.root.property("exaggeration")) or 1.0
        w, h = self.quick.width(), self.quick.height()
        focal = w / 2 / math.tan(math.radians(fov) / 2)
        cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))

        def project(azimuth, distance, height):
            rel = math.radians(azimuth - yaw)
            x, ahead, up = distance * math.sin(rel), distance * math.cos(rel), height * exaggeration
            y, z = up * cp - ahead * sp, ahead * cp + up * sp
            return QPointF(w / 2 + focal * x / z, h / 2 - focal * y / z) if z > 1e-3 * max(1.0, distance) else None

        return project

    # Survol -------------------------------------------------------------------------------

    def _hover(self, x, y, z, hit):
        if not hit or self.mesh is None:
            self.cursorMoved.emit(None)
            self.hovered.emit("")
            return
        exaggeration = float(self.root.property("exaggeration")) or 1.0
        distance = math.hypot(x, z)
        azimuth = math.degrees(math.atan2(x, -z)) % 360
        self.cursorMoved.emit(azimuth)
        drop = distance ** 2 / (2 * self.mesh["k"] * EARTH_RADIUS_M)
        altitude = y / exaggeration + self.mesh["eye"] + drop
        h_factor, h_unit, d_factor, d_unit = units({"metric": self.metric})
        self.hovered.emit(tr("azimut {az:.1f}° — distance {d:.1f} {du} — altitude {alt:.0f} {hu}",
                             az=azimuth, d=distance / d_factor, du=d_unit, alt=altitude / h_factor, hu=h_unit))


class _Labels(QWidget):
    """Noms des localités et repères des autres sites par-dessus la vue 3D, avec les règles du
    panorama 2D : mêmes listes et même seuil d'habitants, repères placés en premier, puis villes
    et localités les plus peuplées ; étiquette suspendue au-dessus de l'horizon, reliée au sol par
    un pointillé, taille et transparence selon la distance. Les repères ont leur mât (du sol au
    sommet de l'antenne), en vert si le sommet est visible depuis le site, en rouge s'il est masqué
    par le relief ; un lieu masqué a un pied creux et son pointillé s'arrête sur la crête. Curseur
    de l'azimut survolé (vue 3D ou carte), comme le panorama 2D."""

    SITE_COLOR = QColor(52, 120, 80)
    HIDDEN_COLOR = QColor(176, 64, 52)

    def __init__(self, view3d):
        super().__init__(view3d)
        self.view3d = view3d
        self.source = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event):
        v2, v3 = self.source, self.view3d
        if v2 is None or not v2._view or v3.mesh is None or not v3.root.property("hasTerrain"):
            return
        self.project = v3.projector()
        self.frame = QRectF(self.rect())
        self.lh = QFontMetricsF(self.font()).height()
        self.eye = v2.data["site_ground"] + v2.height_m
        placed = []
        markers, masts = self._layout_markers(placed)
        signs = self._layout_places(placed) if v2.show_places and v2.places else []
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = v2.palette().color(QPalette.ColorRole.Base)
        bottom = self._paint_azimuth_scale(painter, base)
        self._paint_elevation_scale(painter, base, bottom)
        v2._paint_place_signs(painter, signs, base, self.lh)
        for x, low, high, hidden in masts:            # antennes des repères
            painter.setPen(QPen(self.HIDDEN_COLOR if hidden else self.SITE_COLOR, 2))
            painter.drawLine(QPointF(x, low), QPointF(x, high))
        v2._draw_markers(painter, markers, base, self.lh)
        self._paint_cursor(painter, bottom)
        painter.end()

    def _paint_cursor(self, painter, bottom):
        """Azimut survolé (vue 3D ou carte) : trait vertical et point sur l'horizon visible."""
        v2 = self.source
        if v2.cursor_az is None:
            return
        horizon = float(np.interp(v2.cursor_az, v2.data["azimuth"], v2._view[3], period=360))
        point = self.project(v2.cursor_az, 1.0, math.tan(math.radians(horizon)))
        if point is None or not self.frame.left() <= point.x() <= self.frame.right():
            return
        highlight = v2.palette().color(QPalette.ColorRole.Highlight)
        painter.setOpacity(1.0)
        painter.setPen(QPen(highlight, 1))
        painter.drawLine(QPointF(point.x(), self.frame.top()), QPointF(point.x(), bottom))
        painter.setBrush(highlight)
        painter.drawEllipse(point, 3.5, 3.5)

    def _paint_elevation_scale(self, painter, base, bottom):
        """Vue depuis le site, comme le panorama 2D : horizontale de l'observateur (angle de site
        nul) en pointillés et graduation des angles de site (degrés, vrais angles : l'exagération
        du relief est prise en compte) dans une bande à gauche. Graduation exacte au centre de la
        vue (azimut visé) ; ailleurs, l'écart reste faible tant que l'angle de vue est modéré."""
        v3, lh, frame = self.view3d, self.lh, self.frame
        _yaw, fov, pitch = v3.view()
        exaggeration = float(v3.root.property("exaggeration")) or 1.0
        focal = frame.width() / 2 / math.tan(math.radians(fov) / 2)
        p = math.radians(pitch)

        def y_of(angle):            # ordonnée d'un angle de site au centre de la vue
            scene = math.atan(math.tan(math.radians(angle)) * exaggeration)
            return frame.height() / 2 - focal * math.tan(scene - p)

        def angle_at(y):
            scene = p + math.atan((frame.height() / 2 - y) / focal)
            return math.degrees(math.atan(math.tan(scene) / exaggeration))

        text = self.source.palette().color(QPalette.ColorRole.Text)
        painter.setOpacity(1.0)
        # Horizontale de l'observateur
        painter.setPen(QPen(QColor(104, 120, 142), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(frame.left(), y_of(0.0)), QPointF(frame.right(), y_of(0.0)))
        # Graduation : pas tel que deux valeurs soient séparées d'au moins 1,6 ligne
        fm = QFontMetricsF(self.font())
        band = QRectF(0, 0, fm.horizontalAdvance("-00.00°") + lh * 0.9, bottom)
        fill = QColor(base)
        fill.setAlpha(170)
        painter.fillRect(band, fill)
        low, high = angle_at(bottom - lh / 2), angle_at(frame.top() + lh / 2)
        tick = next((t for t in (0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 15, 30)
                     if abs(y_of(t) - y_of(0.0)) >= lh * 1.6), 30)
        painter.setFont(self.font())
        a = math.ceil(low / tick) * tick
        while a <= high + 1e-9:
            y = y_of(a)
            zero = abs(a) < tick / 2
            painter.setPen(QPen(text if zero else _mix(text, base, 0.4), 1))
            painter.drawLine(QPointF(band.right() - lh * 0.35, y), QPointF(band.right(), y))
            painter.drawText(QRectF(0, y - lh / 2, band.right() - lh * 0.45, lh),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{0.0 if zero else a:+.{2 if tick < 0.1 else 1}f}°")
            a += tick

    def _paint_azimuth_scale(self, painter, base):
        """Vue depuis le site, comme le panorama 2D et la carte : graduation des azimuts dans une
        bande en bas (traits fins, moyens et forts, valeurs sans chevauchement, points cardinaux en
        gras) et repère bleu en forme de flèche posé sur la bande, pointe vers le haut, à l'azimut visé. Azimuts
        placés à l'horizontale de l'observateur. Renvoie le haut de la bande."""
        v3, lh, frame = self.view3d, self.lh, self.frame
        yaw, fov, _pitch = v3.view()
        text = self.source.palette().color(QPalette.ColorRole.Text)
        highlight = self.source.palette().color(QPalette.ColorRole.Highlight)
        small = QFont(self.font())
        bold = QFont(self.font())
        bold.setBold(True)
        top = frame.bottom() - lh * 1.55
        fill = QColor(base)
        fill.setAlpha(170)
        painter.setOpacity(1.0)
        painter.fillRect(QRectF(frame.left(), top, frame.width(), frame.bottom() - top), fill)

        def x_of(azimuth):
            point = self.project(azimuth, 1.0, 0.0)
            return point.x() if point is not None else None

        per_degree = frame.width() / fov
        # Pas des valeurs (comme le panorama 2D) et des traits (au moins 6 pixels entre deux)
        label_step = next((st for st in (1, 2, 5, 10, 15, 30, 45)
                           if st * per_degree >= QFontMetricsF(small).horizontalAdvance("000°") * 1.6), 90)
        tick_step = next((st for st in (0.5, 1, 2, 5, 10) if st * per_degree >= 6), 10)
        start = math.floor((yaw - fov / 2 - tick_step) / tick_step) * tick_step
        a = start
        while a <= yaw + fov / 2 + tick_step:
            x = x_of(a % 360)
            value = round(a % 360, 6) % 360
            if x is not None and frame.left() <= x <= frame.right():
                level = 3 if value % 30 == 0 else 2 if value % label_step == 0 else 1
                painter.setPen(QPen(text, 1.4 if level == 3 else 1))
                painter.drawLine(QPointF(x, top), QPointF(x, top + lh * (0.25, 0.4, 0.55)[level - 1]))
            a += tick_step
        # Valeurs : cardinaux, puis 45°, 30° et les autres, sans chevauchement
        values = []
        a = math.floor((yaw - fov / 2) / label_step) * label_step
        while a <= yaw + fov / 2 + label_step:
            values.append(round(a % 360, 6) % 360)
            a += label_step
        values.sort(key=lambda v: (v % 90 != 0, v % 45 != 0, v % 30 != 0, v))
        placed = []
        for value in values:
            x = x_of(value)
            if x is None:
                continue
            cardinal = CARDINALS.get(int(value)) if value == int(value) else None
            font = bold if cardinal else small
            fm = QFontMetricsF(font)
            label = cardinal or f"{value:g}°"
            w, h = fm.horizontalAdvance(label), fm.height()
            box = QRectF(x - w / 2, top + lh * 0.5, w, h)
            if box.left() < frame.left() or box.right() > frame.right() \
                    or any(box.adjusted(-4, 0, 4, 0).intersects(r) for r in placed):
                continue
            placed.append(box)
            path = QPainterPath()
            path.addText(box.left(), box.top() + fm.ascent(), font, label)
            painter.setPen(QPen(base, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(text if cardinal or value == 0 else _mix(text, base, 0.2))
            painter.drawPath(path)
        # Azimut visé : flèche bleue posée sur la bande, pointe vers le haut (comme sur la carte)
        x = frame.center().x()
        size = lh * 0.4
        foot = top
        tip = foot - size * 1.6
        painter.setPen(QPen(base, 1))
        painter.setBrush(highlight)
        painter.drawPolygon(QPolygonF([QPointF(x - size, foot), QPointF(x, tip), QPointF(x + size, foot)]))
        return top

    # Placement ---------------------------------------------------------------------------

    def _height(self, distance, altitude):
        """Hauteur (m) d'un point d'altitude donnée sous ou sur l'œil, courbure comprise."""
        return altitude - self.eye - distance ** 2 / (2 * self.source.k * EARTH_RADIUS_M)

    def _sky_y(self, azimuth, point):
        """Ordonnée au-dessus de laquelle l'étiquette est suspendue : horizon dans cette direction."""
        v2 = self.source
        ridge = float(np.interp(azimuth, v2.data["azimuth"], v2._view[3], period=360))
        sky = self.project(azimuth, 1.0, math.tan(math.radians(ridge)))
        return min(sky.y(), point.y()) if sky else point.y()

    def _fit(self, x, sky_y, t, width, sign_h, placed):
        """Cadre de l'étiquette au-dessus de sky_y, décalé en cas de chevauchement avec `placed`,
        ou None si aucune place (mêmes règles que le panorama 2D)."""
        rect, lh = self.frame, self.lh
        left = min(max(x - width / 2, rect.left() + 3), rect.right() - 3 - width)
        target = sky_y - lh * (0.4 + 2.8 * (1 - t)) - sign_h
        lowest = sky_y - lh * 0.3 - sign_h
        step = (sign_h + lh * 0.2) / 2
        for offset in (0, -1, 1, -2, 2, -3, 3, -4, -5, -6):
            top = target + offset * step
            if top > lowest:
                continue
            box = QRectF(left, top, width, sign_h)
            if box.top() < rect.top() + 2:
                continue
            if not any(box.adjusted(-lh * 0.3, -2, lh * 0.3, 2).intersects(r) for r in placed):
                return box
        return None

    def _crest(self, azimuth, distance, height):
        """(masqué depuis le site, ordonnée de la crête qui le masque ou None)."""
        crest = self.source._horizon_before(azimuth, distance)
        if math.degrees(math.atan2(height, distance)) >= crest:
            return False, None
        point = self.project(azimuth, 1.0, math.tan(math.radians(crest)))
        return True, point.y() if point else None

    def _on_screen(self, point):
        rect = self.frame
        return (point is not None and rect.left() + 4 <= point.x() <= rect.right() - 4
                and rect.top() <= point.y() <= rect.bottom())

    def _layout_markers(self, placed):
        """Repères des autres sites (nom et distance dans une pastille, au-dessus du mât de
        l'antenne) : ([(distance, t, x, cadre, police, texte, pied, masqué)] pour le dessin du
        panorama 2D, [(x, bas, haut, masqué)] des mâts)."""
        v2, mesh, lh = self.source, self.view3d.mesh, self.lh
        _hf, _hu, d_factor, d_unit = units(v2._unit_profile())
        markers, masts = [], []
        for name, azimuth, altitude, distance in sorted(v2.markers, key=lambda m: m[3]):
            east, north = distance * math.sin(math.radians(azimuth)), distance * math.cos(math.radians(azimuth))
            if max(abs(east), abs(north)) > mesh["half"]:
                continue                                # hors du relief maillé
            top_h = self._height(distance, altitude)
            ground_h = self._height(distance, _ground_at(mesh, east, north))
            top, ground = self.project(azimuth, distance, top_h), self.project(azimuth, distance, ground_h)
            if not self._on_screen(top) or ground is None:
                continue
            if top.y() > ground.y() - lh * 1.5:          # antenne trop courte à l'écran : mât de longueur fixe
                top = QPointF(top.x(), ground.y() - lh * 1.5)
            t = v2._depth(distance)
            font, fm = v2._sign_font(lh, t)
            label = f"{name} · {distance / d_factor:.1f} {d_unit}"
            sign_h = fm.height() * 1.25
            box = self._fit(top.x(), self._sky_y(azimuth, top), t,
                            fm.horizontalAdvance(label) + sign_h * 0.8, sign_h, placed)
            if box is None:
                continue
            hidden, crest_y = self._crest(azimuth, distance, top_h)
            if hidden:                                  # caché : pointillé arrêté sur la crête, pas de mât
                foot_y = crest_y if crest_y is not None else top.y()
            else:                                       # pied éventuellement caché par une crête
                ground_hidden, ground_crest = self._crest(azimuth, distance, ground_h)
                bottom = ground_crest if ground_hidden and ground_crest is not None else ground.y()
                masts.append((top.x(), min(bottom, self.frame.bottom()), top.y(), False))
                foot_y = top.y()
            placed.append(box)
            markers.append((distance, t, top.x(), box, font, label, foot_y, hidden))
        return markers, masts

    def _layout_places(self, placed):
        """Noms des localités : [(distance, t, x, cadre, police, métriques, marge, nom, masqué, pied)]."""
        v2, lh = self.source, self.lh
        candidates = sorted((p for p in v2.places if population(p) >= v2.min_population),
                            key=lambda p: (p["rank"], -population(p), p["distance"]))
        signs, limit = [], MAX_SIGNS + len(placed)
        for place in candidates:
            if len(placed) >= limit:
                break
            azimuth, distance = place["azimuth"], place["distance"]
            height = self._height(distance, place.get("ground", 0.0))
            ground = self.project(azimuth, distance, height)
            if not self._on_screen(ground):
                continue
            t = v2._depth(distance)
            font, fm = v2._sign_font(lh, t)
            name = place["name"].upper()
            pad = fm.height() * 0.25
            box = self._fit(ground.x(), self._sky_y(azimuth, ground), t,
                            fm.horizontalAdvance(name) + 2 * pad, fm.height(), placed)
            if box is None:
                continue
            hidden, crest_y = self._crest(azimuth, distance, height)
            foot_y = crest_y if hidden and crest_y is not None else ground.y()
            placed.append(box)
            signs.append((distance, t, ground.x(), box, font, fm, pad, name, hidden,
                          min(foot_y, self.frame.bottom() - 1)))
        return signs



def _ground_at(mesh, east, north):
    """Altitude du sol (m) du maillage au point (east, north), par interpolation bilinéaire."""
    axis, ground = mesh["axis"], mesh["ground"]
    n = axis.size
    col = float(np.interp(east, axis, np.arange(n)))
    row = n - 1 - float(np.interp(north, axis, np.arange(n)))     # rangée 0 au nord
    c0, r0 = min(int(col), n - 2), min(int(row), n - 2)
    fc, fr = col - c0, row - r0
    return float(ground[r0, c0] * (1 - fr) * (1 - fc) + ground[r0, c0 + 1] * (1 - fr) * fc
                 + ground[r0 + 1, c0] * fr * (1 - fc) + ground[r0 + 1, c0 + 1] * fr * fc)


def coverage_for(image_path):
    """(calque de couverture, GeoRef) d'une carte SPLAT! calée, ou None."""
    ref = basemap.georef_for(image_path)
    if ref is None:
        return None
    image = QImage(str(image_path))
    return (basemap.coverage_layer(image), ref) if not image.isNull() else None
