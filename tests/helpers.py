"""Utilitaires partages par les tests : projet temporaire et client LLM factice.

Aucun test ne touche le reseau : le client scripte ci-dessous rejoue une liste de
reponses preparees, exactement comme le ferait l'API.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.llm_client import LLMResponse, ToolCall

__all__ = [
    "FakeClient",
    "ecrire_structure",
    "reponse_finale",
    "reponse_outil",
]


def ecrire_structure(root: Path) -> Path:
    """Cree une arborescence de projet representative dans ``root``.

    Args:
        root: dossier (generalement ``tmp_path``) ou ecrire les fichiers.

    Returns:
        Le dossier, pour permettre l'enchainement.
    """
    (root / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "src" / "main.py").write_text("print('bonjour')\n", encoding="utf-8")
    (root / "src" / "pkg" / "util.py").write_text("VALUE = 42\n", encoding="utf-8")
    (root / "notes.md").write_text("ligne 1\nligne 2\nligne 3\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01\x02SECRET")
    # Fichiers sensibles : jamais lisibles ni modifiables par le modele.
    (root / ".env").write_text(
        "LLM_API_KEY=sk-secret-a-never-leak\n", encoding="utf-8"
    )
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "node_modules").mkdir(exist_ok=True)
    return root


class FakeClient:
    """Client LLM scripte : rejoue des reponses, sans le moindre appel reseau.

    Example:
        >>> client = FakeClient([reponse_finale("fini")])
        >>> client.chat([{"role": "user", "content": "x"}]).text
        'fini'
    """

    def __init__(self, responses: Sequence[Any]) -> None:
        """Prepare la file de reponses (un ``LLMResponse`` ou une exception)."""
        self.responses: List[Any] = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.tokens_used = 0

    def chat(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[Sequence[Any]] = None,
        stream: bool = False,
        on_token: Optional[Any] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Enregistre l'appel puis renvoie la reponse suivante.

        Raises:
            AssertionError: plus aucune reponse n'est scriptee.
            BaseException: l'element scripte est une exception a propager.
        """
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
                "stream": stream,
                "on_token": on_token,
                "extra": kwargs,
            }
        )
        if not self.responses:
            raise AssertionError("FakeClient : aucune reponse scriptee restante.")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        self.tokens_used += item.total_tokens
        return item


def reponse_outil(
    name: str,
    arguments: Optional[Dict[str, Any]] = None,
    call_id: str = "call-1",
    total_tokens: int = 0,
) -> LLMResponse:
    """Construit une reponse qui demande un appel d'outil."""
    return LLMResponse(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments or {}))],
        total_tokens=total_tokens,
        prompt_tokens=total_tokens,
    )


def reponse_finale(text: str, total_tokens: int = 0) -> LLMResponse:
    """Construit une reponse finale (aucun appel d'outil)."""
    return LLMResponse(text=text, total_tokens=total_tokens, prompt_tokens=total_tokens)
