"""Tests de l'interface Streamlit (`app.py`) : interpreteur Python et secrets.

Le module `streamlit` est remplace par un double avant l'import de `app` : aucun
serveur n'est demarre, aucun sous-processus n'est reellement lance et rien n'est
ecrit sur le disque hors du dossier temporaire du test.
"""

import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


class EtatSession(dict):
    """Double de `st.session_state` : acces par cle et par attribut."""

    def __getattr__(self, nom):
        try:
            return self[nom]
        except KeyError as exc:  # AttributeError explicite plutot qu'un KeyError
            raise AttributeError(nom) from exc

    def __setattr__(self, nom, valeur):
        self[nom] = valeur


class Bloc:
    """Double des gestionnaires de contexte (`st.spinner`, `st.chat_message`)."""

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False


class SecretsAbsents:
    """Double d'un `st.secrets` sans `secrets.toml` : toute lecture echoue."""

    def keys(self):
        raise FileNotFoundError("No secrets files found.")


def faux_streamlit(prompt=None, secrets=None):
    """Module `streamlit` factice dont `chat_input` renvoie `prompt`."""
    module = types.ModuleType("streamlit")
    module.session_state = EtatSession(messages=[])
    module.secrets = {} if secrets is None else secrets
    module.set_page_config = lambda *arguments, **options: None
    module.title = lambda *arguments, **options: None
    module.caption = lambda *arguments, **options: None
    module.markdown = lambda *arguments, **options: None
    module.error = lambda *arguments, **options: None
    module.code = lambda *arguments, **options: None
    module.text = lambda *arguments, **options: None
    module.chat_input = lambda *arguments, **options: prompt
    module.chat_message = lambda *arguments, **options: Bloc()
    module.spinner = lambda *arguments, **options: Bloc()
    return module


@pytest.fixture
def charger_app(monkeypatch):
    """Importe `app.py` (corps du script execute) avec un `streamlit` factice."""

    def _charger(prompt=None, secrets=None):
        monkeypatch.setitem(sys.modules, "streamlit", faux_streamlit(prompt, secrets))
        sys.modules.pop("app", None)
        return importlib.import_module("app")

    yield _charger
    sys.modules.pop("app", None)


def creer_prefixe(racine: Path) -> Path:
    """Cree l'arborescence minimale d'un prefixe Python (venv).

    Returns:
        Chemin de l'interpreteur du prefixe, selon la plateforme.
    """
    if os.name == "nt":
        interpreteur = racine / "Scripts" / "python.exe"
    else:
        interpreteur = racine / "bin" / "python"
    interpreteur.parent.mkdir(parents=True, exist_ok=True)
    interpreteur.write_text("", encoding="utf-8")
    return interpreteur


def test_interpreteur_du_prefixe_prioritaire(charger_app, monkeypatch, tmp_path):
    """L'interpreteur du venv decrit par `sys.prefix` prime sur `sys.executable`."""
    prefixe = tmp_path / "venv"
    interpreteur = creer_prefixe(prefixe)
    monkeypatch.setattr(sys, "prefix", str(prefixe))
    # VIRTUAL_ENV ne doit pas entrer en ligne de compte.
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "autre-venv"))

    app = charger_app()

    assert app.python_executable() == str(interpreteur)


