"""Étape 1 du tunnel : récupération des leads via l'API Apify.

Deux sources sont disponibles :

* ``google-maps`` (défaut) : acteur Apify « Google Maps Scraper »
  (``compass/crawler-google-places``) pour les hôtels / restaurants de
  Polynésie française ;
* ``linkedin`` : acteur Apify « LinkedIn Profile Scraper »s
  (``dev_fusion/linkedin-profile-scraper`` par défaut) pour enrichir des
  profils de décideurs (nom, poste, entreprise, email, URL LinkedIn).

Utilisation
-----------
    python scraper.py                                   # 3 entreprises
    python scraper.py --limit 10                        # 10 entreprises
    python scraper.py --dry-run                         # simulation
    python scraper.py --source linkedin \\
        --linkedin-url https://www.linkedin.com/in/exemple --limit 5
    python scraper.py --source all --linkedin-url ...    # Maps + LinkedIn

Les clés ``APIFY_API_KEY`` (Google Maps) et ``LINKEDIN_API_KEY`` (LinkedIn)
sont lues depuis ``.env`` (voir ``env.example.txt``). La zone Google Maps est
configurable via ``APIFY_LOCATION_QUERY`` (par défaut « Papeete, Polynésie
française »). Les URLs LinkedIn peuvent aussi être fournies via
``LINKEDIN_PROFILE_URLS`` (séparées par des virgules). Les colonnes écrites sont
exactement ``LEAD_COLUMNS`` définies dans ``config.py``, afin que ``scorer.py``
et ``emailer.py`` puissent enchaîner sans retraitement.

Le module est importable sans réseau : aucun appel HTTP n'est effectué à
l'import, et les fonctions ``run_actor`` / ``scrape`` acceptent un objet
``session`` injectable pour les tests hors ligne.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from config import (
    LEAD_COLUMNS,
    LEADS_CSV,
    get_api_key,
    get_option,
    load_env,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
#: URL de base de l'API Apify (v2).
APIFY_BASE_URL = "https://api.apify.com/v2"

#: Acteur utilisé par défaut (surchargeable via ``APIFY_ACTOR_ID``).
DEFAULT_ACTOR_ID = "compass/crawler-google-places"

#: Nombre d'entreprises à récupérer par défaut.
DEFAULT_LIMIT = 3

#: Termes de recherche Google Maps (la zone est définie séparément).
DEFAULT_SEARCHES: List[str] = [
    "hôtel",
    "restaurant",
]

#: Localisation Google Maps par défaut (une seule localisation par run).
DEFAULT_LOCATION_QUERY = "Papeete, Polynésie française"

#: Délai maximal d'attente d'un run Apify (secondes).
RUN_TIMEOUT_SECONDS = 300

#: Intervalle de scrutation de l'état d'un run (secondes).
POLL_INTERVAL_SECONDS = 5

#: Correspondance entre les clés de l'acteur Apify et nos colonnes CSV.
#: Plusieurs clés candidates sont testées dans l'ordre (les acteurs Google
#: Maps font évoluer leurs noms de champs au fil des versions).
FIELD_MAP: Dict[str, List[str]] = {
    "nom": ["title", "name"],
    "categorie": ["categoryName", "category", "categories"],
    "adresse": ["address", "street", "fullAddress"],
    "ville": ["city", "neighborhood"],
    "ile": ["state", "region"],
    "telephone": ["phone", "phoneUnformatted"],
    "site_web": ["website", "url"],
    "email": ["emails", "email"],
    "note_google": ["totalScore", "rating"],
    "nb_avis": ["reviewsCount", "reviews"],
    "url_google_maps": ["url", "googleMapsUrl"],
}

#: Sources acceptées par la ligne de commande.
SOURCE_GOOGLE_MAPS = "google-maps"
SOURCE_LINKEDIN = "linkedin"
SOURCE_ALL = "all"
SOURCES: List[str] = [SOURCE_GOOGLE_MAPS, SOURCE_LINKEDIN, SOURCE_ALL]

#: Valeurs écrites dans la colonne ``source``.
SOURCE_LABEL_GOOGLE_MAPS = "google_maps"
SOURCE_LABEL_LINKEDIN = "linkedin"

#: Acteur LinkedIn utilisé par défaut (surchargeable via ``LINKEDIN_ACTOR_ID``).
DEFAULT_LINKEDIN_ACTOR_ID = "dev_fusion/linkedin-profile-scraper"

#: Format accepté pour une URL de profil LinkedIn public.
LINKEDIN_PROFILE_PATTERN = re.compile(
    r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^\s/?#]+/?$",
    re.IGNORECASE,
)

#: Correspondance entre les clés de l'acteur LinkedIn et nos colonnes CSV.
#: L'acteur ``dev_fusion`` renvoie ``fullName`` / ``headline`` / ``companyName``,
#: mais d'autres acteurs utilisent des noms différents : on teste donc plusieurs
#: candidats, du plus spécifique au plus générique.
LINKEDIN_FIELD_MAP: Dict[str, List[str]] = {
    "nom": ["fullName", "full_name", "name", "profileName"],
    "poste": ["headline", "jobTitle", "position", "occupation", "title"],
    "entreprise": [
        "companyName",
        "currentCompany",
        "company",
        "companyTitle",
        "organizationName",
    ],
    "ville": [
        "locationName",
        "city",
        "addressWithCountry",
        "location",
        "geoLocationName",
    ],
    "email": ["email", "emailAddress", "emails", "contactEmail"],
    "telephone": ["phone", "phoneNumber", "mobileNumber", "contactPhone"],
    "site_web": ["companyWebsite", "website", "companyWebUrl"],
    "profil_linkedin": [
        "linkedinUrl",
        "linkedinPublicUrl",
        "profileUrl",
        "linkedinProfileUrl",
        "url",
    ],
    "categorie": ["companyIndustry", "industry", "sector", "companySector"],
}

#: Colonnes propres aux décideurs LinkedIn (vides pour Google Maps).
LINKEDIN_ONLY_COLUMNS: List[str] = [
    "poste",
    "entreprise",
    "profil_linkedin",
    "source",
]


# ---------------------------------------------------------------------------
# Fonctions utilitaires
# ---------------------------------------------------------------------------
def _first_value(item: Dict[str, Any], keys: Iterable[str]) -> str:
    """Renvoie la première valeur non vide trouvée parmi ``keys``.

    Args:
        item: Élément brut renvoyé par Apify.
        keys: Clés candidates, testées dans l'ordre.

    Returns:
        La valeur convertie en texte (liste jointe par ``"; "``), ou ``""``.
    """
    for key in keys:
        if key not in item:
            continue
        value = item[key]
        if value is None:
            continue
        if isinstance(value, dict):
            # Champs imbriqués fréquents : {"company": {"name": "..."}}.
            text = _first_value(
                value,
                (
                    "name",
                    "title",
                    "text",
                    "value",
                    "fullName",
                    "companyName",
                    "email",
                    "phone",
                ),
            )
            if text:
                return text
            continue
        if isinstance(value, (list, tuple)):
            parts: List[str] = []
            for element in value:
                if isinstance(element, dict):
                    text = _first_value(
                        element,
                        (
                            "name",
                            "title",
                            "text",
                            "value",
                            "fullName",
                            "companyName",
                            "email",
                            "phone",
                        ),
                    )
                else:
                    text = str(element).strip()
                if text and text not in parts:
                    parts.append(text)
            if parts:
                return "; ".join(parts)
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_item(item: Dict[str, Any]) -> Dict[str, str]:
    """Convertit un élément Google Maps en ligne conforme à ``LEAD_COLUMNS``.

    Args:
        item: Élément brut renvoyé par l'acteur.

    Returns:
        Un dictionnaire dont les clés sont exactement ``LEAD_COLUMNS``.
    """
    row: Dict[str, str] = {column: "" for column in LEAD_COLUMNS}
    for column in FIELD_MAP:
        row[column] = _first_value(item, FIELD_MAP[column])
    row["source"] = SOURCE_LABEL_GOOGLE_MAPS
    return row


def _first_experience(item: Dict[str, Any]) -> Dict[str, Any]:
    """Renvoie la première expérience professionnelle d'un profil LinkedIn.

    Les acteurs LinkedIn utilisent ``experiences``, ``experience`` ou
    ``workExperience`` selon leur version. La première entrée sert de repli pour
    le poste et l'entreprise lorsque les champs de premier niveau manquent.
    """
    for key in ("experiences", "experience", "workExperience", "positions"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, (list, tuple)):
            for element in value:
                if isinstance(element, dict):
                    return element
    return {}


def _nested_value(item: Dict[str, Any], paths: Iterable[Iterable[str]]) -> str:
    """Cherche la première valeur texte le long de chemins imbriqués.

    Exemple : ``(("company", "website"), ("contact", "email"))``.
    """
    for path in paths:
        current: Any = item
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is None:
            continue
        if isinstance(current, dict):
            text = _first_value(current, ("name", "title", "text", "value"))
        elif isinstance(current, (list, tuple)):
            text = _first_value({"value": current}, ["value"])
        else:
            text = str(current).strip()
        if text:
            return text
    return ""


def normalize_linkedin_item(item: Dict[str, Any]) -> Dict[str, str]:
    """Convertit un profil LinkedIn Apify en ligne ``LEAD_COLUMNS``.

    Le nom et le prénom sont fusionnés si l'acteur ne fournit pas ``fullName``.
    Le poste et l'entreprise sont cherchés au premier niveau puis, en repli,
    dans la première expérience professionnelle.
    """
    row: Dict[str, str] = {column: "" for column in LEAD_COLUMNS}
    experience = _first_experience(item)

    row["nom"] = _first_value(item, LINKEDIN_FIELD_MAP["nom"])
    if not row["nom"]:
        first_name = _first_value(item, ["firstName", "first_name"])
        last_name = _first_value(item, ["lastName", "last_name"])
        row["nom"] = " ".join(
            part for part in (first_name, last_name) if part
        ).strip()

    row["poste"] = _first_value(
        item, LINKEDIN_FIELD_MAP["poste"]
    ) or _first_value(experience, ["title", "position", "role", "jobTitle"])
    row["entreprise"] = _first_value(
        item, LINKEDIN_FIELD_MAP["entreprise"]
    ) or _first_value(
        experience,
        [
            "companyName",
            "company",
            "companyTitle",
            "organization",
            "organizationName",
        ],
    )
    row["ville"] = _first_value(
        item, LINKEDIN_FIELD_MAP["ville"]
    ) or _nested_value(
        item,
        (
            ("location", "city"),
            ("location", "name"),
            ("location", "locationName"),
        ),
    )
    row["email"] = _first_value(
        item, LINKEDIN_FIELD_MAP["email"]
    ) or _nested_value(item, (("contact", "email"), ("contact", "emailAddress")))
    row["telephone"] = _first_value(
        item, LINKEDIN_FIELD_MAP["telephone"]
    ) or _nested_value(
        item, (("contact", "phone"), ("contact", "phoneNumber"))
    )
    row["site_web"] = _first_value(
        item, LINKEDIN_FIELD_MAP["site_web"]
    ) or _nested_value(item, (("company", "website"), ("company", "url")))
    row["profil_linkedin"] = _first_value(
        item, LINKEDIN_FIELD_MAP["profil_linkedin"]
    )
    row["categorie"] = (
        _first_value(item, LINKEDIN_FIELD_MAP["categorie"]) or "Décideur LinkedIn"
    )
    row["source"] = SOURCE_LABEL_LINKEDIN
    return row


def _google_key(row: Dict[str, str]) -> str:
    """Clé de déduplication d'une entreprise Google Maps."""
    return (row.get("nom") or "").strip().lower()


