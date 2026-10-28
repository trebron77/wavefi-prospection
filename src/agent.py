"""Boucle agentique : raisonner -> agir (outil) -> observer -> conclure.

L'agent envoie la demande au LLM avec les schémas d'outils, exécute les outils
demandés via le registre (lui-même borné par le bac à sable), réinjecte les
résultats dans la conversation et recommence jusqu'à une réponse finale ou
jusqu'à ``MAX_ITERATIONS``.

Utilisation :
    python src/agent.py -p "Résume la structure du projet"
    python src/agent.py -p "Ajoute une fonction somme dans src/main.py" --max-iterations 8
    python src/agent.py -p "Lance les tests" --yes     (sans confirmation des commandes)
    python src/agent.py --repl           (session interactive multi-tours)
    python src/agent.py --dry-run        (aucun appel réseau)
"""

import argparse
import json
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

try:  # exécution en package : python -m src.agent
    from . import __version__
    from .config import PROJECT_ROOT, ConfigError, Settings, load_settings
    from .context import build_project_context, trim_messages
    from .llm_client import LLMClient, LLMError, LLMResponse, ToolCall
    from .tools import ToolRegistry, build_default_registry, read_git_diff
except ImportError:  # exécution directe : python src/agent.py
    from __init__ import __version__
    from config import PROJECT_ROOT, ConfigError, Settings, load_settings
    from context import build_project_context, trim_messages
    from llm_client import LLMClient, LLMError, LLMResponse, ToolCall
    from tools import ToolRegistry, build_default_registry, read_git_diff

__all__ = [
    "Agent",
    "AgentResult",
    "EventLogger",
    "SYSTEM_PROMPT",
    "main",
]

DEFAULT_MAX_ITERATIONS = 15
LOG_DIRECTORY = "logs"
LOG_PREVIEW = 2_000
STOP_FINAL = "final_answer"
STOP_MAX_ITERATIONS = "max_iterations"
STOP_TOKEN_BUDGET = "token_budget"
STOP_ERROR = "error"

#: Valeurs acceptees par le parametre ``tool_choice`` du protocole OpenAI.
TOOL_CHOICE_VALUES = ("auto", "required", "none")

#: Strategie par defaut : le modele decide lui-meme d'appeler un outil.
DEFAULT_TOOL_CHOICE = "auto"

SYSTEM_PROMPT = """Tu es un agent de développement autonome qui travaille sur un projet \
local, en binôme avec un utilisateur.

Règles de travail :
- Réponds en français, de façon concise et factuelle.
- Dès qu'une action est demandée (créer, lire, lister, modifier, exécuter), appelle
  l'outil adapté au lieu d'annoncer ce que tu ferais : l'utilisateur attend le résultat,
  pas une promesse ni une demande de confirmation supplémentaire.
- Observe avant d'agir : ne suppose jamais le contenu d'un fichier, lis-le avec read_file.
- write_file remplace tout le fichier : relis-le d'abord et fournis le contenu complet.
- Vérifie ton travail avec run_command (tests, compilation, git diff) quand c'est pertinent.
- Reste dans le dossier du projet : les chemins hors projet et les fichiers .env sont refusés.
- Un outil en erreur ou une commande refusée n'est pas fatal : lis le message et adapte-toi.
- N'invente jamais un résultat d'outil ; si une tâche est impossible, explique pourquoi.
- Termine par une réponse finale sans appel d'outil : ce que tu as fait, où, et comment tu
  l'as vérifié."""


@dataclass
class AgentResult:
    """Résultat d'une exécution de l'agent."""

    answer: str
    stopped_reason: str = STOP_FINAL
    iterations: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    messages: List[Dict[str, Any]] = field(default_factory=list)
    log_path: Optional[str] = None

    @property
    def success(self) -> bool:
        """Vrai si l'agent s'est arrêté sur une réponse finale (pas une borne)."""
        return self.stopped_reason == STOP_FINAL

    def summary(self) -> str:
        """Résumé d'une ligne, utile pour les logs et la CLI."""
        return (
            f"{self.stopped_reason} | {self.iterations} itération(s) | "
            f"{self.tool_calls} outil(s) | {self.total_tokens} tokens"
        )

    def __repr__(self) -> str:
        return (
            f"AgentResult(success={self.success}, answer={self.answer[:60]!r}, "
            f"iterations={self.iterations}, tool_calls={self.tool_calls}, "
            f"total_tokens={self.total_tokens})"
        )


