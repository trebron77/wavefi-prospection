"""Étape 6 du tunnel : croisement avec les scans Wi-Fi de NetAnalyzer.

Le script lit ``campagne_wifi_final.csv`` (sortie de ``enricher_hunter.py``) et
``donnees_wifi_netanalyzer.csv`` (relevés de scan Wi-Fi : fabricant du routeur
et sécurité du réseau), puis ajoute deux colonnes au fichier principal :

* ``fabricant_routeur`` : constructeur(s) détecté(s) par NetAnalyzer ;
* ``securite_wifi`` : chiffrement observé (``WPA2``, ``WPA3``, ouvert...).

Le résultat est écrit dans ``campagne_wifi_ultime.csv``.

Jointure « intelligente »
-------------------------
Les deux fichiers ne nomment pas toujours l'établissement de la même façon :
``Hôtel Kon Tiki`` d'un côté, ``Boutique Hotel Kon Tiki Tahiti`` ou
``Kon tiki papeete`` de l'autre. La correspondance s'appuie sur une
normalisation (accents, casse, ponctuation) puis sur quatre niveaux de
comparaison, du plus sûr au plus permissif :

1. égalité des noms normalisés (``Hotel Reva`` = ``Hotel Reva``) ;
2. égalité des mots significatifs, après retrait des mots vides
   (``hôtel``, ``restaurant``, ``le``, ``sas``...) ;
3. inclusion : tous les mots du nom le plus court sont présents dans l'autre
   (``Kon Tiki`` ⊂ ``Kon Tiki Papeete``) ;
4. ressemblance orthographique (``difflib``) pour absorber les fautes de
   frappe et les variantes d'écriture.

Deux garde-fous évitent les rapprochements abusifs : une ville différente
suffit à annuler la ressemblance (``Carrefour Punaauia`` ne rejoint pas
``Carrefour Arue``), et une égalité parfaite entre deux fiches de scan est
jugée ambiguë : le lead reste non enrichi et un avertissement est journalisé
plutôt que d'attribuer le mauvais matériel.

Utilisation
-----------
    python enricher_netanalyzer.py                # jointure complète
    python enricher_netanalyzer.py --dry-run      # simulation, sans écriture
    python enricher_netanalyzer.py --min-score 90  # jointures strictes

Aucune connexion réseau n'est effectuée : seuls des fichiers locaux sont lus.
Les fonctions restent testables hors ligne (logger injectable).
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from config import (
    FINAL_CSV,
    NETANALYZER_CSV,
    NETWORK_COLUMNS,
    ULTIMATE_CSV,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: Colonnes lues dans ``donnees_wifi_netanalyzer.csv``.
NETANALYZER_NAME_COLUMN = "nom_etablissement"
NETANALYZER_ROUTER_COLUMN = "fabricant_routeur"
NETANALYZER_SECURITY_COLUMN = "securite"

#: Colonne du CSV de campagne portant le nom de l'entreprise.
CAMPAIGN_NAME_COLUMN = "nom"

#: Colonnes ajoutées au fichier principal (ordre garanti par ``config.py``).
ROUTER_COLUMN = NETWORK_COLUMNS[0]
SECURITY_COLUMN = NETWORK_COLUMNS[1]

#: Mots vides retirés des noms avant comparaison : formes juridiques, types
#: d'établissement et articles. Ils n'identifient pas l'entreprise et
#: empêcheraient ``Hôtel Kon Tiki`` de rejoindre ``Kon Tiki Papeete``.
NAME_STOPWORDS: FrozenSet[str] = frozenset(
    {
        "auberge",
        "bar",
        "bistro",
        "bistrot",
        "boutique",
        "brasserie",
        "brasseries",
        "cafe",
        "cafes",
        "chez",
        "d",
        "de",
        "des",
        "du",
        "et",
        "eurl",
        "gie",
        "hotel",
        "hotels",
        "l",
        "la",
        "le",
        "les",
        "lodge",
        "lounge",
        "pension",
        "pizzeria",
        "restaurant",
        "restaurants",
        "residence",
        "sa",
        "sarl",
        "sas",
        "sci",
        "snack",
        "snc",
        "spa",
        "the",
    }
)

#: Note maximale : noms identiques après normalisation.
EXACT_SCORE = 100

#: Note d'une égalité des mots significatifs, ordre indifférent.
TOKENS_SCORE = 90

#: Note de base d'une inclusion complète (``Kon Tiki`` ⊂ ``Kon Tiki Tahiti``).
CONTAINMENT_SCORE = 80

#: Note de base d'une ressemblance orthographique (``difflib``).
FUZZY_SCORE = 70

#: Ratio minimal de ``difflib`` pour accepter une variante orthographique.
FUZZY_MIN_RATIO = 0.88

#: Note minimale pour retenir une correspondance.
MIN_MATCH_SCORE = 60


# ---------------------------------------------------------------------------
# Normalisation des noms et calcul de ressemblance
# ---------------------------------------------------------------------------
def _text(value: object) -> str:
    """Convertit une valeur CSV en texte nettoyé (jamais ``None``)."""
    if value is None:
        return ""
    return str(value).strip()


def _normalize_name(value: str) -> str:
    """Normalise un nom d'entreprise pour la jointure entre CSV.

    La règle est identique à celle de ``enricher.py`` et de
    ``enricher_hunter.py`` : accents, casse et ponctuation sont retirés afin
    de rapprocher ``Hôtel Tahiti Nui`` de ``Hotel Tahiti Nui``.
    """
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def name_tokens(value: str) -> FrozenSet[str]:
    """Renvoie les mots significatifs d'un nom d'entreprise.

    Les mots vides (``hôtel``, ``restaurant``, ``sas``...) et les lettres
    isolées sont ignorés : ``Hôtel Kon Tiki`` et ``Boutique Hotel Kon Tiki
    Tahiti`` partagent ainsi les mots ``kon`` et ``tiki``.
    """
    return frozenset(
        token
        for token in _normalize_name(value).split()
        if len(token) > 1 and token not in NAME_STOPWORDS
    )


def match_score(lead_name: str, candidate_name: str) -> int:
    """Note la ressemblance entre un lead et une fiche de scan Wi-Fi.

    Args:
        lead_name: Nom de l'entreprise dans la campagne.
        candidate_name: Nom de l'établissement scanné.

    Returns:
        Une note de 0 (aucune ressemblance) à ``EXACT_SCORE`` (noms identiques
        après normalisation).
    """
    lead_key = _normalize_name(lead_name)
    candidate_key = _normalize_name(candidate_name)
    if not lead_key or not candidate_key:
        return 0
    if lead_key == candidate_key:
        return EXACT_SCORE

    lead_tokens = name_tokens(lead_name)
    candidate_tokens = name_tokens(candidate_name)
    if lead_tokens and lead_tokens == candidate_tokens:
        return TOKENS_SCORE

    common = lead_tokens & candidate_tokens
    shortest = min(len(lead_tokens), len(candidate_tokens))
    if common and len(common) == shortest:
        # Tous les mots du nom le plus court figurent dans l'autre : une
        # ville supplémentaire (« Tahiti », « Papeete ») ne bloque pas.
        return CONTAINMENT_SCORE + min(len(common), 5)

    ratio = difflib.SequenceMatcher(None, lead_key, candidate_key).ratio()
    if ratio >= FUZZY_MIN_RATIO:
        return int(round(ratio * FUZZY_SCORE))
    return 0


def best_candidates(
    lead_name: str,
    entries: Sequence[Dict[str, str]],
    *,
    min_score: int = MIN_MATCH_SCORE,
) -> List[Tuple[int, Dict[str, str]]]:
    """Renvoie les fiches de scan de meilleure note pour un lead.

    Args:
        lead_name: Nom de l'entreprise dans la campagne.
        entries: Fiches de ``donnees_wifi_netanalyzer.csv``.
        min_score: Note minimale pour qu'une fiche soit retenue.

    Returns:
        Les ``(note, fiche)`` ex æquo en tête ; liste vide si aucune fiche
        n'atteint ``min_score``. Plusieurs éléments signalent donc une
        correspondance ambiguë.
    """
    scored = [
        (match_score(lead_name, entry[NETANALYZER_NAME_COLUMN]), entry)
        for entry in entries
    ]
    if not scored:
        return []
    best = max(score for score, _ in scored)
    if best < min_score:
        return []
    return [item for item in scored if item[0] == best]


# ---------------------------------------------------------------------------
# Lecture des scans Wi-Fi
# ---------------------------------------------------------------------------
def read_netanalyzer_rows(path: Path) -> List[Dict[str, str]]:
    """Lit les scans Wi-Fi et ne garde que les fiches exploitables.

    Args:
        path: CSV ``donnees_wifi_netanalyzer.csv``.

    Returns:
        Les fiches dont le nom d'établissement est renseigné ; les doublons de
        nom normalisé sont écartés (première fiche conservée).

    Raises:
        RuntimeError: Si le fichier est absent, vide ou dépourvu de la colonne
            ``nom_etablissement``.
    """
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(
            f"Fichier de scans Wi-Fi introuvable : {path}. Exportez les "
            f"résultats NetAnalyzer à cet emplacement."
        )
    rows = read_csv_rows(path)
    if not rows:
        raise RuntimeError(f"Aucun scan Wi-Fi exploitable dans {path}.")
    if NETANALYZER_NAME_COLUMN not in rows[0]:
        found = ", ".join(rows[0]) or "aucune"
        raise RuntimeError(
            f"Colonne {NETANALYZER_NAME_COLUMN} absente de {path} "
            f"(colonnes trouvées : {found})."
        )

    entries: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for row in rows:
        name = _text(row.get(NETANALYZER_NAME_COLUMN))
        key = _normalize_name(name)
        if not name or key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                NETANALYZER_NAME_COLUMN: name,
                NETANALYZER_ROUTER_COLUMN: _text(
                    row.get(NETANALYZER_ROUTER_COLUMN)
                ),
                NETANALYZER_SECURITY_COLUMN: _text(
                    row.get(NETANALYZER_SECURITY_COLUMN)
                ),
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Jointure et écriture du fichier final
# ---------------------------------------------------------------------------
def build_output_columns(rows: Sequence[Dict[str, str]]) -> List[str]:
    """Construit l'en-tête du CSV final à partir du fichier d'entrée.

    Les colonnes de la campagne sont conservées dans le même ordre ;
    ``fabricant_routeur`` et ``securite_wifi`` sont ajoutées en fin de ligne
    (sans doublon si l'entrée les contient déjà).

    Args:
        rows: Lignes du CSV d'entrée.

    Returns:
        La liste des colonnes à écrire, dans l'ordre.
    """
    columns = list(rows[0].keys()) if rows else []
    for column in NETWORK_COLUMNS:
        if column not in columns:
            columns.append(column)
    return columns


def enrich_with_netanalyzer(
    input_path: Path = FINAL_CSV,
    *,
    netanalyzer_path: Path = NETANALYZER_CSV,
    output_path: Path = ULTIMATE_CSV,
    min_score: int = MIN_MATCH_SCORE,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, str]]:
    """Ajoute les données de scan Wi-Fi à chaque ligne de la campagne.

    Args:
        input_path: CSV de campagne (``campagne_wifi_final.csv``).
        netanalyzer_path: CSV des scans Wi-Fi.
        output_path: CSV enrichi à écrire (``campagne_wifi_ultime.csv``).
        min_score: Note minimale de jointure (1 à ``EXACT_SCORE``).
        dry_run: Si ``True``, aucune écriture n'est effectuée.
        logger: Logger à utiliser (``None`` = logger du tunnel).

    Returns:
        Les lignes enrichies ; les colonnes Wi-Fi restent vides quand aucune
        fiche de scan n'a été attribuée.

    Raises:
        ValueError: Si ``min_score`` est hors des bornes autorisées.
        RuntimeError: Si la campagne est absente ou vide, ou si le fichier de
            scans Wi-Fi est inexploitable.
    """
    if not 0 < min_score <= EXACT_SCORE:
        raise ValueError(
            f"min_score doit être compris entre 1 et {EXACT_SCORE}."
        )

    log = logger or setup_logging()
    input_path = Path(input_path)
    netanalyzer_path = Path(netanalyzer_path)
    rows = read_csv_rows(input_path)
    if not rows:
        raise RuntimeError(
            f"Aucun lead à enrichir : {input_path} est vide ou introuvable."
        )
    entries = read_netanalyzer_rows(netanalyzer_path)
    log.info(
        "%d lead(s) dans %s, %d fiche(s) de scan dans %s",
        len(rows),
        input_path.name,
        len(entries),
        netanalyzer_path.name,
    )

    columns = build_output_columns(rows)
    enriched: List[Dict[str, str]] = []
    matched = 0
    ambiguous_count = 0
    used: Set[str] = set()

    for index, row in enumerate(rows, 1):
        output = dict(row)
        for column in NETWORK_COLUMNS:
            output.setdefault(column, "")
        display = _text(row.get(CAMPAIGN_NAME_COLUMN)) or f"ligne {index}"
        candidates = best_candidates(display, entries, min_score=min_score)

        if len(candidates) == 1:
            score, entry = candidates[0]
            output[ROUTER_COLUMN] = entry[NETANALYZER_ROUTER_COLUMN]
            output[SECURITY_COLUMN] = entry[NETANALYZER_SECURITY_COLUMN]
            used.add(_normalize_name(entry[NETANALYZER_NAME_COLUMN]))
            matched += 1
            log.info(
                "[%d/%d] %s -> %s (note %d) : %s | %s",
                index,
                len(rows),
                display,
                entry[NETANALYZER_NAME_COLUMN],
                score,
                output[ROUTER_COLUMN] or "fabricant inconnu",
                output[SECURITY_COLUMN] or "sécurité inconnue",
            )
        elif candidates:
            ambiguous_count += 1
            log.warning(
                "[%d/%d] %s : %d fiche(s) de scan ex æquo (%s), aucune "
                "attribution",
                index,
                len(rows),
                display,
                len(candidates),
                " / ".join(
                    entry[NETANALYZER_NAME_COLUMN] for _, entry in candidates
                ),
            )
        else:
            log.debug(
                "[%d/%d] %s : aucun scan Wi-Fi correspondant",
                index,
                len(rows),
                display,
            )
        enriched.append(output)

    unused = [
        entry[NETANALYZER_NAME_COLUMN]
        for entry in entries
        if _normalize_name(entry[NETANALYZER_NAME_COLUMN]) not in used
    ]
    log.info(
        "Bilan NetAnalyzer : %d lead(s) enrichi(s) sur %d ; %d "
        "correspondance(s) ambiguë(s) ignorée(s) ; %d fiche(s) de scan non "
        "utilisée(s).",
        matched,
        len(enriched),
        ambiguous_count,
        len(unused),
    )
    if unused:
        log.debug("Fiches de scan sans lead associé : %s", ", ".join(unused))

    if dry_run:
        log.info(
            "Mode --dry-run : aucune écriture, %s inchangé.", output_path
        )
        return enriched

    written = write_csv_atomic(Path(output_path), enriched, columns)
    log.info(
        "Campagne ultime écrite dans %s (%d ligne(s), %d enrichie(s))",
        Path(output_path),
        written,
        matched,
    )
    return enriched


# ---------------------------------------------------------------------------
# Interface en ligne de commande
# ---------------------------------------------------------------------------
def _configure_console_encoding() -> None:
    """Évite les erreurs d'encodage des consoles Windows héritées."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments de ``enricher_netanalyzer.py``.

    Returns:
        Un analyseur prêt à lire ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Croise campagne_wifi_final.csv avec les scans Wi-Fi de "
            "donnees_wifi_netanalyzer.csv et écrit campagne_wifi_ultime.csv."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=FINAL_CSV,
        help=(
            "CSV de campagne à enrichir (défaut : campagne_wifi_final.csv)."
        ),
    )
    parser.add_argument(
        "--netanalyzer",
        type=Path,
        default=NETANALYZER_CSV,
        help="CSV des scans Wi-Fi (défaut : donnees_wifi_netanalyzer.csv).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ULTIMATE_CSV,
        help="CSV enrichi de sortie (défaut : campagne_wifi_ultime.csv).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=MIN_MATCH_SCORE,
        help=(
            f"Note minimale de jointure, de 1 à {EXACT_SCORE} "
            f"(défaut : {MIN_MATCH_SCORE})."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les correspondances sans écrire de fichier.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``enricher_netanalyzer.py``.

    Args:
        argv: Arguments ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si le CSV a été produit, 1 en cas d'erreur bloquante.
    """
    _configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not 0 < args.min_score <= EXACT_SCORE:
        parser.error(
            f"--min-score doit être compris entre 1 et {EXACT_SCORE}."
        )

    try:
        rows = enrich_with_netanalyzer(
            args.input,
            netanalyzer_path=args.netanalyzer,
            output_path=args.output,
            min_score=args.min_score,
            dry_run=args.dry_run,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interruption demandée : aucun fichier incomplet n'a été écrit.",
            file=sys.stderr,
        )
        return 130

    joined = [
        row
        for row in rows
        if _text(row.get(ROUTER_COLUMN)) or _text(row.get(SECURITY_COLUMN))
    ]
    if args.dry_run:
        print(
            f"{len(rows)} lead(s) analysé(s) -> {len(joined)} enrichi(s) "
            f"avec les données Wi-Fi (dry-run, aucune écriture)."
        )
        return 0

    print(
        f"{len(rows)} lead(s) dans {args.output} "
        f"({len(joined)} enrichi(s) avec les données Wi-Fi) :"
    )
    for row in joined:
        print(
            f"  - {row.get(CAMPAIGN_NAME_COLUMN) or 'n/a'} : "
            f"{_text(row.get(ROUTER_COLUMN)) or 'fabricant inconnu'} | "
            f"{_text(row.get(SECURITY_COLUMN)) or 'sécurité inconnue'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

