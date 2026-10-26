"""Interface interactive : affichage ``rich`` et REPL multi-tours.

L'interface habille la boucle agentique sans la modifier :

* :class:`Ui` traduit les événements du callback ``on_event`` en lignes lisibles
  (et en couleurs quand ``rich`` est installé) ;
* :class:`Repl` enchaîne plusieurs demandes dans la même session en conservant
  l'historique de la conversation, et expose quelques commandes ``/``.

Utilisation :
    from ui import Repl, Ui

    Repl(agent, Ui()).run()

``rich`` reste optionnel : sans lui, l'affichage retombe sur du texte simple
(aucun test ne dépend donc de son installation).
"""

import json
import sys
from typing import Any, Mapping, Optional, Tuple

try:  # exécution en package : python -m src.agent
    from . import __version__
    from .context import estimate_messages_tokens
except ImportError:  # exécution directe : python src/agent.py
    from __init__ import __version__
    from context import estimate_messages_tokens

try:  # dépendance déclarée dans requirements.txt, mais facultative à l'exécution
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - dépend de l'installation
    Console = None  # type: ignore[assignment]
    Markdown = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]
    RICH_AVAILABLE = False

__all__ = ["COMMANDS", "RICH_AVAILABLE", "Repl", "Ui"]

PROMPT = "vous > "

#: Commandes du REPL et leur description (affichées par ``/aide``).
COMMANDS = {
    "/aide": "afficher cette aide",
    "/outils": "lister les outils disponibles",
    "/reset": "oublier l'historique de la conversation",
    "/stats": "afficher les tokens consommés",
    "/journal": "afficher le chemin du journal JSONL",
    "/oui": "basculer l'approbation automatique des outils sensibles",
    "/quit": "quitter la session (ou Ctrl+D)",
}

_QUIT_ALIASES = frozenset({"/quit", "/exit", "/q", "quit", "exit", "bye"})
_HELP_ALIASES = frozenset({"/aide", "/help", "/?"})

#: Substituts ASCII utilisés quand la console ne sait pas encoder les symboles
#: (Windows en cp1252, par exemple).
_ASCII_FALLBACK = str.maketrans(
    {
        "→": "->",
        "←": "<-",
        "✔": "ok",
        "✖": "x",
        "▶": ">",
        "·": "-",
        "—": "-",
        "…": "...",
    }
)


def _print_safe(texte: str, end: str = "\n", flush: bool = False) -> None:
    """Affiche un texte, même si la console ne supporte pas l'Unicode.

    Les flèches et puces typographiques sont remplacées par des équivalents
    ASCII avant tout échec : l'affichage ne doit jamais lever
    ``UnicodeEncodeError`` (console Windows en cp1252).
    """
    try:
        print(texte, end=end, flush=flush)
    except UnicodeEncodeError:
        encodage = getattr(sys.stdout, "encoding", None) or "ascii"
        secours = texte.translate(_ASCII_FALLBACK)
        try:
            print(secours, end=end, flush=flush)
        except UnicodeEncodeError:  # accents exotiques : on dégrade encore
            print(
                secours.encode(encodage, "replace").decode(encodage, "replace"),
                end=end,
                flush=flush,
            )



def _short_arguments(arguments: Any, limit: int = 120) -> str:
    """Résumé compact des arguments d'un outil (affichage interactif)."""
    try:
        texte = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        texte = str(arguments)
    return texte if len(texte) <= limit else texte[:limit] + "…"


