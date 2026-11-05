"""Étape 1 bis du tunnel : écarter les leads sans enjeu pour le Wi-Fi marketing.

Le scraper remonte tout ce que Google Maps référence sur un secteur : hôtels et
lodges, mais aussi roulottes, snacks, pizzerias à emporter ou chocolatiers. Ces
petits établissements n'ont ni le besoin ni les moyens d'une solution Wi-Fi
marketing : autant les écarter **avant** les étapes coûteuses (scoring TypeSafe,
recherche d'emails Hunter, envoi).

Le script lit ``leads_polynesie.csv`` (sortie de ``scraper.py``), filtre la
colonne ``categorie`` — ou ``type`` / ``catégorie`` si le fichier la nomme
autrement — puis écrit ``leads_polynesie_filtres.csv`` en conservant l'ordre des
lignes, les colonnes et l'encodage du fichier d'origine.

Règle appliquée : une ligne est exclue dès que sa catégorie **contient** l'un des
mots-clés de :data:`MOTS_CLES_EXCLUS`. La comparaison ignore la casse, les
accents et les tirets (``Pizzas à emporter``, ``PIZZAS A EMPORTER`` et
``pizzas-a-emporter`` sont donc équivalents). Tout le reste est conservé :
hôtels, chambres d'hôtes, lodges, restaurants classiques, bars et cafés.

Utilisation
-----------
    python clean_leads.py                        # filtre et écrit le CSV filtré
    python clean_leads.py --dry-run              # affiche le bilan, n'écrit rien
    python clean_leads.py --exclure chinois      # ajoute des mots-clés
    python clean_leads.py -e autre.csv -s sortie.csv

Aucun appel réseau : le script ne dépend que de ``config.py`` (lecture UTF-8 et
écriture atomique), ce qui le rend testable hors ligne.
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from config import FILTERED_CSV, LEADS_CSV, read_csv_rows, write_csv_atomic

try:  # rich est une dépendance déclarée ; on reste tolérant pour les tests.
    from rich.console import Console
    from rich.table import Table
except ModuleNotFoundError:  # pragma: no cover - dépend de l'environnement
    Console = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Règles de filtrage
# ---------------------------------------------------------------------------
#: Mots-clés qui disqualifient un lead, comparés en minuscules sans accents.
#:
#: Les libellés sont volontairement partiels (« pizzeria » couvre « Pizzeria » et
#: « Pizzas à emporter ») : la catégorie Google Maps est un texte libre.
MOTS_CLES_EXCLUS: Tuple[str, ...] = (
    "roulotte",
    "snack",
    "à emporter",
    "pizzeria",
    "fast-food",
    "restauration rapide",
    "chocolatier",
    "traiteur",
)

#: Noms de colonnes acceptés pour la catégorie, par ordre de préférence.
#:
#: ``categorie`` est le nom produit par ``scraper.py`` ; les autres entrées
#: couvrent les fichiers renommés à la main ou issus d'un autre export.
COLONNES_CATEGORIE: Tuple[str, ...] = (
    "categorie",
    "catégorie",
    "category",
    "type",
    "type_etablissement",
)

#: Catégories que le filtre doit conserver (documentation et tests).
CATEGORIES_CONSERVEES: Tuple[str, ...] = (
    "Hôtel",
    "Chambre d'hôtes",
    "Lodge",
    "Hostel",
    "Hébergement",
    "Restaurant",
    "Bar",
    "Café",
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def normaliser(texte: str) -> str:
    """Normalise un libellé pour une comparaison robuste.

    Passe en minuscules, supprime les accents (``é`` devient ``e``), transforme
    tirets, soulignés, apostrophes et espaces insécables en espaces simples,
    puis compresse les espaces multiples.

    Args:
        texte: libellé brut (catégorie d'un lead ou mot-clé de filtrage).

    Returns:
        Le libellé normalisé, sans espace en trop ; chaîne vide si ``texte`` est
        vide.

    Examples:
        >>> normaliser("Pizzas à emporter")
        'pizzas a emporter'
        >>> normaliser("  Fast-Food ")
        'fast food'
    """
    decompose = unicodedata.normalize("NFKD", texte or "")
    sans_accents = "".join(
        caractere for caractere in decompose if not unicodedata.combining(caractere)
    )
    remplace = "".join(
        " " if caractere in "-_'’/\u00a0" else caractere
        for caractere in sans_accents.lower()
    )
    return " ".join(remplace.split())


def mot_cle_correspondant(
    categorie: str,
    mots_cles: Sequence[str] = MOTS_CLES_EXCLUS,
) -> Optional[str]:
    """Cherche le premier mot-clé exclu présent dans une catégorie.

    Args:
        categorie: valeur brute de la colonne de catégorie.
        mots_cles: mots-clés exclus, dans l'ordre de priorité du rapport.

    Returns:
        Le mot-clé déclencheur tel qu'il a été déclaré (pour le rapport), ou
        ``None`` si la catégorie doit être conservée.
    """
    valeur = normaliser(categorie)
    for mot_cle in mots_cles:
        cible = normaliser(mot_cle)
        if cible and cible in valeur:
            return mot_cle
    return None


def detecter_colonne(
    entetes: Sequence[str],
    candidates: Sequence[str] = COLONNES_CATEGORIE,
) -> str:
    """Trouve la colonne de catégorie réellement présente dans le fichier.

    La comparaison des noms ignore la casse, les accents et les espaces autour
    du libellé : ``Categorie``, ``catégorie`` et ``categorie`` sont équivalents.

    Args:
        entetes: noms de colonnes du CSV, dans l'ordre du fichier.
        candidates: noms recherchés, par ordre de préférence.

    Returns:
        Le nom exact de la colonne tel qu'il apparaît dans le fichier.

    Raises:
        ValueError: aucune colonne candidate n'est présente ; le message liste
            les colonnes disponibles pour corriger le fichier ou l'appel.
    """
    index: Dict[str, str] = {normaliser(entete): entete for entete in entetes}
    for candidate in candidates:
        trouve = index.get(normaliser(candidate))
        if trouve is not None:
            return trouve
    attendues = ", ".join(f"« {nom} »" for nom in candidates)
    disponibles = ", ".join(f"« {nom} »" for nom in entetes) or "aucune"
    raise ValueError(
        "Colonne de catégorie introuvable : le fichier devrait contenir l'une "
        f"de ces colonnes : {attendues}. Colonnes trouvées : {disponibles}."
    )


# ---------------------------------------------------------------------------
# Filtrage
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rapport:
    """Bilan d'un filtrage de leads.

    Attributes:
        colonne: nom de la colonne de catégorie utilisée pour filtrer.
        total: nombre de lignes lues dans le fichier d'entrée.
        conserves: lignes gardées, dans l'ordre du fichier.
        exclus: lignes écartées, dans l'ordre du fichier.
        par_mot_cle: nombre d'exclusions par mot-clé déclencheur (les mots-clés
            sans correspondance figurent avec la valeur zéro).
        categories_exclues: nombre de lignes écartées par libellé de catégorie.
        categories_conservees: nombre de lignes gardées par libellé.
        sans_categorie: lignes dont la catégorie est vide (conservées, faute
            d'information permettant de les écarter).
    """

    colonne: str
    total: int
    conserves: List[Dict[str, str]] = field(default_factory=list)
    exclus: List[Dict[str, str]] = field(default_factory=list)
    par_mot_cle: Dict[str, int] = field(default_factory=dict)
    categories_exclues: Dict[str, int] = field(default_factory=dict)
    categories_conservees: Dict[str, int] = field(default_factory=dict)
    sans_categorie: int = 0

    @property
    def nb_conserves(self) -> int:
        """Nombre de leads conservés."""
        return len(self.conserves)

    @property
    def nb_exclus(self) -> int:
        """Nombre de leads exclus."""
        return len(self.exclus)


def filtrer_leads(
    leads: Sequence[Dict[str, str]],
    *,
    colonne: str,
    mots_cles: Sequence[str] = MOTS_CLES_EXCLUS,
) -> Rapport:
    """Sépare les leads selon les mots-clés exclus.

    L'ordre des lignes est préservé de part et d'autre : le CSV filtré garde la
    progression du scraper (le tri par score viendra à l'étape suivante).

    Args:
        leads: lignes du CSV, sous forme de dictionnaires colonne → valeur.
        colonne: nom exact de la colonne de catégorie dans ``leads``.
        mots_cles: mots-clés exclus, dans l'ordre de priorité du rapport.

    Returns:
        Un :class:`Rapport` contenant les deux sous-listes et les compteurs.
    """
    conserves: List[Dict[str, str]] = []
    exclus: List[Dict[str, str]] = []
    par_mot_cle: Counter = Counter({mot_cle: 0 for mot_cle in mots_cles})
    categories_exclues: Counter = Counter()
    categories_conservees: Counter = Counter()
    sans_categorie = 0

    for lead in leads:
        categorie = lead.get(colonne, "") or ""
        mot_cle = mot_cle_correspondant(categorie, mots_cles)
        if mot_cle is None:
            conserves.append(lead)
            if categorie.strip():
                categories_conservees[categorie.strip()] += 1
            else:
                sans_categorie += 1
            continue
        exclus.append(lead)
        par_mot_cle[mot_cle] += 1
        categories_exclues[categorie.strip() or "(vide)"] += 1

    return Rapport(
        colonne=colonne,
        total=len(leads),
        conserves=conserves,
        exclus=exclus,
        par_mot_cle=dict(par_mot_cle),
        categories_exclues=dict(categories_exclues),
        categories_conservees=dict(categories_conservees),
        sans_categorie=sans_categorie,
    )


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------
def _tableau_texte(entetes: Sequence[str], lignes: Sequence[Sequence[str]]) -> str:
    """Met en forme un tableau lisible sans dépendre de ``rich``.

    Args:
        entetes: intitulés des colonnes.
        lignes: lignes déjà converties en texte.

    Returns:
        Le tableau en texte brut, une ligne d'en-tête, un séparateur, puis les
        enregistrements.
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
        "  ".join(valeur.ljust(largeur) for valeur, largeur in zip(ligne, largeurs))
        for ligne in lignes
    ]
    return "\n".join([entete, separateur, *corps])


def _tri_decroissant(compteurs: Dict[str, int]) -> List[Tuple[str, int]]:
    """Trie des compteurs par effectif décroissant, puis par libellé.

    Args:
        compteurs: libellé → nombre d'occurrences.

    Returns:
        La liste des couples ``(libellé, nombre)`` triée pour l'affichage.
    """
    return sorted(compteurs.items(), key=lambda item: (-item[1], item[0].lower()))


def afficher_rapport(
    rapport: Rapport,
    *,
    entree: Path,
    sortie: Path,
    ecrit: bool,
    console: Optional[object] = None,
) -> None:
    """Affiche le bilan du filtrage : exclusions, catégories gardées, totaux.

    Le rendu utilise ``rich`` quand il est disponible (tableaux colorés) et
    retombe sinon sur un tableau texte, pour rester lisible dans un terminal
    minimal ou un test.

    Args:
        rapport: bilan renvoyé par :func:`filtrer_leads`.
        entree: chemin du CSV lu.
        sortie: chemin du CSV filtré.
        ecrit: ``True`` si le fichier de sortie a réellement été écrit.
        console: console ``rich`` à réutiliser ; ``None`` en crée une.
    """
    exclusions = _tri_decroissant(rapport.categories_exclues)
    gardees = _tri_decroissant(rapport.categories_conservees)

    if Table is not None and Console is not None:
        sortie_console = console if console is not None else Console()
        if exclusions:
            table = Table(
                title="Leads exclus",
                title_justify="left",
                title_style="bold red",
                header_style="bold",
            )
            table.add_column("Motif", no_wrap=True)
            table.add_column("Catégorie du fichier", no_wrap=True)
            table.add_column("Nombre", justify="right")
            for categorie, nombre in exclusions:
                motif = mot_cle_correspondant(categorie) or "—"
                table.add_row(motif, categorie, str(nombre))
            sortie_console.print(table)
        if gardees:
            table = Table(
                title="Leads conservés par catégorie",
                title_justify="left",
                title_style="bold green",
                header_style="bold",
            )
            table.add_column("Catégorie", no_wrap=True)
            table.add_column("Nombre", justify="right")
            for categorie, nombre in gardees:
                table.add_row(categorie, str(nombre))
            sortie_console.print(table)
    else:  # repli sans rich : même information, en texte brut
        if exclusions:
            lignes = [
                (mot_cle_correspondant(categorie) or "—", categorie, str(nombre))
                for categorie, nombre in exclusions
            ]
            print("Leads exclus")
            print(_tableau_texte(("Motif", "Catégorie", "Nombre"), lignes))
        if gardees:
            lignes = [(categorie, str(nombre)) for categorie, nombre in gardees]
            print("Leads conservés par catégorie")
            print(_tableau_texte(("Catégorie", "Nombre"), lignes))

    print(f"Source         : {entree}")
    print(f"Colonne lue    : {rapport.colonne}")
    print(f"Lignes lues    : {rapport.total}")
    print(f"Leads gardés   : {rapport.nb_conserves}")
    print(f"Leads exclus   : {rapport.nb_exclus}")
    if rapport.sans_categorie:
        print(
            f"Catégorie vide : {rapport.sans_categorie} lead(s) conservé(s) "
            "faute d'information"
        )
    if ecrit:
        print(f"Fichier écrit  : {sortie}")
    else:
        print("Aucun fichier écrit (--dry-run).")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def analyser_mots_cles(supplementaires: str) -> Tuple[str, ...]:
    """Fusionne les mots-clés par défaut avec ceux passés en ligne de commande.

    Args:
        supplementaires: liste séparée par des virgules (``"chinois, asiatique"``).

    Returns:
        Les mots-clés par défaut suivis des mots-clés supplémentaires, sans
        doublon ni entrée vide, dans l'ordre de déclaration.
    """
    mots_cles: List[str] = list(MOTS_CLES_EXCLUS)
    normalises = {normaliser(mot_cle) for mot_cle in mots_cles}
    for brut in (supplementaires or "").split(","):
        mot_cle = brut.strip()
        if not mot_cle or normaliser(mot_cle) in normalises:
            continue
        mots_cles.append(mot_cle)
        normalises.add(normaliser(mot_cle))
    return tuple(mots_cles)


def construire_analyseur() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments du script.

    Returns:
        L'analyseur configuré (aide en français, valeurs par défaut du pipeline).
    """
    analyseur = argparse.ArgumentParser(
        description=(
            "Écarte de leads_polynesie.csv les roulottes, snacks, pizzerias, "
            "points de restauration rapide et autres petits commerces sans "
            "enjeu Wi-Fi, puis écrit leads_polynesie_filtres.csv."
        )
    )
    analyseur.add_argument(
        "-e",
        "--entree",
        type=Path,
        default=LEADS_CSV,
        help="CSV source (défaut : leads_polynesie.csv du dossier).",
    )
    analyseur.add_argument(
        "-s",
        "--sortie",
        type=Path,
        default=FILTERED_CSV,
        help="CSV filtré à écrire (défaut : leads_polynesie_filtres.csv).",
    )
    analyseur.add_argument(
        "--exclure",
        default="",
        metavar="MOTS",
        help=(
            "Mots-clés supplémentaires à exclure, séparés par des virgules "
            "(ex. « chinois,asiatique »). Ils s'ajoutent à la liste par défaut."
        ),
    )
    analyseur.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche le bilan détaillé sans écrire le fichier filtré.",
    )
    return analyseur


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Filtre le CSV de leads et affiche le bilan.

    Args:
        argv: arguments de la ligne de commande (``None`` = ``sys.argv``).

    Returns:
        ``0`` si le filtrage s'est déroulé jusqu'au bout (écriture comprise),
        ``1`` en cas de fichier absent, de colonne de catégorie introuvable ou
        d'échec d'écriture.
    """
    arguments = construire_analyseur().parse_args(argv)
    entree: Path = arguments.entree
    sortie: Path = arguments.sortie
    mots_cles = analyser_mots_cles(arguments.exclure)

    if not entree.is_file():
        print(
            f"Erreur : fichier introuvable « {entree} ». "
            "Utilisez --entree pour indiquer un autre CSV.",
            file=sys.stderr,
        )
        return 1
    if entree.resolve() == sortie.resolve():
        print(
            f"Erreur : la sortie « {sortie} » écraserait le fichier source.",
            file=sys.stderr,
        )
        return 1

    try:
        leads = read_csv_rows(entree)
        if not leads:
            print(
                f"Erreur : « {entree} » ne contient aucune ligne de données.",
                file=sys.stderr,
            )
            return 1
        colonnes: List[str] = list(leads[0].keys())
        colonne = detecter_colonne(colonnes)
        rapport = filtrer_leads(leads, colonne=colonne, mots_cles=mots_cles)
        ecrit = False
        if not arguments.dry_run:
            write_csv_atomic(sortie, rapport.conserves, colonnes)
            ecrit = True
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    afficher_rapport(rapport, entree=entree, sortie=sortie, ecrit=ecrit)
    return 0


if __name__ == "__main__":  # pragma: no cover - exécution directe
    sys.exit(main())

