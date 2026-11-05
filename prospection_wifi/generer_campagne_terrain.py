"""Étape 9 du tunnel : liste de visite physique des établissements.

La campagne email a démarché les leads de ``campagne_wifi_master.csv``. En
parallèle, des relevés Wi-Fi ont été faits sur le terrain
(``donnees_wifi_netanalyzer.csv``). Deux gisements restent inexploités :

1. les établissements scannés qui **ne figurent pas** dans la campagne — un
   Carrefour, l'aéroport, le Royal Tahitien, le CHPF... On connaît déjà leur
   équipement Wi-Fi, mais ils n'ont jamais reçu d'email : ce sont des cibles de
   visite idéales, le diagnostic étant déjà à moitié fait ;
2. les leads de la campagne **déjà scannés** (``fabricant_routeur`` renseigné)
   mais **sans ``email_contact``** : injoignables par email, donc à aller voir
   sur place.

Le script fusionne ces deux listes dans ``campagne_terrain.csv`` avec quatre
colonnes : ``nom_etablissement``, ``fabricant_routeur``, ``securite_wifi`` et
``adresse``. Aucun des deux fichiers actuels ne comporte d'adresse : la colonne
reste vide, mais elle se remplit automatiquement dès qu'un export futur en
apporte une (``adresse``, ``Adresse``, ``address``...).

Jointure
--------
La comparaison des noms réutilise :func:`enricher_netanalyzer.match_score`, la
même fonction que celle qui a servi à construire le fichier maître. « Non
trouvé » veut donc dire exactement la même chose ici et lors de l'enrichissement
Wi-Fi : ``Hotel Reva`` rejoint ``Hôtel Reva Tahiti``, ``Carrefour Punaauia`` ne
rejoint pas ``Carrefour Arue``.

Utilisation
-----------
    python generer_campagne_terrain.py              # écrit campagne_terrain.csv
    python generer_campagne_terrain.py --dry-run    # bilan, sans écriture
    python generer_campagne_terrain.py --min-score 90   # jointure stricte
    python generer_campagne_terrain.py -n scans.csv -m master.csv -s terrain.csv

Aucune connexion réseau n'est effectuée : seuls des fichiers locaux sont lus,
ce qui rend le script testable hors ligne.
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from config import (
    ENRICHED_COLUMNS,
    MASTER_CSV,
    NETANALYZER_CSV,
    TERRAIN_CSV,
    read_csv_rows,
    write_csv_atomic,
)
from enricher_netanalyzer import (
    CAMPAIGN_NAME_COLUMN,
    EXACT_SCORE,
    MIN_MATCH_SCORE,
    NETANALYZER_NAME_COLUMN,
    NETANALYZER_ROUTER_COLUMN,
    NETANALYZER_SECURITY_COLUMN,
    ROUTER_COLUMN,
    SECURITY_COLUMN,
    match_score,
)

try:  # rich est une dépendance déclarée ; on reste tolérant pour les tests.
    from rich.console import Console
    from rich.table import Table
except ModuleNotFoundError:  # pragma: no cover - dépend de l'environnement
    Console = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Colonnes de la liste de visite
# ---------------------------------------------------------------------------
#: Colonnes écrites dans ``campagne_terrain.csv``, dans cet ordre.
COLONNES_TERRAIN: List[str] = [
    "nom_etablissement",
    "fabricant_routeur",
    "securite_wifi",
    "adresse",
]

#: Colonne d'email de contact de la campagne (``config.ENRICHED_COLUMNS``).
EMAIL_CONTACT_COLUMN: str = ENRICHED_COLUMNS[-1]

#: Noms acceptés pour la colonne d'adresse, par ordre de préférence.
#:
#: Aucun fichier du tunnel ne la produit aujourd'hui : la recherche est donc
#: tolérante (casse, accents, tirets) pour accueillir un export enrichi.
COLONNES_ADRESSE: Tuple[str, ...] = (
    "adresse",
    "adresse_postale",
    "adresse_etablissement",
    "address",
)

#: Origines possibles d'une cible, telles qu'affichées dans le bilan.
ORIGINE_SCAN: str = "Scan non utilisé"
ORIGINE_SANS_EMAIL: str = "Scanné sans email"



# ---------------------------------------------------------------------------
# Utilitaires de lecture
# ---------------------------------------------------------------------------
def _texte(valeur: object) -> str:
    """Convertit une valeur CSV en texte nettoyé (jamais ``None``)."""
    if valeur is None:
        return ""
    return str(valeur).strip()


def _cle_colonne(nom: str) -> str:
    """Normalise un nom de colonne pour une recherche tolérante.

    Args:
        nom: nom de colonne tel qu'il apparaît dans le CSV.

    Returns:
        Le nom en minuscules, sans accent ni séparateur (``Adresse_postale``
        devient ``adresse postale``).
    """
    decompose = unicodedata.normalize("NFKD", nom or "")
    sans_accents = "".join(
        caractere
        for caractere in decompose
        if not unicodedata.combining(caractere)
    )
    return " ".join(sans_accents.casefold().replace("_", " ").split())


def _valeur_colonne(ligne: Dict[str, str], candidats: Sequence[str]) -> str:
    """Lit la première colonne renseignée parmi ``candidats``.

    Args:
        ligne: une ligne du CSV (colonne → valeur).
        candidats: noms de colonnes recherchés, par ordre de préférence.

    Returns:
        La valeur trouvée, ou une chaîne vide si aucune colonne candidate
        n'existe ou si elles sont toutes vides.
    """
    index = {_cle_colonne(cle): cle for cle in ligne}
    for candidat in candidats:
        cle = index.get(_cle_colonne(candidat))
        if cle is None:
            continue
        valeur = _texte(ligne.get(cle))
        if valeur:
            return valeur
    return ""


def lire_scans(chemin: Path) -> List[Dict[str, str]]:
    """Lit les relevés Wi-Fi de NetAnalyzer.

    Les fiches sans nom et les doublons (même nom une fois normalisé) sont
    écartés : une seule visite est planifiée par établissement. Les lignes
    sont renvoyées entières pour garder une éventuelle colonne d'adresse.

    Args:
        chemin: CSV ``donnees_wifi_netanalyzer.csv``.

    Returns:
        Les fiches exploitables, dans l'ordre du fichier.

    Raises:
        RuntimeError: fichier absent, vide, ou colonne ``nom_etablissement``
            introuvable.
    """
    if not chemin.is_file():
        raise RuntimeError(
            f"Fichier de scans Wi-Fi introuvable : {chemin}. Exportez les "
            "résultats NetAnalyzer à cet emplacement ou utilisez "
            "--netanalyzer."
        )
    lignes = read_csv_rows(chemin)
    if not lignes:
        raise RuntimeError(f"Aucun scan Wi-Fi exploitable dans {chemin}.")
    if NETANALYZER_NAME_COLUMN not in lignes[0]:
        trouvees = ", ".join(f"« {nom} »" for nom in lignes[0]) or "aucune"
        raise RuntimeError(
            f"Colonne {NETANALYZER_NAME_COLUMN} absente de {chemin} "
            f"(colonnes trouvées : {trouvees})."
        )

    scans: List[Dict[str, str]] = []
    for ligne in lignes:
        nom = _texte(ligne.get(NETANALYZER_NAME_COLUMN))
        if not nom:
            continue
        deja_vu = any(
            match_score(nom, _texte(scan.get(NETANALYZER_NAME_COLUMN)))
            == EXACT_SCORE
            for scan in scans
        )
        if deja_vu:
            continue
        scans.append(ligne)
    return scans


def lire_campagne(chemin: Path) -> List[Dict[str, str]]:
    """Lit la campagne maître et ne garde que les leads nommés.

    Args:
        chemin: CSV ``campagne_wifi_master.csv``.

    Returns:
        Les leads renseignés, dans l'ordre du fichier. La ligne vide laissée en
        fin d'export par ``emailer.py`` est ignorée.

    Raises:
        RuntimeError: fichier absent, vide, ou colonnes indispensables
            absentes (``nom``, ``fabricant_routeur``, ``email_contact``).
    """
    if not chemin.is_file():
        raise RuntimeError(
            f"Campagne introuvable : {chemin}. Lancez le tunnel email ou "
            "utilisez --master."
        )
    lignes = read_csv_rows(chemin)
    if not lignes:
        raise RuntimeError(f"Aucun lead exploitable dans {chemin}.")

    requises = (CAMPAIGN_NAME_COLUMN, ROUTER_COLUMN, EMAIL_CONTACT_COLUMN)
    manquantes = [colonne for colonne in requises if colonne not in lignes[0]]
    if manquantes:
        attendues = ", ".join(f"« {nom} »" for nom in manquantes)
        trouvees = ", ".join(f"« {nom} »" for nom in lignes[0]) or "aucune"
        raise RuntimeError(
            f"Colonne(s) {attendues} absente(s) de {chemin} "
            f"(colonnes trouvées : {trouvees})."
        )
    return [
        ligne for ligne in lignes if _texte(ligne.get(CAMPAIGN_NAME_COLUMN))
    ]


# ---------------------------------------------------------------------------
# Sélection des cibles
# ---------------------------------------------------------------------------
def trouver_dans_campagne(
    nom: str,
    leads: Sequence[Dict[str, str]],
    *,
    min_score: int = MIN_MATCH_SCORE,
) -> List[str]:
    """Cherche les leads de la campagne correspondant à un nom de scan.

    Args:
        nom: nom de l'établissement tel que relevé par NetAnalyzer.
        leads: lignes de la campagne maître.
        min_score: note minimale de ressemblance (``match_score``).

    Returns:
        Les noms des leads ex æquo au meilleur score ; liste vide si aucun
        n'atteint ``min_score``. Plusieurs noms signalent une correspondance
        ambiguë : l'établissement reste considéré comme déjà présent.
    """
    notes: List[Tuple[int, str]] = []
    for lead in leads:
        nom_lead = _texte(lead.get(CAMPAIGN_NAME_COLUMN))
        if not nom_lead:
            continue
        notes.append((match_score(nom, nom_lead), nom_lead))
    if not notes:
        return []
    meilleure = max(note for note, _ in notes)
    if meilleure < min_score:
        return []

    trouves: List[str] = []
    for note, nom_lead in notes:
        if note == meilleure and nom_lead not in trouves:
            trouves.append(nom_lead)
    return trouves


def _cible(
    nom: str,
    fabricant: str,
    securite: str,
    adresse: str,
    origine: str,
) -> Dict[str, str]:
    """Construit une cible de visite (colonnes du CSV + origine interne).

    Args:
        nom: nom de l'établissement.
        fabricant: constructeur du routeur relevé au scan.
        securite: type de sécurité Wi-Fi relevé.
        adresse: adresse postale, vide si inconnue.
        origine: origine de la cible (:data:`ORIGINE_SCAN` ou
            :data:`ORIGINE_SANS_EMAIL`) ; jamais écrite dans le CSV.

    Returns:
        La ligne prête à être affichée puis écrite.
    """
    return {
        "nom_etablissement": nom,
        "fabricant_routeur": fabricant,
        "securite_wifi": securite,
        "adresse": adresse,
        "origine": origine,
    }


def cibles_depuis_scans(
    scans: Sequence[Dict[str, str]],
    leads: Sequence[Dict[str, str]],
    *,
    min_score: int = MIN_MATCH_SCORE,
) -> Tuple[List[Dict[str, str]], List[Tuple[str, List[str]]]]:
    """Isole les établissements scannés absents de la campagne.

    Args:
        scans: fiches de ``donnees_wifi_netanalyzer.csv``.
        leads: lignes de la campagne maître.
        min_score: note minimale de jointure.

    Returns:
        Un couple ``(cibles, rattaches)`` : les scans orphelins, dans l'ordre du
        fichier de scans, et les couples ``(nom du scan, leads correspondants)``
        déjà couverts par la campagne.
    """
    cibles: List[Dict[str, str]] = []
    rattaches: List[Tuple[str, List[str]]] = []
    for scan in scans:
        nom = _texte(scan.get(NETANALYZER_NAME_COLUMN))
        if not nom:
            continue
        trouves = trouver_dans_campagne(nom, leads, min_score=min_score)
        if trouves:
            rattaches.append((nom, trouves))
            continue
        cibles.append(
            _cible(
                nom,
                _texte(scan.get(NETANALYZER_ROUTER_COLUMN)),
                _texte(scan.get(NETANALYZER_SECURITY_COLUMN)),
                _valeur_colonne(scan, COLONNES_ADRESSE),
                ORIGINE_SCAN,
            )
        )
    return cibles, rattaches


def cibles_depuis_leads(
    leads: Sequence[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Isole les leads scannés mais sans email de contact.

    Un lead est retenu quand ``fabricant_routeur`` est renseigné (donc scanné)
    et ``email_contact`` vide : le tunnel email n'a aucun moyen de le joindre,
    seule une visite physique permet de l'approcher.

    Args:
        leads: lignes de la campagne maître.

    Returns:
        Les cibles correspondantes, dans l'ordre de la campagne.
    """
    cibles: List[Dict[str, str]] = []
    for lead in leads:
        nom = _texte(lead.get(CAMPAIGN_NAME_COLUMN))
        fabricant = _texte(lead.get(ROUTER_COLUMN))
        if not nom or not fabricant:
            continue
        if _texte(lead.get(EMAIL_CONTACT_COLUMN)):
            continue
        cibles.append(
            _cible(
                nom,
                fabricant,
                _texte(lead.get(SECURITY_COLUMN)),
                _valeur_colonne(lead, COLONNES_ADRESSE),
                ORIGINE_SANS_EMAIL,
            )
        )
    return cibles


