"""Client d'API LLM compatible OpenAI.

Ce module isole tout le code réseau : le reste de l'agent ne consomme que des
objets Python simples (:class:`LLMResponse`, :class:`ToolCall`) et n'a jamais
besoin de connaître le SDK.

Compatibilité : toute API « compatible OpenAI » — OpenAI, Groq, Mistral,
Together, Ollama, LM Studio… Le fournisseur et le modèle se choisissent
uniquement via les variables d'environnement de :mod:`config`.

Les tentatives en cas d'erreur réseau, de quota (429) ou de 5xx sont gérées par
le SDK (``max_retries`` + backoff exponentiel) ; ce module se contente de
traduire les erreurs en messages explicites et actionnables.

Test rapide de la configuration et de la connexion :
    python src/llm_client.py
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

try:  # exécution en module : python -m src.llm_client
    from .config import ConfigError, Settings, load_settings
except ImportError:  # exécution directe : python src/llm_client.py
    from config import ConfigError, Settings, load_settings

__all__ = [
    "LLMError",
    "ToolCall",
    "LLMResponse",
    "LLMClient",
    "ConfigError",
    "Settings",
]


class LLMError(RuntimeError):
    """Erreur d'appel à l'API du LLM (réseau, authentification, format)."""


@dataclass
class ToolCall:
    """Demande d'exécution d'outil formulée par le modèle."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    arguments_error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """Faux si le modèle a produit des arguments inexploitables."""
        return self.arguments_error is None and bool(self.name)

    def to_dict(self) -> Dict[str, Any]:
        """Représentation ``tool_calls`` du protocole OpenAI."""
        arguments = self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False)
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": arguments},
        }


@dataclass
class LLMResponse:
    """Réponse normalisée du LLM : texte, appels d'outils et consommation."""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    model: str = ""
    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        """Vrai si le modèle demande au moins un outil."""
        return bool(self.tool_calls)

    def to_assistant_message(self) -> Dict[str, Any]:
        """Message ``assistant`` à réinjecter dans l'historique de conversation."""
        message: Dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        return message

    def __repr__(self) -> str:  # évite d'inonder les logs avec tout le texte
        return (
            f"LLMResponse(model={self.model!r}, text={self.text[:60]!r}, "
            f"tool_calls={[call.name for call in self.tool_calls]}, "
            f"finish_reason={self.finish_reason!r}, tokens={self.total_tokens})"
        )


