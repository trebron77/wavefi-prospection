"""Tests de la boucle agentique : bornes, erreurs, journal et evenements."""

import json
import sys

import pytest

import src.agent as agent_module
from helpers import FakeClient, reponse_finale, reponse_outil
from src.agent import (
    STOP_ERROR,
    STOP_FINAL,
    STOP_MAX_ITERATIONS,
    STOP_TOKEN_BUDGET,
    Agent,
    AgentResult,
    EventLogger,
)
from src.llm_client import LLMError, LLMResponse, ToolCall
from src.tools import Tool, ToolRegistry


def test_boucle_outil_puis_reponse(settings, registry, tmp_path):
    chemin_journal = tmp_path / "logs" / "session.jsonl"
    journal = EventLogger(chemin_journal)
    evenements = []
    client = FakeClient(
        [
            reponse_outil(
                "read_file", {"path": "src/main.py"}, call_id="c1", total_tokens=10
            ),
            reponse_finale("Le fichier affiche bonjour.", total_tokens=12),
        ]
    )
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        logger=journal,
        verbose=False,
        on_event=lambda nom, champs: evenements.append(nom),
    )

    resultat = agent.run("Que fait src/main.py ?")

    assert resultat.success is True
    assert resultat.stopped_reason == STOP_FINAL
    assert resultat.answer == "Le fichier affiche bonjour."
    assert resultat.iterations == 2
    assert resultat.tool_calls == 1
    assert resultat.total_tokens == 22

    roles = [message["role"] for message in resultat.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    message_outil = resultat.messages[3]
    assert "print('bonjour')" in message_outil["content"]
    assert message_outil["tool_call_id"] == "c1"
    assert "tool_calls" in resultat.messages[2]

    assert len(client.calls[0]["tools"]) == 5
    assert client.calls[0]["tools"][0]["type"] == "function"
    assert "agent" in resultat.messages[0]["content"].lower()

    assert "session_start" in evenements
    assert "tool_call" in evenements
    assert "tool_result" in evenements
    assert "session_end" in evenements

    journal.close()
    lignes = [
        json.loads(ligne)
        for ligne in chemin_journal.read_text(encoding="utf-8").splitlines()
        if ligne.strip()
    ]
    noms = [ligne["event"] for ligne in lignes]
    assert "session_start" in noms
    assert "tool_result" in noms
    assert "session_end" in noms
    assert resultat.log_path == str(chemin_journal)
    assert len(agent.history) == 5


def test_ecriture_via_un_outil(settings, registry, projet):
    client = FakeClient(
        [
            reponse_outil(
                "write_file",
                {"path": "src/nouveau.py", "content": "X = 1\n"},
                call_id="w1",
                total_tokens=5,
            ),
            reponse_finale("Fichier cree.", total_tokens=5),
        ]
    )
    agent = Agent(settings=settings, client=client, registry=registry, verbose=False)

    resultat = agent.run("Cree src/nouveau.py")

    assert resultat.answer == "Fichier cree."
    assert (projet / "src" / "nouveau.py").read_text(encoding="utf-8") == "X = 1\n"


def test_borne_max_iterations(settings, registry):
    reponses = [
        reponse_outil(
            "list_dir", {"path": "src", "max_depth": index % 2 + 1}, total_tokens=3
        )
        for index in range(5)
    ]
    client = FakeClient(reponses)
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        max_iterations=2,
        verbose=False,
    )

    resultat = agent.run("Explore")

    assert resultat.stopped_reason == STOP_MAX_ITERATIONS
    assert resultat.iterations == 2
    assert len(client.calls) == 2
    assert "Limite de 2 itérations" in resultat.answer
    assert resultat.success is False


def test_arguments_json_invalides_renvoyes_au_modele(settings, registry):
    client = FakeClient(
        [
            LLMResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        id="bad",
                        name="read_file",
                        arguments={},
                        arguments_error="Expecting value",
                    )
                ],
                total_tokens=2,
            ),
            reponse_finale("ok", total_tokens=2),
        ]
    )
    agent = Agent(settings=settings, client=client, registry=registry, verbose=False)

    resultat = agent.run("Lis")

def test_appel_repete_non_reexecute(settings):
    compteur = {"n": 0}

    def compter():
        compteur["n"] += 1
        return "resultat stable"

    registre = ToolRegistry(
        [Tool("ping", "Test.", {"type": "object", "properties": {}}, compter)]
    )
    client = FakeClient(
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="p1", name="ping")]),
            LLMResponse(text="", tool_calls=[ToolCall(id="p2", name="ping")]),
            reponse_finale("fini"),
        ]
    )
    agent = Agent(settings=settings, client=client, registry=registre, verbose=False)

    resultat = agent.run("Ping")

    assert compteur["n"] == 1
    dernier_outil = [m for m in resultat.messages if m["role"] == "tool"][-1]
    assert "déjà exécuté" in dernier_outil["content"]
    assert resultat.answer == "fini"


