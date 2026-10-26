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
    assert args.token_budget is None
    assert args.log_dir == "logs"
    assert args.no_log is False
    assert args.stream is False
    assert args.dry_run is False
    assert args.quiet is False


def test_analyseur_options():
    args = _build_parser().parse_args(
        [
            "-p",
            "tache",
            "--max-iterations",
            "7",
            "--token-budget",
            "900",
            "--log-dir",
            "tmp-logs",
            "--no-log",
            "--stream",
            "--dry-run",
            "-q",
        ]
    )

    assert args.prompt == "tache"
    assert args.max_iterations == 7
    assert args.token_budget == 900
    assert args.log_dir == "tmp-logs"
    assert args.no_log is True
    assert args.stream is True
    assert args.dry_run is True
    assert args.quiet is True


def test_option_inconnue_rejetee():
    with pytest.raises(SystemExit) as info:
        _build_parser().parse_args(["--inexistant"])

    assert info.value.code == 2

