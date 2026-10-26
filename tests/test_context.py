"""Tests de la gestion du contexte : estimation, troncature et instantané.

Aucun réseau : on travaille sur des messages en mémoire et sur ``tmp_path``.
"""

import pytest

from src.context import (
    CONTEXT_NOTE_PREFIX,
    build_project_context,
    estimate_messages_tokens,
    estimate_tokens,
    trim_messages,
)


def echanges(nombre, taille=40):
    """Construit ``nombre`` échanges user/assistant de taille contrôlée."""
    messages = [{"role": "system", "content": "prompt système"}]
    for index in range(nombre):
        messages.append({"role": "user", "content": f"question {index} " + "x" * taille})
        messages.append(
            {"role": "assistant", "content": f"réponse {index} " + "y" * taille}
        )
    return messages


# --- estimation ------------------------------------------------------------


def test_estimation_tokens_vide_et_texte():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens(12345678) == 2


def test_estimation_messages_compte_contenus_et_appels():
    messages = [
        {"role": "system", "content": "abcd"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "abcd"},
    ]

    estimation = estimate_messages_tokens(messages)

    # 3 messages * 4 + contenus + schéma d'appel
    assert estimation >= 3 * 4 + 2
    assert estimation == estimate_messages_tokens(list(messages))


def test_estimation_message_sans_contenu():
    assert estimate_messages_tokens([{"role": "assistant", "content": None}]) == 4


# --- troncature ------------------------------------------------------------


def test_troncature_sans_budget_ne_change_rien():
    messages = echanges(3)

    retenus, omis = trim_messages(messages, None)
    assert retenus == messages
    assert omis == 0
    assert trim_messages(messages, 0) == (messages, 0)


def test_troncature_sous_budget_ne_change_rien():
    messages = echanges(2)

    retenus, omis = trim_messages(messages, max_tokens=10_000)

    assert retenus == messages
    assert omis == 0


def test_troncature_garde_systeme_et_dernier_echange():
    messages = echanges(6, taille=400)
    budget = estimate_messages_tokens(messages) // 3

    retenus, omis = trim_messages(messages, max_tokens=budget)

    assert omis > 0
    assert retenus[0] == messages[0]
    assert retenus[1]["role"] == "system"
    assert retenus[1]["content"].startswith(CONTEXT_NOTE_PREFIX)
    assert retenus[-2:] == messages[-2:]
    assert estimate_messages_tokens(retenus) <= budget + estimate_messages_tokens(
        retenus[-2:]
    )


def test_troncature_ne_separe_pas_appels_et_resultats():
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "ancienne " + "a" * 400},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "b" * 400},
        {"role": "assistant", "content": "fini"},
        {"role": "user", "content": "nouvelle " + "c" * 400},
        {"role": "assistant", "content": "réponse finale"},
    ]

    retenus, omis = trim_messages(messages, max_tokens=80)

    assert omis > 0
    assert [m["role"] for m in retenus] == ["system", "system", "user", "assistant"]
    assert retenus[-2]["content"].startswith("nouvelle")


def test_troncature_ne_duplique_pas_la_note():
    messages = echanges(4, taille=200)
    premier, omis1 = trim_messages(messages, max_tokens=120)
    assert omis1 > 0

    second, omis2 = trim_messages(premier, max_tokens=120)

    notes = [
        m
        for m in second
        if m["role"] == "system" and m["content"].startswith(CONTEXT_NOTE_PREFIX)
    ]
    assert len(notes) == 1
    assert omis2 == 0  # deja sous le budget


def test_troncature_garde_toujours_le_dernier_echange_trop_gros():
    messages = echanges(3, taille=400)
    retenus, omis = trim_messages(messages, max_tokens=10)

    assert omis > 0
    assert retenus[-2:] == messages[-2:]


def test_troncature_historique_vide():
    assert trim_messages([], max_tokens=100) == ([], 0)


# --- instantané du projet --------------------------------------------------


def test_instantane_liste_arborescence_et_ignore_secrets(projet):
    contexte = build_project_context(projet)

    assert "Instantané du projet" in contexte
    assert "src/" in contexte
    assert "main.py" in contexte
    assert "notes.md" in contexte
    # Secrets, dossiers techniques et fichiers caches sont exclus.
    assert ".env" not in contexte
    assert "node_modules" not in contexte
    assert ".git" not in contexte


def test_instantane_inclut_un_extrait_du_readme(projet):
    (projet / "README.md").write_text(
        "# Projet\n\nObjectif : tester la gestion du contexte.\n", encoding="utf-8"
    )

    contexte = build_project_context(projet, readme_chars=20)

    assert "README (extrait)" in contexte
    assert "# Projet" in contexte
    assert "tronqué" in contexte


def test_instantane_borne_l_arborescence(projet):
    for index in range(20):
        (projet / f"fichier_{index}.txt").write_text("x", encoding="utf-8")

    contexte = build_project_context(projet, max_entries=5, max_depth=1)

    assert "…" in contexte  # marqueur de troncature de l'arborescence
    assert "fichier_19.txt" not in contexte


def test_instantane_dossier_absent_ou_vide(tmp_path):
    vide = tmp_path / "vide"
    vide.mkdir()

    assert build_project_context(None) == ""
    assert build_project_context(tmp_path / "absent") == ""
    assert build_project_context(vide) == ""


def test_instantane_inclut_le_statut_git(projet, monkeypatch):
    monkeypatch.setattr(
        "src.context.read_git_status", lambda *_args, **_kwargs: " M src/main.py"
    )

    contexte = build_project_context(projet)

    assert "git status :" in contexte
    assert "M src/main.py" in contexte


