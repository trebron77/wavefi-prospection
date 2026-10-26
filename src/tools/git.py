"""Lecture seule de l'état git d'un projet : ``git status`` et ``git diff``.

Ces helpers sont volontairement tolérants : si ``git`` est absent, si le dossier
n'est pas un dépôt ou si la commande échoue, ils renvoient une chaîne vide.
L'agent peut ainsi consigner un diff avant d'écraser un fichier sans jamais
interrompre la boucle agentique.

Utilisation :
    from tools.git import read_git_diff

    print(read_git_diff(".", "src/main.py"))   # "" si rien à afficher
"""

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Union

__all__ = ["GIT_TIMEOUT", "read_git_diff", "read_git_status"]

#: Délai maximal d'une commande git de lecture (secondes).
GIT_TIMEOUT = 10.0

_MARQUEUR_TRONCATURE = "\n… (sortie tronquée)"


def _git(
    root: Optional[Union[str, Path]],
    arguments: List[str],
    max_chars: int,
) -> str:
    """Exécute une commande git de lecture et renvoie sa sortie normalisée.

    Args:
        root: dossier du projet (``None`` = pas de dépôt connu).
        arguments: arguments passés à git, par exemple ``["diff", "--no-color"]``.
        max_chars: longueur maximale de la sortie renvoyée.

    Returns:
        La sortie standard nettoyée, tronquée si nécessaire ; ``""`` si git est
        indisponible, si ``root`` n'est pas un dossier ou en cas d'erreur.
    """
    if root is None:
        return ""
    dossier = Path(root)
    if not dossier.is_dir():
        return ""
    executable = shutil.which("git")
    if executable is None:
        return ""
    try:
        termine = subprocess.run(
            [executable, "--no-pager", *arguments],
            cwd=str(dossier),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if termine.returncode != 0:
        return ""
    sortie = (termine.stdout or "").strip()
    limite = max(1, int(max_chars))
    if len(sortie) > limite:
        sortie = sortie[:limite] + _MARQUEUR_TRONCATURE
    return sortie


def read_git_diff(
    root: Optional[Union[str, Path]] = None,
    path: Optional[str] = None,
    max_chars: int = 4000,
) -> str:
    """Renvoie ``git diff`` pour le projet, éventuellement limité à un fichier.

    Args:
        root: racine du dépôt ; ``None`` renvoie ``""``.
        path: chemin relatif à observer ; ``None`` = tout le dépôt.
        max_chars: longueur maximale du diff renvoyé.

    Returns:
        Le diff sans couleur, ou ``""`` si git est indisponible ou sans objet.
    """
    arguments: List[str] = ["diff", "--no-color"]
    if path:
        arguments.extend(["--", str(path)])
    return _git(root, arguments, max_chars)


def read_git_status(
    root: Optional[Union[str, Path]] = None, max_chars: int = 2000
) -> str:
    """Renvoie ``git status --short --branch`` (état des fichiers du projet).

    Args:
        root: racine du dépôt ; ``None`` renvoie ``""``.
        max_chars: longueur maximale de la sortie.

    Returns:
        Le statut compact, ou ``""`` si git est indisponible.
    """
    return _git(root, ["status", "--short", "--branch"], max_chars)