@dataclass(frozen=True)
class Rapport:
    """Bilan de la liste de visite.

    Attributes:
        scans_lus: nombre de fiches de scan lues (doublons écartés).
        scans_rattaches: couples ``(nom du scan, leads correspondants)`` déjà
            présents dans la campagne.
        cibles_scans: cibles issues des scans non utilisés.
        leads_scannes: nombre de leads de la campagne portant un fabricant de
            routeur (donc déjà scannés).
        cibles_leads: cibles issues des leads scannés sans email de contact.
        doublons: cibles écartées parce que l'établissement était déjà retenu
            par l'autre liste.
        cibles: liste finale — scans d'abord, puis leads.
        min_score: note minimale de jointure appliquée.
    """

    scans_lus: int
    scans_rattaches: List[Tuple[str, List[str]]] = field(default_factory=list)
    cibles_scans: List[Dict[str, str]] = field(default_factory=list)
    leads_scannes: int = 0
    cibles_leads: List[Dict[str, str]] = field(default_factory=list)
    doublons: int = 0
    cibles: List[Dict[str, str]] = field(default_factory=list)
    min_score: int = MIN_MATCH_SCORE

    @property
    def nb_cibles(self) -> int:
        """Nombre de cibles de la visite terrain."""
        return len(self.cibles)

    @property
    def nb_scans_rattaches(self) -> int:
        """Nombre de scans déjà couverts par la campagne."""
        return len(self.scans_rattaches)