def test_outil_inconnu_renvoye_au_modele(settings, registry):
    client = FakeClient(
        [
            LLMResponse(
                text="",
                tool_calls=[ToolCall(id="u1", name="rm_rf", arguments={"path": "/"})],
            ),
            reponse_finale("Je ne peux pas."),
        ]
    )
    agent = Agent(settings=settings, client=client, registry=registry, verbose=False)

    resultat = agent.run("Supprime tout")

    message = [m for m in resultat.messages if m["role"] == "tool"][0]["content"]
    assert "inconnu" in message.lower()
    assert "Je ne peux pas." == resultat.answer


def test_budget_de_tokens(settings, registry):
    reponses = [
        reponse_outil(
            "list_dir", {"path": "src", "max_depth": index % 2 + 1}, total_tokens=100
        )
        for index in range(5)
    ]
    client = FakeClient(reponses)
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        token_budget=50,
        verbose=False,
    )

    resultat = agent.run("Explore")

    assert resultat.stopped_reason == STOP_TOKEN_BUDGET
    assert len(client.calls) == 1
    assert "Budget de tokens atteint" in resultat.answer


def test_erreur_llm(settings, registry):
    client = FakeClient([LLMError("quota depasse")])
    agent = Agent(settings=settings, client=client, registry=registry, verbose=False)

    resultat = agent.run("Bonjour")

    assert resultat.stopped_reason == STOP_ERROR
    assert "quota depasse" in resultat.answer
    assert resultat.success is False


def test_tache_vide(settings, registry):
    agent = Agent(
        settings=settings,
        client=FakeClient([]),
        registry=registry,
        verbose=False,
    )

    with pytest.raises(ValueError):
        agent.run("   ")


def test_historique_initial_respecte(settings, registry):
    client = FakeClient([reponse_finale("ok")])
    agent = Agent(settings=settings, client=client, registry=registry, verbose=False)
    historique = [
        {"role": "system", "content": "systeme personnalise"},
        {"role": "user", "content": "question precedente"},
        {"role": "assistant", "content": "reponse precedente"},
    ]

    resultat = agent.run("suite", history=historique)

    roles = [message["role"] for message in resultat.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert resultat.messages[0]["content"] == "systeme personnalise"


def test_reset_history(settings, registry):
    client = FakeClient([reponse_finale("ok")])
    agent = Agent(settings=settings, client=client, registry=registry, verbose=False)

    agent.run("Bonjour")
    assert agent.history

    agent.reset_history()
    assert agent.history == []


def test_summary_et_repr(settings, registry):
    client = FakeClient([reponse_finale("fini", total_tokens=4)])
    agent = Agent(settings=settings, client=client, registry=registry, verbose=False)

    resultat = agent.run("Bonjour")

    assert isinstance(resultat, AgentResult)
    assert "final_answer" in resultat.summary()
    assert "fake-model" in repr(agent)
    assert "tools=5" in repr(agent)
    assert "Outils disponibles" in agent.capabilities()


@pytest.mark.parametrize(
    "champs",
    [
        {"max_iterations": 0},
        {"token_budget": 0},
        {"token_budget": -10},
    ],
)
def test_validations_de_construction(settings, registry, champs):
    with pytest.raises(ValueError):
        Agent(
            settings=settings,
            client=FakeClient([]),
            registry=registry,
            verbose=False,
            **champs,
        )


def test_journal_desactive(settings, registry):
    client = FakeClient([reponse_finale("ok")])
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        logger=None,
        verbose=False,
    )

    resultat = agent.run("Bonjour")

    assert resultat.log_path is None


# --- Garde-fous : confirmation des outils sensibles et trace git avant ecriture


def _commande_python(code: str) -> str:
    """Commande ``python -c`` avec l'interpreteur courant (nom autorise)."""
    return f'"{sys.executable}" -c "{code}"'


def _messages_outil(resultat):
    """Contenus des messages ``tool`` renvoyes au modele."""
    return [
        message["content"] for message in resultat.messages if message["role"] == "tool"
    ]


