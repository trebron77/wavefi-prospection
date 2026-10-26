"""Tests de la recherche plein texte (regex) dans le projet."""

import pytest


def test_recherche_trouve(registry):
    resultat = registry.execute("search_code", {"pattern": "VALUE", "glob": "*.py"})

    assert resultat.ok
    assert "src/pkg/util.py:1" in resultat.content
    assert "notes.md" not in resultat.content


def test_recherche_insensible_a_la_casse(registry):
    resultat = registry.execute("search_code", {"pattern": "vAlUe"})

    assert resultat.ok
    assert "util.py" in resultat.content


def test_recherche_sensible_a_la_casse(registry):
    resultat = registry.execute(
        "search_code", {"pattern": "vAlUe", "case_sensitive": True}
    )

    assert resultat.ok
    assert "Aucune correspondance" in resultat.content


def test_recherche_ignore_les_secrets(registry):
    resultat = registry.execute("search_code", {"pattern": "LLM_API_KEY"})

    assert resultat.ok
    assert "sk-secret" not in resultat.content


def test_recherche_ignore_les_binaires(registry):
    resultat = registry.execute("search_code", {"pattern": "SECRET"})

    assert resultat.ok
    assert "image.bin" not in resultat.content


def test_regex_invalide(registry):
    resultat = registry.execute("search_code", {"pattern": "("})

    assert resultat.ok is False
    assert "invalide" in resultat.content


def test_sans_correspondance(registry):
    resultat = registry.execute("search_code", {"pattern": "zzz-introuvable-zzz"})

    assert resultat.ok
    assert "Aucune correspondance" in resultat.content


def test_motif_vide(registry):
    resultat = registry.execute("search_code", {"pattern": "   "})

    assert resultat.ok is False
    assert "vide" in resultat.content


def test_chemin_hors_projet(registry):
    resultat = registry.execute("search_code", {"pattern": "x", "path": ".."})

    assert resultat.ok is False
    assert "hors du dossier" in resultat.content


def test_limite_de_resultats(registry):
    resultat = registry.execute("search_code", {"pattern": "ligne", "max_results": 1})

    assert resultat.ok
    assert "limite de 1 résultats" in resultat.content


def test_recherche_dans_un_fichier(registry):
    resultat = registry.execute(
        "search_code", {"pattern": "ligne 3", "path": "notes.md"}
    )

    assert resultat.ok
    assert "notes.md:3:" in resultat.content


@pytest.mark.parametrize("max_results", [0, -3])
def test_max_results_borne(registry, max_results):
    resultat = registry.execute(
        "search_code", {"pattern": "ligne", "max_results": max_results}
    )

    assert resultat.ok
    assert "limite de 1 résultats" in resultat.content
