"""Tests de l'interface interactive : rendu des evenements et REPL multi-tours.

Aucun test ne touche au reseau ni a un vrai terminal : les saisies sont
simulees et l'agent est remplace par un double.
"""

import io
from types import SimpleNamespace

import pytest

import src.ui as ui_module
from src.ui import COMMANDS, RICH_AVAILABLE, Repl, Ui


class FauxOutil:
    """Outil minimal (nom + description) pour les listes du REPL."""

    def __init__(self, nom, description="Fait quelque chose. Details inutiles."):
        self.name = nom
        self.description = description


class FauxResultat:
    """Resultat d'agent scripte."""

    def __init__(
        self,
        answer="fini",
        success=True,
        reason="final_answer",
        iterations=1,
        tool_calls=0,
        total_tokens=12,
        log_path=None,
    ):
        self.answer = answer
        self.success = success
        self.stopped_reason = reason
        self.iterations = iterations
        self.tool_calls = tool_calls
        self.total_tokens = total_tokens
        self.log_path = log_path

    def summary(self):
        return (
            f"{self.stopped_reason} | {self.iterations} iteration(s) | "
            f"{self.tool_calls} outil(s) | {self.total_tokens} tokens"
        )


class FauxAgent:
    """Double d'agent : enregistre les taches et rejoue des resultats."""

    def __init__(self, reponses=None, confirm=None):
        self.tasks = []
        self.reponses = list(reponses or [])
        self.registry = [FauxOutil("read_file"), FauxOutil("write_file")]
        self.settings = SimpleNamespace(model="fake-model")
        self.root = "C:/projet"
        self.max_iterations = 5
        self.stream = False
        self.verbose = True
        self.on_event = None
        self.on_token = None
        self.confirm = confirm
        self.client = SimpleNamespace(tokens_used=42)
        self.logger = None
        self._history = []
        self.resets = 0

    @property
    def history(self):
        return list(self._history)

    def reset_history(self):
        self.resets += 1
        self._history = []

    def run(self, task):
        self.tasks.append(task)
        self._history.append({"role": "user", "content": task})
        self._history.append({"role": "assistant", "content": "ok"})
        if not self.reponses:
            return FauxResultat(answer=f"réponse à : {task}")
        item = self.reponses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FauxResultat):
            return item
        return FauxResultat(answer=str(item))


@pytest.fixture
def entrees(monkeypatch):
    """Installe une suite de saisies simulees (EOFError au-dela)."""

    def installer(*lignes):
        file_entrees = iter(lignes)

        def faux_input(prompt=""):
            try:
                return next(file_entrees)
            except StopIteration:
                raise EOFError

        monkeypatch.setattr("builtins.input", faux_input)

    return installer


# --- rendu des evenements (texte simple) -----------------------------------


def test_evenements_rendus_en_texte_simple(capsys):
    ui = Ui(rich=False)

    ui.event("session_start", {"model": "m", "tools": ["a", "b"], "task": "t"})
    ui.event("tool_call", {"name": "read_file", "arguments": {"path": "src/main.py"}})
    ui.event("tool_result", {"name": "read_file", "ok": True, "content": "x" * 12})
    ui.event("tool_result", {"name": "run_command", "ok": False, "error": "refusée"})
    ui.event("tool_approved", {"name": "run_command"})
    ui.event("tool_refused", {"name": "run_command"})
    ui.event("git_diff", {"path": "src/main.py", "diff": "abc"})
    ui.event("error", {"error": "réseau indisponible"})
    ui.event(
        "session_end",
        {
            "reason": "final_answer",
            "iterations": 2,
            "tool_calls": 1,
            "total_tokens": 30,
        },
    )

    sortie = capsys.readouterr().out
    assert "2 outil(s)" in sortie
    assert "read_file" in sortie and "src/main.py" in sortie
    assert "ok (12 caractères)" in sortie
    assert "erreur — refusée" in sortie
    assert "autorisé" in sortie and "refusé" in sortie
    assert "[git] diff de src/main.py" in sortie
    assert "[ERREUR] réseau indisponible" in sortie
    assert "final_answer" in sortie and "30 tokens" in sortie


