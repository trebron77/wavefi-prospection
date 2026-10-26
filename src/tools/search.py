"""Recherche plein texte (expression régulière) dans les fichiers du projet.

La recherche ignore les dossiers techniques, les fichiers binaires, les fichiers
volumineux et les fichiers de secrets (`.env`), pour ne jamais envoyer de clé API
au modèle et pour borner le volume renvoyé.
"""

import os
import re
from pathlib import Path
from typing import Iterator, List

from .base import ToolError
from .sandbox import EXCLUDED_DIRS, Sandbox, is_secret_name

__all__ = ["search_code", "MAX_RESULTS"]

MAX_RESULTS = 200
MAX_FILE_BYTES = 5_000_000
LINE_PREVIEW = 200

# Extensions traitées comme binaires (jamais analysées).
BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
        ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar", ".xz", ".whl",
        ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".pyo", ".class",
        ".o", ".obj", ".lib", ".a", ".bin", ".dat",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".mp3", ".mp4", ".avi", ".mov", ".wav", ".mkv", ".webm",
        ".db", ".sqlite", ".sqlite3", ".xls", ".xlsx", ".doc", ".docx", ".ppt",
    }
)


def _looks_binary(file: Path) -> bool:
    """Heuristique : extension binaire connue ou octet nul dans l'en-tête."""
    if file.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with open(file, "rb") as handle:
            return b"\x00" in handle.read(4096)
    except OSError:
        return True  # illisible : on l'ignore


def _iter_files(root: Path, glob: str) -> Iterator[Path]:
    """Parcourt ``root`` (sans les dossiers techniques) en appliquant ``glob``."""
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name.lower() not in EXCLUDED_DIRS)
        for filename in sorted(filenames):
            candidate = Path(directory) / filename
            if glob in ("", "*", "**/*") or candidate.match(glob):
                yield candidate


def search_code(
    sandbox: Sandbox,
    pattern: str,
    path: str = ".",
    glob: str = "*",
    case_sensitive: bool = False,
    max_results: int = 50,
    max_file_bytes: int = 1_000_000,
) -> str:
    """Cherche une expression régulière dans le code du projet.

    Args:
        sandbox: racine de travail.
        pattern: expression régulière Python (le texte littéral fonctionne tel
            quel ; échappez les caractères spéciaux avec ``\\``).
        path: fichier ou dossier de départ, relatif au projet.
        glob: filtre de nom de fichier (``"*.py"``, ``"test_*.py"``…).
        case_sensitive: respecter la casse (insensible par défaut).
        max_results: nombre maximal de lignes renvoyées.
        max_file_bytes: taille au-delà de laquelle un fichier est ignoré.

    Returns:
        Les correspondances au format ``chemin:ligne: texte``, précédées d'un
        résumé, ou un message explicite si rien ne correspond.

    Raises:
        ToolError: chemin invalide/hors projet, motif vide ou expression
            régulière incorrecte.
    """
    if not pattern or not pattern.strip():
        raise ToolError("« pattern » ne peut pas être vide.")
    max_results = max(1, min(int(max_results), MAX_RESULTS))
    max_file_bytes = max(1_024, min(int(max_file_bytes), MAX_FILE_BYTES))

    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise ToolError(
            f"Expression régulière invalide ({exc}). Échappez les caractères "
            "spéciaux (par exemple « print\\( ») ou simplifiez le motif."
        ) from None

    target = sandbox.resolve(path, must_exist=True)
    candidates: List[Path] = [target] if target.is_file() else list(_iter_files(target, glob))

    results: List[str] = []
    scanned = 0
    for file in candidates:
        if len(results) >= max_results:
            break
        if is_secret_name(file.name) or file.suffix.lower() in BINARY_EXTENSIONS:
            continue
        try:
            if file.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        if _looks_binary(file):
            continue
        scanned += 1
        relative = sandbox.relative(file)
        try:
            with open(file, "r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if regex.search(line):
                        results.append(f"{relative}:{number}: {line.strip()[:LINE_PREVIEW]}")
                        if len(results) >= max_results:
                            break
        except OSError:
            continue

    if not results:
        return (
            f"Aucune correspondance pour « {pattern} » "
            f"({scanned} fichier(s) analysé(s))."
        )

    header = (
        f"{len(results)} correspondance(s) pour « {pattern} » "
        f"dans {scanned} fichier(s) analysé(s) :"
    )
    body = "\n".join(results)
    if len(results) >= max_results:
        body += (
            f"\n[limite de {max_results} résultats atteinte : précisez « path » "
            "ou « glob » pour affiner.]"
        )
    return f"{header}\n{body}"
