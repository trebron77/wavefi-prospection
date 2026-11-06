"""Génère le PDF du one-pager WaveFi à partir du HTML local (Playwright).

Le script ouvre ``one_pager_atn.html`` dans un navigateur Chromium headless
piloté par Playwright, attend la fin du chargement de la page et de toutes ses
images, puis exporte le rendu en PDF A4 en conservant les fonds de couleur
(``print_background=True``).

Utilisation
-----------
    python generate_pdf.py                        # valeurs par défaut
    python generate_pdf.py --sortie build/OP.pdf  # autre destination
    python generate_pdf.py --canal msedge         # navigateur déjà installé
    python generate_pdf.py --dry-run              # contrôles, sans navigateur

Prérequis
---------
    python -m pip install -r requirements.txt     # installe playwright
    python -m playwright install chromium         # télécharge le navigateur

Détails utiles
--------------
* La page est ouverte via une URI ``file://`` (``Path.as_uri()``), ce qui gère
  correctement les espaces et les accents d'un chemin Windows.
* ``wait_until="networkidle"`` attend la fin des requêtes réseau ; les images
  sont ensuite vérifiées une à une (``complete`` et ``naturalWidth > 0``), car
  une ressource ``file://`` ne déclenche pas toujours d'événement réseau.
* Le fichier HTML ne doit pas être réécrit : si l'export doit garder la
  répartition verticale égale obtenue avec ``justify-content: space-between``,
  l'option ``--imposer-hauteur-a4`` injecte une hauteur A4 pour compenser le
  ``min-height: auto`` de la feuille ``@media print`` du document.
* Codes de sortie : ``0`` succès, ``1`` échec de génération, ``2`` prérequis
  manquant ou option invalide (playwright absent, HTML introuvable...).

Aucun navigateur n'est lancé à l'import : Playwright est importé dans les
fonctions concernées, ce qui permet d'utiliser ``--help`` ou ``--dry-run``
même quand la dépendance n'est pas encore installée.
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Sequence

__all__ = [
    "CANAUX_REPLI",
    "DELAI_CHARGEMENT_S",
    "DELAI_IMAGES_S",
    "FORMAT_PDF",
    "HTML_DEFAUT",
    "MARGE_MM",
    "PDF_DEFAUT",
    "attendre_images",
    "construire_analyseur",
    "exporter_pdf",
    "lancer_navigateur",
    "main",
    "preparer_sortie",
    "verifier_page",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: Racine du projet (ce script vit à côté de ``requirements.txt``).
PROJECT_ROOT = Path(__file__).resolve().parent

#: Page HTML à convertir (surchargée par ``--html``).
HTML_DEFAUT = PROJECT_ROOT / "one_pager_atn.html"

#: PDF produit (surchargé par ``--sortie``).
PDF_DEFAUT = PROJECT_ROOT / "WaveFi_OnePager_ATN.pdf"

#: Format papier demandé à Chromium.
FORMAT_PDF = "A4"

#: Marge d'impression par défaut, en millimètres (``--marges``).
MARGE_MM = 10

#: Délai maximal de chargement de la page, en secondes (``--delai``).
DELAI_CHARGEMENT_S = 30

#: Délai maximal d'attente des images, en secondes.
DELAI_IMAGES_S = 15

#: Navigateurs réutilisés si le Chromium de Playwright n'est pas installé.
CANAUX_REPLI: Sequence[str] = ("msedge", "chrome")

#: Vérifie que chaque image est chargée ; renvoie les ``src`` en échec.
SCRIPT_IMAGES = """
() => Array.from(document.images)
    .filter((img) => !img.complete || img.naturalWidth === 0)
    .map((img) => img.getAttribute('src') || '(sans src)')