def _linkedin_key(row: Dict[str, str]) -> str:
    """Clé de déduplication d'un décideur LinkedIn."""
    profile = (row.get("profil_linkedin") or "").strip().lower()
    if profile:
        return profile
    return (
        f"{(row.get('nom') or '').strip().lower()}|"
        f"{(row.get('entreprise') or '').strip().lower()}"
    )


def _lead_key(row: Dict[str, str]) -> str:
    """Clé de fusion utilisée lors de l'ajout au CSV existant."""
    if row.get("profil_linkedin"):
        return _linkedin_key(row)
    if row.get("url_google_maps"):
        return f"maps|{(row.get('url_google_maps') or '').strip().lower()}"
    return (
        f"{(row.get('source') or '').strip().lower()}|"
        f"{(row.get('nom') or '').strip().lower()}|"
        f"{(row.get('entreprise') or '').strip().lower()}|"
        f"{(row.get('ville') or '').strip().lower()}"
    )


def _merge_rows(
    existing: Iterable[Dict[str, str]], new_rows: Iterable[Dict[str, str]]
) -> List[Dict[str, str]]:
    """Fusionne deux jeux de lignes en dédupliquant par ``_lead_key``.

    Les nouvelles lignes remplacent les anciennes à clé identique, mais gardent
    leur position d'origine pour conserver un CSV stable.
    """
    merged: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    for row in list(existing) + list(new_rows):
        key = _lead_key(row)
        if key not in merged:
            order.append(key)
        merged[key] = row
    return [merged[key] for key in order]


