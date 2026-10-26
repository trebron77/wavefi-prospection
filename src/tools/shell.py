"""Exécution de commandes : liste blanche, sans shell, avec délai maximal.

Règles appliquées avant chaque exécution :

- ``shell=False`` : aucun interpréteur n'est lancé, donc ni ``&&``, ni ``|``,
  ni redirection de fichier (les commandes sont de simples listes d'arguments) ;
- seul le nom de l'exécutable est autorisé, et uniquement s'il figure dans
  :data:`ALLOWED_COMMANDS` ;
- les sous-commandes destructrices ou réseau (``git push``, ``pip install``…)
  sont refusées même si l'exécutable est autorisé ;
- le dossier d'exécution est résolu par le bac à sable (jamais hors projet) ;
- un délai maximal interrompt la commande et un plafond borne la sortie.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Dict, List, Sequence, Union

from .base import ToolError
from .sandbox import Sandbox

__all__ = ["run_command", "ALLOWED_COMMANDS", "MAX_TIMEOUT", "DEFAULT_TIMEOUT"]

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
DEFAULT_MAX_OUTPUT = 20_000
MAX_OUTPUT_CEILING = 100_000

# Exécutables autorisés (outils de développement uniquement).
ALLOWED_COMMANDS = frozenset(
    {
        "git",
        "python",
        "python3",
        "py",
        "pip",
        "pip3",
        "pytest",
        "node",
        "npm",
        "tsc",
        "eslint",
        "prettier",
        "ruff",
        "black",
        "isort",
        "mypy",
        "flake8",
        "bandit",
        "cargo",
        "rustc",
        "go",
        "dotnet",
        "java",
        "javac",
        "mvn",
        "gradle",
        "cmake",
        "make",
        "mingw32-make",
        "ninja",
    }
)

# (motif sur la commande normalisée, explication) — refus catégorique.
BLOCKED_COMMANDS = (
    (r"\bgit\s+push\b", "envoi vers un dépôt distant"),
    (r"\bgit\s+reset\s+--hard\b", "perte des modifications locales"),
    (r"\bgit\s+clean\b[^|]*\s-\w*f", "suppression de fichiers non suivis"),
    (r"\bgit\s+(checkout|restore)\s+--\s", "écrasement de modifications locales"),
    (r"\bgit\s+branch\s+-D\b", "suppression d'une branche"),
    (r"\bgit\s+filter-(branch|repo)\b", "réécriture de l'historique git"),
    (r"\bgit\s+update-ref\s+-d\b", "suppression d'une référence git"),
    (r"\bdotnet\s+nuget\s+push\b", "publication de paquet"),
    (r"\bmvn\s+.*\bdeploy\b", "publication de paquet"),
    (r"\bnpm\s+(install|i|add|publish|unpublish|audit\s+fix)\b", "paquets npm (réseau)"),
    (r"\bnpx\s", "exécution d'un paquet téléchargé (réseau)"),
    (r"\b(pip|pip3)\s+(install|uninstall|download)\b", "paquets Python (réseau)"),
    (r"\bpython3?\s+-m\s+pip\s+(install|uninstall|download)\b", "paquets Python (réseau)"),
    (r"\bcargo\s+(install|publish)\b", "paquets Cargo (réseau)"),
    (r"\bgo\s+(install|get)\b", "paquets Go (réseau)"),
    (r"\bmake\s+install\b", "installation de fichiers"),
)

# Opérateurs de shell : inutiles et dangereux sans interpréteur. On ne refuse
# que les jetons *isolés* (« git log | head »), pas un « ; » interne à un
# programme passé à « python -c ».
SHELL_OPERATORS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>&1"})
SUBSTITUTION = re.compile(r"`|\$\(")


def _strip_quotes(token: str) -> str:
    """Retire les guillemets englobants laissés par ``shlex`` en mode Windows."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _split_command(command: Union[str, Sequence[str]]) -> List[str]:
    """Transforme la commande du modèle en liste d'arguments.

    Args:
        command: ligne de commande (``"git status --short"``) ou liste déjà
            découpée (``["git", "status"]``), plus sûre.

    Returns:
        Les arguments, sans chaîne vide.

    Raises:
        ToolError: commande vide, mal formée ou contenant un opérateur de shell.
    """
    if isinstance(command, (list, tuple)):
        parts = [str(item) for item in command]
    else:
        text = str(command or "").strip()
        if not text:
            raise ToolError("« command » est vide.")
        try:
            # posix=False sous Windows : les antislashs des chemins sont préservés.
            parts = shlex.split(text, posix=os.name != "nt")
        except ValueError as exc:
            raise ToolError(
                f"Commande mal formée (guillemets non fermés ?) : {exc}"
            ) from None

    if os.name == "nt":
        # shlex en mode non-posix conserve les guillemets englobants.
        parts = [_strip_quotes(item) for item in parts]
    parts = [item for item in parts if item]
    if not parts:
        raise ToolError("« command » est vide.")
    for item in parts:
        if item in SHELL_OPERATORS:
            raise ToolError(
                f"Opérateur de shell refusé (« {item} ») : les commandes sont "
                "exécutées sans shell. Lancez des commandes séparées, sans "
                "tube, redirection ni « && »."
            )
        if SUBSTITUTION.search(item):
            raise ToolError(
                f"Substitution de commande refusée (« {item} ») : elle serait "
                "exécutée par un shell, or les commandes sont lancées sans shell."
            )
    return parts


