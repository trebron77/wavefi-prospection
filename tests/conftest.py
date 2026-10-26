"""Fixtures partagees : projet temporaire, bac a sable, registre et settings.

Chaque test recoit un dossier ``tmp_path`` isole : rien n'est ecrit dans le vrai
projet et aucun test ne depend d'un fichier local (ni ``.env``, ni ``logs/``).
"""

from pathlib import Path

import pytest

from helpers import ecrire_structure
from src.config import Settings
from src.tools import Sandbox, build_default_registry


@pytest.fixture
def projet(tmp_path: Path) -> Path:
    """Arborescence de projet representative, dans un dossier temporaire."""
    return ecrire_structure(tmp_path)


@pytest.fixture
def sandbox(projet: Path) -> Sandbox:
    """Bac a sable dont la racine est le projet temporaire."""
    return Sandbox(projet)


@pytest.fixture
def registry(projet: Path):
    """Registre standard (cinq outils) borne au projet temporaire."""
    return build_default_registry(root=projet)


@pytest.fixture
def settings() -> Settings:
    """Configuration deterministe, sans lecture de ``.env`` ni de l'environnement."""
    return Settings(
        api_key="sk-test",
        model="fake-model",
        base_url="https://api.openai.com/v1",
        temperature=0.0,
        max_tokens=512,
        timeout=5.0,
        max_retries=0,
        max_iterations=5,
    )
