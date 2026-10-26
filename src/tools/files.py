"""Outils fichiers : lecture, écriture et exploration de l'arborescence.

Tous les chemins passent par :class:`~tools.sandbox.Sandbox` : un chemin hors du
projet, un fichier `.env` ou un dossier technique (`.git`, `.venv`) est refusé
avant tout accès au disque.
"""

from pathlib import Path
from typing import List, Optional

from .base import ToolError
from .sandbox import EXCLUDED_DIRS, Sandbox, is_secret_name

__all__ = [
    "read_file",
    "write_file",
    "list_dir",
    "MAX_WRITE_BYTES",
    "MAX_LIST_ENTRIES",
]

# Garde-fous de volume : au-delà, on demande au modèle de cibler sa demande.
MAX_WRITE_BYTES = 512_000
MAX_LIST_ENTRIES = 500
MAX_READ_LINES = 2_000
BINARY_SNIFF_BYTES = 4_096


def _ensure_text_file(sandbox: Sandbox, target: Path) -> None:
    """Refuse les fichiers de secrets et les fichiers binaires.

    Args:
        sandbox: racine de travail (pour les chemins affichés).
        target: chemin déjà résolu.

    Raises:
        ToolError: fichier de secrets ou contenu binaire (octet nul détecté).
    """
    relative = sandbox.relative(target)
    if is_secret_name(target.name):
        raise ToolError(
            f"Lecture refusée : « {relative} » contient des secrets (clé API). "
            "Demandez à l'utilisateur la valeur dont vous avez besoin."
        )
    try:
        with open(target, "rb") as handle:
            head = handle.read(BINARY_SNIFF_BYTES)
    except OSError as exc:
        raise ToolError(f"Lecture impossible de « {relative} » : {exc}") from None
    if b"\x00" in head:
        raise ToolError(
            f"« {relative} » semble binaire (image, archive, exécutable) : "
            "seuls les fichiers texte sont lisibles."
        )


def read_file(
    sandbox: Sandbox,
    path: str,
    start_line: int = 1,
    max_lines: int = 400,
) -> str:
    """Lit un fichier texte du projet, par plage de lignes.

    Args:
        sandbox: racine de travail.
        path: chemin relatif au projet.
        start_line: première ligne lue (numérotation à partir de 1).
        max_lines: nombre maximal de lignes renvoyées.

    Returns:
        Le contenu demandé (fins de ligne normalisées en ``\\n``), suivi d'une
        note explicite si le fichier continue au-delà de ``max_lines``.

    Raises:
        ToolError: chemin invalide/hors projet, fichier absent, binaire ou plage
            incohérente.
    """
    if start_line < 1:
        raise ToolError("start_line doit être >= 1 (la première ligne porte le 1).")
    if max_lines < 1:
        raise ToolError("max_lines doit être >= 1.")
    max_lines = min(max_lines, MAX_READ_LINES)

    target = sandbox.resolve(path, must_exist=True, kind="file")
    relative = sandbox.relative(target)
    _ensure_text_file(sandbox, target)

    try:
        with open(target, "r", encoding="utf-8", errors="replace", newline="") as handle:
            for _ in range(start_line - 1):
                if not handle.readline():
                    raise ToolError(
                        f"start_line={start_line} dépasse la fin de « {relative} »."
                    )
            lines: List[str] = []
            for _ in range(max_lines):
                line = handle.readline()
                if not line:
                    break
                lines.append(line.rstrip("\r\n"))
            following = handle.readline()
    except OSError as exc:
        raise ToolError(f"Lecture impossible de « {relative} » : {exc}") from None

    if not lines:
        return f"« {relative} » est vide à partir de la ligne {start_line}."

    last = start_line + len(lines) - 1
    text = "\n".join(lines)
    if following:
        text += (
            f"\n[... tronqué : lignes {start_line} à {last} affichées, le fichier "
            f"continue. Relancez avec start_line={last + 1} pour la suite.]"
        )
    return text


