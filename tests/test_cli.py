"""Tests de la ligne de commande de l'agent (aucun appel reseau, aucun disque).

``Agent`` est remplace par un double : on verifie les arguments transmis, les
messages affiches et le code de sortie de chaque scenario.
"""

import io

import pytest

import src.agent as agent_module
from src.agent import STOP_FINAL, STOP_MAX_ITERATIONS, AgentResult, _build_parser, main
from src.config import ConfigError


class FauxAgent:
    """Double d'agent : enregistre sa construction et rejoue un resultat."""

    instances = []
    resultat_defaut = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.logger = None
        self.tasks = []
        self.closed = False
        self.interrupt = False
        self.result = FauxAgent.resultat_defaut or AgentResult(answer="fini")
        FauxAgent.instances.append(self)

    def capabilities(self):
        return "CAPACITES-DRY-RUN"

    def run(self, task):
        self.tasks.append(task)
        if self.interrupt:
            raise KeyboardInterrupt
        if self.logger is not None:
            self.result.log_path = str(self.logger.path)
        return self.result

    def close(self):
        self.closed = True


@pytest.fixture
def cli(monkeypatch, settings):
    """Neutralise la configuration et remplace ``Agent`` par ``FauxAgent``."""
    FauxAgent.instances = []
    FauxAgent.resultat_defaut = None
    monkeypatch.setattr(agent_module, "load_settings", lambda: settings)
    monkeypatch.setattr(agent_module, "Agent", FauxAgent)
    yield FauxAgent
    FauxAgent.instances = []
    FauxAgent.resultat_defaut = None


