"""Outils locaux exposés au modèle : registre, bac à sable et handlers.

Utilisation :
    from tools import build_default_registry

    registre = build_default_registry()
    print(registre.names)
    resultat = registre.execute("list_dir", {"path": "."})
    print(resultat.content)

Les cinq outils disponibles sont ``read_file``, ``write_file``, ``list_dir``,
``search_code`` et ``run_command``. Chaque handler reçoit un
:class:`~tools.sandbox.Sandbox` en premier argument (lié par le registre), donc
tous les chemins sont vérifiés avant d'atteindre le disque.
"""

from .base import Tool, ToolError, ToolRegistry, ToolResult, validate_arguments
from .files import list_dir, read_file, write_file
from .registry import TOOL_NAMES, build_default_registry
from .sandbox import EXCLUDED_DIRS, Sandbox
from .search import search_code
from .shell import ALLOWED_COMMANDS, run_command

__all__ = [
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "validate_arguments",
    "Sandbox",
    "EXCLUDED_DIRS",
    "build_default_registry",
    "TOOL_NAMES",
    "read_file",
    "write_file",
    "list_dir",
    "search_code",
    "run_command",
    "ALLOWED_COMMANDS",
]
