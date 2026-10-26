"""Socle commun des outils : erreurs, définition, registre et validation.

Un outil se compose de trois éléments :
    - un nom, celui que le modèle renvoie dans ``tool_calls`` ;
    - un schéma JSON au format « function calling » envoyé au LLM ;
    - un *handler* Python, exécuté uniquement si le nom existe dans le registre.

Aucune exécution arbitraire n'est possible : seuls les outils enregistrés sont
joignables, et les arguments sont validés contre le schéma avant l'appel du
handler (arguments inconnus, manquants ou mal typés refusés).

Utilisation :
    from tools import build_default_registry

    registre = build_default_registry()
    resultat = registre.execute("list_dir", {"path": "."})
    print(resultat.content)
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional

__all__ = [
    "ToolError",
    "ToolResult",
    "Tool",
    "ToolRegistry",
    "validate_arguments",
]

# Signature d'un handler : reçoit les arguments validés, renvoie du texte.
ToolHandler = Callable[..., str]

# Types JSON Schema acceptés -> types Python correspondants.
_JSON_TYPES: Dict[str, tuple] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


class ToolError(RuntimeError):
    """Erreur d'outil destinée au modèle (message clair, sans trace technique).

    Le registre l'attrape et la transforme en résultat ``ok=False`` : l'agent
    renvoie le message au LLM pour qu'il puisse se corriger, au lieu de planter.
    """


def validate_arguments(
    parameters: Mapping[str, Any],
    arguments: Optional[Mapping[str, Any]],
    tool_name: str = "?",
) -> Dict[str, Any]:
    """Valide les arguments d'un appel d'outil contre son schéma JSON.

    Args:
        parameters: schéma de l'outil (``{"type": "object", "properties": ...}``).
        arguments: arguments fournis par le modèle (``None`` = aucun).
        tool_name: nom utilisé dans les messages d'erreur.

    Returns:
        Un nouveau dict ne contenant que les arguments déclarés dans le schéma.

    Raises:
        ToolError: argument inconnu, obligatoire manquant, type incorrect ou
            valeur hors énumération.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ToolError(
            f"Les arguments de « {tool_name} » doivent être un objet JSON "
            f"(reçu : {type(arguments).__name__})."
        )

    properties: Mapping[str, Any] = parameters.get("properties") or {}
    required = list(parameters.get("required") or [])

    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ToolError(
            f"Argument(s) inconnu(s) pour « {tool_name} » : {', '.join(unknown)}. "
            f"Arguments acceptés : {', '.join(sorted(properties)) or '(aucun)'}."
        )

    missing = [item for item in required if arguments.get(item) is None]
    if missing:
        raise ToolError(
            f"Argument(s) obligatoire(s) manquant(s) pour « {tool_name} » : "
            f"{', '.join(missing)}."
        )

    cleaned: Dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None:
            # Optionnel explicitement nul : on laisse la valeur par défaut du handler.
            continue
        spec = properties.get(key) or {}
        expected = spec.get("type")
        types = _JSON_TYPES.get(str(expected)) if expected else None
        if types is not None:
            if isinstance(value, bool) and expected in ("integer", "number"):
                raise ToolError(
                    f"« {key} » doit être de type {expected} (booléen reçu)."
                )
            if not isinstance(value, types):
                raise ToolError(
                    f"« {key} » doit être de type {expected} "
                    f"(reçu : {type(value).__name__})."
                )
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            raise ToolError(
                f"« {key} » doit valoir l'une de ces valeurs : "
                f"{', '.join(str(item) for item in allowed)}."
            )
        cleaned[key] = value
    return cleaned


@dataclass
class ToolResult:
    """Résultat d'un appel d'outil, prêt à être réinjecté dans la conversation."""

    name: str
    ok: bool
    content: str
    error: Optional[str] = None

    def to_message(self, call_id: str) -> Dict[str, Any]:
        """Message ``tool`` au format OpenAI, à ajouter à l'historique."""
        return {"role": "tool", "tool_call_id": call_id, "content": self.content}

    def __repr__(self) -> str:
        return f"ToolResult(name={self.name!r}, ok={self.ok}, content={self.content[:60]!r})"


