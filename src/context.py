"""Gestion du contexte : estimation de tokens, troncature et amorçage du projet.

Deux problèmes distincts sont traités ici :

* **fenêtre finie** : une longue session (REPL) accumule les échanges. Il faut
  tronquer l'historique sans casser les paires ``assistant(tool_calls)`` /
  ``tool``, et sans perdre le prompt système ni la dernière demande ;
* **démarrage à froid** : le modèle ignore l'état du projet. Un instantané borné
  (arborescence, extrait du README, ``git status``) lui évite des explorations
  coûteuses.

Utilisation :
    from context import build_project_context, trim_messages

    messages, omis = trim_messages(historique, max_tokens=6000)
    print(build_project_context("."))

L'estimation (≈ 4 caractères par token) est volontairement grossière : elle ne
sert qu'à décider d'une troncature, jamais à facturer quoi que ce soit.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

try:  # exécution en package : python -m src.agent
    from .tools.git import read_git_status
    from .tools.sandbox import EXCLUDED_DIRS
except ImportError:  # exécution directe : python src/agent.py
    from tools.git import read_git_status
    from tools.sandbox import EXCLUDED_DIRS

__all__ = [
    "CHARS_PER_TOKEN",
    "CONTEXT_NOTE_PREFIX",
    "MESSAGE_OVERHEAD",
    "build_project_context",
    "estimate_messages_tokens",
    "estimate_tokens",
    "trim_messages",
]

#: Nombre moyen de caractères par token (fr/en, tous modèles confondus).
CHARS_PER_TOKEN = 4

#: Coût forfaitaire d'un message (rôle, séparateurs, identifiant d'appel).
MESSAGE_OVERHEAD = 4

#: Préfixe des notes ajoutées par :func:`trim_messages`.
CONTEXT_NOTE_PREFIX = "[contexte]"

#: Bornes de l'instantané de projet injecté au démarrage.
TREE_MAX_ENTRIES = 60
TREE_MAX_DEPTH = 2
README_MAX_CHARS = 1200
GIT_STATUS_MAX_CHARS = 800

_TRUNCATION_MARKER = "\n… (tronqué)"


def estimate_tokens(texte: Any) -> int:
    """Estime le nombre de tokens d'un texte (arrondi supérieur).

    Args:
        texte: chaîne (ou objet converti en chaîne) ; ``None``/vide vaut 0.

    Returns:
        Une estimation grossière, toujours ``>= 0``.
    """
    if not texte:
        return 0
    longueur = len(texte if isinstance(texte, str) else str(texte))
    return max(1, (longueur + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def _texte_message(valeur: Any) -> str:
    """Rend un contenu de message sérialisable en texte pour l'estimation."""
    if isinstance(valeur, str):
        return valeur
    if not valeur:
        return ""
    try:
        return json.dumps(valeur, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - objets exotiques
        return str(valeur)


def estimate_messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estime le coût en tokens d'une conversation complète.

    Args:
        messages: messages au format OpenAI (``role``/``content``/``tool_calls``).

    Returns:
        Somme des contenus, des schémas d'appels et d'un coût fixe par message.
    """
    total = 0
    for message in messages:
        total += MESSAGE_OVERHEAD
        total += estimate_tokens(_texte_message(message.get("content")))
        appels = message.get("tool_calls")
        if appels:
            total += estimate_tokens(_texte_message(appels))
    return total


def _decouper(
    messages: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]]]:
    """Sépare le prompt système des échanges.

    Un échange = un message ``user`` suivi de la réponse de l'assistant et de
    ses résultats d'outils (jusqu'au message ``user`` suivant).

    Args:
        messages: conversation d'origine (non modifiée).

    Returns:
        ``(messages_systeme, echanges)``, en copies indépendantes.
    """
    systeme: List[Dict[str, Any]] = []
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        systeme.append(dict(messages[index]))
        index += 1

    echanges: List[List[Dict[str, Any]]] = []
    for message in messages[index:]:
        copie = dict(message)
        if message.get("role") == "user" or not echanges:
            echanges.append([copie])
        else:
            echanges[-1].append(copie)
    return systeme, echanges


def _est_note_de_troncature(message: Mapping[str, Any]) -> bool:
    """Vrai si le message est une note ``[contexte]`` posée par ce module."""
    if message.get("role") != "system":
        return False
    contenu = message.get("content")
    return isinstance(contenu, str) and contenu.startswith(CONTEXT_NOTE_PREFIX)


def trim_messages(
    messages: Sequence[Mapping[str, Any]],
    max_tokens: Optional[int],
    keep_last_exchanges: int = 1,
) -> Tuple[List[Dict[str, Any]], int]:
    """Tronque l'historique pour rester sous ``max_tokens`` estimés.

    Les messages système d'origine sont toujours conservés, ainsi que les
    ``keep_last_exchanges`` derniers échanges **entiers** : un message
    ``assistant`` porteur d'appels d'outils n'est jamais séparé de ses messages
    ``tool``. La coupe se fait donc par échange complet, du plus ancien au plus
    récent.

    Args:
        messages: conversation (historique de l'agent, par exemple).
        max_tokens: budget estimé ; ``None`` ou ``<= 0`` = aucune limite.
        keep_last_exchanges: nombre minimal d'échanges récents à préserver.

    Returns:
        ``(messages_retenus, nombre_de_messages_omis)``. Une note
        ``[contexte] …`` remplace les échanges omis pour que le modèle sache que
        quelque chose manque.
    """
    copies = [dict(message) for message in messages]
    if max_tokens is None or max_tokens <= 0:
        return copies, 0
    if estimate_messages_tokens(copies) <= max_tokens:
        return copies, 0

    systeme, echanges = _decouper(copies)
    # Une seule note à la fois : les précédentes sont remplacées.
    systeme = [message for message in systeme if not _est_note_de_troncature(message)]

    budget = max_tokens - estimate_messages_tokens(systeme)
    retenus: List[List[Dict[str, Any]]] = []
    for position, echange in enumerate(reversed(echanges)):
        cout = estimate_messages_tokens(echange)
        # Les derniers échanges restent toujours, même s'ils dépassent à eux
        # seuls le budget : mieux vaut un contexte trop gros qu'une demande
        # sans son propre message utilisateur.
        if position >= keep_last_exchanges and cout > budget:
            break
        retenus.insert(0, echange)
        budget -= cout

    omis = echanges[: len(echanges) - len(retenus)]
    nombre_omis = sum(len(echange) for echange in omis)
    if not omis:
        return copies, 0

    note = {
        "role": "system",
        "content": (
            f"{CONTEXT_NOTE_PREFIX} {len(omis)} échange(s) antérieur(s) omis "
            f"({nombre_omis} message(s)) pour respecter le budget de "
            f"{max_tokens} tokens estimés. Relisez un fichier avec read_file si "
            "un détail manque."
        ),
    }
    resultat = systeme + [note]
    for echange in retenus:
        resultat.extend(echange)
    return resultat, nombre_omis


def _arborescence(base: Path, max_entries: int, max_depth: int) -> str:
    """Arborescence compacte du projet, bornée en entrées et en profondeur.

    Les dossiers techniques (:data:`~tools.sandbox.EXCLUDED_DIRS`) et les
    fichiers cachés (dont ``.env``, qui contient la clé API) sont ignorés.
    """
    lignes: List[str] = []
    compte = 0

    def parcourir(dossier: Path, profondeur: int, prefixe: str) -> None:
        nonlocal compte
        try:
            enfants = sorted(
                (
                    enfant
                    for enfant in dossier.iterdir()
                    if enfant.name not in EXCLUDED_DIRS
                    and not enfant.name.startswith(".")
                ),
                key=lambda enfant: (enfant.is_file(), enfant.name.lower()),
            )
        except OSError:  # dossier illisible : on n'insiste pas
            return
        for enfant in enfants:
            if compte >= max_entries:
                lignes.append(prefixe + "…")
                return
            compte += 1
            if enfant.is_dir():
                lignes.append(prefixe + enfant.name + "/")
                if profondeur < max_depth:
                    parcourir(enfant, profondeur + 1, prefixe + "  ")
            else:
                lignes.append(prefixe + enfant.name)

    parcourir(base, 1, "")
    return "\n".join(lignes)


def _extrait_readme(base: Path, max_chars: int) -> str:
    """Début du README du projet (``""`` s'il est absent ou illisible)."""
    for nom in ("README.md", "readme.md", "README.txt", "README"):
        chemin = base / nom
        if not chemin.is_file():
            continue
        try:
            texte = chemin.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if not texte:
            return ""
        if len(texte) > max_chars:
            return texte[:max_chars].rstrip() + _TRUNCATION_MARKER
        return texte
    return ""


def build_project_context(
    root: Optional[Union[str, Path]] = None,
    max_entries: int = TREE_MAX_ENTRIES,
    max_depth: int = TREE_MAX_DEPTH,
    readme_chars: int = README_MAX_CHARS,
    git_chars: int = GIT_STATUS_MAX_CHARS,
) -> str:
    """Construit un instantané textuel du projet, injecté au démarrage.

    Args:
        root: racine du projet ; ``None`` ou dossier invalide renvoie ``""``.
        max_entries: nombre maximal de fichiers/dossiers listés.
        max_depth: profondeur maximale de l'arborescence.
        readme_chars: longueur maximale de l'extrait de README.
        git_chars: longueur maximale du ``git status``.

    Returns:
        Un texte prêt à être ajouté comme message système, ou ``""`` si rien
        n'a pu être collecté (dossier vide, droits insuffisants…).
    """
    if root is None:
        return ""
    base = Path(root)
    if not base.is_dir():
        return ""

    sections = [
        "## Instantané du projet (généré automatiquement au démarrage)\n"
        "Utilisez-le comme point de départ ; relisez un fichier avant de le "
        "modifier."
    ]
    arbre = _arborescence(base, max(1, int(max_entries)), max(1, int(max_depth)))
    if arbre:
        sections.append(f"Arborescence (profondeur {max_depth}) :\n{arbre}")
    readme = _extrait_readme(base, max(1, int(readme_chars)))
    if readme:
        sections.append(f"README (extrait) :\n{readme}")
    statut = read_git_status(base, max_chars=max(1, int(git_chars)))
    if statut:
        sections.append(f"git status :\n{statut}")
    if len(sections) == 1:  # rien de concret à montrer
        return ""
    return "\n\n".join(sections)