@pytest.fixture
def stdin_vide(monkeypatch):
    """Simule un terminal sans saisie possible."""

    def pas_de_saisie(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("builtins.input", pas_de_saisie)


def test_dry_run_naffiche_que_les_capacites(cli, capsys):
    code = main(["--dry-run", "-p", "n'importe quoi"])

    sortie = capsys.readouterr().out
    assert code == 0
    assert "CAPACITES-DRY-RUN" in sortie
    assert cli.instances[0].tasks == []


def test_prompt_execute_et_affiche_les_stats(cli, tmp_path, capsys):
    code = main(["-p", "liste les fichiers", "--no-log"])

    sortie = capsys.readouterr().out
    agent = cli.instances[0]
    assert code == 0
    assert agent.tasks == ["liste les fichiers"]
    assert agent.closed is True
    assert "fini" in sortie
    assert "[stats] final_answer" in sortie
    assert "journal" not in sortie


def test_quiet_naffiche_que_la_reponse(cli, capsys):
    code = main(["-p", "x", "-q", "--no-log"])

    sortie = capsys.readouterr().out
    assert code == 0
    assert "fini" in sortie
    assert "[stats]" not in sortie
    assert cli.instances[0].kwargs["verbose"] is False


def test_arret_sur_borne_retourne_2(cli, capsys):
    cli.resultat_defaut = AgentResult(
        answer="borne atteinte", stopped_reason=STOP_MAX_ITERATIONS
    )

    code = main(["-p", "x", "--no-log"])

    assert code == 2
    assert "borne atteinte" in capsys.readouterr().out
    assert cli.instances[0].closed is True


def test_journal_cree_dans_le_dossier_demande(cli, tmp_path, capsys):
    code = main(["-p", "x", "--log-dir", str(tmp_path)])

    agent = cli.instances[0]
    assert code == 0
    assert agent.logger is not None
    assert agent.logger.path.parent == tmp_path
    assert agent.logger.path.exists()
    agent.logger.close()
    assert "journal" in capsys.readouterr().out


def test_no_log_desactive_le_journal(cli):
    code = main(["-p", "x", "--no-log"])

    assert code == 0
    assert cli.instances[0].logger is None


def test_option_stream_transmise(cli):
    main(["-p", "x", "--no-log", "--stream", "--max-iterations", "3"])

    agent = cli.instances[0]
    assert agent.kwargs["stream"] is True
    assert agent.kwargs["max_iterations"] == 3


def test_tache_lue_sur_entree_standard(cli, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("liste les fichiers\n"))

    code = main(["--no-log"])

    assert code == 0
    assert cli.instances[0].tasks == ["liste les fichiers"]


def test_aucune_tache(cli, stdin_vide, capsys):
    code = main(["--no-log"])

    assert code == 1
    assert "Aucune tâche" in capsys.readouterr().out
    assert cli.instances[0].tasks == []


def test_configuration_invalide(monkeypatch, capsys):
    def echec():
        raise ConfigError("LLM_API_KEY manquante")

    monkeypatch.setattr(agent_module, "load_settings", echec)

    code = main(["-p", "x"])

    assert code == 1
    assert "Configuration invalide" in capsys.readouterr().out


def test_initialisation_impossible(cli, monkeypatch, capsys):
    def echec(**_kwargs):
        raise ValueError("max_iterations doit être >= 1.")

    monkeypatch.setattr(agent_module, "Agent", echec)

    code = main(["-p", "x", "--max-iterations", "0"])

    assert code == 1
    assert "Initialisation impossible" in capsys.readouterr().out


def test_interruption_clavier(cli, monkeypatch, capsys):
    def interrompre(**kwargs):
        agent = FauxAgent(**kwargs)
        agent.interrupt = True
        return agent

    monkeypatch.setattr(agent_module, "Agent", interrompre)

    code = main(["-p", "x", "--no-log"])

    assert code == 130
    assert "INTERROMPU" in capsys.readouterr().out


def test_analyseur_valeurs_par_defaut():
    args = _build_parser().parse_args([])

    assert args.prompt is None
    assert args.max_iterations is None
    assert args.model is None
    assert args.token_budget is None
    assert args.context_tokens == 16000
    assert args.no_context is False
    assert args.log_dir == "logs"
    assert args.no_log is False
    assert args.stream is False
    assert args.dry_run is False
    assert args.quiet is False
    assert args.yes is False


def test_analyseur_options():
    args = _build_parser().parse_args(
        [
            "-p",
            "tache",
            "--max-iterations",
            "7",
            "--model",
            "modele-cli",
            "--token-budget",
            "900",
            "--context-tokens",
            "4000",
            "--no-context",
            "--log-dir",
            "tmp-logs",
            "--no-log",
            "--stream",
            "--dry-run",
            "-y",
            "-q",
        ]
    )

    assert args.prompt == "tache"
    assert args.max_iterations == 7
    assert args.model == "modele-cli"
    assert args.token_budget == 900
    assert args.context_tokens == 4000
    assert args.no_context is True
    assert args.log_dir == "tmp-logs"
    assert args.no_log is True
    assert args.stream is True
    assert args.dry_run is True
    assert args.quiet is True
    assert args.yes is True


def test_option_inconnue_rejetee():
    with pytest.raises(SystemExit) as info:
        _build_parser().parse_args(["--inexistant"])

    assert info.value.code == 2


# --- Gestion du contexte (CLI)


def test_contexte_projet_injecte_par_defaut(cli):
    main(["-p", "x", "--no-log"])

    kwargs = cli.instances[0].kwargs
    assert kwargs["initial_context"] is True
    assert kwargs["context_tokens"] == 16000


def test_no_context_desactive_l_instantane(cli):
    main(["-p", "x", "--no-log", "--no-context"])

    assert cli.instances[0].kwargs["initial_context"] is False


def test_context_tokens_transmis(cli):
    main(["-p", "x", "--no-log", "--context-tokens", "2500"])

    assert cli.instances[0].kwargs["context_tokens"] == 2500


def test_context_tokens_zero_desactive_la_troncature(cli):
    main(["-p", "x", "--no-log", "--context-tokens", "0"])

    assert cli.instances[0].kwargs["context_tokens"] is None


def test_context_tokens_negatif_rejete(cli, capsys):
    code = main(["-p", "x", "--no-log", "--context-tokens", "-1"])

    assert code == 1
    assert "--context-tokens" in capsys.readouterr().out
    assert cli.instances == []


# --- Garde-fous CLI : modele force, confirmation et version


class FauxTTY(io.StringIO):
    """Flux minimal imitant un terminal interactif."""

    def isatty(self):
        return True


def test_option_model_surcharge_la_configuration(cli, settings):
    code = main(["-p", "x", "--no-log", "--model", "  modele-force  "])

    assert code == 0
    assert cli.instances[0].kwargs["settings"].model == "modele-force"
    # La configuration d'origine reste intacte (dataclass gelee).
    assert settings.model == "fake-model"


def test_option_model_vide_rejetee(cli, capsys):
    code = main(["-p", "x", "--no-log", "--model", "   "])

    assert code == 1
    assert "--model" in capsys.readouterr().out
    assert cli.instances == []


def test_yes_desactive_la_confirmation(cli):
    main(["-p", "x", "--no-log", "--yes"])

    assert cli.instances[0].kwargs["confirm"] is None


def test_confirmation_active_par_defaut(cli):
    main(["-p", "x", "--no-log"])

    confirm = cli.instances[0].kwargs["confirm"]
    assert callable(confirm)


def test_confirmation_interactive_acceptee(cli, monkeypatch):
    invites = []
    monkeypatch.setattr("sys.stdin", FauxTTY(""))

    def repondre(invite):
        invites.append(invite)
        return "o"

    monkeypatch.setattr("builtins.input", repondre)
    main(["-p", "x", "--no-log"])

    accepte = cli.instances[0].kwargs["confirm"]("run_command", {"command": "pytest"})

    assert accepte is True
    assert "run_command" in invites[0]
    assert "pytest" in invites[0]


def test_confirmation_interactive_refusee(cli, monkeypatch):
    invites = []
    monkeypatch.setattr("sys.stdin", FauxTTY(""))

    def repondre(invite):
        invites.append(invite)
        return ""

    monkeypatch.setattr("builtins.input", repondre)
    main(["-p", "x", "--no-log"])

    accepte = cli.instances[0].kwargs["confirm"]("run_command", {"command": "pytest"})

    assert accepte is False
    assert "run_command" in invites[0]


def test_confirmation_hors_terminal_refusee(cli, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(["-p", "x", "--no-log"])

    accepte = cli.instances[0].kwargs["confirm"]("run_command", {"command": "pytest"})

    sortie = capsys.readouterr().out
    assert accepte is False
    assert "[REFUS]" in sortie
    assert "--yes" in sortie


def test_confirmation_entree_fermee(cli, monkeypatch):
    monkeypatch.setattr("sys.stdin", FauxTTY(""))

    def ferme(invite):
        raise EOFError

    monkeypatch.setattr("builtins.input", ferme)
    main(["-p", "x", "--no-log"])

    assert cli.instances[0].kwargs["confirm"]("run_command", {}) is False


def test_version_affichee(capsys):
    from src import __version__

    with pytest.raises(SystemExit) as info:
        main(["--version"])

    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out