@dataclass
class Tool:
    """Déclaration complète d'un outil : nom, schéma JSON et handler."""

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler
    dangerous: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(
                f"Nom d'outil invalide : {self.name!r} "
                "(lettres ASCII, chiffres et « _ » uniquement)."
            )
        if self.parameters.get("type") != "object":
            raise ValueError(
                f"Le schéma de « {self.name} » doit être un objet JSON "
                "(« type »: « object »)."
            )
        if not callable(self.handler):
            raise ValueError(f"Le handler de « {self.name} » doit être appelable.")

    @property
    def schema(self) -> Dict[str, Any]:
        """Schéma au format « function calling » attendu par le client LLM."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Collection d'outils joignables par leur nom.

    Example:
        >>> registre = ToolRegistry([Tool("ping", "Test", {"type": "object"}, lambda: "pong")])
        >>> registre.execute("ping", {}).content
        'pong'
        >>> registre.execute("rm_rf", {}).ok
        False
    """

    def __init__(self, tools: Optional[Iterable[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        """Ajoute un outil ; deux outils ne peuvent pas partager un nom."""
        if not isinstance(tool, Tool):
            raise TypeError(f"Objet attendu : Tool (reçu : {type(tool).__name__}).")
        if tool.name in self._tools:
            raise ValueError(f"Outil déjà enregistré : {tool.name!r}.")
        self._tools[tool.name] = tool
        return tool

    @property
    def names(self) -> List[str]:
        """Noms des outils, triés (ordre stable pour les schémas et les logs)."""
        return sorted(self._tools)

    @property
    def schemas(self) -> List[Dict[str, Any]]:
        """Schémas à passer à ``LLMClient.chat(tools=...)``."""
        return [self._tools[name].schema for name in self.names]

    def get(self, name: str) -> Tool:
        """Retourne un outil par son nom.

        Raises:
            ToolError: nom inconnu (le message liste les outils disponibles).
        """
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(
                f"Outil inconnu « {name} ». Outils disponibles : "
                f"{', '.join(self.names) or '(aucun)'}."
            ) from None

    def execute(
        self, name: str, arguments: Optional[Mapping[str, Any]] = None
    ) -> ToolResult:
        """Exécute un outil après validation des arguments.

        Les erreurs d'outil ne sont jamais propagées : elles deviennent un
        résultat ``ok=False`` que l'agent renvoie au modèle pour qu'il corrige
        son appel.

        Args:
            name: nom de l'outil demandé par le modèle.
            arguments: arguments issus du JSON de l'appel (peut être ``None``).

        Returns:
            Le résultat, avec le texte destiné au modèle dans ``content``.
        """
        try:
            tool = self.get(name)
            cleaned = validate_arguments(tool.parameters, arguments, tool.name)
            output = tool.handler(**cleaned)
        except ToolError as exc:
            return ToolResult(
                name=name, ok=False, content=f"ERREUR ({name}) : {exc}", error=str(exc)
            )
        except TypeError as exc:
            return ToolResult(
                name=name,
                ok=False,
                content=(
                    f"ERREUR ({name}) : arguments incompatibles avec l'outil "
                    f"({exc}). Vérifiez le schéma et réémettez l'appel."
                ),
                error=str(exc),
            )
        except Exception as exc:  # jamais de crash : l'agent doit pouvoir continuer
            message = (
                f"ERREUR inattendue dans l'outil « {name} » "
                f"({type(exc).__name__}) : {exc}. Signalez ce problème."
            )
            return ToolResult(name=name, ok=False, content=message, error=str(exc))
        return ToolResult(name=name, ok=True, content=str(output))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools[name] for name in self.names)

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry({self.names})"