def construire_rapport(
    scans: Sequence[Dict[str, str]],
    leads: Sequence[Dict[str, str]],
    *,
    min_score: int = MIN_MATCH_SCORE,
) -> Rapport:
    """Fusionne les deux gisements en une liste de visite sans doublon.

    L'ordre est stable : les scans orphelins (ordre du fichier de scans) puis
    les leads sans email (ordre de la campagne). Un établissement déjà retenu
    par la première liste n'est pas ajouté une seconde fois.

    Args:
        scans: fiches de ``donnees_wifi_netanalyzer.csv``.
        leads: lignes de la campagne maître.
        min_score: note minimale de jointure.

    Returns:
        Le :class:`Rapport` complet, prêt à être affiché et écrit.
    """
    cibles_scans, rattaches = cibles_depuis_scans(
        scans, leads, min_score=min_score
    )
    cibles_leads = cibles_depuis_leads(leads)

    cibles: List[Dict[str, str]] = []
    doublons = 0
    for cible in [*cibles_scans, *cibles_leads]:
        nom = cible["nom_etablissement"]
        if any(
            match_score(nom, retenue["nom_etablissement"]) == EXACT_SCORE
            for retenue in cibles
        ):
            doublons += 1
            continue
        cibles.append(cible)

    return Rapport(
        scans_lus=len(scans),
        scans_rattaches=rattaches,
        cibles_scans=cibles_scans,
        leads_scannes=sum(
            1 for lead in leads if _texte(lead.get(ROUTER_COLUMN))
        ),
        cibles_leads=cibles_leads,
        doublons=doublons,
        cibles=cibles,
        min_score=min_score,
    )


