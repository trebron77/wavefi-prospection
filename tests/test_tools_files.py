"""Tests des outils fichiers : lecture, ecriture et exploration."""

import pytest


def test_read_file_plage(registry):
    resultat = registry.execute(
        "read_file", {"path": "notes.md", "start_line": 2, "max_lines": 1}
    )

    assert resultat.ok
    assert resultat.content.startswith("ligne 2")
    assert "tronqué" in resultat.content


def test_read_file_complet(registry):
    resultat = registry.execute("read_file", {"path": "notes.md"})

    assert resultat.ok
    assert resultat.content.splitlines() == ["ligne 1", "ligne 2", "ligne 3"]


@pytest.mark.parametrize(
    "path, extrait",
    [
        ("absent.txt", "introuvable"),
        ("image.bin", "binaire"),
        (".env", "secrets"),
        ("src", "n'est pas un fichier"),
        ("../x.txt", "hors du dossier"),
    ],
)
def test_read_file_refus(registry, path, extrait):
    resultat = registry.execute("read_file", {"path": path})

    assert resultat.ok is False
    assert extrait in resultat.content


@pytest.mark.parametrize(
    "arguments, extrait",
    [
        ({"path": "notes.md", "start_line": 0}, "start_line"),
        ({"path": "notes.md", "start_line": 99}, "dépasse"),
        ({"path": "notes.md", "max_lines": 0}, "max_lines"),
    ],
)
def test_read_file_plage_invalide(registry, arguments, extrait):
    resultat = registry.execute("read_file", arguments)

    assert resultat.ok is False
    assert extrait in resultat.content


def test_write_file_creation_utf8_lf(registry, projet):
    resultat = registry.execute(
        "write_file", {"path": "src/pkg/nouveau.py", "content": "X = 1\nY = 2\n"}
    )

    assert resultat.ok
    assert "créé" in resultat.content
    ecrit = (projet / "src" / "pkg" / "nouveau.py").read_bytes()
    assert b"\r\n" not in ecrit
    assert ecrit == b"X = 1\nY = 2\n"


def test_write_file_remplacement(registry, projet):
    registry.execute("write_file", {"path": "src/pkg/f.py", "content": "A = 1\n"})

    resultat = registry.execute("write_file", {"path": "src/pkg/f.py", "content": "B = 2\n"})

    assert resultat.ok
    assert "remplacé" in resultat.content
    assert (projet / "src" / "pkg" / "f.py").read_text(encoding="utf-8") == "B = 2\n"


def test_write_file_sans_creation_de_dossier(registry):
    resultat = registry.execute(
        "write_file",
        {"path": "nouveau/dossier/f.py", "content": "x = 1\n", "create_dirs": False},
    )

    assert resultat.ok is False
    assert "Dossier parent introuvable" in resultat.content


@pytest.mark.parametrize(
    "path, extrait",
    [
        (".git/config", "protégé"),
        (".env.local", "secrets"),
        ("../hors.txt", "hors du dossier"),
        ("src", "est un dossier"),
    ],
)
def test_write_file_refus(registry, path, extrait):
    resultat = registry.execute("write_file", {"path": path, "content": "x"})

    assert resultat.ok is False
    assert extrait in resultat.content


def test_write_file_trop_volumineux(registry):
    resultat = registry.execute(
        "write_file", {"path": "gros.txt", "content": "a" * 600_000}
    )

    assert resultat.ok is False
    assert "trop volumineux" in resultat.content


def test_list_dir_profondeur(registry):
    resultat = registry.execute("list_dir", {"path": ".", "max_depth": 2})
    contenu = resultat.content

    assert resultat.ok
    assert "src/" in contenu
    assert "main.py" in contenu
    assert "  pkg/" in contenu
    assert ".git/ (ignoré)" in contenu
    assert ".git/config" not in contenu
    assert "node_modules/ (ignoré)" in contenu


def test_list_dir_profondeur_un(registry):
    contenu = registry.execute("list_dir", {"path": ".", "max_depth": 1}).content

    assert "  pkg/" not in contenu


def test_list_dir_sur_fichier(registry):
    resultat = registry.execute("list_dir", {"path": "notes.md"})

    assert resultat.ok
    assert "est un fichier" in resultat.content
    assert "read_file" in resultat.content


def test_list_dir_dossier_vide(registry, projet):
    (projet / "vide").mkdir()

    resultat = registry.execute("list_dir", {"path": "vide"})

    assert resultat.ok
    assert "(dossier vide)" in resultat.content


def test_list_dir_hors_projet(registry):
    resultat = registry.execute("list_dir", {"path": ".."})

    assert resultat.ok is False
    assert "hors du dossier" in resultat.content