"""

#: Compte les images de la page.
SCRIPT_COMPTER_IMAGES = "() => document.images.length"

#: Feuille injectée par ``--imposer-hauteur-a4`` : ``@media print`` du document
#: met ``min-height: auto`` ; on rétablit une hauteur A4 moins les marges pour
#: que ``justify-content: space-between`` répartisse l'espace.
STYLE_HAUTEUR_A4 = (
    "@media print {{ .page {{ min-height: calc(297mm - 2 * {marge}); }} }}"
)

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def configurer_journalisation(verbeux: bool = False) -> logging.Logger:
    """Retourne le logger du script, configuré une seule fois.

    Args:
        verbeux: si vrai, passe le niveau à ``DEBUG``.

    Returns:
        Le logger ``generate_pdf``, prêt à l'emploi.
    """
    log = logging.getLogger("generate_pdf")
    if not log.handlers:
        gestionnaire = logging.StreamHandler()
        gestionnaire.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(gestionnaire)
    log.setLevel(logging.DEBUG if verbeux else logging.INFO)
    return log


def _premiere_ligne(erreur: BaseException) -> str:
    """Résume une exception sur une seule ligne (logs lisibles)."""
    texte = " ".join(str(erreur).split())
    return texte or erreur.__class__.__name__


def verifier_page(html: Path) -> Path:
    """Vérifie que la page HTML existe, puis renvoie son chemin absolu.

    Args:
        html: chemin du fichier HTML à convertir.

    Returns:
        Le chemin absolu du fichier HTML.

    Raises:
        FileNotFoundError: si le fichier n'existe pas.
        IsADirectoryError: si le chemin désigne un dossier.
        ValueError: si le fichier est vide.
    """
    chemin = Path(html).expanduser()
    if not chemin.exists():
        raise FileNotFoundError(f"Page HTML introuvable : {chemin}")
    if not chemin.is_file():
        raise IsADirectoryError(f"Le chemin HTML n'est pas un fichier : {chemin}")
    if chemin.stat().st_size == 0:
        raise ValueError(f"La page HTML est vide : {chemin}")
    return chemin.resolve()


def preparer_sortie(pdf: Path, creer: bool = True) -> Path:
    """Prépare le chemin du PDF et crée son dossier parent si nécessaire.

    Args:
        pdf: chemin souhaité pour le PDF (l'extension ``.pdf`` est ajoutée si
            elle est absente).
        creer: si vrai, crée le dossier parent manquant ; ``False`` se contente
            de calculer le chemin (utile en ``--dry-run``).

    Returns:
        Le chemin absolu du PDF à écrire.

    Raises:
        IsADirectoryError: si le chemin désigne un dossier existant.
    """
    chemin = Path(pdf).expanduser()
    if chemin.suffix.lower() != ".pdf":
        chemin = chemin.with_suffix(".pdf")
    if chemin.is_dir():
        raise IsADirectoryError(f"Le chemin de sortie est un dossier : {chemin}")
    if creer:
        chemin.parent.mkdir(parents=True, exist_ok=True)
    return chemin.resolve()


def _images_referencees(page: Path) -> List[str]:
    """Liste les images locales référencées par la page HTML.

    Args:
        page: chemin du fichier HTML.

    Returns:
        Les attributs ``src`` relatifs, dans l'ordre du document (les URL
        absolues, ``data:`` et les chemins commençant par ``/`` sont ignorés).
    """
    contenu = page.read_text(encoding="utf-8", errors="replace")
    sources = re.findall(
        r"<img[^>]*\ssrc\s*=\s*[\"']([^\"']+)[\"']", contenu, re.IGNORECASE
    )
    return [
        src for src in sources if not re.match(r"^(?:[a-z]+:|//|/)", src, re.I)
    ]


def attendre_images(
    page: Any,
    delai_s: float = DELAI_IMAGES_S,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Attend que toutes les images de la page soient décodées.

    Args:
        page: objet ``Page`` de Playwright (typé ``Any`` pour rester importable
            sans Playwright).
        delai_s: délai maximal d'attente, en secondes.
        logger: logger à utiliser (par défaut celui du script).

    Returns:
        Le nombre d'images chargées avec succès.
    """
    from playwright.sync_api import Error as ErreurPlaywright

    log = logger or configurer_journalisation()
    total = int(page.evaluate(SCRIPT_COMPTER_IMAGES) or 0)
    if total == 0:
        log.info("Aucune image dans la page.")
        return 0
    try:
        page.wait_for_function(
            "() => Array.from(document.images)"
            ".every((img) => img.complete && img.naturalWidth > 0)",
            timeout=int(delai_s * 1000),
        )
    except ErreurPlaywright as exc:
        en_echec: List[str] = list(page.evaluate(SCRIPT_IMAGES) or [])
        log.warning(
            "%d image(s) non chargée(s) après %s s : %s",
            len(en_echec),
            delai_s,
            ", ".join(en_echec) or _premiere_ligne(exc),
        )
        return total - len(en_echec)
    log.info("%d image(s) chargée(s).", total)
    return total