def ecrire_cibles(chemin: Path, cibles: Sequence[Dict[str, str]]) -> int:
    """Écrit la liste de visite dans un CSV, de façon atomique.

    La colonne interne ``origine`` n'est pas écrite : seules les colonnes de
    :data:`COLONNES_TERRAIN` figurent dans le fichier.

    Args:
        chemin: CSV à produire (``campagne_terrain.csv``).
        cibles: cibles renvoyées par :func:`construire_rapport`.

    Returns:
        Le nombre de lignes écrites (hors en-tête).

    Raises:
        OSError: si l'écriture échoue ; aucun fichier tronqué n'est laissé.
    """
    return write_csv_atomic(chemin, cibles, COLONNES_TERRAIN)


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------
#: En-têtes du tableau des cibles, dans l'ordre des cellules produites par
#: :func:`_lignes_cibles`. Ils servent aussi à construire le tableau ``rich``,
#: ce qui garantit un rendu identique avec et sans ``rich``.
ENTETES_CIBLES: Tuple[str, ...] = (
    "N°",
    "Établissement",
    "Fabricant",
    "Sécurité",
    "Adresse",
    "Origine",
)

#: En-têtes du tableau des scans déjà présents dans la campagne.
ENTETES_RATTACHES: Tuple[str, ...] = ("Scan", "Lead de la campagne")