def build_actor_input(
    limit: int,
    searches: Optional[List[str]] = None,
    location_query: Optional[str] = None,
) -> Dict[str, Any]:
    """Construit la charge utile envoyée à l'acteur Apify.

    L'acteur Google Maps attend au minimum ``searchStringsArray`` et
    ``locationQuery``. Sans localisation, il peut ignorer la zone demandée ou
    échouer avec ``400 run-failed``.

    Args:
        limit: Nombre total d'établissements souhaités.
        searches: Termes de recherche ; ``DEFAULT_SEARCHES`` si ``None``.
        location_query: Localisation Google Maps ; ``DEFAULT_LOCATION_QUERY``
            si ``None`` ou vide.

    Returns:
        Le dictionnaire d'entrée de l'acteur.
    """
    terms = searches or DEFAULT_SEARCHES
    location = (location_query or "").strip() or DEFAULT_LOCATION_QUERY
    # On répartit la limite sur les recherches, au moins 1 par terme.
    per_search = max(1, -(-limit // max(1, len(terms))))
    return {
        "searchStringsArray": terms,
        "locationQuery": location,
        "maxCrawledPlacesPerSearch": per_search,
        "language": "fr",
        "countryCode": "pf",
        "skipClosedPlaces": True,
        "scrapePlaceDetailPage": False,
    }


def build_linkedin_actor_input(urls: List[str]) -> Dict[str, Any]:
    """Construit la charge utile de l'acteur LinkedIn Profile Scraper.

    Args:
        urls: URLs de profils LinkedIn déjà validées.

    Returns:
        Le dictionnaire d'entrée de l'acteur (``profileUrls``).
    """
    return {"profileUrls": list(urls)}


def parse_linkedin_urls(raw_urls: Iterable[str]) -> List[str]:
    """Valide, normalise et déduplique une liste d'URLs de profils LinkedIn.

    Accepte les URLs avec ou sans schéma (``www.linkedin.com/in/...``) et les
    valeurs séparées par des virgules. Les entrées invalides sont ignorées.
    """
    valid: List[str] = []
    seen: set[str] = set()
    for raw in raw_urls:
        for candidate in str(raw).split(","):
            candidate = candidate.strip()
            if not candidate:
                continue
            if "://" not in candidate:
                candidate = "https://" + candidate
            candidate = candidate.split("?", 1)[0].split("#", 1)[0]
            if not LINKEDIN_PROFILE_PATTERN.match(candidate):
                continue
            url = candidate.rstrip("/")
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            valid.append(url)
    return valid


def _resolve_linkedin_urls(linkedin_urls: Optional[Iterable[str]]) -> List[str]:
    """Résout les URLs LinkedIn depuis la CLI ou ``LINKEDIN_PROFILE_URLS``."""
    logger = setup_logging()
    if linkedin_urls:
        raw = list(linkedin_urls)
    else:
        raw = [get_option("LINKEDIN_PROFILE_URLS", "")]
    urls = parse_linkedin_urls(raw)
    valid_keys = {url.lower() for url in urls}
    ignored = []
    for value in raw:
        for candidate in str(value).split(","):
            candidate = candidate.strip()
            if not candidate:
                continue
            normalized = candidate
            if "://" not in normalized:
                normalized = "https://" + normalized
            normalized = (
                normalized.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
            )
            if normalized not in valid_keys and candidate not in ignored:
                ignored.append(candidate)
    if ignored:
        logger.warning(
            "URL(s) LinkedIn ignorée(s), format attendu "
            "https://www.linkedin.com/in/<slug> : %s",
            ", ".join(ignored),
        )
    return urls


def get_linkedin_api_key() -> str:
    """Lit la clé Apify dédiée à LinkedIn avec un message d'erreur explicite."""
    try:
        return get_api_key("LINKEDIN_API_KEY")
    except RuntimeError as exc:
        raise RuntimeError(
            "Clé LinkedIn manquante : renseignez LINKEDIN_API_KEY dans .env "
            "(token Apify autorisé pour l'acteur LinkedIn ; il peut être "
            "identique à APIFY_API_KEY)."
        ) from exc


def _normalize_items(
    items: Iterable[Dict[str, Any]],
    limit: int,
    normalizer: Any,
    key_func: Any,
) -> List[Dict[str, str]]:
    """Normalise et déduplique les éléments d'un dataset Apify."""
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        row = normalizer(item)
        if not row.get("nom"):
            continue
        key = key_func(row)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------------------
# Appels Apify
# ---------------------------------------------------------------------------
def run_actor(
    api_key: str,
    actor_id: str,
    actor_input: Dict[str, Any],
    *,
    session: Optional[requests.Session] = None,
    timeout: int = RUN_TIMEOUT_SECONDS,
    poll_interval: int = POLL_INTERVAL_SECONDS,
    key_env: str = "APIFY_API_KEY",
) -> List[Dict[str, Any]]:
    """Lance un run Apify synchrone et renvoie les éléments du dataset.

    L'appel utilise l'endpoint ``runs/sync`` : Apify bloque jusqu'à la fin du
    run (ou jusqu'à ``timeout``) et renvoie directement les éléments.

    Args:
        api_key: Token Apify.
        actor_id: Identifiant de l'acteur (``utilisateur/acteur``).
        actor_input: Charge utile d'entrée de l'acteur.
        session: Session HTTP injectable (tests hors ligne).
        timeout: Délai maximal d'attente en secondes.
        poll_interval: Conservé pour compatibilité (non utilisé en mode sync).
        key_env: Nom de la variable d'environnement citée dans l'erreur 401.

    Returns:
        La liste des éléments bruts du dataset.

    Raises:
        RuntimeError: En cas d'erreur réseau, d'authentification ou d'API.
    """
    del poll_interval  # mode synchrone : pas de scrutation
    http = session or requests.Session()
    # L'API Apify attend « utilisateur~acteur » dans le chemin, pas « / ».
    actor_path = actor_id.strip().replace("/", "~")
    url = f"{APIFY_BASE_URL}/acts/{actor_path}/run-sync-get-dataset-items"
    params = {"token": api_key, "timeout": timeout, "format": "json"}

    try:
        response = http.post(
            url, params=params, json=actor_input, timeout=timeout + 30
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Appel Apify impossible : {exc}") from exc

    if response.status_code == 401:
        raise RuntimeError(
            f"Apify a refusé le token (401). Vérifiez {key_env} dans .env."
        )
    if response.status_code == 402:
        raise RuntimeError(
            "Crédits Apify insuffisants (402). Rechargez votre compte Apify."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Erreur Apify {response.status_code} : {response.text[:300]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Réponse Apify illisible (JSON invalide).") from exc

    if isinstance(payload, dict):
        # Certains endpoints encapsulent les données dans {"data": {"items": [...]}}.
        data = payload.get("data", payload)
        if isinstance(data, dict):
            items = data.get("items", [])
        else:
            items = data
    else:
        items = payload

    if not isinstance(items, list):
        raise RuntimeError("Format de réponse Apify inattendu (liste attendue).")
    return items


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _fetch_google_maps(
    limit: int,
    api_key: str,
    *,
    actor_id: str,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, str]]:
    """Interroge l'acteur Google Maps et renvoie les entreprises normalisées."""
    logger = setup_logging()
    actor_input = build_actor_input(
        limit,
        location_query=get_option("APIFY_LOCATION_QUERY", DEFAULT_LOCATION_QUERY),
    )
    logger.info(
        "Lancement de l'acteur Google Maps %s (limite %d, localisation %s)…",
        actor_id,
        limit,
        actor_input["locationQuery"],
    )
    started = time.monotonic()
    items = run_actor(api_key, actor_id, actor_input, session=session)
    logger.info(
        "%d élément(s) Google Maps reçu(s) en %.1f s.",
        len(items),
        time.monotonic() - started,
    )
    rows = _normalize_items(items, limit, normalize_item, _google_key)
    if not rows:
        logger.warning("Aucune entreprise exploitable renvoyée par Google Maps.")
    return rows


def _fetch_linkedin(
    limit: int,
    api_key: str,
    urls: List[str],
    *,
    actor_id: str,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, str]]:
    """Interroge l'acteur LinkedIn et renvoie les décideurs normalisés."""
    logger = setup_logging()
    selected = urls[:limit]
    actor_input = build_linkedin_actor_input(selected)
    logger.info(
        "Lancement de l'acteur LinkedIn %s (%d profil(s))…",
        actor_id,
        len(selected),
    )
    started = time.monotonic()
    items = run_actor(
        api_key,
        actor_id,
        actor_input,
        session=session,
        key_env="LINKEDIN_API_KEY",
    )
    logger.info(
        "%d élément(s) LinkedIn reçu(s) en %.1f s.",
        len(items),
        time.monotonic() - started,
    )
    rows = _normalize_items(items, limit, normalize_linkedin_item, _linkedin_key)
    if not rows:
        logger.warning("Aucun décideur exploitable renvoyé par LinkedIn.")
    return rows


def scrape(
    limit: int = DEFAULT_LIMIT,
    *,
    dry_run: bool = False,
    source: str = SOURCE_GOOGLE_MAPS,
    linkedin_urls: Optional[Iterable[str]] = None,
    append: Optional[bool] = None,
    output_path: Path = LEADS_CSV,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, str]]:
    """Récupère des leads Apify et les écrit dans ``output_path``.

    Args:
        limit: Nombre de leads à conserver par source.
        dry_run: Si ``True``, n'appelle pas Apify et n'écrit rien.
        source: ``google-maps``, ``linkedin`` ou ``all``.
        linkedin_urls: URLs de profils LinkedIn. Si vide, l'option
            ``LINKEDIN_PROFILE_URLS`` est utilisée.
        append: Conserve les lignes existantes. Par défaut, l'ajout est actif
            pour LinkedIn et ``all``, et désactivé pour Google Maps seul.
        output_path: CSV de destination (injectable pour les tests).
        session: Session HTTP injectable (tests hors ligne).

    Returns:
        Les nouvelles lignes normalisées (hors lignes déjà présentes).

    Raises:
        ValueError: Si ``limit`` ou ``source`` est invalide.
        RuntimeError: Si une clé ou une URL LinkedIn manque, ou si Apify échoue.
    """
    logger = setup_logging()
    load_env()

    if limit < 1:
        raise ValueError("limit doit être un entier supérieur ou égal à 1.")
    if source not in SOURCES:
        raise ValueError(
            f"Source inconnue : {source} (attendu : {', '.join(SOURCES)})."
        )

    google_source = source in (SOURCE_GOOGLE_MAPS, SOURCE_ALL)
    linkedin_source = source in (SOURCE_LINKEDIN, SOURCE_ALL)
    if append is None:
        append = source in (SOURCE_LINKEDIN, SOURCE_ALL)

    urls = _resolve_linkedin_urls(linkedin_urls) if linkedin_source else []
    google_actor_id = get_option("APIFY_ACTOR_ID", DEFAULT_ACTOR_ID)
    linkedin_actor_id = get_option(
        "LINKEDIN_ACTOR_ID", DEFAULT_LINKEDIN_ACTOR_ID
    )

    if dry_run:
        logger.info("Mode --dry-run : aucun appel réseau, aucune écriture.")
        if google_source:
            logger.info("Source : Google Maps")
            logger.info("Acteur : %s", google_actor_id)
            logger.info(
                "Entrée : %s",
                build_actor_input(
                    limit,
                    location_query=get_option(
                        "APIFY_LOCATION_QUERY", DEFAULT_LOCATION_QUERY
                    ),
                ),
            )
        if linkedin_source:
            logger.info("Source : LinkedIn")
            logger.info("Acteur : %s", linkedin_actor_id)
            if urls:
                logger.info(
                    "Entrée : %s", build_linkedin_actor_input(urls[:limit])
                )
            else:
                logger.warning(
                    "Aucune URL LinkedIn fournie : utilisez --linkedin-url "
                    "ou LINKEDIN_PROFILE_URLS."
                )
        return []

    if linkedin_source and not urls:
        raise RuntimeError(
            "Aucune URL de profil LinkedIn fournie. Utilisez --linkedin-url "
            "https://www.linkedin.com/in/<slug> (répétable ou séparé par des "
            "virgules) ou renseignez LINKEDIN_PROFILE_URLS dans .env."
        )

    # Valider toutes les clés avant le premier appel réseau : évite un run
    # Google Maps inutile si la clé LinkedIn manque pour --source all.
    google_key = get_api_key("APIFY_API_KEY") if google_source else ""
    linkedin_key = get_linkedin_api_key() if linkedin_source else ""

    new_rows: List[Dict[str, str]] = []
    if google_source:
        new_rows.extend(
            _fetch_google_maps(
                limit, google_key, actor_id=google_actor_id, session=session
            )
        )
    if linkedin_source:
        new_rows.extend(
            _fetch_linkedin(
                limit,
                linkedin_key,
                urls,
                actor_id=linkedin_actor_id,
                session=session,
            )
        )

    if not new_rows:
        logger.warning("Aucun lead exploitable renvoyé par Apify.")
        return []

    rows_to_write = (
        _merge_rows(read_csv_rows(output_path), new_rows) if append else new_rows
    )
    written = write_csv_atomic(output_path, rows_to_write, LEAD_COLUMNS)
    logger.info(
        "%d lead(s) écrit(s) dans %s (%d nouveau(x)).",
        written,
        output_path,
        len(new_rows),
    )
    return new_rows


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée en ligne de commande.

    Args:
        argv: Arguments (``sys.argv[1:]`` par défaut).

    Returns:
        Code de sortie du processus (0 = succès).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Récupère des entreprises (Google Maps) ou des décideurs (LinkedIn) "
            "en Polynésie via Apify."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=(
            f"Nombre de leads à récupérer par source (défaut : {DEFAULT_LIMIT})."
        ),
    )
    parser.add_argument(
        "--source",
        choices=SOURCES,
        default=SOURCE_GOOGLE_MAPS,
        help=(
            "Source des leads : google-maps (défaut), linkedin, ou all "
            "(Google Maps + LinkedIn)."
        ),
    )
    parser.add_argument(
        "--linkedin-url",
        "--linkedin-urls",
        dest="linkedin_urls",
        action="append",
        metavar="URL",
        help=(
            "URL de profil LinkedIn (répétable ou séparée par des virgules). "
            "Si absent, LINKEDIN_PROFILE_URLS est utilisé."
        ),
    )
    append_group = parser.add_mutually_exclusive_group()
    append_group.add_argument(
        "--append",
        dest="append",
        action="store_true",
        help=(
            "Conserve les lignes existantes du CSV (défaut pour linkedin et all)."
        ),
    )
    append_group.add_argument(
        "--replace",
        dest="append",
        action="store_false",
        help="Remplace le CSV au lieu de fusionner avec les lignes existantes.",
    )
    parser.set_defaults(append=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche la requête sans appeler Apify ni écrire de CSV.",
    )
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit doit être un entier supérieur ou égal à 1.")

    try:
        rows = scrape(
            args.limit,
            dry_run=args.dry_run,
            source=args.source,
            linkedin_urls=args.linkedin_urls,
            append=args.append,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    if rows:
        print(f"{len(rows)} lead(s) enregistré(s) dans {LEADS_CSV.name} :")
        for row in rows:
            if row.get("source") == SOURCE_LABEL_LINKEDIN:
                print(
                    f"  - {row['nom']} ({row['poste'] or 'n/a'}) — "
                    f"{row['entreprise'] or row['ville'] or 'n/a'}"
                )
            else:
                print(
                    f"  - {row['nom']} ({row['categorie'] or 'n/a'}) — "
                    f"{row['ville']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