def test_evenement_inconnu_ignore(capsys):
    Ui(rich=False).event("mystere", {"x": 1})

    assert capsys.readouterr().out == ""


def test_mode_quiet_masque_les_evenements(capsys):
    Ui(rich=False, quiet=True).event(
        "tool_call", {"name": "read_file", "arguments": {}}
    )

    assert capsys.readouterr().out == ""


def test_banniere_texte_simple(capsys):
    ui = Ui(rich=False)

    ui.banner("fake-model", 5, "C:/projet", "0.8.0", 15, True)

    sortie = capsys.readouterr().out
    assert "agent 0.8.0" in sortie
    assert "modèle=fake-model" in sortie
    assert "5 outil(s)" in sortie
    assert "confirmation active" in sortie
    assert "/quit" in sortie


def test_reponse_et_stats(capsys):
    ui = Ui(rich=False)

    ui.answer("Voici la réponse.")
    ui.stats(FauxResultat(answer="x", log_path="logs/s.jsonl"))

    sortie = capsys.readouterr().out
    assert "Voici la réponse." in sortie
    assert "[stats] final_answer" in sortie
    assert "12 tokens" in sortie
    assert "logs/s.jsonl" in sortie


def test_aide_liste_les_commandes(capsys):
    Ui(rich=False).help()

    sortie = capsys.readouterr().out
    assert "/aide" in sortie
    assert "/quit" in sortie
    assert all(commande in sortie for commande in COMMANDS)


def test_flux_de_reponse_texte_simple(capsys):
    ui = Ui(rich=False)

    ui.token("bon")
    ui.token("jour")
    ui.finish_stream()

    assert capsys.readouterr().out == "bonjour\n"


def test_console_sans_unicode_ne_plante_pas(monkeypatch):
    """Les flèches typographiques sont remplacées sur une console cp1252."""

    class FluxAscii:
        encoding = "ascii"

        def __init__(self):
            self.ecrits = []

        def write(self, texte):
            texte.encode("ascii")  # lève UnicodeEncodeError
            self.ecrits.append(texte)

        def flush(self):
            pass

    flux = FluxAscii()
    monkeypatch.setattr("sys.stdout", flux)

    Ui(rich=False).print("→ ok · fin")

    assert "".join(flux.ecrits) == "-> ok - fin\n"


# --- rendu rich (saute si rich n'est pas installe) -------------------------


@pytest.mark.skipif(not RICH_AVAILABLE, reason="rich n'est pas installé")
def test_rendu_rich_ne_leve_pas():
    sortie = io.StringIO()
    console = ui_module.Console(file=sortie, width=100, force_terminal=False)
    ui = Ui(console=console)

    ui.banner("fake-model", 5, "C:/projet", "0.8.0", 15, True)
    ui.event("tool_call", {"name": "read_file", "arguments": {"path": "a[b].py"}})
    ui.event("tool_result", {"name": "read_file", "ok": True, "content": "abc"})
    ui.event("tool_result", {"name": "write_file", "ok": False, "error": "refus"})
    ui.answer("# Titre\n\n- point [un]")
    ui.stats(FauxResultat())
    ui.help()

    texte = sortie.getvalue()
    assert "fake-model" in texte
    assert "read_file" in texte
    assert "réponse" in texte
    assert "/quit" in texte


# --- REPL multi-tours ------------------------------------------------------


def test_repl_execute_plusieurs_taches(entrees, capsys):
    agent = FauxAgent()
    entrees("liste les fichiers", "et src ?", "/quit")

    code = Repl(agent, Ui(rich=False)).run()

    sortie = capsys.readouterr().out
    assert code == 0
    assert agent.tasks == ["liste les fichiers", "et src ?"]
    assert "réponse à : liste les fichiers" in sortie
    assert "réponse à : et src ?" in sortie
    assert "À bientôt." in sortie
    # L'interface prend la main sur l'affichage des evenements.
    assert agent.verbose is False
    assert callable(agent.on_event)