def test_repli_sur_sys_executable_si_prefixe_sans_interpreteur(
    charger_app, monkeypatch, tmp_path
):
    """Prefixe sans interpreteur sur le disque : repli sur `sys.executable`."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefixe-vide"))

    app = charger_app()

    assert app.python_executable() == sys.executable


def test_sous_processus_place_l_interpreteur_en_tete_du_path(charger_app, monkeypatch):
    """Le `PATH` transmis commence par le dossier de l'interpreteur courant."""
    dossier_python = os.path.dirname(sys.executable)
    monkeypatch.setenv("PATH", os.pathsep.join(["/systeme/bin", "/autre/bin"]))

    capture = {}

    def faux_run(commande, **options):
        capture["options"] = options
        return subprocess.CompletedProcess(commande, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", faux_run)

    charger_app(prompt="Bonjour")

    elements = capture["options"]["env"]["PATH"].split(os.pathsep)
    assert elements[0] == dossier_python
    assert elements[1:] == ["/systeme/bin", "/autre/bin"]
    # La copie ne doit pas modifier l'environnement du serveur.
    assert os.environ["PATH"] == os.pathsep.join(["/systeme/bin", "/autre/bin"])


def test_sous_processus_utilise_l_interpreteur_du_prefixe(
    charger_app, monkeypatch, tmp_path
):
    """`subprocess.run` recoit l'interpreteur du venv et une copie de l'environnement."""
    prefixe = tmp_path / "venv"
    interpreteur = creer_prefixe(prefixe)
    monkeypatch.setattr(sys, "prefix", str(prefixe))
    monkeypatch.setenv("LLM_API_KEY", "sk-test-interface")

    capture = {}

    def faux_run(commande, **options):
        capture["commande"] = commande
        capture["options"] = options
        return subprocess.CompletedProcess(commande, 0, stdout="reponse", stderr="")

    # `app` importe le module `subprocess` : le remplacer avant l'import du script
    # intercepte l'appel declenche pendant l'execution du corps de `app.py`.
    monkeypatch.setattr(subprocess, "run", faux_run)

    app = charger_app(prompt="Bonjour")

    assert capture["commande"] == [
        str(interpreteur),
        "src/agent.py",
        "-p",
        "Bonjour",
        "--model",
        "deepseek-chat",
        "--yes",
    ]
    options = capture["options"]
    assert options["env"] is not os.environ, "l'environnement doit etre copie"
    assert options["env"]["LLM_API_KEY"] == "sk-test-interface"
    assert options["capture_output"] is True
    assert options["encoding"] == "utf-8"
    assert options["errors"] == "replace"
    assert app.st.session_state.messages[-1] == {"role": "assistant", "content": "reponse"}
    options = capture["options"]
    assert options["env"] is not os.environ, "l'environnement doit etre copie"
    assert options["env"]["LLM_API_KEY"] == "sk-test-interface"
    assert options["capture_output"] is True
    assert options["encoding"] == "utf-8"
    assert options["errors"] == "replace"
    assert app.st.session_state.messages[-1] == {"role": "assistant", "content": "reponse"}


def test_secrets_injectes_dans_l_environnement(charger_app, monkeypatch):
    """Chaque secret scalaire devient une variable d'environnement texte."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    app = charger_app(
        secrets={
            "LLM_API_KEY": "sk-depuis-secrets",
            "LLM_MAX_TOKENS": 4096,
            "LLM_TEMPERATURE": 0.0,
        }
    )

    injectees = app.injecter_secrets()

    assert sorted(injectees) == ["LLM_API_KEY", "LLM_MAX_TOKENS", "LLM_TEMPERATURE"]
    assert os.environ["LLM_API_KEY"] == "sk-depuis-secrets"
    assert os.environ["LLM_MAX_TOKENS"] == "4096"
    assert os.environ["LLM_TEMPERATURE"] == "0.0"


def test_secrets_absents_ne_cassent_rien(charger_app, monkeypatch):
    """Pas de `secrets.toml` (`FileNotFoundError`) : aucune injection, pas de crash."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    app = charger_app(secrets=SecretsAbsents())

    assert app.injecter_secrets() == []
    assert "LLM_API_KEY" not in os.environ


def test_secrets_ne_ecrasent_pas_l_environnement(charger_app, monkeypatch):
    """Une variable deja definie (`.env`, shell) garde la priorite."""
    monkeypatch.setenv("LLM_API_KEY", "sk-environnement")
    app = charger_app(secrets={"LLM_API_KEY": "sk-depuis-secrets"})

    assert app.injecter_secrets() == []
    assert os.environ["LLM_API_KEY"] == "sk-environnement"


def test_sections_toml_ignorees(charger_app, monkeypatch):
    """Une section TOML n'est pas une variable d'environnement : elle est ignoree."""
    monkeypatch.delenv("mongodb", raising=False)
    monkeypatch.delenv("REGION", raising=False)
    app = charger_app(secrets={"mongodb": {"uri": "mongodb://exemple"}, "REGION": "eu"})

    assert app.injecter_secrets() == ["REGION"]
    assert "mongodb" not in os.environ


def test_sous_processus_recoit_les_secrets(charger_app, monkeypatch):
    """Bout en bout : un secret absent de l'environnement arrive au sous-processus."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    capture = {}

    def faux_run(commande, **options):
        capture["options"] = options
        return subprocess.CompletedProcess(commande, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", faux_run)

    charger_app(prompt="Bonjour", secrets={"LLM_API_KEY": "sk-depuis-secrets"})

    assert capture["options"]["env"]["LLM_API_KEY"] == "sk-depuis-secrets"


def test_secret_deepseek_alimente_llm_api_key(charger_app, monkeypatch):
    """`DEEPSEEK_API_KEY` sert aussi de `LLM_API_KEY`, le nom lu par src/config.py."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = charger_app(secrets={"DEEPSEEK_API_KEY": "sk-depuis-deepseek"})

    injectees = app.injecter_secrets()

    assert sorted(injectees) == ["DEEPSEEK_API_KEY", "LLM_API_KEY"]
    assert os.environ["LLM_API_KEY"] == "sk-depuis-deepseek"
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-depuis-deepseek"


def test_secret_deepseek_alimente_endpoint_et_modele(charger_app, monkeypatch):
    """Les autres noms `DEEPSEEK_*` suivent la meme correspondance."""
    for nom in ("LLM_BASE_URL", "LLM_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(nom, raising=False)
    app = charger_app(
        secrets={
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
            "DEEPSEEK_MODEL": "deepseek-chat",
        }
    )

    app.injecter_secrets()

    assert os.environ["LLM_BASE_URL"] == "https://api.deepseek.com/v1"
    assert os.environ["LLM_MODEL"] == "deepseek-chat"


def test_llm_api_key_prioritaire_sur_l_alias(charger_app, monkeypatch):
    """Si les deux noms sont presents dans les secrets, `LLM_API_KEY` gagne."""
    monkeypatch.setenv("LLM_API_KEY", "sk-prioritaire")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = charger_app(
        secrets={"LLM_API_KEY": "sk-llm", "DEEPSEEK_API_KEY": "sk-deepseek"}
    )

    app.injecter_secrets()

    assert os.environ["LLM_API_KEY"] == "sk-prioritaire"


def test_alias_n_ecrase_pas_l_environnement(charger_app, monkeypatch):
    """Une `LLM_API_KEY` deja definie (shell) n'est pas remplacee par l'alias."""
    monkeypatch.setenv("LLM_API_KEY", "sk-environnement")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = charger_app(secrets={"DEEPSEEK_API_KEY": "sk-deepseek"})

    injectees = app.injecter_secrets()

    assert "LLM_API_KEY" not in injectees
    assert os.environ["LLM_API_KEY"] == "sk-environnement"


def test_alias_vide_ignore(charger_app, monkeypatch):
    """Un `DEEPSEEK_API_KEY` vide ne cree pas de `LLM_API_KEY` fantome."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = charger_app(secrets={"DEEPSEEK_API_KEY": "   "})

    assert app.injecter_secrets() == ["DEEPSEEK_API_KEY"]
    assert "LLM_API_KEY" not in os.environ


def test_sous_processus_recoit_la_cle_deepseek(charger_app, monkeypatch):
    """Bout en bout : `DEEPSEEK_API_KEY` arrive au sous-processus sous les deux noms."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    capture = {}

    def faux_run(commande, **options):
        capture["options"] = options
        return subprocess.CompletedProcess(commande, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", faux_run)

    charger_app(prompt="Bonjour", secrets={"DEEPSEEK_API_KEY": "sk-deepseek"})

    environnement = capture["options"]["env"]
    assert environnement["LLM_API_KEY"] == "sk-deepseek"
    assert environnement["DEEPSEEK_API_KEY"] == "sk-deepseek"
