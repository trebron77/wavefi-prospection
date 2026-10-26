"""Tests de l'execution de commandes : liste blanche, sans shell, timeout."""

import sys

import pytest

from src.tools.base import ToolError
from src.tools.shell import ALLOWED_COMMANDS, _split_command, run_command


def _python(code: str) -> str:
    """Commande ``python -c`` utilisant l'interpreteur courant (nom autorise)."""
    return f'"{sys.executable}" -c "{code}"'


def test_commande_reussie(sandbox):
    resultat = run_command(sandbox, _python("print(42)"))

    assert "42" in resultat
    assert "code de sortie : 0" in resultat
    assert "$ " in resultat


def test_echec_visible(sandbox):
    resultat = run_command(sandbox, _python("raise SystemExit(3)"))

    assert "[échec, code de sortie : 3]" in resultat


def test_stderr_capture(sandbox):
    resultat = run_command(
        sandbox, _python("import sys; sys.stderr.write('souci')")
    )

    assert "[stderr]" in resultat
    assert "souci" in resultat


def test_sortie_vide(sandbox):
    resultat = run_command(sandbox, _python("pass"))

    assert "(aucune sortie)" in resultat


def test_timeout(sandbox):
    with pytest.raises(ToolError) as info:
        run_command(sandbox, _python("import time; time.sleep(5)"), timeout=1)

    assert "dépassé" in str(info.value)


def test_cwd_sous_dossier(sandbox):
    resultat = run_command(sandbox, _python("print('depuis-src')"), cwd="src")

    assert "depuis-src" in resultat
    assert "(dossier : src)" in resultat


def test_cwd_hors_projet(sandbox):
    with pytest.raises(ToolError):
        run_command(sandbox, ["git", "status"], cwd="..")


@pytest.mark.parametrize(
    "commande, extrait",
    [
        ("git push origin main", "dépôt distant"),
        ("git reset --hard", "perte"),
        ("pip install requests", "réseau"),
        ("python -m pip install requests", "réseau"),
        ("npm install", "npm"),
        ("npx create-app", "npx"),
        ("powershell -c dir", "non autorisée"),
        ("git status && git log", "Opérateur de shell"),
        ("git status | cat", "Opérateur de shell"),
        ("git log > sortie.txt", "Opérateur de shell"),
        ("git log `whoami`", "Substitution"),
        ("   ", "vide"),
    ],
)
def test_commandes_refusees(sandbox, commande, extrait):
    with pytest.raises(ToolError) as info:
        run_command(sandbox, commande)

    assert extrait in str(info.value)


def test_commande_vide_liste(sandbox):
    with pytest.raises(ToolError):
        run_command(sandbox, [])


def test_split_command_liste():
    assert _split_command(["git", "status", "--short"]) == [
        "git",
        "status",
        "--short",
    ]


def test_liste_blanche():
    assert "git" in ALLOWED_COMMANDS
    assert "pytest" in ALLOWED_COMMANDS
    assert "python" in ALLOWED_COMMANDS
    assert "powershell" not in ALLOWED_COMMANDS
    assert "cmd" not in ALLOWED_COMMANDS


def test_schema_exige_une_chaine(registry):
    resultat = registry.execute("run_command", {"command": ["git", "status"]})

    assert resultat.ok is False
    assert "type string" in resultat.content


def test_sortie_tronquee(sandbox):
    resultat = run_command(
        sandbox, _python("print('x' * 5000)"), max_output=500
    )

    assert "sortie tronquée" in resultat