def _tableau_texte(entetes: Sequence[str], lignes: Sequence[Sequence[str]]) -> str:
    """Met en forme un tableau lisible sans dépendre de ``rich``.

    Args:
        entetes: intitulés des colonnes.
        lignes: lignes déjà converties en texte.

    Returns:
        Le tableau en texte brut : en-tête, séparateur, puis enregistrements.
    """
    largeurs = [len(entete) for entete in entetes]
    for ligne in lignes:
        for index, cellule in enumerate(ligne):
            largeurs[index] = max(largeurs[index], len(cellule))
    entete = "  ".join(
        valeur.ljust(largeur) for valeur, largeur in zip(entetes, largeurs)
    )
    separateur = "  ".join("-" * largeur for largeur in largeurs)
    corps = [
        "  ".join(
            valeur.ljust(largeur) for valeur, largeur in zip(ligne, largeurs)
        )
        for ligne in lignes
    ]
    return "\n".join([entete, separateur, *corps])


def _lignes_cibles(cibles: Sequence[Dict[str, str]]) -> List[Tuple[str, ...]]:
    """Convertit les cibles en cellules de tableau.

    Args:
        cibles: cibles renvoyées par :func:`construire_rapport`.

    Returns:
        Une ligne de cellules par cible : numéro, nom, fabricant, sécurité,
        adresse et origine. Les valeurs absentes sont remplacées par ``—``.
    """
    return [
        (
            str(index),
            cible.get("nom_etablissement", ""),
            cible.get("fabricant_routeur") or "—",
            cible.get("securite_wifi") or "—",
            cible.get("adresse") or "—",
            cible.get("origine", ""),
        )
        for index, cible in enumerate(cibles, 1)
    ]