class LLMClient:
    """Client minimal autour de ``openai.OpenAI().chat.completions``.

    Example:
        >>> from config import load_settings
        >>> client = LLMClient(load_settings())
        >>> response = client.chat([{"role": "user", "content": "Bonjour"}])
        >>> response.text
        '...'
    """

    def __init__(
        self, settings: Optional[Settings] = None, client: Optional[Any] = None
    ) -> None:
        """Initialise le client.

        Args:
            settings: configuration ; chargée depuis l'environnement si absente.
            client: client SDK déjà construit (injection de dépendance pour les
                tests : aucun réseau n'est alors utilisé).
        """
        self.settings = settings or load_settings()
        self._client = client
        self._tokens_used = 0

    @property
    def client(self) -> Any:
        """Client SDK, construit au premier appel (import paresseux d'openai)."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    @property
    def tokens_used(self) -> int:
        """Total de tokens consommés depuis la création du client."""
        return self._tokens_used

    def reset_token_counter(self) -> None:
        """Remet le compteur de tokens à zéro."""
        self._tokens_used = 0

    def __repr__(self) -> str:
        return (
            f"LLMClient(model={self.settings.model!r}, "
            f"base_url={self.settings.base_url!r}, "
            f"api_key={self.settings.masked_api_key()}, "
            f"tokens_used={self._tokens_used})"
        )

    def _create_client(self) -> Any:
        """Instancie le client `openai` à partir des paramètres validés."""
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:  # pragma: no cover - dépend de l'install
            raise LLMError(
                "Le paquet « openai » n'est pas installé. "
                "Lancez : python -m pip install -r requirements.txt"
            ) from exc
        options: Dict[str, Any] = {
            "api_key": self.settings.effective_api_key,
            "timeout": self.settings.timeout,
            "max_retries": self.settings.max_retries,
        }
        if self.settings.base_url:
            options["base_url"] = self.settings.base_url
        return OpenAI(**options)

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Any]] = None,
        tool_choice: Optional[str] = None,
        stream: bool = False,
        on_token: Optional[Callable[[str], None]] = None,
        extra_params: Optional[Mapping[str, Any]] = None,
    ) -> LLMResponse:
        """Envoie une conversation au modèle et normalise la réponse.

        Args:
            messages: historique au format OpenAI (``{"role": ..., ...}``).
            tools: schémas d'outils (dicts OpenAI) ou objets exposant ``.schema``.
            tool_choice: ``"auto"`` par défaut dès que ``tools`` est fourni.
            stream: active le flux ; le texte est transmis à ``on_token``.
            on_token: callback appelé à chaque fragment de texte en mode flux.
            extra_params: paramètres additionnels passés au SDK ; une valeur
                ``None`` retire un paramètre construit automatiquement, par
                exemple ``{"max_tokens": None, "max_completion_tokens": 8192}``
                pour les modèles de raisonnement.

        Returns:
            La réponse normalisée ; les compteurs de tokens sont cumulés.

        Raises:
            LLMError: erreur réseau, d'authentification ou de format.
            ConfigError: clé API absente pour une API distante.
        """
        if not messages:
            raise ValueError("`messages` ne peut pas être vide.")
        params = self._build_params(messages, tools, tool_choice, extra_params)
        if stream:
            params["stream"] = True
            return self._stream(params, on_token)
        return self._complete(params)

    def _build_params(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Optional[Sequence[Any]],
        tool_choice: Optional[str],
        extra_params: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Assemble les paramètres de l'appel API."""
        params: Dict[str, Any] = {
            "model": self.settings.model,
            "messages": [dict(message) for message in messages],
        }
        if self.settings.temperature is not None:
            params["temperature"] = self.settings.temperature
        params["max_tokens"] = self.settings.max_tokens

        schemas = self._tool_schemas(tools)
        if schemas:
            params["tools"] = schemas
            params["tool_choice"] = tool_choice or "auto"

        for key, value in (extra_params or {}).items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        return params

    @staticmethod
    def _tool_schemas(tools: Optional[Sequence[Any]]) -> List[Dict[str, Any]]:
        """Accepte des dicts OpenAI ou des objets ``Tool`` (étape 5) avec ``.schema``."""
        schemas: List[Dict[str, Any]] = []
        for tool in tools or []:
            schema = getattr(tool, "schema", tool)
            if not isinstance(schema, dict):
                raise TypeError(f"Schéma d'outil invalide : {tool!r}")
            schemas.append(schema)
        return schemas

    def _complete(self, params: Dict[str, Any]) -> LLMResponse:
        """Exécute un appel bloquant (sans flux)."""
        try:
            completion = self.client.chat.completions.create(**params)
        except ConfigError:
            raise
        except Exception as exc:
            raise LLMError(self._explain_error(exc)) from exc
        return self._from_completion(completion)

    def _from_completion(self, completion: Any) -> LLMResponse:
        """Convertit une réponse du SDK en :class:`LLMResponse`."""
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise LLMError("Réponse de l'API sans `choices` : format inattendu.")
        choice = choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        text = content.strip() if isinstance(content, str) else ""
        calls = [
            self._parse_tool_call(call)
            for call in (getattr(message, "tool_calls", None) or [])
        ]
        return self._build_response(
            text=text,
            tool_calls=calls,
            model=getattr(completion, "model", "") or self.settings.model,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=getattr(completion, "usage", None),
        )

    def _stream(
        self, params: Dict[str, Any], on_token: Optional[Callable[[str], None]]
    ) -> LLMResponse:
        """Exécute un appel en flux, en accumulant texte et appels d'outils.

        Note: selon le fournisseur, ``usage`` peut être absent en mode flux
        (les compteurs de tokens valent alors 0).
        """
        chunks: List[str] = []
        fragments: Dict[int, Dict[str, str]] = {}
        finish_reason: Optional[str] = None
        model = params.get("model", "")
        usage: Any = None
        try:
            stream = self.client.chat.completions.create(**params)
            for chunk in stream:
                model = getattr(chunk, "model", None) or model
                usage = getattr(chunk, "usage", None) or usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if content:
                    chunks.append(content)
                    if on_token is not None:
                        on_token(content)
                for fragment in getattr(delta, "tool_calls", None) or []:
                    self._accumulate_fragment(fragments, fragment)
        except ConfigError:
            raise
        except Exception as exc:
            raise LLMError(self._explain_error(exc)) from exc

        tool_calls = [
            self._tool_call_from_parts(item["id"], item["name"], item["arguments"])
            for _, item in sorted(fragments.items())
        ]
        return self._build_response(
            text="".join(chunks).strip(),
            tool_calls=tool_calls,
            model=model,
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _accumulate_fragment(
        fragments: Dict[int, Dict[str, str]], fragment: Any
    ) -> None:
        """Recolle les fragments de ``tool_calls`` envoyés en mode flux."""
        index = getattr(fragment, "index", 0)
        entry = fragments.setdefault(
            0 if index is None else int(index), {"id": "", "name": "", "arguments": ""}
        )
        call_id = getattr(fragment, "id", None)
        if call_id:
            entry["id"] = call_id
        function = getattr(fragment, "function", None)
        if function is None:
            return
        name = getattr(function, "name", None)
        if name:
            entry["name"] += name
        arguments = getattr(function, "arguments", None)
        if arguments:
            entry["arguments"] += arguments

    def _parse_tool_call(self, call: Any) -> ToolCall:
        """Convertit un ``tool_call`` du SDK en :class:`ToolCall`."""
        function = getattr(call, "function", None)
        name = (getattr(function, "name", "") or "").strip()
        raw = getattr(function, "arguments", "") or ""
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        return self._tool_call_from_parts(getattr(call, "id", "") or "", name, raw)

    @staticmethod
    def _tool_call_from_parts(call_id: str, name: str, raw: str) -> ToolCall:
        """Parse les arguments JSON ; l'erreur est conservée, pas levée.

        Un JSON invalide ne doit pas interrompre la boucle agentique : l'agent
        renvoie l'erreur au modèle pour qu'il corrige son appel.
        """
        arguments: Dict[str, Any] = {}
        error: Optional[str] = None
        text = (raw or "").strip()
        if text:
            try:
                decoded = json.loads(text)
            except ValueError as exc:
                error = f"Arguments JSON invalides pour l'outil « {name} » : {exc}"
            else:
                if isinstance(decoded, dict):
                    arguments = decoded
                else:
                    error = (
                        f"Les arguments de l'outil « {name} » doivent former un "
                        f"objet JSON (reçu : {type(decoded).__name__})."
                    )
        return ToolCall(
            id=call_id,
            name=name,
            arguments=arguments,
            raw_arguments=raw or "",
            arguments_error=error,
        )

    def _build_response(
        self,
        text: str,
        tool_calls: List[ToolCall],
        model: str,
        finish_reason: Optional[str],
        usage: Any,
    ) -> LLMResponse:
        """Construit la réponse et met à jour le compteur de tokens."""
        prompt_tokens = self._usage_value(usage, "prompt_tokens")
        completion_tokens = self._usage_value(usage, "completion_tokens")
        total_tokens = self._usage_value(usage, "total_tokens")
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens
        self._tokens_used += total_tokens
        return LLMResponse(
            text=text,
            tool_calls=list(tool_calls),
            model=model,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _usage_value(usage: Any, name: str) -> int:
        """Lit un champ de ``usage`` (objet SDK ou dict)."""
        if usage is None:
            return 0
        if isinstance(usage, Mapping):
            return int(usage.get(name, 0) or 0)
        return int(getattr(usage, name, 0) or 0)

    def check_connection(self) -> str:
        """Envoie une requête minimale pour valider clé, endpoint et modèle.

        Returns:
            Le texte renvoyé par le modèle.

        Raises:
            LLMError: si l'appel échoue (message explicite).
            ConfigError: si la clé API est absente pour une API distante.
        """
        response = self.chat(
            [{"role": "user", "content": "Réponds uniquement par : OK"}],
            extra_params={"max_tokens": 16, "temperature": 0},
        )
        return response.text

    def _explain_error(self, exc: Exception) -> str:
        """Traduit les exceptions du SDK en messages actionnables."""
        kind = type(exc).__name__
        model = self.settings.model
        key = self.settings.masked_api_key()
        base_url = self.settings.base_url

        if kind == "AuthenticationError":
            return (
                f"Authentification refusée (401) par {base_url} : la clé API est "
                f"invalide ou expirée (clé utilisée : {key})."
            )
        if kind == "PermissionDeniedError":
            return (
                f"Accès refusé (403) au modèle « {model} » : la clé ne dispose pas "
                "des droits nécessaires."
            )
        if kind == "NotFoundError":
            return (
                f"Modèle ou endpoint introuvable (404) : vérifiez "
                f"LLM_MODEL={model!r} et LLM_BASE_URL={base_url!r}."
            )
        if kind == "RateLimitError":
            return (
                "Quota ou débit dépassé (429) malgré les tentatives automatiques : "
                "réessayez plus tard ou réduisez la taille des requêtes."
            )
        if kind == "BadRequestError":
            return (
                f"Requête refusée (400) par l'API : {exc}. Vérifiez le nom du "
                f"modèle « {model} » et les paramètres de génération."
            )
        if kind in ("APIConnectionError", "APITimeoutError"):
            return (
                f"Impossible de joindre {base_url} ({kind}). Pour un modèle local, "
                "vérifiez qu'Ollama ou LM Studio est démarré ; sinon contrôlez la "
                "connexion réseau et LLM_TIMEOUT."
            )
        return f"Erreur lors de l'appel au LLM ({kind}) : {exc}"


def main() -> int:
    """Teste la configuration puis la connexion au LLM.

    Returns:
        Code de sortie du processus (0 = succès).
    """
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[ERREUR] Configuration invalide : {exc}")
        return 1

    print(f"Modèle   : {settings.model}")
    print(f"Endpoint : {settings.base_url}")
    print(f"Clé API  : {settings.masked_api_key()}")

    try:
        answer = LLMClient(settings).check_connection()
    except (ConfigError, LLMError) as exc:
        print(f"[ERREUR] Connexion impossible : {exc}")
        return 1

    print(f"[OK] Connexion établie. Réponse du modèle : {answer!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