def test_repl_ignore_les_saisies_vides(entrees):
    agent = FauxAgent()
    entrees("", "   ", "/quit")

    Repl(agent, Ui(rich=False)).run()

    assert agent.tasks == []


def test_repl_quitte_sur_entree_fermee(entrees, capsys):
    agent = FauxAgent()
    entrees()  # EOF immediat (Ctrl+D)

    code = Repl(agent, Ui(rich=False)).run()

    assert code == 0
    assert agent.tasks == []
    assert "À bientôt." in capsys.readouterr().out


def test_repl_ctrl_c_au_prompt_continue(monkeypatch, capsys):
    appels = []

    def faux_input(prompt=""):
        appels.append(prompt)
        if len(appels) == 1:
            raise KeyboardInterrupt
        return "/quit"

    monkeypatch.setattr("builtins.input", faux_input)

    code = Repl(FauxAgent(), Ui(rich=False)).run()

    sortie = capsys.readouterr().out
    assert code == 0
    assert "/quit" in sortie  # rappel de la commande pour quitter


def test_repl_ctrl_c_pendant_une_tache(entrees, capsys):
    agent = FauxAgent(reponses=[KeyboardInterrupt()])
    entrees("tache interrompue", "seconde tache", "/quit")

    code = Repl(agent, Ui(rich=False)).run()

    sortie = capsys.readouterr().out
    assert code == 0
    assert agent.tasks == ["tache interrompue", "seconde tache"]
    assert "tâche annulée" in sortie
    assert "réponse à : seconde tache" in sortie


def test_repl_erreur_de_tache_ne_quitte_pas(entrees, capsys):
    agent = FauxAgent(reponses=[ValueError("La tâche est vide.")])
    entrees("x", "/quit")

    Repl(agent, Ui(rich=False)).run()

    assert "[ERREUR] La tâche est vide." in capsys.readouterr().out


def test_repl_reset_efface_historique(entrees, capsys):
    agent = FauxAgent()
    entrees("une tache", "/reset", "/quit")

    Repl(agent, Ui(rich=False)).run()

    assert agent.resets == 1
    assert agent.history == []
    assert "Historique effacé" in capsys.readouterr().out


def test_repl_bascule_approbation_des_outils(entrees, capsys):
    def confirme(name, arguments):
        return True

    agent = FauxAgent(confirm=confirme)
    entrees("/oui", "/oui", "/quit")

    Repl(agent, Ui(rich=False)).run()

    sortie = capsys.readouterr().out
    assert agent.confirm is confirme  # restaure apres deux bascules
    assert "automatique (plus de confirmation)" in sortie
    assert "demandée à chaque outil sensible" in sortie


def test_repl_commandes_information(entrees, capsys):
    agent = FauxAgent()
    agent.logger = SimpleNamespace(path="logs/session.jsonl")
    entrees("/outils", "/stats", "/journal", "/aide", "/quit")

    Repl(agent, Ui(rich=False)).run()

    sortie = capsys.readouterr().out
    assert "read_file" in sortie and "write_file" in sortie
    assert "tokens consommés : 42" in sortie
    assert "logs/session.jsonl" in sortie
    assert "/reset" in sortie


def test_repl_journal_absent(entrees, capsys):
    entrees("/journal", "/quit")

    Repl(FauxAgent(), Ui(rich=False)).run()

    assert "aucun journal actif" in capsys.readouterr().out


def test_repl_commande_inconnue(entrees, capsys):
    entrees("/inconnue", "/quit")

    Repl(FauxAgent(), Ui(rich=False)).run()

    sortie = capsys.readouterr().out
    assert "Commande inconnue : /inconnue" in sortie
    assert "/aide" in sortie


def test_repl_quiet_masque_les_evenements(entrees):
    agent = FauxAgent()
    entrees("x", "/quit")

    Repl(agent, Ui(rich=False, quiet=True)).run()

    assert agent.on_event is None


def test_repl_alias_de_sortie(entrees, capsys):
    agent = FauxAgent()
    entrees("quit")

    code = Repl(agent, Ui(rich=False)).run()

    assert code == 0
    assert agent.tasks == []
    assert "À bientôt." in capsys.readouterr().out



