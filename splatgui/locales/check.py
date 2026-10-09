"""Catalogues de traduction : clés = premiers arguments littéraux de tr() et N_() dans
splatgui, plus quelques constantes affichées.

    python splatgui/locales/check.py               → liste des clés (JSON)
    python splatgui/locales/check.py --check en    → clés absentes ou en trop dans en.json,
                                                     {champs} différents de l'original
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # splatgui/
EXTRA = ["SPLAT! de KD2BD / SPLAT!Gui de F4CWH",           # CREDITS (splatgui/__init__.py)
         "Licence GNU GPL v2 ou ultérieure"]                    # LICENSE_SHORT


def keys():
    found = {}
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("tr", "N_") and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value, f"{path.name}:{node.lineno}")
    for text in EXTRA:
        found.setdefault(text, "__init__.py")
    return found


def fields(text):
    return sorted(set(re.findall(r"\{(\w*)[^{}]*\}", text)))


if __name__ == "__main__":
    all_keys = keys()
    if "--check" in sys.argv:
        code = sys.argv[sys.argv.index("--check") + 1]
        catalog = json.loads((ROOT / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        missing = [k for k in all_keys if k not in catalog]
        extra = [k for k in catalog if k not in all_keys]
        bad = [k for k in catalog if k in all_keys and fields(k) != fields(catalog[k])]
        print(f"{code} : {len(all_keys)} clés, {len(missing)} manquantes, {len(extra)} en trop, "
              f"{len(bad)} avec des champs différents")
        for label, items in (("MANQUANTE", missing), ("EN TROP", extra), ("CHAMPS", bad)):
            for k in items:
                print(f"{label}\t{k!r}")
    else:
        print(json.dumps(all_keys, ensure_ascii=False, indent=1))
