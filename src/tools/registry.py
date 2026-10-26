"""Registre par défaut : les cinq outils locaux exposés au LLM.

Chaque outil déclare son schéma JSON (envoyé au modèle) et son handler Python.
Le handler n'est appelé que si le nom existe dans le registre et si les
arguments respectent le schéma : aucune exécution arbitraire n'est possible.
"""

from functools import partial
from pathlib import Path
from typing import List, Optional, Union

from .base import Tool, ToolRegistry
from .files import list_dir, read_file, write_file
from .sandbox import Sandbox
from .search import search_code
from .shell import run_command

__all__ = ["build_default_registry", "TOOL_NAMES"]

TOOL_NAMES = ("read_file", "write_file", "list_dir", "search_code", "run_command")

_PATH_DESCRIPTION = (
    "Chemin relatif à la racine du projet, par exemple « src/main.py » ou "
    "« tests/ ». Les chemins absolus hors du projet sont refusés."
)


def build_default_registry(
    root: Optional[Union[str, Path]] = None,
    sandbox: Optional[Sandbox] = None,
) -> ToolRegistry:
    """Construit le registre standard de l'agent.

    Args:
        root: dossier de travail (par défaut la racine du projet).
        sandbox: bac à sable déjà construit ; prioritaire sur ``root`` et
            pratique pour les tests (dossier temporaire).

    Returns:
        Un registre contenant ``read_file``, ``write_file``, ``list_dir``,
        ``search_code`` et ``run_command``.

    Raises:
        ToolError: si la racine de travail n'est pas un dossier valide.
    """
    box = sandbox or Sandbox(root)
    tools: List[Tool] = [
        Tool(
            name="read_file",
            description=(
                "Lit un fichier texte du projet et renvoie son contenu. "
                "Pour un gros fichier, lisez par plages avec start_line et "
                "max_lines. À appeler avant toute modification."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": _PATH_DESCRIPTION},
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Première ligne lue (1 par défaut).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "description": "Nombre de lignes renvoyées (400 par défaut).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=partial(read_file, box),
        ),
        Tool(
            name="write_file",
            description=(
                "Crée ou remplace entièrement un fichier texte du projet. Le "
                "contenu doit être complet : pour une modification ciblée, "
                "lisez d'abord le fichier puis réécrivez-le en entier. "
                "Écriture en UTF-8 avec fins de ligne LF."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": _PATH_DESCRIPTION},
                    "content": {
                        "type": "string",
                        "description": "Contenu complet du fichier après écriture.",
                    },
                    "create_dirs": {
                        "type": "boolean",
                        "description": "Créer les dossiers parents manquants (true par défaut).",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=partial(write_file, box),
        ),
        Tool(
            name="list_dir",
            description=(
                "Liste l'arborescence d'un dossier du projet (fichiers et "
                "sous-dossiers). Les dossiers techniques (.git, .venv, "
                "__pycache__, node_modules) ne sont pas explorés."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": _PATH_DESCRIPTION},
                    "max_depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Profondeur d'exploration (2 par défaut).",
                    },
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Nombre maximal d'entrées affichées (200 par défaut).",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=partial(list_dir, box),
        ),
        Tool(
            name="search_code",
            description=(
                "Recherche une expression régulière dans les fichiers du projet "
                "et renvoie les lignes « chemin:numéro: texte ». Le motif est "
                "insensible à la casse par défaut ; échappez les caractères "
                "spéciaux (ex. « print\\( »)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Expression régulière à rechercher.",
                    },
                    "path": {
                        "type": "string",
                        "description": _PATH_DESCRIPTION + " « . » par défaut.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Filtre sur le nom des fichiers (ex. « *.py », « test_*.py »).",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Respecter la casse (false par défaut).",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Nombre maximal de lignes renvoyées (50 par défaut).",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=partial(search_code, box),
        ),
        Tool(
            name="run_command",
            description=(
                "Exécute une commande de développement autorisée (git, python, "
                "pytest, node, npm…), sans shell et dans le dossier du projet. "
                "Les tubes, redirections et « && » sont refusés, tout comme les "
                "commandes destructrices ou d'installation réseau (git push, "
                "pip install…). Le code de sortie et les erreurs sont renvoyés."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Commande complète, ex. « git status --short » ou "
                            "« .venv/Scripts/python.exe -m pytest -q »."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Dossier d'exécution relatif au projet "
                            "(« . » par défaut)."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 300,
                        "description": "Délai maximal en secondes (30 par défaut).",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=partial(run_command, box),
            dangerous=True,
        ),
    ]
    return ToolRegistry(tools)
