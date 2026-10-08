# Compilation de SPLAT!Gui : pyinstaller SplatGui.spec --noconfirm
# Résultat : dist/SPLAT!Gui/SPLAT!Gui.exe, bibliothèques dans dist/SPLAT!Gui/lib (au lieu de _internal),
# données livrées (geojson, icons, antennas) copiées à côté de l'exécutable. Les répertoires de
# travail (bin, deps, tools, terrain, profiles, runs) sont créés au premier lancement.

import shutil
from pathlib import Path

ROOT = Path(SPECPATH)
NAME = "SPLAT!Gui"
DATA_DIRS = ("geojson", "icons", "antennas")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "splatgui" / "resources" / "splash_map.png"), "splatgui/resources"),
           (str(ROOT / "icons" / "splat_icon.ico"), "icons"),           # icône de fenêtre de secours
           (str(ROOT / "splatgui" / "locales" / "*.json"), "splatgui/locales"),    # traductions
           (str(ROOT / "splatgui" / "help" / "*.html"), "splatgui/help"),          # aide intégrée (fr, en, es)
           (str(ROOT / "splatgui" / "help" / "*.pdf"), "splatgui/help"),           # documentation d'origine
           (str(ROOT / "LICENSE"), ".")],                                    # GNU GPL v2 (secours)
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=str(ROOT / "icons" / "splat_icon.ico"),
    console=False,
    upx=False,
    contents_directory="lib",
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

dist = Path(DISTPATH) / NAME
for folder in DATA_DIRS:
    if (ROOT / folder).is_dir():
        shutil.copytree(ROOT / folder, dist / folder, dirs_exist_ok=True)
shutil.copy2(ROOT / "LICENSE", dist / "LICENSE")          # texte de la GNU GPL v2, à côté de l'exécutable
