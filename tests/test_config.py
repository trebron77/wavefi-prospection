"""Tests de la configuration : valeurs par defaut, validation, secrets masques."""

import os
from dataclasses import FrozenInstanceError

import pytest

from src.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    NO_KEY_PLACEHOLDER,
    ConfigError,
    Settings,
    load_dotenv_file,
    load_settings,
)


def test_valeurs_par_defaut():
    parametres = load_settings(env={}, load_file=False)

    assert parametres.model == DEFAULT_MODEL == "gpt-4o-mini"
    assert parametres.base_url == DEFAULT_BASE_URL == "https://api.openai.com/v1"
    assert parametres.api_key is None
    assert parametres.temperature == 0.0
    assert parametres.max_tokens == 4096
    assert parametres.timeout == 60.0
    assert parametres.max_retries == 3
    assert parametres.max_iterations == 15


def test_variables_lues_et_normalisees():
    parametres = load_settings(
        env={
            "LLM_API_KEY": "  sk-abcdefgh-WXYZ  ",
            "LLM_BASE_URL": "http://localhost:11434/v1",
            "LLM_MODEL": "qwen2.5-coder",
            "LLM_TEMPERATURE": "0,7",
            "LLM_MAX_TOKENS": "512",
            "LLM_TIMEOUT": "12.5",
            "LLM_MAX_RETRIES": "1",
            "MAX_ITERATIONS": "7",
        },
        load_file=False,
    )

    assert parametres.api_key == "sk-abcdefgh-WXYZ"
    assert parametres.model == "qwen2.5-coder"
    assert parametres.temperature == pytest.approx(0.7)
    assert parametres.max_tokens == 512
    assert parametres.timeout == pytest.approx(12.5)
    assert parametres.max_retries == 1
    assert parametres.max_iterations == 7


def test_variable_vide_ignoree():
    parametres = load_settings(env={"LLM_MODEL": "   "}, load_file=False)

    assert parametres.model == DEFAULT_MODEL


@pytest.mark.parametrize(
    "variable, valeur",
    [
        ("LLM_MAX_TOKENS", "beaucoup"),
        ("LLM_TIMEOUT", "vite"),
        ("LLM_MAX_RETRIES", "3.5"),
        ("MAX_ITERATIONS", "quinze"),
    ],
)
def test_entier_invalide_nomme_la_variable(variable, valeur):
    with pytest.raises(ConfigError) as info:
        load_settings(env={variable: valeur}, load_file=False)

    assert variable in str(info.value)


@pytest.mark.parametrize(
    "variable, valeur, borne",
    [
        ("LLM_MAX_TOKENS", "0", ">= 1"),
        ("LLM_TIMEOUT", "0", ">= 1.0"),
        ("LLM_MAX_RETRIES", "-1", ">= 0"),
        ("MAX_ITERATIONS", "0", ">= 1"),
        ("LLM_TEMPERATURE", "3", "<= 2.0"),
    ],
)
def test_valeur_hors_bornes(variable, valeur, borne):
    with pytest.raises(ConfigError) as info:
        load_settings(env={variable: valeur}, load_file=False)

    assert variable in str(info.value)
    assert borne in str(info.value)


def test_decimal_invalide():
    with pytest.raises(ConfigError) as info:
        load_settings(env={"LLM_TEMPERATURE": "chaud"}, load_file=False)

    assert "LLM_TEMPERATURE" in str(info.value)


@pytest.mark.parametrize("valeur", ["none", "NONE", "off", "null", "vide"])
def test_temperature_omissible(valeur):
    parametres = load_settings(env={"LLM_TEMPERATURE": valeur}, load_file=False)

    assert parametres.temperature is None


def test_api_locale_sans_cle():
    parametres = Settings(base_url="http://127.0.0.1:1234/v1")

    assert parametres.is_local_base_url is True
    assert parametres.has_api_key is False
    assert parametres.effective_api_key == NO_KEY_PLACEHOLDER


def test_api_distante_sans_cle_refusee():
    parametres = Settings(api_key=None, base_url="https://api.openai.com/v1")

    assert parametres.is_local_base_url is False
    with pytest.raises(ConfigError) as info:
        _ = parametres.effective_api_key

    assert "LLM_API_KEY" in str(info.value)


def test_cle_masquee():
    assert Settings(api_key=None).masked_api_key() == "(non configurée)"
    assert Settings(api_key="sk-1234").masked_api_key() == "***"
    assert Settings(api_key="sk-abcdefgh-WXYZ").masked_api_key() == "sk-a...WXYZ"


def test_repr_naffiche_jamais_la_cle():
    parametres = Settings(api_key="sk-super-secret-1234")

    representation = repr(parametres)

    assert "sk-super-secret-1234" not in representation
    assert "sk-s...1234" in representation
    assert "gpt-4o-mini" in representation


def test_settings_immuable():
    parametres = Settings()

    with pytest.raises(FrozenInstanceError):
        parametres.model = "autre-modele"


def test_chargement_du_fichier_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    fichier = tmp_path / ".env"
    fichier.write_text("LLM_MODEL=modele-du-fichier\n", encoding="utf-8")

    assert load_dotenv_file(fichier) is True
    assert os.environ["LLM_MODEL"] == "modele-du-fichier"
    assert load_settings(load_file=False).model == "modele-du-fichier"