def write_file(
    sandbox: Sandbox,
    path: str,
    content: str,
    create_dirs: bool = True,
) -> str:
    """Crée ou remplace un fichier texte du projet.

    Le contenu est écrit tel quel, en UTF-8 et avec des fins de ligne ``\\n``
    (pas de conversion CRLF), pour rester cohérent avec git.

    Args:
        sandbox: racine de travail.
        path: chemin relatif au projet.
        content: contenu complet du fichier.
        create_dirs: créer les dossiers parents manquants.

    Returns:
        Un message de confirmation (action, taille, nombre de lignes).

    Raises:
        ToolError: chemin hors projet, zone protégée, dossier cible ou contenu
            trop volumineux.
    """
    if not isinstance(content, str):
        raise ToolError("« content » doit être une chaîne de caractères.")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError(
            f"Contenu trop volumineux ({len(data)} octets > {MAX_WRITE_BYTES}) : "
            "écrivez le fichier en plusieurs appels successifs."
        )

    target = sandbox.resolve(path, must_exist=False)
    sandbox.ensure_writable(target)
    relative = sandbox.relative(target)
    if target.is_dir():
        raise ToolError(f"« {relative} » est un dossier : impossible d'y écrire.")

    existed = target.exists()
    parent = target.parent
    try:
        if create_dirs:
            parent.mkdir(parents=True, exist_ok=True)
        elif not parent.is_dir():
            raise ToolError(
                f"Dossier parent introuvable : « {sandbox.relative(parent)} » "
                "(create_dirs=false)."
            )
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except OSError as exc:
        raise ToolError(f"Écriture impossible de « {relative} » : {exc}") from None

    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    action = "remplacé" if existed else "créé"
    return f"Fichier {action} : {relative} ({len(data)} octets, {line_count} lignes)."


def list_dir(
    sandbox: Sandbox,
    path: str = ".",
    max_depth: int = 2,
    max_entries: int = 200,
) -> str:
    """Liste l'arborescence d'un dossier du projet.

    Les dossiers techniques (``.git``, ``.venv``, ``__pycache__``…) sont
    signalés mais jamais explorés, pour éviter de noyer le modèle.

    Args:
        sandbox: racine de travail.
        path: dossier à explorer (relatif au projet).
        max_depth: profondeur maximale (1 = enfants directs uniquement).
        max_entries: nombre maximal de lignes renvoyées.

    Returns:
        Une arborescence textuelle (dossiers suffixés par ``/``, taille des
        fichiers entre parenthèses) et une note si la liste est tronquée.

    Raises:
        ToolError: chemin invalide/hors projet ou cible absente.
    """
    max_depth = max(1, min(int(max_depth), 5))
    max_entries = max(1, min(int(max_entries), MAX_LIST_ENTRIES))

    target = sandbox.resolve(path, must_exist=True)
    relative = sandbox.relative(target)
    if target.is_file():
        return (
            f"« {relative} » est un fichier ({target.stat().st_size} octets). "
            "Utilisez read_file pour le lire."
        )

    lines: List[str] = []
    truncated = False

    def walk(directory: Path, depth: int) -> None:
        nonlocal truncated
        indent = "  " * depth
        try:
            children = sorted(
                directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())
            )
        except OSError as exc:
            lines.append(f"{indent}[dossier illisible : {exc}]")
            return
        for child in children:
            if len(lines) >= max_entries:
                truncated = True
                return
            if child.is_dir():
                if child.name.lower() in EXCLUDED_DIRS:
                    lines.append(f"{indent}{child.name}/ (ignoré)")
                    continue
                lines.append(f"{indent}{child.name}/")
                if depth + 1 < max_depth:
                    walk(child, depth + 1)
                continue
            try:
                size: Optional[int] = child.stat().st_size
            except OSError:
                size = None
            suffix = f" ({size} o)" if size is not None else ""
            lines.append(f"{indent}{child.name}{suffix}")

    walk(target, 0)

    header = f"Arborescence de « {relative} » (profondeur {max_depth}) :"
    if not lines:
        return f"{header}\n(dossier vide)"
    body = "\n".join(lines)
    if truncated:
        body += (
            f"\n[... tronqué à {max_entries} entrées. Ciblez un sous-dossier ou "
            "utilisez search_code pour aller plus loin.]"
        )
    return f"{header}\n{body}"
