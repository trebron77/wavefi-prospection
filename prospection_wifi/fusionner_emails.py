"""Étape de consolidation : un seul fichier de leads avec tous les emails.

Pourquoi ce script ?
--------------------
Chaque étape d'enrichissement écrit son propre CSV, et ``enricher_hunter.py``
ne recopie que ``nom``, les textes des trois emails et ``email_contact`` : les
adresses trouvées à la **première** passe (celle qui lit le site web de
l'établissement) ne survivent donc pas dans ``campagne_wifi_final.csv``. Après
deux ré-exécutions, les emails déjà payés sont perdus alors qu'ils dorment
toujours dans les fichiers précédents.

``fusionner_emails.py`` relit ces fichiers et reconstruit, pour les 96 leads de
base, une colonne ``email_contact`` unique.

Sources lues, par ordre de priorité
-----------------------------------
La première valeur non vide rencontrée gagne, source par source :

1. ``leads_scores.csv`` — les leads de base et leur score d'achat (colonnes
   ``email_contact`` puis ``email``) ;
2. ``campagne_wifi_enrichie.csv`` — première passe : emails lus sur le site web
   de l'établissement (``enricher.py``), donc les plus fiables ;
3. ``campagne_wifi_final.csv`` — emails Hunter.io (``enricher_hunter.py``) ;
4. ``campagne_wifi_facebook.csv`` — emails des pages Facebook publiques
   (``enricher_facebook.py``).

La première passe est optionnelle (``--sans-passe1``) : sur une machine où le
fichier n'existe pas encore, la fusion se fait sans elle.

Jointure
--------
Les noms d'entreprise ne s'écrivent pas toujours de la même façon d'un fichier
à l'autre. La comparaison réutilise :func:`enricher_netanalyzer.match_score`,
la même fonction que celle qui a servi à construire le fichier maître :
accents, casse et ponctuation sont neutralisés (``Hôtel Reva Tahiti`` rejoint
``Hotel Reva``), puis les mots vides et la ressemblance orthographique prennent
le relais. « Non trouvé » veut donc dire exactement la même chose ici et lors
de l'enrichissement Wi-Fi, et ``Carrefour Punaauia`` ne rejoint pas
``Carrefour Arue``. Deux fiches ex æquo proposant des adresses différentes sont
jugées ambiguës : aucune adresse n'est retenue et le cas est journalisé.

Fichier produit
---------------
``leads_enrichis_globaux.csv`` : les colonnes de ``leads_scores.csv`` dans le
même ordre, plus

* ``email_contact`` : l'adresse retenue (vide si aucune source n'a rien trouvé) ;
* ``source_email`` : sa provenance — ``base``, ``site``, ``hunter`` ou
  ``facebook`` — pour savoir d'où vient chaque adresse.

Utilisation
-----------
    python fusionner_emails.py                   # fusion complète
    python fusionner_emails.py --dry-run         # bilan, sans écriture
    python fusionner_emails.py --sans-passe1     # sans la première passe
    python fusionner_emails.py --min-score 90    # jointure stricte
    python fusionner_emails.py -s autre.csv      # autre fichier de sortie

Garanties
---------
* Aucune connexion réseau : seuls des fichiers locaux sont lus.
* Aucun fichier d'entrée n'est modifié ; la sortie est écrite de façon
  atomique (jamais de CSV tronqué).
* Une source absente ou vide est signalée et ignorée, sans interrompre la
  fusion ; seul le fichier de base est obligatoire.
* Les adresses manifestement inexploitables (syntaxe invalide, ``noreply@``...)
  sont écartées par :func:`enricher_hunter.is_usable_email` et comptées.
* Les fonctions acceptent un ``logger`` et un validateur injectables : la suite
  de tests reste rapide et hors ligne.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from config import (
    ENRICHED_CSV,
    FACEBOOK_CSV,
    FINAL_CSV,
    LEADS_ENRICHIS_CSV,
    MERGED_COLUMNS,
    SCORES_CSV,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)
from enricher_hunter import is_usable_email
from enricher_netanalyzer import (
    CAMPAIGN_NAME_COLUMN,
    EXACT_SCORE,
    MIN_MATCH_SCORE,
    match_score,
)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: Colonne du nom d'entreprise, identique dans tous les CSV du tunnel.
NOM_COLONNE = CAMPAIGN_NAME_COLUMN

#: Colonne qui reçoit l'adresse retenue.
EMAIL_COLONNE = MERGED_COLUMNS[0]

#: Colonne qui trace l'origine de l'adresse retenue.
SOURCE_COLONNE = MERGED_COLUMNS[1]

#: Colonnes examinées pour trouver un email dans un fichier du tunnel :
#: ``email_contact`` (campagnes) puis ``email`` (leads du scraper).
COLONNES_EMAIL: Tuple[str, ...] = ("email_contact", "email")

#: Étiquettes lisibles, utilisées par le bilan.
ETIQUETTE_BASE = "leads de base"
ETIQUETTE_SITE = "première passe (site web)"
ETIQUETTE_HUNTER = "Hunter.io"
ETIQUETTE_FACEBOOK = "Facebook"

#: Valeurs écrites dans ``source_email`` (provenance courte, lisible en tableur).
CLE_BASE = "base"
CLE_SITE = "site"
CLE_HUNTER = "hunter"
CLE_FACEBOOK = "facebook"


# ---------------------------------------------------------------------------
# Modèles
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    """Fichier d'enrichissement à dépouiller.

    Attributes:
        etiquette: Nom lisible affiché dans le bilan.
        cle: Valeur écrite dans la colonne ``source_email``.
        chemin: CSV à lire.
    """

    etiquette: str
    cle: str
    chemin: Path


#: Sources interrogées par défaut, après le fichier de base.
SOURCES_DEFAUT: Tuple[Source, ...] = (
    Source(ETIQUETTE_SITE, CLE_SITE, ENRICHED_CSV),
    Source(ETIQUETTE_HUNTER, CLE_HUNTER, FINAL_CSV),
    Source(ETIQUETTE_FACEBOOK, CLE_FACEBOOK, FACEBOOK_CSV),
)


@dataclass(frozen=True)
class BilanSource:
    """Compteurs d'une source pour le bilan final.

    ``trouves`` se répartit exactement en ``retenus`` (adresses écrites dans le
    fichier final), ``ignores`` (adresses d'un lead qui en avait déjà une) et
    ``orphelins`` (adresses sans lead correspondant).

    Attributes:
        etiquette: Nom lisible de la source.
        cle: Valeur écrite dans ``source_email``.
        chemin: CSV lu.
        present: ``False`` si le fichier n'existe pas (source ignorée).
        trouves: Adresses exploitables trouvées dans la source.
        retenus: Adresses effectivement retenues dans le fichier final.
        ignores: Adresses non retenues car le lead avait déjà un email.
        orphelins: Adresses sans lead correspondant, ou correspondance
            ambiguë (plusieurs fiches ex æquo).
        rejetes: Valeurs écartées par le validateur (syntaxe, ``noreply@``...).
    """

    etiquette: str
    cle: str
    chemin: Path
    present: bool
    trouves: int = 0
    retenus: int = 0
    ignores: int = 0
    orphelins: int = 0
    rejetes: int = 0


@dataclass(frozen=True)
class Correspondance:
    """Résultat de la recherche d'une adresse pour un nom d'entreprise.

    Attributes:
        note: Meilleure note de correspondance (``0`` si aucune fiche).
        email: Adresse retenue (vide si aucune fiche n'atteint la note
            minimale, ou si plusieurs fiches ex æquo la contredisent).
        candidats: Noms des fiches ayant obtenu la meilleure note.
    """

    note: int
    email: str
    candidats: Tuple[str, ...]

    @property
    def ambigue(self) -> bool:
        """Vrai quand plusieurs fiches ex æquo empêchent de trancher."""
        return not self.email and len(self.candidats) > 1


@dataclass(frozen=True)
class Rapport:
    """Résultat complet de la fusion, affiché par le bilan.

    Attributes:
        leads: Nombre de leads lus dans le fichier de base.
        avec_email: Nombre de leads disposant d'une adresse après fusion.
        sources: Compteurs du fichier de base puis de chaque source.
        base: Fichier de base lu.
        sortie: Fichier de sortie (écrit ou non selon ``ecrit``).
        ecrit: ``False`` en mode ``--dry-run``.
        partages: Adresses attribuées à plusieurs établissements, avec leurs
            noms ; à vérifier à la main (faux positif d'une source).
        colonnes: Colonnes du fichier produit.
        lignes: Lignes fusionnées (ordre du fichier de base).
    """

    leads: int
    avec_email: int
    sources: Tuple[BilanSource, ...]
    base: Path
    sortie: Path
    ecrit: bool
    partages: Tuple[Tuple[str, Tuple[str, ...]], ...]
    colonnes: Tuple[str, ...]
    lignes: Tuple[Dict[str, str], ...]

    @property
    def sans_email(self) -> int:
        """Nombre de leads restés sans adresse."""
        return self.leads - self.avec_email


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def _texte(value: object) -> str:
    """Convertit une valeur CSV en texte nettoyé (jamais ``None``)."""
    if value is None:
        return ""
    return str(value).strip()


def normaliser_nom(value: str) -> str:
    """Normalise un nom d'entreprise pour la jointure entre CSV.

    La règle est identique à celle de ``enricher_netanalyzer.py`` : accents,
    casse et ponctuation sont retirés afin de rapprocher ``Hôtel Tahiti Nui``
    de ``Hotel Tahiti Nui``.
    """
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def premier_email(row: Dict[str, str]) -> str:
    """Renvoie la première adresse non vide d'une ligne du tunnel."""
    for colonne in COLONNES_EMAIL:
        valeur = _texte(row.get(colonne))
        if valeur:
            return valeur
    return ""


# ---------------------------------------------------------------------------
# Lecture des fichiers
# ---------------------------------------------------------------------------
def lire_leads_base(path: Path) -> List[Dict[str, str]]:
    """Lit le CSV des leads de base (``leads_scores.csv``).

    Args:
        path: CSV de base, une ligne par lead.

    Returns:
        Les lignes du fichier, dans leur ordre d'origine.

    Raises:
        RuntimeError: Si le fichier est absent, vide ou sans colonne ``nom``.
    """
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(
            f"Fichier de leads introuvable : {path}. Lancez d'abord "
            f"scraper.py, clean_leads.py et scorer.py."
        )
    rows = read_csv_rows(path)
    if not rows:
        raise RuntimeError(f"Aucun lead de base dans {path}.")
    if NOM_COLONNE not in rows[0]:
        found = ", ".join(rows[0]) or "aucune"
        raise RuntimeError(
            f"Colonne {NOM_COLONNE} absente de {path} "
            f"(colonnes trouvées : {found})."
        )
    return rows


def indexer_source(
    source: Source,
    *,
    logger: Optional[logging.Logger] = None,
    valider: Callable[[str], bool] = is_usable_email,
) -> Tuple[List[Tuple[str, str]], BilanSource]:
    """Relève les adresses d'un fichier d'enrichissement.

    La première adresse exploitable rencontrée pour un nom donné est conservée ;
    les doublons de nom normalisé sont ignorés (les fichiers du tunnel
    contiennent une ligne par lead).

    Args:
        source: Fichier à dépouiller.
        logger: Logger à utiliser (``None`` = logger du tunnel).
        valider: Prédicat d'exploitabilité d'une adresse.

    Returns:
        ``(index, bilan)`` où ``index`` associe ``(nom, adresse)`` dans l'ordre
        du fichier, et ``bilan`` porte les compteurs de lecture. Un fichier
        absent donne un index vide et ``bilan.present`` à ``False`` : la fusion
        continue avec les autres sources.
    """
    log = logger or setup_logging()
    chemin = Path(source.chemin)
    bilan = BilanSource(
        etiquette=source.etiquette,
        cle=source.cle,
        chemin=chemin,
        present=chemin.is_file(),
    )
    if not bilan.present:
        log.warning(
            "Source ignorée (fichier absent) : %s", chemin
        )
        return [], bilan

    index: List[Tuple[str, str]] = []
    vus: Set[str] = set()
    rejetes = 0
    for row in read_csv_rows(chemin):
        nom = _texte(row.get(NOM_COLONNE))
        email = premier_email(row)
        cle = normaliser_nom(nom)
        if not nom or cle in vus:
            continue
        if not email:
            continue
        if not valider(email):
            rejetes += 1
            log.warning(
                "%s : adresse inexploitable ignorée pour %s (%s)",
                chemin.name,
                nom,
                email,
            )
            continue
        vus.add(cle)
        index.append((nom, email))

    bilan = replace(bilan, trouves=len(index), rejetes=rejetes)
    if not index:
        log.info("%s : aucune adresse exploitable.", chemin.name)
    return index, bilan


def meilleure_correspondance(
    nom: str,
    index: Sequence[Tuple[str, str]],
    *,
    min_score: int = MIN_MATCH_SCORE,
) -> Correspondance:
    """Cherche l'adresse du lead dans l'index d'une source.

    Args:
        nom: Nom de l'entreprise tel qu'il figure dans le fichier de base.
        index: Couples ``(nom, adresse)`` d'une source.
        min_score: Note minimale de correspondance (1 à ``EXACT_SCORE``).

    Returns:
        La meilleure correspondance. ``email`` reste vide si aucune fiche
        n'atteint ``min_score`` ; ``candidats`` liste alors les noms ex æquo
        quand plusieurs fiches proposent des adresses différentes.
    """
    if not nom:
        return Correspondance(0, "", ())

    meilleure_note = 0
    retenus: List[Tuple[int, str, str]] = []
    for candidat, email in index:
        note = match_score(nom, candidat)
        if note > meilleure_note:
            meilleure_note = note
            retenus = [(note, email, candidat)]
        elif note == meilleure_note:
            retenus.append((note, email, candidat))

    if meilleure_note < min_score:
        return Correspondance(meilleure_note, "", ())

    adresses = {email.casefold() for _, email, _ in retenus}
    candidats = tuple(candidat for _, _, candidat in retenus)
    if len(adresses) > 1:
        return Correspondance(meilleure_note, "", candidats)
    return Correspondance(meilleure_note, retenus[0][1], candidats)


def construire_colonnes(rows: Sequence[Dict[str, str]]) -> List[str]:
    """Construit l'en-tête du fichier produit.

    Les colonnes du fichier de base sont conservées dans le même ordre ;
    ``email_contact`` (si elle manque) puis ``source_email`` sont ajoutées en
    fin de ligne.

    Args:
        rows: Lignes du fichier de base.

    Returns:
        La liste des colonnes à écrire, dans l'ordre.
    """
    colonnes = list(rows[0].keys()) if rows else []
    for colonne in MERGED_COLUMNS:
        if colonne not in colonnes:
            colonnes.append(colonne)
    return colonnes


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------
def adresses_partagees(
    rows: Sequence[Dict[str, str]],
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """Repère les adresses attribuées à plusieurs établissements.

    Une même adresse pour deux entreprises trahit presque toujours une
    jointure abusive (page Facebook commune, homonymie, faux positif Hunter) :
    le bilan la signale pour une vérification manuelle.

    Args:
        rows: Lignes fusionnées.

    Returns:
        Les couples ``(adresse, noms)`` partagés, triés par adresse.
    """
    groupes: Dict[str, Tuple[str, List[str]]] = {}
    for row in rows:
        email = _texte(row.get(EMAIL_COLONNE))
        if not email:
            continue
        nom = _texte(row.get(NOM_COLONNE)) or "?"
        origine, noms = groupes.get(email.casefold(), (email, []))
        if nom not in noms:
            noms.append(nom)
        groupes[email.casefold()] = (origine, noms)
    return tuple(
        sorted(
            (origine, tuple(noms))
            for origine, noms in groupes.values()
            if len(noms) > 1
        )
    )


def fusionner_emails(
    base_path: Path = SCORES_CSV,
    *,
    sources: Sequence[Source] = SOURCES_DEFAUT,
    output_path: Path = LEADS_ENRICHIS_CSV,
    min_score: int = MIN_MATCH_SCORE,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
    valider: Callable[[str], bool] = is_usable_email,
) -> Rapport:
    """Réunit les emails de toutes les passes d'enrichissement.

    Args:
        base_path: CSV des leads de base (``leads_scores.csv``).
        sources: Fichiers d'enrichissement, du plus fiable au moins fiable.
        output_path: CSV à écrire (``leads_enrichis_globaux.csv``).
        min_score: Note minimale de jointure (1 à ``EXACT_SCORE``).
        dry_run: Si ``True``, aucune écriture n'est effectuée.
        logger: Logger à utiliser (``None`` = logger du tunnel).
        valider: Prédicat d'exploitabilité d'une adresse.

    Returns:
        Le rapport de fusion (lignes produites et compteurs du bilan).

    Raises:
        ValueError: Si ``min_score`` est hors des bornes autorisées.
        RuntimeError: Si le fichier de base est absent, vide ou sans colonne
            ``nom``.
    """
    if not 0 < min_score <= EXACT_SCORE:
        raise ValueError(
            f"min_score doit être compris entre 1 et {EXACT_SCORE}."
        )

    log = logger or setup_logging()
    base_path = Path(base_path)
    output_path = Path(output_path)
    proteges = {base_path.resolve()}
    proteges.update(Path(source.chemin).resolve() for source in sources)
    if output_path.resolve() in proteges:
        raise RuntimeError(
            f"Le fichier de sortie {output_path} écraserait un fichier "
            f"d'entrée ; choisissez un autre chemin."
        )
    rows = lire_leads_base(base_path)
    colonnes = construire_colonnes(rows)
    log.info("%d lead(s) de base lus dans %s", len(rows), base_path.name)

    noms = [_texte(row.get(NOM_COLONNE)) for row in rows]
    sorties: List[Dict[str, str]] = []
    rejetes_base = 0
    for row in rows:
        sortie = dict(row)
        email = premier_email(row)
        if email and not valider(email):
            rejetes_base += 1
            log.warning(
                "%s : adresse inexploitable ignorée (%s)",
                _texte(row.get(NOM_COLONNE)) or "ligne sans nom",
                email,
            )
            email = ""
        sortie[EMAIL_COLONNE] = email
        sortie[SOURCE_COLONNE] = CLE_BASE if email else ""
        sorties.append(sortie)

    base_trouves = sum(1 for sortie in sorties if sortie[EMAIL_COLONNE])
    bilans: List[BilanSource] = [
        BilanSource(
            etiquette=ETIQUETTE_BASE,
            cle=CLE_BASE,
            chemin=base_path,
            present=True,
            trouves=base_trouves,
            retenus=base_trouves,
            rejetes=rejetes_base,
        )
    ]

    for source in sources:
        index, bilan = indexer_source(source, logger=log, valider=valider)
        appariees: Set[str] = set()
        retenues: Set[str] = set()
        for nom, sortie in zip(noms, sorties):
            if not index:
                break
            correspondance = meilleure_correspondance(
                nom, index, min_score=min_score
            )
            if correspondance.ambigue:
                log.warning(
                    "%s : %d fiche(s) ex æquo dans %s (%s), aucune adresse "
                    "retenue (note %d)",
                    nom or "ligne sans nom",
                    len(correspondance.candidats),
                    bilan.chemin.name,
                    " / ".join(correspondance.candidats),
                    correspondance.note,
                )
                continue
            if not correspondance.email:
                continue
            cle = normaliser_nom(correspondance.candidats[0])
            appariees.add(cle)
            if sortie[EMAIL_COLONNE]:
                continue
            sortie[EMAIL_COLONNE] = correspondance.email
            sortie[SOURCE_COLONNE] = source.cle
            retenues.add(cle)
            log.debug(
                "%s -> %s (%s, note %d)",
                nom,
                correspondance.email,
                source.cle,
                correspondance.note,
            )
        bilans.append(
            replace(
                bilan,
                retenus=len(retenues),
                ignores=len(appariees - retenues),
                orphelins=len(index) - len(appariees),
            )
        )
        orphelins = [
            nom
            for nom, _ in index
            if normaliser_nom(nom) not in appariees
        ]
        if orphelins:
            log.info(
                "%s : %d adresse(s) sans lead correspondant (%s)",
                bilan.chemin.name,
                len(orphelins),
                ", ".join(orphelins),
            )

    avec_email = sum(1 for sortie in sorties if sortie[EMAIL_COLONNE])
    partages = adresses_partagees(sorties)
    for email, noms_partages in partages:
        log.warning(
            "Adresse %s attribuée à %d établissements : %s",
            email,
            len(noms_partages),
            ", ".join(noms_partages),
        )
    log.info(
        "Fusion : %d lead(s), %d avec une adresse email.",
        len(sorties),
        avec_email,
    )

    ecrit = False
    if dry_run:
        log.info(
            "Mode --dry-run : aucune écriture, %s inchangé.", output_path
        )
    else:
        write_csv_atomic(output_path, sorties, colonnes)
        ecrit = True
        log.info(
            "Leads enrichis écrits dans %s (%d ligne(s), %d avec email).",
            output_path,
            len(sorties),
            avec_email,
        )

    return Rapport(
        leads=len(sorties),
        avec_email=avec_email,
        sources=tuple(bilans),
        base=base_path,
        sortie=output_path,
        ecrit=ecrit,
        partages=partages,
        colonnes=tuple(colonnes),
        lignes=tuple(sorties),
    )


# ---------------------------------------------------------------------------
# Bilan
# ---------------------------------------------------------------------------
def pourcentage(rapport: Rapport) -> str:
    """Formate la part de leads avec email, à la française (``15,6 %``)."""
    if not rapport.leads:
        return "0 %"
    part = 100.0 * rapport.avec_email / rapport.leads
    return f"{part:.1f} %".replace(".", ",")


def lignes_bilan(rapport: Rapport) -> List[str]:
    """Construit les lignes du bilan de fusion.

    Args:
        rapport: Rapport renvoyé par :func:`fusionner_emails`.

    Returns:
        Les lignes à afficher, dans l'ordre.
    """
    lignes = [
        f"{rapport.base.name} : {rapport.leads} lead(s) de base",
        f"Emails après fusion : {rapport.avec_email} / {rapport.leads} "
        f"lead(s) ({pourcentage(rapport)})",
    ]
    for bilan in rapport.sources:
        if not bilan.present:
            lignes.append(f"  {bilan.etiquette} : fichier absent")
            continue
        detail = f"{bilan.retenus} retenu(s) sur {bilan.trouves} trouvé(s)"
        if bilan.orphelins:
            detail += f", {bilan.orphelins} sans lead correspondant"
        if bilan.rejetes:
            detail += f", {bilan.rejetes} écartée(s)"
        lignes.append(f"  {bilan.etiquette} : {detail}")
    lignes.append(f"Sans email : {rapport.sans_email} lead(s)")
    if rapport.partages:
        lignes.append("Adresses partagées par plusieurs établissements :")
        for email, noms in rapport.partages:
            lignes.append(f"  {email} -> {', '.join(noms)}")
    if rapport.ecrit:
        lignes.append(f"Fichier écrit : {rapport.sortie}")
    else:
        lignes.append(f"Aucune écriture (--dry-run) : {rapport.sortie}")
    return lignes


def afficher_rapport(rapport: Rapport) -> None:
    """Écrit le bilan de la fusion sur la sortie standard."""
    for ligne in lignes_bilan(rapport):
        print(ligne)


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


def construire_analyseur() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments de ``fusionner_emails.py``.

    Returns:
        Un analyseur prêt à lire ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Fusionne les emails de leads_scores.csv (base), "
            "campagne_wifi_enrichie.csv (site web), campagne_wifi_final.csv "
            "(Hunter.io) et campagne_wifi_facebook.csv (Facebook) dans "
            "leads_enrichis_globaux.csv."
        )
    )
    parser.add_argument(
        "--base",
        "-b",
        type=Path,
        default=SCORES_CSV,
        help="CSV des leads de base (défaut : leads_scores.csv).",
    )
    parser.add_argument(
        "--passe1",
        type=Path,
        default=None,
        help=(
            "CSV de la première passe, emails du site web "
            "(défaut : campagne_wifi_enrichie.csv)."
        ),
    )
    parser.add_argument(
        "--sans-passe1",
        action="store_true",
        help="Ignore la première passe (fichier absent ou non pertinent).",
    )
    parser.add_argument(
        "--hunter",
        type=Path,
        default=FINAL_CSV,
        help="CSV des emails Hunter.io (défaut : campagne_wifi_final.csv).",
    )
    parser.add_argument(
        "--facebook",
        type=Path,
        default=FACEBOOK_CSV,
        help=(
            "CSV des emails Facebook "
            "(défaut : campagne_wifi_facebook.csv)."
        ),
    )
    parser.add_argument(
        "--sortie",
        "-s",
        type=Path,
        default=LEADS_ENRICHIS_CSV,
        help="CSV fusionné à écrire (défaut : leads_enrichis_globaux.csv).",
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
        help="Affiche le bilan sans écrire de fichier.",
    )
    return parser


def sources_depuis_options(args: argparse.Namespace) -> Tuple[Source, ...]:
    """Construit la liste des sources à partir des options de la commande.

    Args:
        args: Arguments analysés par :func:`construire_analyseur`.

    Returns:
        Les sources à dépouiller, dans l'ordre de priorité.
    """
    sources: List[Source] = []
    if not args.sans_passe1:
        sources.append(Source(ETIQUETTE_SITE, CLE_SITE, Path(args.passe1)))
    sources.append(Source(ETIQUETTE_HUNTER, CLE_HUNTER, Path(args.hunter)))
    sources.append(
        Source(ETIQUETTE_FACEBOOK, CLE_FACEBOOK, Path(args.facebook))
    )
    return tuple(sources)


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``fusionner_emails.py``.

    Args:
        argv: Arguments ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si la fusion a abouti, 1 en cas d'erreur bloquante.
    """
    _configure_console_encoding()
    parser = construire_analyseur()
    args = parser.parse_args(argv)

    if not 0 < args.min_score <= EXACT_SCORE:
        parser.error(
            f"--min-score doit être compris entre 1 et {EXACT_SCORE}."
        )
    if args.sans_passe1 and args.passe1 is not None:
        parser.error("--sans-passe1 et --passe1 sont incompatibles.")
    if args.passe1 is None:
        args.passe1 = ENRICHED_CSV

    try:
        rapport = fusionner_emails(
            args.base,
            sources=sources_depuis_options(args),
            output_path=args.sortie,
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

    afficher_rapport(rapport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