def _environment() -> Dict[str, str]:
    """Environnement d'exécution : pagination désactivée, sortie non colorée."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    return env


def _truncate(text: str, limit: int) -> str:
    """Conserve le début et la fin d'une sortie trop longue."""
    if len(text) <= limit:
        return text
    keep = max(limit // 2, 1)
    removed = len(text) - 2 * keep
    return (
        f"{text[:keep]}\n[... {removed} caractères omis (sortie tronquée à "
        f"{limit}) ...]\n{text[-keep:]}"
    )


def run_command(
    sandbox: Sandbox,
    command: Union[str, Sequence[str]],
    cwd: str = ".",
    timeout: int = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> str:
    """Exécute une commande de développement autorisée, dans le projet.

    Le code de sortie est renvoyé au modèle (une commande en échec n'est pas une
    exception) afin qu'il puisse lire les erreurs et corriger son travail.

    Args:
        sandbox: racine de travail (dossier d'exécution par défaut).
        command: ligne de commande ou liste d'arguments.
        cwd: dossier d'exécution, relatif au projet.
        timeout: délai maximal en secondes (1 à 300).
        max_output: taille maximale de la sortie renvoyée.

    Returns:
        Un compte rendu : commande, code de sortie, stdout puis stderr.

    Raises:
        ToolError: commande vide, exécutable non autorisé, motif destructeur,
            dossier invalide, exécutable introuvable ou délai dépassé.
    """
    parts = _split_command(command)

    executable = Path(parts[0]).name.lower()
    if executable.endswith(".exe"):
        executable = executable[: -len(".exe")]
    if executable not in ALLOWED_COMMANDS:
        raise ToolError(
            f"Commande non autorisée : « {parts[0]} ». Exécutables acceptés : "
            f"{', '.join(sorted(ALLOWED_COMMANDS))}."
        )

    normalized = " ".join(parts).lower()
    for pattern, reason in BLOCKED_COMMANDS:
        if re.search(pattern, normalized):
            raise ToolError(
                f"Commande refusée ({reason}) : « {' '.join(parts)} ». "
                "Proposez-la à l'utilisateur : elle doit être lancée à la main."
            )

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    max_output = max(500, min(int(max_output), MAX_OUTPUT_CEILING))

    workdir = sandbox.resolve(cwd, must_exist=True, kind="dir")
    rendered = (
        subprocess.list2cmdline(parts)
        if os.name == "nt"
        else " ".join(shlex.quote(item) for item in parts)
    )
    header = f"$ {rendered}  (dossier : {sandbox.relative(workdir)})"

    try:
        completed = subprocess.run(
            parts,
            cwd=str(workdir),
            env=_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError:
        raise ToolError(
            f"Exécutable introuvable : « {parts[0]} ». Vérifiez qu'il est "
            "installé et accessible dans le PATH (ou utilisez le python du venv)."
        ) from None
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"Délai de {timeout} s dépassé pour « {rendered} » : commande "
            "interrompue. Augmentez « timeout » si le travail est légitime."
        ) from None
    except OSError as exc:
        raise ToolError(f"Exécution impossible de « {rendered} » : {exc}") from None

    output = (completed.stdout or "").rstrip("\n")
    errors = (completed.stderr or "").rstrip("\n")
    if errors:
        output = f"{output}\n[stderr]\n{errors}" if output else f"[stderr]\n{errors}"
    if not output.strip():
        output = "(aucune sortie)"

    status = (
        f"[code de sortie : {completed.returncode}]"
        if completed.returncode == 0
        else f"[échec, code de sortie : {completed.returncode}]"
    )
    return f"{header}\n{status}\n{_truncate(output, max_output)}"

