"""Configuration partagée du tunnel de prospection Wi-Fi (Polynésie française).

Ce module centralise :

* le chargement du fichier ``.env`` (via ``python-dotenv``) ;
* la lecture des clés d'API et des options facultatives ;
* les constantes de chemins de fichiers (CSV, dashboard HTML) ;
* quelques utilitaires communs (journalisation, écriture CSV atomique).

Il est importé par ``scraper.py``, ``scorer.py``, ``dashboard.py``,
``emailer.py`` et ``enricher.py`` afin que les étapes du tunnel partagent
exactement les mêmes noms de colonnes et les mêmes chemins de fichiers.

Aucune connexion réseau n'est effectuée à l'import : le module est sûr à
importer dans des tests hors ligne.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:  # python-dotenv est une dépendance déclarée, mais on reste tolérant.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dépend de l'environnement
    load_dotenv = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent

#: Fichier de configuration locale (jamais commité).
ENV_FILE: Path = BASE_DIR / ".env"

#: Sortie de ``scraper.py`` : entreprises brutes récupérées via Apify.
LEADS_CSV: Path = BASE_DIR / "leads_polynesie.csv"

#: Sortie de ``scorer.py`` : leads enrichis d'un score d'achat.
SCORES_CSV: Path = BASE_DIR / "leads_scores.csv"

#: Sortie de ``dashboard.py`` : tableau de bord HTML autonome.
DASHBOARD_HTML: Path = BASE_DIR / "leads_scores.html"

#: Sortie de ``emailer.py`` : séquence de 3 emails par lead.
CAMPAIGN_CSV: Path = BASE_DIR / "campagne_wifi_polynesie.csv"

#: Sortie de ``enricher.py`` : campagne + adresse email de contact du site.
ENRICHED_CSV: Path = BASE_DIR / "campagne_wifi_enrichie.csv"

#: Sortie de ``enricher_hunter.py`` : campagne + email trouvé via Hunter.io.
FINAL_CSV: Path = BASE_DIR / "campagne_wifi_final.csv"


# ---------------------------------------------------------------------------
# Colonnes partagées entre les étapes du tunnel
# ---------------------------------------------------------------------------
#: Colonnes produites par le scraper.
#:
#: Les quatre dernières colonnes sont propres aux décideurs LinkedIn ; elles
#: restent vides pour les entreprises issues de Google Maps. Elles sont ajoutées
#: en fin de liste pour ne pas modifier l'ordre des colonnes historiques.
LEAD_COLUMNS: List[str] = [
    "nom",
    "categorie",
    "adresse",
    "ville",
    "ile",
    "telephone",
    "site_web",
    "email",
    "note_google",
    "nb_avis",
    "url_google_maps",
    "poste",
    "entreprise",
    "profil_linkedin",
    "source",
]

#: Colonnes ajoutées par le scorer.
SCORE_COLUMNS: List[str] = [
    "score_achat",
    "probabilite_achat",
    "cas_usage",
    "justification",
    "priorite",
]

#: Colonnes produites par l'emailer (une cellule = un email complet).
EMAIL_COLUMNS: List[str] = [
    "nom",
    "email_1",
    "email_2",
    "email_3",
]

#: Colonnes produites par enricher.py : la campagne + l'email du site.
ENRICHED_COLUMNS: List[str] = EMAIL_COLUMNS + ["email_contact"]


# ---------------------------------------------------------------------------
# Journalisation
# ---------------------------------------------------------------------------
def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure la journalisation console et renvoie le logger du tunnel.

    Args:
        level: Niveau de journalisation (``logging.INFO`` par défaut).

    Returns:
        Le logger nommé ``"prospection_wifi"``, prêt à l'emploi.
    """
    logger = logging.getLogger("prospection_wifi")
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


# ---------------------------------------------------------------------------
# Chargement de la configuration
# ---------------------------------------------------------------------------
def load_env() -> None:
    """Charge le fichier ``.env`` s'il existe.

    Les variables déjà présentes dans l'environnement du processus ne sont
    jamais écrasées (``override=False``), ce qui permet de surcharger une clé
    ponctuellement depuis le shell.
    """
    if load_dotenv is None:
        return
    if ENV_FILE.is_file():
        load_dotenv(dotenv_path=ENV_FILE, override=False)


def get_api_key(name: str, *, required: bool = True) -> Optional[str]:
    """Lit une clé d'API dans l'environnement.

    Args:
        name: Nom de la variable d'environnement (ex. ``"APIFY_API_KEY"``).
        required: Si ``True``, lève une erreur quand la clé est absente.

    Returns:
        La valeur de la clé, ou ``None`` si elle est absente et non requise.

    Raises:
        RuntimeError: Si ``required`` vaut ``True`` et que la clé est vide.
    """
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    if required:
        raise RuntimeError(
            f"Variable d'environnement manquante : {name}. "
            f"Copiez env.example.txt en .env puis renseignez la clé."
        )
    return None


def get_option(name: str, default: str) -> str:
    """Lit une option facultative dans l'environnement.

    Args:
        name: Nom de la variable d'environnement.
        default: Valeur de repli si la variable est absente ou vide.

    Returns:
        La valeur trouvée, ou ``default``.
    """
    value = (os.getenv(name) or "").strip()
    return value or default


# ---------------------------------------------------------------------------
# Utilitaires CSV
# ---------------------------------------------------------------------------
def write_csv_atomic(
    path: Path,
    rows: Iterable[Dict[str, Any]],
    columns: List[str],
) -> int:
    """Écrit une liste de dictionnaires dans un CSV, de façon atomique.

    Le fichier est d'abord écrit dans un fichier temporaire du même dossier,
    puis renommé : une interruption ne laisse jamais un CSV tronqué.

    Args:
        path: Chemin du CSV de destination.
        rows: Lignes à écrire (dictionnaires dont les clés sont les colonnes).
        columns: Ordre des colonnes à écrire.

    Returns:
        Le nombre de lignes écrites.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=columns, extrasaction="ignore"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in columns})
                count += 1
        os.replace(tmp_name, path)
    except BaseException:
        # Nettoyage du fichier temporaire en cas d'échec.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return count


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Lit un CSV en liste de dictionnaires (valeurs texte, vides normalisées).

    Args:
        path: Chemin du CSV à lire.

    Returns:
        Les lignes du fichier ; liste vide si le fichier n'existe pas.

    Raises:
        RuntimeError: Si le fichier existe mais ne peut pas être lu.
    """
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows: List[Dict[str, str]] = []
            for raw in reader:
                rows.append(
                    {
                        (key or "").strip(): (value or "").strip()
                        for key, value in raw.items()
                    }
                )
            return rows
    except (OSError, csv.Error) as exc:
        raise RuntimeError(f"Lecture impossible du CSV {path} : {exc}") from exc