class EventLogger:
    """Journal JSONL d'une session : une ligne = un événement horodaté.

    Le journal sert au débogage et à l'audit (quel outil, quels arguments, quel
    résultat). Il ne doit jamais interrompre l'agent : les erreurs d'écriture
    sont ignorées silencieusement.

    Example:
        >>> with EventLogger("logs/demo.jsonl") as journal:
        ...     journal.log("tool_call", name="read_file", path="README.md")
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a", encoding="utf-8", newline="\n")

    @classmethod
    def for_session(
        cls, directory: Union[str, Path] = LOG_DIRECTORY, prefix: str = "session"
    ) -> "EventLogger":
        """Crée un journal horodaté, par exemple ``logs/session-20261025-143012.jsonl``."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return cls(Path(directory) / f"{prefix}-{stamp}.jsonl")

    def log(self, event: str, **fields: Any) -> None:
        """Ajoute un événement (jamais d'exception remontée)."""
        payload: Dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
        }
        payload.update(fields)
        try:
            self._handle.write(
                json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            )
            self._handle.flush()
        except (OSError, TypeError, ValueError):  # pragma: no cover - disque plein…
            pass

    def close(self) -> None:
        """Ferme le fichier de journal (idempotent)."""
        try:
            self._handle.close()
        except OSError:  # pragma: no cover
            pass

    def __enter__(self) -> "EventLogger":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"EventLogger({str(self.path)!r})"


