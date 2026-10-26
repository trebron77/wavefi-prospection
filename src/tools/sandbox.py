"""Cloisonnement (sandbox) : tout chemin doit rester dans le dossier du projet.

La protection repose sur deux règles appliquées à **chaque** accès :

1. ``Path.resolve()`` normalise le chemin (élimine les ``..``, suit les liens
   symboliques et résout les chemins relatifs au lecteur) ;
2. le chemin résolu doit être le dossier racine ou l'un de ses descendants.

Un chemin absolu hors du projet, un ``..`` qui remonte trop haut ou un lien
symbolique pointant à l'extérieur sont donc refusés avant toute lecture ou
écriture. En écriture, les emplacements sensibles (``.git``, ``.venv``) et les
fichiers de secrets (``.env``) sont en plus interdits.
"""

from pathlib import Path
from typing import Optional, Union

try:  # exécution en package : python -m src.agent
    from ..config import PROJECT_ROOT
except ImportError:  # exécution directe : python src/agent.py
    from config import PROJECT_ROOT

from .base import ToolError

__all__ = [
    "Sandbox",
    "EXCLUDED_DIRS",
    "DEFAULT_ROOT",
]

# Dossiers techniques jamais explorés ni indexés (bruit et volume inutiles).
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".eggs",
    }
)

# Dossiers interdits en écriture : les modifier casserait l'outillage ou git.
PROTECTED_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules"})

# Fichiers de secrets : lisibles par l'utilisateur, jamais par le modèle.
SECRET_FILES = frozenset({".env"})

DEFAULT_ROOT = PROJECT_ROOT


def is_secret_name(name: str) -> bool:
    """Vrai pour un fichier de secrets (``.env``, ``.env.local``, ``.env.prod``)."""
    lowered = name.lower()
    return lowered in SECRET_FILES or lowered.startswith(".env.")


class Sandbox:
    """Racine de travail d'un agent et résolution sûre des chemins.

    Example:
        >>> sandbox = Sandbox(".")
        >>> sandbox.relative(sandbox.resolve("src/main.py"))
        'src/main.py'
    """

    def __init__(self, root: Optional[Union[str, Path]] = None) -> None:
        """Fixe la racine du bac à sable.

        Args:
            root: dossier de travail ; par défaut celui du projet.

        Raises:
            ToolError: racine inexistante ou qui n'est pas un dossier.
        """
        resolved = Path(root if root is not None else DEFAULT_ROOT).expanduser()
        try:
            self.root = resolved.resolve()
        except OSError as exc:  # pragma: no cover - système de fichiers exotique
            raise ToolError(f"Racine de travail illisible ({resolved}) : {exc}") from None
        if not self.root.is_dir():
            raise ToolError(f"Racine de travail introuvable : {self.root}")

    def __repr__(self) -> str:
        return f"Sandbox({str(self.root)!r})"

    def _inside(self, path: Path) -> bool:
        """Vrai si ``path`` est la racine ou l'un de ses descendants."""
        return path == self.root or self.root in path.parents

    def resolve(
        self,
        path: Optional[Union[str, Path]] = ".",
        must_exist: bool = False,
        kind: Optional[str] = None,
    ) -> Path:
        """Résout un chemin en garantissant qu'il reste dans le projet.

        Args:
            path: chemin relatif au projet (ou absolu, s'il est dans le projet).
            must_exist: exiger que la cible existe déjà.
            kind: ``"file"`` ou ``"dir"`` pour vérifier le type de la cible.

        Returns:
            Le chemin absolu résolu.

        Raises:
            ToolError: chemin invalide, hors du projet, introuvable ou du
                mauvais type.
        """
        raw = "." if path is None else str(path).strip()
        if not raw:
            raw = "."
        if "\x00" in raw:
            raise ToolError("Chemin invalide : caractère nul présent.")

        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"Chemin illisible « {raw} » : {exc}") from None

        if not self._inside(resolved):
            raise ToolError(
                f"Chemin hors du dossier du projet, accès refusé : « {raw} ». "
                f"Utilisez un chemin relatif à {self.root.name}/."
            )
        if must_exist and not resolved.exists():
            raise ToolError(f"Chemin introuvable : « {self.relative(resolved)} ».")
        if kind == "file" and not resolved.is_file():
            raise ToolError(
                f"« {self.relative(resolved)} » n'est pas un fichier."
            )
        if kind == "dir" and not resolved.is_dir():
            raise ToolError(
                f"« {self.relative(resolved)} » n'est pas un dossier."
            )
        return resolved


    def relative(self, path: Union[str, Path]) -> str:
        """Chemin affichable, relatif au projet et au format POSIX."""
        resolved = Path(path).resolve()
        if resolved == self.root:
            return "."
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return str(resolved)

    @staticmethod
    def is_excluded(path: Union[str, Path]) -> bool:
        """Vrai pour un dossier technique à ignorer (``.git``, ``.venv``…)."""
        return Path(path).name.lower() in EXCLUDED_DIRS

    def ensure_writable(self, path: Union[str, Path]) -> Path:
        """Refuse l'écriture dans les zones protégées et les fichiers de secrets.

        Args:
            path: chemin *déjà* résolu par :meth:`resolve`.

        Returns:
            Le chemin, inchangé, pour permettre l'enchaînement.

        Raises:
            ToolError: cible dans ``.git``/``.venv``/``node_modules`` ou fichier
                ``.env*``.
        """
        resolved = Path(path)
        if not self._inside(resolved):  # ceinture et bretelles
            raise ToolError(f"Écriture hors du projet refusée : {resolved}")
        if is_secret_name(resolved.name):
            raise ToolError(
                f"Écriture refusée dans « {self.relative(resolved)} » : ce fichier "
                "contient des secrets (clé API). Demandez à l'utilisateur de le "
                "modifier lui-même."
            )
        for part in resolved.relative_to(self.root).parts[:-1]:
            if part.lower() in PROTECTED_DIRS:
                raise ToolError(
                    f"Écriture refusée dans « {part}/ » : dossier technique protégé."
                )
        return resolved
