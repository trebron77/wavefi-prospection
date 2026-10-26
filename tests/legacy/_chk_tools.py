"""Vérification hors ligne des outils (aucun réseau, dossier temporaire)."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from tools import build_default_registry
from tools.base import Tool, ToolError, ToolRegistry, validate_arguments
from tools.sandbox import Sandbox

RESULTATS = []


def check(label, condition, detail=""):
    suffixe = (" | " + str(detail)) if (detail and not condition) else ""
    RESULTATS.append(("OK " if condition else "KO ") + label + suffixe)


root = Path(tempfile.mkdtemp(prefix="agent-tools-"))
try:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('bonjour')\n", encoding="utf-8")
    (root / "src" / "pkg" / "util.py").write_text("VALUE = 42\n", encoding="utf-8")
    (root / "notes.md").write_text("ligne 1\nligne 2\nligne 3\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01\x02SECRET")
    (root / ".env").write_text("LLM_API_KEY=sk-secret-a-never-leak\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "node_modules").mkdir()

    registry = build_default_registry(root=root)
    attendus = ["list_dir", "read_file", "run_command", "search_code", "write_file"]
    check("5 outils enregistres", registry.names == attendus, registry.names)
    schemas = registry.schemas
    check("schemas au format function", all(s["type"] == "function" and s["function"]["name"] for s in schemas))

    # --- bac a sable -------------------------------------------------------
    box = Sandbox(root)
    mauvais_chemins = ["../secret.txt", "..", str(root.parent / "hors.txt"), "src/../../hors.txt"]
    for mauvais in mauvais_chemins:
        try:
            box.resolve(mauvais)
            check("refus %r" % mauvais, False, "chemin accepte")
        except ToolError:
            check("refus %r" % mauvais, True)
    check("racine acceptee", box.resolve(".") == box.root.resolve())

    # --- lecture -----------------------------------------------------------
    resultat = registry.execute("read_file", {"path": "notes.md", "start_line": 2, "max_lines": 1})
    check("read_file plage", resultat.ok and resultat.content.startswith("ligne 2") and "tronqué" in resultat.content, resultat.content)
    resultat = registry.execute("read_file", {"path": "notes.md"})
    check("read_file complet", resultat.ok and resultat.content.splitlines() == ["ligne 1", "ligne 2", "ligne 3"], resultat.content)
    check("read_file introuvable", not registry.execute("read_file", {"path": "absent.txt"}).ok)
    check("read_file binaire refuse", not registry.execute("read_file", {"path": "image.bin"}).ok)
    check("read_file .env refuse", not registry.execute("read_file", {"path": ".env"}).ok)
    check("read_file dossier refuse", not registry.execute("read_file", {"path": "src"}).ok)
    check("read_file hors projet refuse", not registry.execute("read_file", {"path": "../x"}).ok)

    # --- ecriture ----------------------------------------------------------
    resultat = registry.execute("write_file", {"path": "src/pkg/nouveau.py", "content": "X = 1\nY = 2\n"})
    check("write_file creation", resultat.ok and "créé" in resultat.content, resultat.content)
    ecrit = (root / "src" / "pkg" / "nouveau.py").read_bytes()
    check("write_file fins de ligne LF", b"\r\n" not in ecrit, ecrit)
    check("write_file contenu exact", ecrit == b"X = 1\nY = 2\n")
    resultat = registry.execute("write_file", {"path": "src/pkg/nouveau.py", "content": "X = 3\n"})
    check("write_file remplacement", resultat.ok and "remplacé" in resultat.content, resultat.content)
    check("write_file .git refuse", not registry.execute("write_file", {"path": ".git/config", "content": "x"}).ok)
    check("write_file .env refuse", not registry.execute("write_file", {"path": ".env.local", "content": "x"}).ok)
    check("write_file hors projet refuse", not registry.execute("write_file", {"path": "../hors.txt", "content": "x"}).ok)
    check("write_file dossier refuse", not registry.execute("write_file", {"path": "src", "content": "x"}).ok)
    check("write_file trop gros refuse", not registry.execute("write_file", {"path": "gros.txt", "content": "a" * 600000}).ok)

    # --- exploration -------------------------------------------------------
    resultat = registry.execute("list_dir", {"path": ".", "max_depth": 2})
    contenu = resultat.content
    check("list_dir fichier", resultat.ok and "src/" in contenu and "main.py" in contenu, contenu)
    check("list_dir ignore .git", ".git/\n" not in contenu and "  .git/" not in contenu, contenu)
    check("list_dir profondeur 1", "pkg/" not in registry.execute("list_dir", {"path": ".", "max_depth": 1}).content)
    check("list_dir profondeur 2", "  pkg/" in contenu, contenu)
    check("list_dir cible fichier", "read_file" in registry.execute("list_dir", {"path": "notes.md"}).content)

    # --- recherche ---------------------------------------------------------
    resultat = registry.execute("search_code", {"pattern": "VALUE", "glob": "*.py"})
    check("search_code trouve", resultat.ok and "src/pkg/util.py:1" in resultat.content, resultat.content)
    check("search_code glob", "notes.md" not in resultat.content)
    insensible = registry.execute("search_code", {"pattern": "vAlUe"})
    check("search_code insensible casse", insensible.ok and "util.py" in insensible.content, insensible.content)
    secret = registry.execute("search_code", {"pattern": "LLM_API_KEY"})
    check("search_code secret ignore", secret.ok and "sk-secret" not in secret.content, secret.content)
    check("search_code binaire ignore", "image.bin" not in registry.execute("search_code", {"pattern": "SECRET"}).content)
    resultat = registry.execute("search_code", {"pattern": "("})
    check("search_code regex invalide", not resultat.ok and "invalide" in resultat.content, resultat.content)
    resultat = registry.execute("search_code", {"pattern": "zzz-introuvable-zzz"})
    check("search_code sans resultat", resultat.ok and "Aucune correspondance" in resultat.content, resultat.content)
    check("search_code hors projet", not registry.execute("search_code", {"pattern": "x", "path": ".."}).ok)

    # --- commandes ---------------------------------------------------------
    python = sys.executable
    resultat = registry.execute("run_command", {"command": '"%s" -c "print(42)"' % python})
    check("run_command succes", resultat.ok and "42" in resultat.content and "code de sortie : 0" in resultat.content, resultat.content)
    resultat = registry.execute("run_command", {"command": '"%s" -c "raise SystemExit(3)"' % python})
    check("run_command echec visible", resultat.ok and "code de sortie : 3" in resultat.content, resultat.content)
    resultat = registry.execute("run_command", {"command": ["git", "status"]})
    check("run_command liste refusee (schema string)", (not resultat.ok) and "type string" in resultat.content, resultat.content)
    resultat = registry.execute("run_command", {"command": '"%s" -c "import time; time.sleep(5)"' % python, "timeout": 1})
    check("run_command timeout", not resultat.ok and "dépassé" in resultat.content, resultat.content)
    check("run_command git push refuse", not registry.execute("run_command", {"command": "git push origin main"}).ok)
    check("run_command pip install refuse", not registry.execute("run_command", {"command": "pip install requests"}).ok)
    check("run_command exe non autorise", not registry.execute("run_command", {"command": "powershell -c dir"}).ok)
    check("run_command operateur refuse", not registry.execute("run_command", {"command": "git status && git log"}).ok)
    check("run_command cwd hors projet", not registry.execute("run_command", {"command": "git status", "cwd": ".."}).ok)
    check("run_command vide", not registry.execute("run_command", {"command": "   "}).ok)
finally:
    shutil.rmtree(root, ignore_errors=True)

print("\n".join(RESULTATS))
print("TOTAL=%d KO=%d" % (len(RESULTATS), sum(1 for ligne in RESULTATS if ligne.startswith("KO"))))
