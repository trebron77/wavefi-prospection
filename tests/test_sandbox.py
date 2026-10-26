"""Tests du bac a sable : aucun chemin ne doit sortir du dossier du projet."""

import os

import pytest

from src.tools.base import ToolError
from src.tools.sandbox import Sandbox, is_secret_name


def test_racine_et_chemin_relatif(sandbox, projet):
    assert sandbox.root == projet.resolve()
    cible = sandbox.resolve("src/main.py")

    assert cible == (projet / "src" / "main.py").resolve()
    assert sandbox.relative(cible) == "src/main.py"
    assert sandbox.relative(sandbox.resolve(".")) == "."


@pytest.mark.parametrize(
    "chemin",
    ["../secret.txt", "..", "src/../../hors.txt", "/etc/passwd"],
)
def test_chemins_hors_projet_refuses(sandbox, chemin):
    with pytest.raises(ToolError) as info:
        sandbox.resolve(chemin)

    assert "hors du dossier du projet" in str(info.value) or "Chemin" in str(
        info.value
    )


def test_chemin_absolu_dans_le_projet_accepte(sandbox, projet):
    cible = projet / "notes.md"

    assert sandbox.resolve(str(cible)) == cible.resolve()


def test_chemin_null_refuse(sandbox):
    with pytest.raises(ToolError):
        sandbox.resolve("notes.md\x00.txt")


def test_must_exist(sandbox):
    with pytest.raises(ToolError) as info:
        sandbox.resolve("absent.txt", must_exist=True)

    assert "introuvable" in str(info.value)


def test_kind_file_et_dir(sandbox):
    with pytest.raises(ToolError) as info:
        sandbox.resolve("src", kind="file")
    assert "n'est pas un fichier" in str(info.value)

    with pytest.raises(ToolError) as info:
        sandbox.resolve("notes.md", kind="dir")
    assert "n'est pas un dossier" in str(info.value)

    assert sandbox.resolve("src", kind="dir").name == "src"
    assert sandbox.resolve("notes.md", kind="file").name == "notes.md"


@pytest.mark.parametrize(
    "chemin",
    [".env", ".env.local", ".git/config", ".venv/pyvenv.cfg", "node_modules/x.js"],
)
def test_ecriture_dans_zones_protegees_refusee(sandbox, chemin):
    cible = sandbox.resolve(chemin)

    with pytest.raises(ToolError):
        sandbox.ensure_writable(cible)


def test_ecriture_normale_autorisee(sandbox):
    cible = sandbox.resolve("src/nouveau.py")

    assert sandbox.ensure_writable(cible) == cible


@pytest.mark.parametrize(
    "nom, attendu",
    [
        (".env", True),
        (".ENV", True),
        (".env.prod", True),
        ("env", False),
        ("notes.md", False),
        ("config.py", False),
    ],
)
def test_is_secret_name(nom, attendu):
    assert is_secret_name(nom) is attendu


def test_racine_absente_refusee(tmp_path):
    with pytest.raises(ToolError) as info:
        Sandbox(tmp_path / "dossier-absent")

    assert "introuvable" in str(info.value)


def test_lien_symbolique_sortant_refuse(projet, tmp_path):
    exterieur = tmp_path.parent / "exterieur-agent-test"
    exterieur.mkdir(exist_ok=True)
    lien = projet / "lien-sortant"
    try:
        os.symlink(str(exterieur), str(lien), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("liens symboliques indisponibles (privileges Windows)")

    box = Sandbox(projet)
    with pytest.raises(ToolError):
        box.resolve("lien-sortant")


def test_repr_sandbox(sandbox, projet):
    assert repr(str(projet.resolve())) in repr(sandbox)