def lancer_navigateur(
    playwright: Any,
    canal: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """Lance Chromium en mode headless, avec repli sur un navigateur installé.

    Args:
        playwright: instance ``Playwright`` déjà démarrée.
        canal: canal explicite (``msedge``, ``chrome``...) ; ``None`` essaie le
            Chromium fourni par Playwright, puis les canaux de ``CANAUX_REPLI``.
        logger: logger à utiliser (par défaut celui du script).

    Returns:
        Le navigateur lancé (à fermer par l'appelant).

    Raises:
        RuntimeError: si aucun navigateur n'a pu être lancé.
    """
    from playwright.sync_api import Error as ErreurPlaywright

    log = logger or configurer_journalisation()
    essais: List[Optional[str]] = [canal] if canal else [None, *CANAUX_REPLI]
    derniere: Optional[BaseException] = None
    for candidat in essais:
        etiquette = candidat or "chromium (Playwright)"
        options = {"channel": candidat} if candidat else {}
        try:
            navigateur = playwright.chromium.launch(headless=True, **options)
        except (ErreurPlaywright, OSError) as exc:
            derniere = exc
            log.warning(
                "Navigateur %s indisponible : %s", etiquette, _premiere_ligne(exc)
            )
            continue
        log.info("Navigateur %s lancé (headless).", etiquette)
        return navigateur
    raise RuntimeError(
        "Impossible de lancer un navigateur. Installez le Chromium de Playwright "
        "avec 'python -m playwright install chromium', ou réutilisez un "
        f"navigateur déjà présent via --canal msedge (dernière erreur : "
        f"{_premiere_ligne(derniere) if derniere else 'aucun essai'})"
    )


def exporter_pdf(
    html: Path = HTML_DEFAUT,
    pdf: Path = PDF_DEFAUT,
    canal: Optional[str] = None,
    marges_mm: int = MARGE_MM,
    delai_s: float = DELAI_CHARGEMENT_S,
    imposer_a4: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Path:
    """Convertit la page HTML locale en PDF A4, fonds de couleur conservés.

    Args:
        html: page HTML à convertir.
        pdf: chemin du PDF à écrire.
        canal: navigateur à réutiliser (``msedge``, ``chrome``...) ; ``None``
            essaie le Chromium de Playwright puis les canaux de repli.
        marges_mm: marge d'impression appliquée par Chromium, en millimètres.
        delai_s: délai maximal de chargement de la page, en secondes.
        imposer_a4: si vrai, injecte une hauteur A4 en impression afin de
            préserver ``justify-content: space-between`` (le ``@media print``
            du document force sinon ``min-height: auto``).
        logger: logger à utiliser (par défaut celui du script).

    Returns:
        Le chemin absolu du PDF écrit.

    Raises:
        ModuleNotFoundError: si Playwright n'est pas installé.
        FileNotFoundError: si la page HTML est absente.
        RuntimeError: si le navigateur, le chargement ou l'export échoue.
    """
    log = logger or configurer_journalisation()
    page_html = verifier_page(html)
    if Path(pdf).expanduser().resolve() == page_html:
        raise RuntimeError(
            "Le PDF de sortie écraserait la page HTML ; choisissez un autre "
            "chemin."
        )
    sortie = preparer_sortie(pdf)

    from playwright.sync_api import (
        Error as ErreurPlaywright,
        sync_playwright,
    )

    marge = f"{marges_mm}mm"
    url = page_html.as_uri()
    log.info("Page source : %s", page_html)
    log.info("URL locale  : %s", url)
    log.debug("PDF cible   : %s", sortie)

    with sync_playwright() as playwright:
        navigateur = lancer_navigateur(playwright, canal=canal, logger=log)
        try:
            page = navigateur.new_page()
            page.goto(url, wait_until="networkidle", timeout=int(delai_s * 1000))
            log.info("Chargement terminé (networkidle).")
            attendre_images(page, logger=log)
            if imposer_a4:
                page.add_style_tag(content=STYLE_HAUTEUR_A4.format(marge=marge))
                log.info(
                    "Hauteur A4 imposée en impression (marges %s).", marge
                )
            page.pdf(
                path=str(sortie),
                format=FORMAT_PDF,
                print_background=True,
                prefer_css_page_size=False,
                margin={
                    "top": marge,
                    "bottom": marge,
                    "left": marge,
                    "right": marge,
                },
            )
        except ErreurPlaywright as exc:
            raise RuntimeError(
                f"Export PDF interrompu : {_premiere_ligne(exc)}"
            ) from exc
        finally:
            navigateur.close()

    if not sortie.exists():
        raise RuntimeError(f"Le PDF n'a pas été créé : {sortie}")
    return sortie


# ---------------------------------------------------------------------------
# Interface en ligne de commande
# ---------------------------------------------------------------------------
def construire_analyseur() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments du script.

    Returns:
        L'analyseur prêt à lire ``sys.argv[1:]``.
    """
    analyseur = argparse.ArgumentParser(
        description="Convertit une page HTML locale en PDF A4 avec Playwright "
        "en mode headless (fonds de couleur conservés)."
    )
    analyseur.add_argument(
        "--html",
        type=Path,
        default=HTML_DEFAUT,
        help="page HTML à convertir (défaut : %(default)s).",
    )
    analyseur.add_argument(
        "--sortie",
        "-s",
        type=Path,
        default=PDF_DEFAUT,
        help="PDF à écrire (défaut : %(default)s).",
    )
    analyseur.add_argument(
        "--canal",
        default=None,
        help="navigateur à réutiliser (msedge, chrome...) ; par défaut, le "
        "Chromium installé pour Playwright.",
    )
    analyseur.add_argument(
        "--marges",
        type=int,
        default=MARGE_MM,
        help="marge d'impression en millimètres, de 0 à 50 "
        "(défaut : %(default)s).",
    )
    analyseur.add_argument(
        "--delai",
        type=float,
        default=DELAI_CHARGEMENT_S,
        help="délai maximal de chargement, en secondes (défaut : %(default)s).",
    )
    analyseur.add_argument(
        "--imposer-hauteur-a4",
        action="store_true",
        help="force une hauteur A4 à l'impression pour conserver la "
        "répartition verticale égale du document.",
    )
    analyseur.add_argument(
        "--dry-run",
        action="store_true",
        help="contrôle la page et ses images sans lancer de navigateur ni "
        "écrire de PDF.",
    )
    analyseur.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="journalisation détaillée.",
    )
    return analyseur


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Convertit le one-pager en PDF et affiche le résultat.

    Args:
        argv: arguments de la ligne de commande (par défaut ``sys.argv[1:]``).

    Returns:
        ``0`` si le PDF est écrit, ``1`` en cas d'échec de génération, ``2`` si
        un prérequis manque (Playwright, page HTML) ou si une option est
        invalide, ``130`` si l'utilisateur interrompt le script.
    """
    analyseur = construire_analyseur()
    arguments = analyseur.parse_args(argv)
    log = configurer_journalisation(arguments.verbose)

    if not 0 <= arguments.marges <= 50:
        analyseur.error("--marges doit être comprise entre 0 et 50 mm.")
    if arguments.delai <= 0:
        analyseur.error("--delai doit être strictement positif.")

    try:
        page_html = verifier_page(arguments.html)
        sortie = preparer_sortie(arguments.sortie, creer=not arguments.dry_run)
    except (FileNotFoundError, IsADirectoryError, ValueError) as exc:
        log.error("%s", exc)
        return 2

    if arguments.dry_run:
        images = _images_referencees(page_html)
        manquantes = [
            src for src in images if not (page_html.parent / src).is_file()
        ]
        log.info("Page source : %s", page_html)
        log.info("URL locale  : %s", page_html.as_uri())
        log.info("PDF prévu   : %s", sortie)
        log.info(
            "Images      : %d référencée(s), %d manquante(s)",
            len(images),
            len(manquantes),
        )
        for src in manquantes:
            log.warning("Image introuvable : %s", src)
        log.info("Aucun navigateur lancé, aucun PDF écrit (--dry-run).")
        return 0

    try:
        chemin = exporter_pdf(
            html=page_html,
            pdf=sortie,
            canal=arguments.canal,
            marges_mm=arguments.marges,
            delai_s=arguments.delai,
            imposer_a4=arguments.imposer_hauteur_a4,
            logger=log,
        )
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] != "playwright":
            raise
        log.error(
            "Playwright n'est pas installé. Lancez 'python -m pip install -r "
            "requirements.txt' puis 'python -m playwright install chromium'."
        )
        return 2
    except (FileNotFoundError, IsADirectoryError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("Export interrompu par l'utilisateur.")
        return 130
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:  # dernier filet : message lisible, pas de trace.
        log.error("Échec inattendu : %s", _premiere_ligne(exc))
        return 1

    taille_ko = chemin.stat().st_size / 1024
    print(
        f"Succès : PDF généré dans {chemin} "
        f"({taille_ko:.0f} Ko, format A4, fonds de couleur conservés)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