def test_outil_sensible_refuse(settings, registry):
    demandes = []
    evenements = []

    def refuser(name, arguments):
        demandes.append((name, arguments))
        return False

    commande = _commande_python("print('ok')")
    client = FakeClient(
        [
            reponse_outil("run_command", {"command": commande}, call_id="r1"),
            reponse_finale("Commande non executee."),
        ]
    )
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        verbose=False,
        confirm=refuser,
        on_event=lambda nom, champs: evenements.append((nom, champs)),
    )

    resultat = agent.run("Lance la commande")

    assert demandes == [("run_command", {"command": commande})]
    assert "tool_refused" in [nom for nom, _ in evenements]
    (contenu,) = _messages_outil(resultat)
    assert "ACTION REFUSÉE" in contenu
    assert "code de sortie" not in contenu
    assert resultat.answer == "Commande non executee."


def test_outil_sensible_accepte(settings, registry):
    commande = _commande_python("print('ok')")
    evenements = []
    client = FakeClient(
        [
            reponse_outil("run_command", {"command": commande}, call_id="r1"),
            reponse_finale("Commande lancee."),
        ]
    )
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        verbose=False,
        confirm=lambda name, arguments: True,
        on_event=lambda nom, champs: evenements.append((nom, champs)),
    )

    resultat = agent.run("Lance la commande")

    assert "tool_approved" in [nom for nom, _ in evenements]
    (contenu,) = _messages_outil(resultat)
    assert "code de sortie : 0" in contenu
    assert "ok" in contenu
    assert resultat.answer == "Commande lancee."


def test_confirmation_en_erreur_refuse(settings, registry):
    def exploser(name, arguments):
        raise RuntimeError("interface indisponible")

    client = FakeClient(
        [
            reponse_outil("run_command", {"command": _commande_python("pass")}),
            reponse_finale("Abandon."),
        ]
    )
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        verbose=False,
        confirm=exploser,
    )

    resultat = agent.run("Lance la commande")

    (contenu,) = _messages_outil(resultat)
    assert "ACTION REFUSÉE" in contenu
    assert resultat.success is True


def test_outil_inoffensif_non_soumis_a_confirmation(settings, registry):
    appels = []

    def confirmer(name, arguments):
        appels.append(name)
        return False

    client = FakeClient(
        [
            reponse_outil("read_file", {"path": "src/main.py"}),
            reponse_finale("Fichier lu."),
        ]
    )
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        verbose=False,
        confirm=confirmer,
    )

    resultat = agent.run("Lis src/main.py")

    (contenu,) = _messages_outil(resultat)
    assert "print('bonjour')" in contenu
    assert appels == []


def test_confirm_non_appelable_rejete(settings, registry):
    with pytest.raises(TypeError):
        Agent(
            settings=settings,
            client=FakeClient([]),
            registry=registry,
            verbose=False,
            confirm="oui",
        )


def test_diff_git_journalise_avant_ecriture(settings, registry, projet, monkeypatch):
    monkeypatch.setattr(
        agent_module, "read_git_diff", lambda root, path: "- ancien\n+ nouveau\n"
    )
    evenements = []
    client = FakeClient(
        [
            reponse_outil(
                "write_file",
                {"path": "src/nouveau.py", "content": "X = 2\n"},
                call_id="w1",
            ),
            reponse_finale("Ecrit."),
        ]
    )
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        verbose=False,
        on_event=lambda nom, champs: evenements.append((nom, champs)),
    )

    resultat = agent.run("Ecris src/nouveau.py")

    diffs = [champs for nom, champs in evenements if nom == "git_diff"]
    assert len(diffs) == 1
    assert diffs[0]["path"] == "src/nouveau.py"
    assert diffs[0]["diff"].startswith("- ancien")
    assert (projet / "src" / "nouveau.py").read_text(encoding="utf-8") == "X = 2\n"
    assert resultat.answer == "Ecrit."


def test_pas_devenement_git_sans_diff(settings, registry, monkeypatch):
    monkeypatch.setattr(agent_module, "read_git_diff", lambda root, path: "")
    evenements = []
    client = FakeClient(
        [
            reponse_outil(
                "write_file", {"path": "notes.md", "content": "x\n"}, call_id="w1"
            ),
            reponse_finale("Ecrit."),
        ]
    )
    agent = Agent(
        settings=settings,
        client=client,
        registry=registry,
        verbose=False,
        on_event=lambda nom, champs: evenements.append((nom, champs)),
    )

    agent.run("Ecris notes.md")

    assert "git_diff" not in [nom for nom, _ in evenements]


def test_capabilities_indique_la_confirmation(settings, registry):
    sans = Agent(
        settings=settings, client=FakeClient([]), registry=registry, verbose=False
    )
    avec = Agent(
        settings=settings,
        client=FakeClient([]),
        registry=registry,
        verbose=False,
        confirm=lambda name, arguments: True,
    )

    assert "non demandée (--yes)" in sans.capabilities()
    assert "demandée avant chaque outil sensible" in avec.capabilities()