def afficher_rapport(
    rapport: Rapport,
    *,
    netanalyzer: Path,
    master: Path,
    sortie: Path,
    ecrit: bool,
    console: Optional[object] = None,
) -> None:
    """Affiche les cibles de la visite puis le bilan des comptages.

    Le rendu utilise ``rich`` quand il est disponible (tableaux colorés) et
    retombe sinon sur un tableau texte, pour rester lisible dans un terminal
    minimal ou dans un test.

    Args:
        rapport: bilan renvoyé par :func:`construire_rapport`.
        netanalyzer: CSV des scans Wi-Fi lu.
        master: CSV de la campagne lu.
        sortie: CSV de visite écrit (ou à écrire).
        ecrit: ``True`` si le fichier de sortie a réellement été écrit.
        console: console ``rich`` à réutiliser ; ``None`` en crée une.
    """
    lignes = _lignes_cibles(rapport.cibles)
    rattaches = [
        (nom, ", ".join(leads)) for nom, leads in rapport.scans_rattaches
    ]

    if Table is not None and Console is not None:
        sortie_console = console if console is not None else Console()
        if lignes:
            table = Table(
                title=f"Cibles de la visite terrain ({rapport.nb_cibles})",
                title_justify="left",
                title_style="bold green",
                header_style="bold",
            )
            table.add_column("N°", justify="right", no_wrap=True)
            table.add_column("Établissement")
            table.add_column("Fabricant")
            table.add_column("Sécurité", no_wrap=True)
            table.add_column("Adresse")
            table.add_column("Origine")
            for ligne in lignes:
                table.add_row(*ligne)
            sortie_console.print(table)
        if rattaches:
            table = Table(
                title="Déjà dans la campagne (écartés)",
                title_justify="left",
                title_style="bold yellow",
                header_style="bold",
            )
            table.add_column("Scan", no_wrap=True)
            table.add_column("Lead de la campagne")
            for nom, lead in rattaches:
                table.add_row(nom, lead)
            sortie_console.print(table)
    else:  # repli sans rich : même information, en texte brut
        if lignes:
            print(f"Cibles de la visite terrain ({rapport.nb_cibles})")
            print(_tableau_texte(ENTETES_CIBLES, lignes))
        if rattaches:
            print("Déjà dans la campagne (écartés)")
            print(_tableau_texte(ENTETES_RATTACHES, rattaches))

    if not rapport.cibles:
        print("Aucune cible retenue : la liste de visite est vide.")

    print(f"Scans lus             : {rapport.scans_lus}")
    print(f"Déjà dans la campagne : {rapport.nb_scans_rattaches}")
    print(f"Leads déjà scannés    : {rapport.leads_scannes}")
    print(f"Sans email de contact : {len(rapport.cibles_leads)}")
    if rapport.min_score != MIN_MATCH_SCORE:
        print(f"Jointure (min-score)  : {rapport.min_score}")
    if rapport.doublons:
        print(f"Doublons écartés      : {rapport.doublons}")
    print(f"Cibles de visite      : {rapport.nb_cibles}")
    print(f"Source des scans      : {netanalyzer}")
    print(f"Source de la campagne : {master}")
    if ecrit:
        print(f"Fichier écrit         : {sortie}")
    else:
        print("Aucun fichier écrit (--dry-run).")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def construire_analyseur() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments du script.

    Returns:
        L'analyseur configuré (aide en français, chemins du pipeline).
    """
    analyseur = argparse.ArgumentParser(
        description=(
            "Croise donnees_wifi_netanalyzer.csv et campagne_wifi_master.csv "
            "pour produire campagne_terrain.csv : la liste des établissements "
            "à visiter physiquement (scans non utilisés et leads scannés sans "
            "email de contact)."
        )
    )
    analyseur.add_argument(
        "-n",
        "--netanalyzer",
        type=Path,
        default=NETANALYZER_CSV,
        help="CSV des scans Wi-Fi (défaut : donnees_wifi_netanalyzer.csv).",
    )
    analyseur.add_argument(
        "-m",
        "--master",
        type=Path,
        default=MASTER_CSV,
        help="CSV de la campagne email (défaut : campagne_wifi_master.csv).",
    )
    analyseur.add_argument(
        "-s",
        "--sortie",
        type=Path,
        default=TERRAIN_CSV,
        help="CSV de visite à écrire (défaut : campagne_terrain.csv).",
    )
    analyseur.add_argument(
        "--min-score",
        type=int,
        default=MIN_MATCH_SCORE,
        help=(
            "Note minimale pour considérer un scan comme déjà présent dans la "
            f"campagne, de 1 à {EXACT_SCORE} (défaut : {MIN_MATCH_SCORE})."
        ),
    )
    analyseur.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les cibles sans écrire le fichier de visite.",
    )
    return analyseur


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Produit la liste de visite terrain et affiche le bilan.

    Args:
        argv: arguments de la ligne de commande (``None`` = ``sys.argv``).

    Returns:
        ``0`` si la liste a été produite (ou simulée avec ``--dry-run``),
        ``1`` en cas de fichier absent, de colonne indispensable manquante ou
        d'échec d'écriture.
    """
    analyseur = construire_analyseur()
    arguments = analyseur.parse_args(argv)
    if not 0 < arguments.min_score <= EXACT_SCORE:
        analyseur.error(
            f"--min-score doit être compris entre 1 et {EXACT_SCORE}."
        )

    netanalyzer: Path = arguments.netanalyzer
    master: Path = arguments.master
    sortie: Path = arguments.sortie

    sources = ((netanalyzer, "--netanalyzer"), (master, "--master"))
    for chemin, option in sources:
        if not chemin.is_file():
            print(
                f"Erreur : fichier introuvable « {chemin} ». "
                f"Utilisez {option} pour indiquer un autre CSV.",
                file=sys.stderr,
            )
            return 1
    for chemin, _ in sources:
        if chemin.resolve() == sortie.resolve():
            print(
                f"Erreur : la sortie « {sortie} » écraserait « {chemin.name} ».",
                file=sys.stderr,
            )
            return 1

    try:
        scans = lire_scans(netanalyzer)
        leads = lire_campagne(master)
        rapport = construire_rapport(
            scans, leads, min_score=arguments.min_score
        )
        ecrit = False
        if not arguments.dry_run:
            ecrire_cibles(sortie, rapport.cibles)
            ecrit = True
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    afficher_rapport(
        rapport,
        netanalyzer=netanalyzer,
        master=master,
        sortie=sortie,
        ecrit=ecrit,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exécution directe
    sys.exit(main())