class Ui:
    """Affichage de la session : ``rich`` si disponible, texte simple sinon.

    Example:
        >>> ui = Ui(rich=False)
        >>> ui.event("tool_call", {"name": "read_file", "arguments": {}})
        → read_file({})
    """

    #: Style par défaut de chaque type d'événement (rendu ``rich``).
    STYLES = {
        "session_start": "dim",
        "session_end": "dim",
        "llm_request": "dim",
        "llm_response": "dim",
        "tool_call": "cyan",
        "tool_result": "green",
        "tool_approved": "yellow",
        "tool_refused": "yellow",
        "git_diff": "magenta",
        "token_budget_reached": "yellow",
        "max_iterations_reached": "yellow",
        "context_injected": "dim",
        "context_trimmed": "yellow",
        "interrupted": "yellow",
        "error": "red",
    }

    def __init__(
        self,
        console: Optional[Any] = None,
        rich: Optional[bool] = None,
        quiet: bool = False,
    ) -> None:
        """Prépare l'affichage.

        Args:
            console: console riche existante (injection, tests) ; ignorée en
                mode texte simple.
            rich: forcer (``True``) ou interdire (``False``) ``rich`` ;
                ``None`` = automatique selon l'installation.
            quiet: n'afficher que les réponses finales (les événements sont
                alors masqués par :class:`Repl`).
        """
        self.rich = RICH_AVAILABLE if rich is None else bool(rich and RICH_AVAILABLE)
        if self.rich and console is None:
            console = Console(highlight=False, emoji=False)
        self.console = console if self.rich else None
        self.quiet = quiet
        self._streaming = False

    def _rich_print(self, rendu: Any, **options: Any) -> bool:
        """Affiche via ``rich`` ; bascule en texte simple si la console refuse.

        Certaines consoles Windows (cp437/cp1252) ne savent pas encoder les
        cadres ni les accents : plutôt que d'interrompre la session, on repasse
        définitivement en texte simple.

        Returns:
            ``True`` si l'affichage riche a réussi.
        """
        try:
            self.console.print(rendu, **options)
            return True
        except UnicodeEncodeError:
            self.rich = False
            self.console = None
            return False


    # -- briques de base ---------------------------------------------------

    def print(self, texte: str = "", style: Optional[str] = None) -> None:
        """Affiche une ligne, avec style en mode riche."""
        if self.rich:
            rendu = Text(texte, style=style) if style else Text(texte)
            if self._rich_print(rendu):
                return
        _print_safe(texte)

    def rule(self, titre: str = "") -> None:
        """Séparateur horizontal (ligne vide en mode simple)."""
        if self.rich:
            try:
                self.console.rule(
                    Text(titre, style="dim") if titre else "", style="dim"
                )
                return
            except UnicodeEncodeError:
                self.rich = False
                self.console = None
        _print_safe("")

    def warning(self, texte: str) -> None:
        """Avertissement (jaune en mode riche)."""
        self.print(texte, style="yellow")

    def error(self, texte: str) -> None:
        """Erreur (rouge en mode riche)."""
        self.print(texte, style="red")

    def ask(self, prompt: str = PROMPT) -> str:
        """Lit une saisie utilisateur.

        Raises:
            EOFError: entrée fermée (Ctrl+D) : la session doit se terminer.
            KeyboardInterrupt: Ctrl+C au prompt : la ligne est abandonnée.
        """
        if self.rich:
            try:
                self.console.print(Text(prompt, style="bold cyan"), end="")
                return input()
            except UnicodeEncodeError:
                self.rich = False
                self.console = None
        return input(prompt)

    # -- session -----------------------------------------------------------

    def banner(
        self,
        model: str,
        tools: Any,
        root: Any,
        version: str = "",
        max_iterations: Optional[int] = None,
        confirmation: bool = True,
    ) -> None:
        """Affiche l'en-tête de la session interactive.

        Args:
            model: modèle LLM utilisé.
            tools: registre (itérable, ``len``) ou nombre d'outils.
            root: racine du bac à sable.
            version: version de l'agent (affichée si fournie).
            max_iterations: borne d'itérations, si connue.
            confirmation: vrai si les outils sensibles sont confirmés.
        """
        try:
            nombre = len(tools)
        except TypeError:
            nombre = int(tools)
        titre = f"agent {version}".strip()
        details = f"modèle={model} · {nombre} outil(s)"
        if max_iterations:
            details += f" · {max_iterations} itération(s) max"
        details += " · confirmation " + ("active" if confirmation else "désactivée")
        aide = "Tapez votre demande, /aide pour les commandes, /quit pour quitter."
        if self.rich:
            corps = Text()
            corps.append(details + "\n", style="white")
            corps.append(f"bac à sable : {root}\n", style="dim")
            corps.append(aide, style="dim italic")
            cadre = Panel(
                corps, title=Text(titre, style="bold cyan"), border_style="cyan"
            )
            if self._rich_print(cadre):
                return
        _print_safe(f"{titre} | {details}")
        _print_safe(f"bac à sable : {root}")
        _print_safe(aide)

    # -- événements de l'agent --------------------------------------------

    def event(self, name: str, fields: Mapping[str, Any]) -> None:
        """Affiche un événement émis par :meth:`agent.Agent._emit`.

        Args:
            name: type d'événement (``tool_call``, ``tool_result``…).
            fields: champs associés (sans le message déjà formaté par l'agent).
        """
        if self.quiet:
            return
        try:
            texte, style = self._render(name, fields or {})
        except Exception:  # un affichage ne doit jamais casser la session
            return
        if not texte:
            return
        self.finish_stream()
        self.print(texte, style=style)

    def _render(self, name: str, fields: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
        """Construit (texte, style) pour un événement ; ``("", None)`` = muet."""
        style = self.STYLES.get(name)
        if name == "session_start":
            outils = fields.get("tools") or []
            return (
                f"\n— {fields.get('model', '?')} · {len(outils)} outil(s) · "
                f"tâche : {fields.get('task', '')}",
                style,
            )
        if name == "llm_request":
            return (
                f"[{fields.get('iteration', '?')}/{fields.get('max_iterations', '?')}] "
                f"appel du modèle ({fields.get('messages', '?')} messages)…",
                style,
            )
        if name == "llm_response":
            outils = fields.get("tool_calls") or []
            return (
                f"  ← réponse reçue : {len(fields.get('text') or '')} caractère(s), "
                f"outils demandés : {', '.join(outils) if outils else 'aucun'}",
                style,
            )
        if name == "tool_call":
            return (
                f"  → {fields.get('name', '?')}"
                f"({_short_arguments(fields.get('arguments', {}))})",
                style,
            )
        if name == "tool_result":
            nom = fields.get("name", "?")
            contenu = fields.get("content") or ""
            if fields.get("ok"):
                return f"  ← {nom} : ok ({len(contenu)} caractères)", style
            erreur = fields.get("error") or "échec"
            return f"  ← {nom} : erreur — {erreur}", "red"
        if name == "tool_approved":
            return f"  ✔ {fields.get('name', '?')} autorisé", style
        if name == "tool_refused":
            return f"  ✖ {fields.get('name', '?')} refusé", style
        if name == "git_diff":
            chemin = fields.get("path", "?")
            diff = fields.get("diff") or ""
            return f"  [git] diff de {chemin} avant écriture ({len(diff)} car.)", style
        if name == "error":
            return f"[ERREUR] {fields.get('error', 'échec inconnu')}", style
        if name in ("token_budget_reached", "max_iterations_reached"):
            return f"[agent] {fields.get('iterations', '?')} itération(s) : {name}", style
        if name == "session_end":
            return (
                f"— {fields.get('reason', '?')} | {fields.get('iterations', 0)} "
                f"itération(s) | {fields.get('tool_calls', 0)} outil(s) | "
                f"{fields.get('total_tokens', 0)} tokens",
                style,
            )
        if name == "interrupted":
            return "[INTERROMPU] session interrompue par l'utilisateur", style
        if name == "context_injected":
            return (
                f"[contexte] instantané du projet injecté "
                f"({fields.get('chars', 0)} caractères)",
                style,
            )
        if name == "context_trimmed":
            return (
                f"[contexte] {fields.get('omitted', 0)} message(s) omis "
                f"(budget {fields.get('max_tokens', '?')} tokens)",
                style,
            )
        return "", None

    # -- réponse finale ----------------------------------------------------

    def token(self, fragment: str) -> None:
        """Affiche un fragment du flux de réponse (``stream=True``)."""
        if not fragment:
            return
        self._streaming = True
        if self.rich:
            if self._rich_print(Text(fragment), end=""):
                return
        _print_safe(fragment, end="", flush=True)

    def finish_stream(self) -> None:
        """Termine la ligne du flux, si une réponse était en cours."""
        if not self._streaming:
            return
        self._streaming = False
        print()

    def answer(self, texte: str) -> None:
        """Affiche la réponse finale (panneau Markdown en mode riche)."""
        self.finish_stream()
        contenu = texte or "(aucune réponse)"
        if self.rich:
            cadre = Panel(
                Markdown(contenu),
                title="réponse",
                border_style="green",
                padding=(0, 1),
            )
            if self._rich_print(cadre):
                return
        _print_safe(contenu)

    def stats(self, result: Any) -> None:
        """Affiche le résumé d'une tâche (raison d'arrêt, compteurs, journal)."""
        resume = getattr(result, "summary", None)
        texte = resume() if callable(resume) else str(result)
        self.print(f"[stats] {texte}", style="dim")
        journal = getattr(result, "log_path", None)
        if journal:
            self.print(f"[stats] journal : {journal}", style="dim")

    # -- aide --------------------------------------------------------------

    def help(self, commands: Optional[Mapping[str, str]] = None) -> None:
        """Affiche les commandes disponibles."""
        table = commands or COMMANDS
        if self.rich:
            grille = Table(show_header=True, header_style="bold cyan", box=None)
            grille.add_column("commande")
            grille.add_column("description")
            for commande, description in table.items():
                grille.add_row(commande, description)
            if self._rich_print(grille):
                return
        for commande, description in table.items():
            _print_safe(f"  {commande:<9} {description}")


def _stdin_is_tty() -> bool:
    """Vrai si l'entrée standard est un terminal (et non un tube)."""
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        return False


class Repl:
    """Session interactive multi-tours : une demande par saisie.

    L'historique de l'agent est conservé d'une tâche à l'autre (le modèle se
    souvient des échanges), sauf après ``/reset``.

    Example:
        >>> Repl(agent, Ui()).run()      # boucle jusqu'à /quit ou Ctrl+D
        0
    """

    def __init__(
        self,
        agent: Any,
        ui: Optional[Ui] = None,
        prompt: str = PROMPT,
    ) -> None:
        """Prépare la session.

        Args:
            agent: objet exposant ``run(task)``, ``reset_history()``,
                ``registry``, ``settings``, ``logger`` et ``client`` (un
                :class:`agent.Agent` en pratique).
            ui: interface d'affichage ; créée si absente.
            prompt: invite affichée avant chaque saisie.
        """
        self.agent = agent
        self.ui = ui or Ui()
        self.prompt = prompt
        self._confirm = getattr(agent, "confirm", None)
        self.auto_approve = self._confirm is None

    # -- boucle principale -------------------------------------------------

    def run(self) -> int:
        """Démarre la session et boucle jusqu'à ``/quit`` ou Ctrl+D.

        Ctrl+C pendant une tâche annule la tâche en cours et rend la main ;
        Ctrl+D (ou ``/quit``) termine la session.

        Returns:
            ``0`` : session terminée normalement.
        """
        self._installer()
        self.banner()
        while True:
            try:
                ligne = self.ui.ask(self.prompt)
            except EOFError:
                print()
                self.ui.print("À bientôt.")
                return 0
            except KeyboardInterrupt:
                print()
                self.ui.warning("(Ctrl+D ou /quit pour quitter)")
                continue
            demande = (ligne or "").strip()
            if not demande:
                continue
            if demande.lower() in _QUIT_ALIASES:
                self.ui.print("À bientôt.")
                return 0
            if demande.startswith("/") or demande.lower() in _HELP_ALIASES:
                self.commande(demande)
                continue
            self.tache(demande)

    def _installer(self) -> None:
        """Branche l'interface sur l'agent (événements, flux, verbosité)."""
        self.agent.verbose = False  # l'interface affiche les événements
        self.agent.on_event = None if self.ui.quiet else self.ui.event
        if getattr(self.agent, "stream", False):
            self.agent.on_token = self.ui.token

    def banner(self) -> None:
        """Affiche l'en-tête de la session."""
        if self.ui.quiet:
            return
        settings = getattr(self.agent, "settings", None)
        self.ui.banner(
            model=getattr(settings, "model", "?"),
            tools=getattr(self.agent, "registry", []) or [],
            root=getattr(self.agent, "root", "?"),
            version=__version__,
            max_iterations=getattr(self.agent, "max_iterations", None),
            confirmation=self._confirm is not None,
        )

    # -- actions -----------------------------------------------------------

    def tache(self, demande: str) -> bool:
        """Exécute une demande et affiche le résultat.

        Args:
            demande: texte saisi par l'utilisateur.

        Returns:
            ``True`` si la tâche s'est terminée sur une réponse finale.
        """
        self.ui.rule()
        self.ui.print(f"▶ {demande}", style="bold")
        try:
            resultat = self.agent.run(demande)
        except KeyboardInterrupt:
            self.ui.warning(
                "[INTERROMPU] tâche annulée ; la session continue "
                "(/quit pour quitter)."
            )
            return False
        except ValueError as exc:  # tâche vide ou paramètre invalide
            self.ui.error(f"[ERREUR] {exc}")
            return False
        self.ui.answer(getattr(resultat, "answer", str(resultat)))
        if not self.ui.quiet:
            self.ui.stats(resultat)
        return bool(getattr(resultat, "success", True))

    def commande(self, ligne: str) -> bool:
        """Traite une commande ``/``.

        Args:
            ligne: commande saisie, par exemple ``/reset``.

        Returns:
            ``True`` si la session continue, ``False`` si elle doit s'arrêter.
        """
        nom = ligne.split()[0].lower()
        if nom in _HELP_ALIASES:
            self.ui.help()
        elif nom in ("/outils", "/tools"):
            self._lister_outils()
        elif nom == "/reset":
            self.agent.reset_history()
            self.ui.print("Historique effacé : la prochaine demande repart de zéro.")
        elif nom in ("/stats", "/tokens"):
            self._afficher_stats()
        elif nom == "/journal":
            journal = getattr(getattr(self.agent, "logger", None), "path", None)
            self.ui.print(f"journal : {journal}" if journal else "aucun journal actif")
        elif nom in ("/oui", "/yes"):
            self._basculer_approbation()
        else:
            self.ui.warning(f"Commande inconnue : {nom} — /aide pour la liste.")
        return True

    # -- commandes ---------------------------------------------------------

    def _lister_outils(self) -> None:
        """Affiche les outils disponibles et leur résumé."""
        for outil in getattr(self.agent, "registry", []) or []:
            resume = outil.description.split(".")[0]
            self.ui.print(f"  {outil.name} : {resume}.")

    def _afficher_stats(self) -> None:
        """Affiche les tokens consommés depuis le début de la session."""
        client = getattr(self.agent, "client", None)
        total = getattr(client, "tokens_used", 0)
        historique = getattr(self.agent, "history", []) or []
        self.ui.print(
            f"tokens consommés : {total} | messages en mémoire : "
            f"{len(historique)} (≈ {estimate_messages_tokens(historique)} tokens)"
        )

    def _basculer_approbation(self) -> None:
        """Active ou désactive l'approbation automatique des outils sensibles."""
        self.auto_approve = not self.auto_approve
        self.agent.confirm = None if self.auto_approve else self._confirm
        etat = (
            "automatique (plus de confirmation)"
            if self.auto_approve
            else "demandée à chaque outil sensible"
        )
        self.ui.print(f"Confirmation des outils sensibles : {etat}.")




