"""Tests des helpers git en lecture seule (``git diff`` / ``git status``).

Un vrai depot temporaire est cree dans ``tmp_path`` : rien n'est ecrit dans le
projet courant. Si ``git`` n'est pas installe, le module entier est ignore.
"""

import shutil
import subprocess

import pytest

from src.tools.git import read_git_diff, read_git_status

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git indisponible dans cet environnement"
)


def _git(cwd, *arguments):
    """Execute une commande git de preparation (identite fournie en option)."""
    subprocess.run(
        [
            shutil.which("git"),
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            *arguments,
        ],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def depot(tmp_path):
    """Depot git contenant deux fichiers suivis, dont un modifie."""
    (tmp_path / "notes.md").write_text("ligne 1\nligne 2\n", encoding="utf-8")
    (tmp_path / "autre.txt").write_text("inchange\n", encoding="utf-8")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "--quiet", "-m", "init")
    (tmp_path / "notes.md").write_text("ligne 1 modifiee\nligne 2\n", encoding="utf-8")
    return tmp_path


def test_diff_montre_la_modification(depot):
    diff = read_git_diff(depot)

    assert "-ligne 1\n" in diff
    assert "+ligne 1 modifiee\n" in diff
    assert "notes.md" in diff


def test_diff_limite_a_un_fichier(depot):
    (depot / "autre.txt").write_text("change aussi\n", encoding="utf-8")

    diff = read_git_diff(depot, "notes.md")

    assert "notes.md" in diff
    assert "autre.txt" not in diff


def test_diff_vide_sans_modification(depot):
    _git(depot, "checkout", "--", "notes.md")

    assert read_git_diff(depot) == ""


def test_diff_tronque(depot):
    complet = read_git_diff(depot)
    diff = read_git_diff(depot, max_chars=12)

    assert diff.startswith("diff --git")
    assert diff.endswith("(sortie tronquée)")
    assert len(diff) < len(complet)
    assert len(diff) < 60


def test_racine_absente_ou_sans_depot(tmp_path):
    assert read_git_diff(None) == ""
    assert read_git_status(None) == ""
    assert read_git_diff(tmp_path / "inexistant") == ""
    # Dossier existant mais hors depot : git echoue, la fonction reste muette.
    assert read_git_diff(tmp_path) == ""


def test_status_liste_les_fichiers_modifies(depot):
    statut = read_git_status(depot)

    assert "notes.md" in statut
    assert statut.startswith("##")