class Agent:
    """Boucle agentique bornée, avec outils locaux et journal JSONL.

    Example:
        >>> from config import load_settings
        >>> agent = Agent(load_settings(), verbose=False)
        >>> resultat = agent.run("Liste les fichiers de src/")
        >>> resultat.stopped_reason
        'final_answer'
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        client: Optional[Any] = None,
        registry: Optional[ToolRegistry] = None,
        root: Optional[Union[str, Path]] = None,
        max_iterations: Optional[int] = None,
        token_budget: Optional[int] = None,
        logger: Optional[EventLogger] = None,
        verbose: bool = True,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        on_token: Optional[Callable[[str], None]] = None,
        confirm: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        context_tokens: Optional[int] = None,
        initial_context: bool = False,
        tool_choice: Optional[str] = None,
    ) -> None:
        """Prépare l'agent.

        Args:
            settings: configuration ; chargée depuis ``.env`` si absente.
            client: client LLM alternatif (injection de dépendance pour les
                tests : un objet exposant ``chat(...)`` suffit).
            registry: registre d'outils ; construit avec ``root`` si absent.
            root: racine du bac à sable (par défaut la racine du projet).
            max_iterations: borne d'itérations ; ``MAX_ITERATIONS`` par défaut.
            token_budget: budget de tokens ; ``None`` = pas de limite.
            logger: journal JSONL à alimenter (facultatif).
            verbose: afficher la progression sur la sortie standard.
            on_event: callback ``(evenement, champs)`` pour une interface.
            system_prompt: prompt système alternatif.
            stream: demander la réponse du modèle en flux.
            on_token: callback appelé à chaque fragment de texte (mode flux).
            confirm: callback ``(outil, arguments) -> bool`` consulté avant tout
                outil ``dangerous`` (``run_command``). ``None`` = aucune
                confirmation (comportement de ``--yes``).
            context_tokens: budget estimé de l'historique envoyé au modèle ;
                ``None`` = pas de troncature.
            initial_context: injecter au premier tour un instantané du projet
                (arborescence, extrait du README, ``git status``).
            tool_choice: stratégie d'appel des outils : ``"auto"`` (défaut, le
                modèle décide), ``"required"`` (imposer un appel d'outil au
                premier tour, puis revenir à ``"auto"`` pour laisser le modèle
                conclure) ou ``"none"`` (aucun outil : conversation pure).

        Raises:
            ValueError: ``max_iterations`` < 1, ``token_budget`` <= 0,
                ``context_tokens`` <= 0 ou ``tool_choice`` inconnu.
            TypeError: ``confirm`` fourni mais non appelable.
        """
        self.settings = settings or load_settings()
        self.client = client or LLMClient(self.settings)
        self.registry = registry or build_default_registry(root=root)
        self.root = Path(root if root is not None else PROJECT_ROOT).resolve()

        bound = self.settings.max_iterations if max_iterations is None else max_iterations
        if int(bound) < 1:
            raise ValueError("max_iterations doit être >= 1.")
        self.max_iterations = int(bound)
        if token_budget is not None and int(token_budget) <= 0:
            raise ValueError("token_budget doit être > 0.")
        self.token_budget = int(token_budget) if token_budget is not None else None

        self.logger = logger
        self.verbose = verbose
        self.on_event = on_event
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.stream = stream
        self.on_token = on_token
        if confirm is not None and not callable(confirm):
            raise TypeError("confirm doit être appelable ou None.")
        self.confirm = confirm
        if context_tokens is not None and int(context_tokens) <= 0:
            raise ValueError("context_tokens doit être > 0 (ou None).")
        self.context_tokens = int(context_tokens) if context_tokens is not None else None
        self.initial_context = bool(initial_context)
        choix = (tool_choice or DEFAULT_TOOL_CHOICE).strip().lower()
        if choix not in TOOL_CHOICE_VALUES:
            raise ValueError(
                f"tool_choice doit être l'un de "
                f"{', '.join(TOOL_CHOICE_VALUES)} (reçu : {tool_choice!r})."
            )
        self.tool_choice = choix
        self._context_cache: Optional[str] = None
        self._context_injected = False
        self._history: List[Dict[str, Any]] = []

    @property
    def history(self) -> List[Dict[str, Any]]:
        """Copie de l'historique de la dernière conversation."""
        return [dict(message) for message in self._history]

    def reset_history(self) -> None:
        """Efface l'historique (l'agent redevient vierge pour la tâche suivante).

        L'instantané du projet, s'il était injecté, le sera de nouveau à la
        prochaine demande (le texte est déjà en cache).
        """
        self._history = []
        self._context_injected = False

    def __repr__(self) -> str:
        return (
            f"Agent(model={self.settings.model!r}, "
            f"max_iterations={self.max_iterations}, "
            f"tools={len(self.registry)}, "
            f"tokens_used={getattr(self.client, 'tokens_used', 0)})"
        )

    def run(
        self,
        task: str,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> AgentResult:
        """Exécute une tâche de bout en bout (raisonner / agir / observer).

        La boucle s'arrête dès que le modèle répond sans appel d'outil, ou
        lorsqu'une borne est atteinte (``max_iterations`` ou ``token_budget``).
        Les erreurs d'outil sont renvoyées au modèle, jamais levées.

        Args:
            task: demande en langage naturel.
            history: historique initial ; celui de l'agent par défaut.

        Returns:
            Le résultat complet (réponse, raison d'arrêt, compteurs, historique).

        Raises:
            ValueError: tâche vide.
        """
        prompt = (task or "").strip()
        if not prompt:
            raise ValueError("La tâche est vide : décrivez ce que l'agent doit faire.")

        messages: List[Dict[str, Any]] = [
            dict(item)
            for item in (self._history if history is None else history)
        ]
        if not any(message.get("role") == "system" for message in messages):
            messages.insert(0, {"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})
        self._injecter_contexte(messages)
        messages, omis = trim_messages(messages, self.context_tokens)
        if omis:
            self._emit(
                "context_trimmed",
                f"[contexte] {omis} message(s) omis (budget = "
                f"{self.context_tokens} tokens estimés)",
                omitted=omis,
                max_tokens=self.context_tokens,
            )

        self._emit(
            "session_start",
            f"[agent] modèle={self.settings.model} | outils={len(self.registry)} | "
            f"itérations max={self.max_iterations}",
            task=prompt,
            model=self.settings.model,
            max_iterations=self.max_iterations,
            tools=self.registry.names,
        )

        answer = ""
        stop = STOP_MAX_ITERATIONS
        iterations = 0
        executed = 0
        prompt_tokens = completion_tokens = total_tokens = 0
        cache: Dict[str, str] = {}

        try:
            for iteration in range(1, self.max_iterations + 1):
                iterations = iteration
                response = self._ask(messages, iteration)
                prompt_tokens += response.prompt_tokens
                completion_tokens += response.completion_tokens
                total_tokens += response.total_tokens

                if not response.tool_calls:
                    answer = response.text.strip() or "(le modèle n'a renvoyé aucun texte)"
                    messages.append({"role": "assistant", "content": response.text})
                    stop = STOP_FINAL
                    break

                messages.append(response.to_assistant_message())
                for call in response.tool_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": self._execute(call, cache, iteration),
                        }
                    )
                    executed += 1

                if self.token_budget is not None and total_tokens >= self.token_budget:
                    answer = (
                        f"Budget de tokens atteint ({total_tokens} >= "
                        f"{self.token_budget}) avant la réponse finale. Relancez avec "
                        "un budget plus élevé ou découpez la tâche."
                    )
                    stop = STOP_TOKEN_BUDGET
                    self._emit(
                        "token_budget_reached",
                        f"[agent] budget de {self.token_budget} tokens atteint",
                        iterations=iteration,
                        total_tokens=total_tokens,
                    )
                    break
            else:
                answer = (
                    f"Limite de {self.max_iterations} itérations atteinte sans réponse "
                    "finale. Le travail est peut-être partiel : vérifiez l'état du projet "
                    "(git diff) et relancez avec --max-iterations plus grand si besoin."
                )
                stop = STOP_MAX_ITERATIONS
                self._emit(
                    "max_iterations_reached",
                    f"[agent] limite de {self.max_iterations} itérations atteinte",
                    iterations=iterations,
                    tool_calls=executed,
                )
        except (LLMError, ConfigError) as exc:
            answer = f"Arrêt sur erreur : {exc}"
            stop = STOP_ERROR
            self._emit("error", f"[ERREUR] {exc}", error=str(exc))
        except KeyboardInterrupt:
            answer = "Session interrompue par l'utilisateur."
            stop = STOP_ERROR
            self._emit("interrupted", "[agent] session interrompue")
            raise

        self._history = messages
        result = AgentResult(
            answer=answer,
            stopped_reason=stop,
            iterations=iterations,
            tool_calls=executed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            messages=[dict(item) for item in messages],
            log_path=str(self.logger.path) if self.logger is not None else None,
        )
        self._emit(
            "session_end",
            f"[agent] {result.summary()}",
            reason=stop,
            iterations=iterations,
            tool_calls=executed,
            total_tokens=total_tokens,
        )
        return result

    def _injecter_contexte(self, messages: List[Dict[str, Any]]) -> None:
        """Insère l'instantané du projet une seule fois par conversation.

        Le texte est ajouté comme second message système (juste après le prompt
        système) pour ne pas perturber l'ordre des rôles attendu par l'API.

        Args:
            messages: conversation en cours de construction (modifiée sur place).
        """
        if not self.initial_context or self._context_injected:
            return
        contexte = self._project_context()
        if not contexte:
            return
        messages.insert(1, {"role": "system", "content": contexte})
        self._context_injected = True
        self._emit(
            "context_injected",
            f"[contexte] instantané du projet injecté ({len(contexte)} caractères)",
            chars=len(contexte),
        )

    def _project_context(self) -> str:
        """Instantané du projet, calculé une seule fois puis mis en cache."""
        if self._context_cache is None:
            try:
                self._context_cache = build_project_context(self.root)
            except Exception:  # un instantané ne bloque jamais la session
                self._context_cache = ""
        return self._context_cache

    def _tool_choice_pour(self, iteration: int) -> str:
        """Stratégie ``tool_choice`` transmise pour cette itération.

        ``"required"`` n'est appliqué qu'au premier tour : au-delà, le modèle
        doit pouvoir conclure par une réponse finale, sinon il appellerait des
        outils jusqu'à la borne ``max_iterations``.

        Args:
            iteration: numéro d'itération courant (1 = premier appel).

        Returns:
            ``"auto"``, ``"required"`` ou ``"none"``.
        """
        if self.tool_choice == "required" and iteration > 1:
            return "auto"
        return self.tool_choice

    def _ask(self, messages: Sequence[Mapping[str, Any]], iteration: int) -> LLMResponse:
        """Envoie la conversation au modèle et journalise la réponse.

        Args:
            messages: conversation courante.
            iteration: numéro d'itération (affichage et journal).

        Returns:
            La réponse normalisée du client LLM.

        Raises:
            LLMError: erreur réseau, d'authentification ou de format.
            ConfigError: configuration incomplète (clé API absente).
        """
        choix_outils = self._tool_choice_pour(iteration)
        nb_outils = len(self.registry)
        self._emit(
            "llm_request",
            f"[{iteration}/{self.max_iterations}] appel du modèle "
            f"({len(messages)} messages, {nb_outils} outils, "
            f"tool_choice={choix_outils})…",
            iteration=iteration,
            max_iterations=self.max_iterations,
            messages=len(messages),
            tools=nb_outils,
            tool_choice=choix_outils,
        )
        response = self.client.chat(
            messages,
            tools=self.registry.schemas,
            tool_choice=choix_outils,
            stream=self.stream,
            on_token=self.on_token,
        )
        names = [call.name for call in response.tool_calls]
        self._emit(
            "llm_response",
            f"  ← réponse reçue : {len(response.text)} caractère(s), "
            f"outils demandés : {', '.join(names) if names else 'aucun'}",
            iteration=iteration,
            text=response.text[:LOG_PREVIEW],
            tool_calls=names,
            finish_reason=response.finish_reason,
            total_tokens=response.total_tokens,
        )
        return response

    def _execute(self, call: ToolCall, cache: Dict[str, str], iteration: int) -> str:
        """Exécute un appel d'outil et renvoie le texte destiné au modèle.

        Trois cas particuliers sont traités sans lever d'exception : arguments
        JSON invalides, appel strictement identique déjà exécuté (protection
        anti-boucle) et erreur d'outil (déjà convertie par le registre).

        Args:
            call: appel demandé par le modèle.
            cache: résultats déjà obtenus pendant cette tâche.
            iteration: numéro d'itération (affichage et journal).

        Returns:
            Le contenu du message ``tool`` à réinjecter.
        """
        self._emit(
            "tool_call",
            f"  → {call.name}({_format_arguments(call.arguments)})",
            iteration=iteration,
            name=call.name,
            arguments=call.arguments,
        )

        if call.arguments_error:
            content = (
                f"ERREUR ({call.name}) : arguments JSON invalides "
                f"({call.arguments_error}). Réémettez l'appel avec un objet JSON "
                "valide, sans texte autour."
            )
            self._emit(
                "tool_result",
                f"  ← {call.name} : JSON invalide",
                iteration=iteration,
                name=call.name,
                ok=False,
                content=content,
            )
            return content

        key = ""
        try:
            key = (
                f"{call.name}:"
                f"{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)}"
            )
        except (TypeError, ValueError):  # arguments exotiques : pas de cache
            key = ""
        if key and key in cache:
            content = cache[key] + (
                "\n\n[Attention] Appel identique déjà exécuté : le résultat "
                "ci-dessus est inchangé. Changez d'approche au lieu de répéter cet "
                "appel."
            )
            self._emit(
                "tool_result",
                f"  ← {call.name} : appel répété, résultat mis en cache",
                iteration=iteration,
                name=call.name,
                ok=False,
                content=content[:LOG_PREVIEW],
                repeated=True,
            )
            return content

        if self._refuse(call, iteration):
            content = (
                f"ACTION REFUSÉE ({call.name}) : l'utilisateur n'a pas autorisé cet "
                "appel. Ne le réémettez pas à l'identique : proposez une approche "
                "sans effet de bord, ou demandez à l'utilisateur de relancer avec "
                "--yes."
            )
            if key:
                cache[key] = content
            self._emit(
                "tool_result",
                f"  ← {call.name} : refusé par l'utilisateur",
                iteration=iteration,
                name=call.name,
                ok=False,
                error="confirmation refusée",
                content=content[:LOG_PREVIEW],
                refused=True,
            )
            return content

        if call.name == "write_file":
            # Trace de l'état git avant écrasement : audit et retour arrière aisés.
            self._log_git_diff(call.arguments, iteration)

        result = self.registry.execute(call.name, call.arguments)
        if key:
            cache[key] = result.content
        self._emit(
            "tool_result",
            f"  ← {call.name} : {'ok' if result.ok else 'erreur'} "
            f"({len(result.content)} caractères)",
            iteration=iteration,
            name=call.name,
            ok=result.ok,
            error=result.error,
            content=result.content[:LOG_PREVIEW],
            truncated=len(result.content) > LOG_PREVIEW,
        )
        return result.content

    def _refuse(self, call: ToolCall, iteration: int) -> bool:
        """Demande l'autorisation avant un outil sensible (``dangerous``).

        Args:
            call: appel demandé par le modèle.
            iteration: numéro d'itération (journal).

        Returns:
            ``True`` si l'appel doit être refusé. Sans callback ``confirm``, ou
            pour un outil inconnu ou inoffensif, renvoie ``False``.
        """
        if self.confirm is None or call.name not in self.registry:
            return False
        if not self.registry.get(call.name).dangerous:
            return False
        try:
            accepte = bool(self.confirm(call.name, dict(call.arguments or {})))
        except Exception:  # une interface défaillante ne casse pas l'agent
            accepte = False
        self._emit(
            "tool_approved" if accepte else "tool_refused",
            "" if accepte else f"  ← {call.name} : action non autorisée",
            iteration=iteration,
            name=call.name,
            arguments=call.arguments,
            approved=accepte,
        )
        return not accepte

    def _log_git_diff(self, arguments: Mapping[str, Any], iteration: int) -> None:
        """Consigne l'état git du fichier avant un écrasement par ``write_file``.

        Le diff est un simple outil d'audit : son absence (git indisponible,
        fichier non suivi, dépôt inexistant) ne bloque jamais l'écriture.
        """
        path = arguments.get("path")
        diff = read_git_diff(self.root, path)
        if not diff:
            return
        self._emit(
            "git_diff",
            f"  [git] diff de {path} avant écriture ({len(diff)} caractères)",
            iteration=iteration,
            path=path,
            diff=diff[:LOG_PREVIEW],
            truncated=len(diff) > LOG_PREVIEW,
        )

    def _emit(self, event: str, message: str = "", **fields: Any) -> None:
        """Journalise un événement et l'affiche si ``verbose`` est actif."""
        if self.logger is not None:
            self.logger.log(event, **fields)
        if self.on_event is not None:
            try:
                self.on_event(event, fields)
            except Exception:  # une interface défaillante ne casse pas l'agent
                pass
        if self.verbose and message:
            print(message)

    def capabilities(self) -> str:
        """Description du prompt système et des outils (mode ``--dry-run``)."""
        lines = [
            f"Modèle         : {self.settings.model}",
            f"Endpoint       : {self.settings.base_url}",
            f"Bac à sable    : {self.root}",
            f"Itérations max : {self.max_iterations}",
            f"Budget tokens  : {self.token_budget or 'illimité'}",
            f"Contexte projet: {'instantané injecté' if self.initial_context else 'non injecté'}",
            f"Budget contexte: {self.context_tokens or 'illimité'} tokens estimés",
            "Appels d'outils: "
            + self.tool_choice
            + (" (un appel imposé au premier tour)" if self.tool_choice == "required" else ""),
            "Confirmation   : "
            + (
                "non demandée (--yes)"
                if self.confirm is None
                else "demandée avant chaque outil sensible"
            ),
            "",
            "Outils disponibles :",
        ]
        for tool in self.registry:
            summary = tool.description.split(".")[0]
            lines.append(f"  - {tool.name} : {summary}.")
        lines.extend(["", "Prompt système :", self.system_prompt])
        return "\n".join(lines)

    def close(self) -> None:
        """Ferme le journal s'il est ouvert."""
        if self.logger is not None:
            self.logger.close()


def _format_arguments(arguments: Any, limit: int = 160) -> str:
    """Résumé compact des arguments, pour l'affichage et le journal."""
    try:
        text = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(arguments)
    return text if len(text) <= limit else text[:limit] + "…"


def _build_parser() -> argparse.ArgumentParser:
    """Analyseur de ligne de commande de l'agent."""
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Agent de développement autonome local (boucle LLM + outils).",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        help="Tâche à confier à l'agent (sinon saisie interactive).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Borne d'itérations (MAX_ITERATIONS du .env par défaut).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Modèle à utiliser (LLM_MODEL du .env par défaut).",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help="Budget de tokens pour la tâche (illimité par défaut).",
    )
    parser.add_argument(
        "--context-tokens",
        type=int,
        default=16000,
        help=(
            "Budget estimé de l'historique envoyé au modèle "
            "(16000 par défaut, 0 = illimité)."
        ),
    )
    parser.add_argument(
        "--no-context",
        action="store_true",
        help=(
            "Ne pas injecter l'instantané du projet au démarrage "
            "(arborescence, README, git status)."
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Approuver automatiquement les outils sensibles (run_command).",
    )
    parser.add_argument(
        "--log-dir",
        default=LOG_DIRECTORY,
        help="Dossier des journaux JSONL (logs/ par défaut).",
    )
    parser.add_argument(
        "--no-log", action="store_true", help="Ne pas écrire de journal JSONL."
    )
    parser.add_argument(
        "--stream", action="store_true", help="Afficher la réponse du modèle en flux."
    )
    parser.add_argument(
        "--tool-choice",
        choices=TOOL_CHOICE_VALUES,
        default=None,
        help=(
            "Stratégie d'appel des outils : « auto » (défaut, le modèle décide), "
            "« required » (imposer un outil au premier tour, puis laisser "
            "conclure) ou « none » (aucun outil)."
        ),
    )
    parser.add_argument(
        "--repl",
        action="store_true",
        help="Session interactive multi-tours (défaut sans -p dans un terminal).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Afficher la configuration et les outils sans appeler le LLM.",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="N'afficher que la réponse finale."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Afficher la version et quitter.",
    )
    return parser


def _configurer_sortie() -> None:
    """Force l'UTF-8 sur les flux texte quand c'est possible.

    Python utilise l'encodage local (cp1252 ou cp437 sous Windows) par défaut,
    ce qui fait échouer l'affichage des accents et des flèches. Le mode
    ``errors="replace"`` garantit qu'une écriture ne lève jamais
    ``UnicodeEncodeError`` : mieux vaut un caractère substitué qu'une session
    interrompue.
    """
    for flux in (sys.stdout, sys.stderr):
        reconfigure = getattr(flux, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # flux non reconfigurable (test, capture)
            pass


def _stdin_is_tty() -> bool:
    """Vrai si l'entrée standard est un terminal interactif.

    Un tube, un fichier redirigé ou une entrée absente ne permettent pas de
    répondre aux confirmations : ces cas sont traités comme non interactifs.
    """
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _make_confirmation() -> Callable[[str, Dict[str, Any]], bool]:
    """Construit le callback de confirmation des outils sensibles.

    En session interactive, l'utilisateur répond ``o``/``n``. Hors terminal
    (pipe, CI, redirection), la demande est refusée par défaut avec un rappel de
    l'option ``--yes`` : un agent ne doit jamais exécuter une commande
    destructrice parce que personne ne pouvait répondre.

    Returns:
        Une fonction ``(outil, arguments) -> bool`` à passer à ``Agent``.
    """

    def confirmer(name: str, arguments: Mapping[str, Any]) -> bool:
        resume = _format_arguments(arguments)
        if not _stdin_is_tty():
            print(
                f"[REFUS] « {name}({resume}) » nécessite une confirmation : "
                "aucun terminal interactif. Relancez avec --yes pour l'autoriser."
            )
            return False
        try:
            reponse = input(f"Exécuter « {name}({resume}) » ? [o/N] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return reponse.strip().lower() in ("o", "oui", "y", "yes")

    return confirmer


def _run_repl(agent: Agent, args: argparse.Namespace) -> int:
    """Lance la session interactive multi-tours (module :mod:`ui`).

    Args:
        agent: agent déjà configuré (confirmations, journal, modèle).
        args: options de la ligne de commande (``--quiet``).

    Returns:
        Code de sortie : 0 session terminée, 130 interrompue par un signal.
    """
    try:  # exécution en package : python -m src.agent
        from .ui import Repl, Ui
    except ImportError:  # exécution directe : python src/agent.py
        from ui import Repl, Ui

    ui = Ui(quiet=args.quiet)
    try:
        return Repl(agent, ui).run()
    except KeyboardInterrupt:  # filet de sécurité : Ctrl+C hors boucle
        print("\n[INTERROMPU] Session arrêtée par l'utilisateur.")
        return 130
    finally:
        ui.finish_stream()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Point d'entrée CLI de l'agent.

    Args:
        argv: arguments (par défaut ceux du processus).

    Returns:
        Code de sortie : 0 succès, 1 erreur, 2 arrêt sur borne, 130 interruption.
    """
    args = _build_parser().parse_args(argv)
    if args.repl and (args.prompt or "").strip():
        print("[ERREUR] --repl lance une session interactive : retirez -p/--prompt.")
        return 1
    _configurer_sortie()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[ERREUR] Configuration invalide : {exc}")
        return 1

    if args.model:
        model = args.model.strip()
        if not model:
            print("[ERREUR] --model ne peut pas être vide.")
            return 1
        settings = replace(settings, model=model)

    if args.context_tokens is not None and args.context_tokens < 0:
        print("[ERREUR] --context-tokens doit être >= 0 (0 = illimité).")
        return 1
    budget_contexte = args.context_tokens or None

    try:
        agent = Agent(
            settings=settings,
            max_iterations=args.max_iterations,
            token_budget=args.token_budget,
            verbose=not args.quiet,
            stream=args.stream,
            confirm=None if args.yes else _make_confirmation(),
            context_tokens=budget_contexte,
            initial_context=not args.no_context,
            tool_choice=args.tool_choice,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"[ERREUR] Initialisation impossible : {exc}")
        return 1

    if args.dry_run:
        print(agent.capabilities())
        return 0

    task = (args.prompt or "").strip()
    repl = args.repl
    if not task and not repl:
        if _stdin_is_tty():
            repl = True  # terminal : session multi-tours par défaut
        else:
            try:
                task = sys.stdin.read().strip()
            except (AttributeError, OSError):
                task = ""
    if not task and not repl:
        try:
            task = input("Tâche à confier à l'agent > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[ERREUR] Aucune tâche fournie.")
            return 1
    if not task and not repl:
        print("[ERREUR] Aucune tâche fournie.")
        return 1

    if not args.no_log:
        directory = Path(args.log_dir)
        if not directory.is_absolute():
            directory = Path(PROJECT_ROOT) / directory
        try:
            agent.logger = EventLogger.for_session(directory)
        except OSError as exc:
            print(f"[AVERTISSEMENT] Journal désactivé : {exc}")

    if repl:
        try:
            return _run_repl(agent, args)
        finally:
            agent.close()

    try:
        result = agent.run(task)
    except KeyboardInterrupt:
        print("\n[INTERROMPU] Session arrêtée par l'utilisateur.")
        return 130
    except (ValueError, ConfigError) as exc:
        print(f"[ERREUR] {exc}")
        return 1
    finally:
        agent.close()

    print()
    print(result.answer)
    if not args.quiet:
        print(f"[stats] {result.summary()}")
        if result.log_path:
            print(f"[stats] journal : {result.log_path}")
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
