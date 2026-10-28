"""Étape 2 du tunnel : scoring des leads via l'API TypeSafe (modèle Jev).

Ce script lit ``leads_polynesie.csv`` (sortie de ``scraper.py``), interroge
l'API TypeSafe ``POST /v1/systemone`` avec le modèle **Jev**, puis écrit
``leads_scores.csv`` trié par probabilité d'achat décroissante.

Utilisation
-----------
    python scorer.py                  # score tous les leads
    python scorer.py --limit 10       # score les 10 premiers leadss
    python scorer.py --dry-run        # affiche la requête sans appeler l'API

La clé ``TYPESAFE_API_KEY`` est lue depuis le fichier ``.env`` (voir
``env.example.txt``). Le modèle Jev est désigné par l'identifiant API
``jev-latest`` ; il est surchargeable via ``TYPESAFE_MODEL`` (par exemple
``jev-1.13.0`` ou l'alias ``jev``).

Notes
-----
* Jev ne génère pas de texte : il renvoie des décisions typées (``choice``,
  ``score``, ``noul``). La colonne ``justification`` est donc composée dans ce
  module à partir de la décision, des probabilités et de la confiance.
* Les consignes (``instructions``) sont en anglais car Jev est principalement
  entraîné dans cette langue ; les libellés du CSV restent en français.
* Aucun appel réseau n'est effectué à l'import. ``call_system_one`` accepte une
  ``session`` HTTP injectable pour les tests hors ligne.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import (
    LEAD_COLUMNS,
    LEADS_CSV,
    SCORE_COLUMNS,
    SCORES_CSV,
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
#: URL de base de l'API TypeSafe.
TYPESAFE_BASE_URL = "https://api.typesafe.ai"

#: Endpoint System One : un état + plusieurs questions par requête.
SYSTEM_ONE_PATH = "/v1/systemone"

#: Modèle Jev. L'identifiant API officiel est ``jev-latest`` (alias ``jev``).
DEFAULT_MODEL = "jev-latest"

#: Délai maximal d'une requête HTTP (secondes).
REQUEST_TIMEOUT_SECONDS = 60

#: Nombre total de tentatives en cas d'erreur réseau, 5xx ou 429.
MAX_ATTEMPTS = 3

#: Délai de base du backoff exponentiel (secondes).
RETRY_BACKOFF_SECONDS = 1.5

#: Plafond du délai imposé par l'en-tête ``Retry-After`` (secondes).
MAX_RETRY_AFTER_SECONDS = 30.0

#: Seuil de probabilité (en %) pour une priorité « haute ».
HIGH_PRIORITY_SCORE = 70

#: Seuil de probabilité (en %) pour une priorité « moyenne ».
MEDIUM_PRIORITY_SCORE = 40

#: Confiance minimale exigée pour conserver une priorité « haute ».
HIGH_PRIORITY_MIN_CONFIDENCE = 0.50

#: Description de l'offre envoyée à Jev pour contextualiser la décision.
OFFER_DESCRIPTION = (
    "Guest Wi-Fi marketing for hotels and restaurants: branded captive "
    "portal, consented e-mail capture, automated Google review requests, "
    "guest retargeting and audience analytics."
)

#: Cas d'usage proposés au modèle. Les clés sont les valeurs écrites dans le
#: CSV ; les descriptions aident Jev à distinguer les options.
USE_CASES: Dict[str, str] = {
    "Collecte d'emails": (
        "Capture consented customer e-mail addresses through a guest Wi-Fi "
        "portal to build a marketing list."
    ),
    "Boost d'avis Google": (
        "Automatically ask guests to leave a Google review after they connect, "
        "to improve local reputation and ranking."
    ),
    "Fidélisation clients": (
        "Recognise returning guests and send them targeted offers or loyalty "
        "messages after their visit."
    ),
    "Promotion locale": (
        "Promote the venue's events, menus or last-minute offers to guests "
        "while they are on site."
    ),
    "Analyse d'audience": (
        "Measure footfall, repeat visits and guest demographics from Wi-Fi "
        "connection data."
    ),
    "Aucun": (
        "None of these use cases is relevant: the business is too small, "
        "already equipped, or shows no plausible need."
    ),
}

#: Identifiants des questions (jamais envoyés au modèle, utilisés dans la
#: réponse). Ils reprennent les noms de colonnes de ``SCORE_COLUMNS``.
QUESTION_BUY = "probabilite_achat"
QUESTION_USE_CASE = "cas_usage"

#: Correspondance entre les colonnes du CSV et les clés de l'état JSON.
STATE_FIELD_MAP: Dict[str, str] = {
    "nom": "name",
    "categorie": "category",
    "adresse": "address",
    "ville": "city",
    "ile": "island",
    "telephone": "phone",
    "site_web": "website",
    "email": "email",
    "note_google": "google_rating",
    "nb_avis": "review_count",
    "url_google_maps": "google_maps_url",
}

#: Colonnes d'une ligne LinkedIn regroupées dans l'objet ``contact`` de l'état.
CONTACT_FIELD_MAP: Dict[str, str] = {
    "nom": "name",
    "poste": "job_title",
    "profil_linkedin": "linkedin_url",
    "email": "email",
    "telephone": "phone",
}


# ---------------------------------------------------------------------------
# Erreur métier
# ---------------------------------------------------------------------------
class TypeSafeError(RuntimeError):
    """Erreur d'appel ou d'exploitation de la réponse TypeSafe.

    Attributes:
        status_code: Code HTTP renvoyé par l'API, si disponible.
        fatal: ``True`` si l'erreur doit interrompre tout le scoring
            (authentification, requête invalide, quota persistant).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fatal = fatal


# ---------------------------------------------------------------------------
# Fonctions utilitaires
# ---------------------------------------------------------------------------
def _as_float(value: Any) -> Optional[float]:
    """Convertit une valeur en flottant, ou renvoie ``None``."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: Any) -> int:
    """Convertit un compteur de tokens en entier, avec repli à zéro."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error_snippet(response: requests.Response) -> str:
    """Renvoie un extrait lisible du corps d'erreur HTTP (sans secret)."""
    text = (getattr(response, "text", "") or "").strip().replace("\n", " ")
    return text[:300] or "(corps vide)"


def _backoff_seconds(attempt: int) -> float:
    """Calcule le délai de backoff exponentiel pour une tentative."""
    return RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _retry_after_seconds(response: requests.Response) -> Optional[float]:
    """Lit l'en-tête ``Retry-After`` (en secondes), s'il est exploitable."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


# ---------------------------------------------------------------------------
# Construction de la requête
# ---------------------------------------------------------------------------
def build_state(lead: Dict[str, str]) -> Dict[str, Any]:
    """Construit l'état JSON envoyé à Jev à partir d'une ligne de leads.

    Pour une ligne Google Maps, ``nom`` désigne l'entreprise. Pour une ligne
    LinkedIn, ``entreprise`` désigne l'entreprise et ``nom`` le décideur : ce
    dernier est alors placé dans un objet ``contact`` avec son poste et son URL
    LinkedIn, afin que Jev évalue l'entreprise et non la personne.

    Args:
        lead: Ligne issue de ``leads_polynesie.csv``.

    Returns:
        Un objet décrivant l'entreprise, l'offre et le marché.
    """
    is_linkedin = (
        str(lead.get("source") or "").strip().lower() == "linkedin"
        or bool(str(lead.get("profil_linkedin") or "").strip())
    )

    business: Dict[str, str] = {}
    for column, state_key in STATE_FIELD_MAP.items():
        if is_linkedin and column in ("nom", "email", "telephone"):
            continue
        value = str(lead.get(column) or "").strip()
        if value:
            business[state_key] = value

    state: Dict[str, Any] = {
        "business": business,
        "offer": OFFER_DESCRIPTION,
        "market": "French Polynesia",
    }

    if is_linkedin:
        company = str(lead.get("entreprise") or "").strip()
        if company:
            business["name"] = company

        contact: Dict[str, str] = {}
        for column, state_key in CONTACT_FIELD_MAP.items():
            value = str(lead.get(column) or "").strip()
            if value:
                contact[state_key] = value
        if contact:
            state["contact"] = contact

    return state


def build_questions() -> Dict[str, Dict[str, Any]]:
    """Construit les questions posées à Jev pour chaque entreprise.

    Returns:
        Un dictionnaire ``identifiant -> question`` conforme à l'API System One.
    """
    return {
        QUESTION_BUY: {
            "type": "choice",
            "instructions": (
                "Will this business purchase a guest Wi-Fi marketing solution "
                "within the next six months? The solution provides a branded "
                "captive portal, consented e-mail capture, automated Google "
                "review requests and guest retargeting for hotels and "
                "restaurants."
            ),
            "criteria": {
                "Oui": (
                    "A purchase is plausible within six months: the business "
                    "shows a clear marketing need, enough guest volume and no "
                    "obvious blocker."
                ),
                "Non": (
                    "A purchase is unlikely within six months: no clear need, "
                    "too few guests, already equipped, or a structural blocker."
                ),
            },
        },
        QUESTION_USE_CASE: {
            "type": "choice",
            "instructions": (
                "Which use case would bring the most value to this business "
                "if it adopted a guest Wi-Fi marketing solution?"
            ),
            "criteria": dict(USE_CASES),
        },
    }


def build_request(lead: Dict[str, str], model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """Construit le corps JSON complet d'une requête System One.

    Args:
        lead: Ligne issue de ``leads_polynesie.csv``.
        model: Identifiant du modèle Jev à utiliser.

    Returns:
        Le dictionnaire ``{"model", "state", "questions"}``.
    """
    return {
        "model": model,
        "state": build_state(lead),
        "questions": build_questions(),
    }


# ---------------------------------------------------------------------------
# Appel de l'API TypeSafe
# ---------------------------------------------------------------------------
def call_system_one(
    api_key: str,
    request_body: Dict[str, Any],
    *,
    session: Optional[requests.Session] = None,
    base_url: str = TYPESAFE_BASE_URL,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Envoie une requête System One à TypeSafe et renvoie la réponse JSON.

    Les erreurs réseau, les 5xx et les 429 sont retentés avec un backoff
    exponentiel. Les erreurs d'authentification et de validation sont
    considérées comme fatales : elles se reproduiraient pour chaque lead.

    Args:
        api_key: Clé ``TYPESAFE_API_KEY``.
        request_body: Corps JSON produit par ``build_request``.
        session: Session HTTP injectable (tests hors ligne).
        base_url: URL de base de l'API (surchargeable pour un gateway).
        timeout: Délai maximal d'une requête (secondes).

    Returns:
        La réponse TypeSafe décodée (dictionnaire).

    Raises:
        TypeSafeError: En cas d'erreur réseau, HTTP ou de réponse illisible.
    """
    logger = setup_logging()
    url = base_url.rstrip("/") + SYSTEM_ONE_PATH
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    owns_session = session is None
    http = session or requests.Session()
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = http.post(
                    url,
                    headers=headers,
                    json=request_body,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise TypeSafeError(
                        "Appel TypeSafe impossible après "
                        f"{MAX_ATTEMPTS} tentatives : {exc}"
                    ) from exc
                delay = _backoff_seconds(attempt)
                logger.warning(
                    "TypeSafe injoignable (%s) ; nouvelle tentative dans %.1f s.",
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue

            status = response.status_code

            if status == 429:
                if attempt >= MAX_ATTEMPTS:
                    raise TypeSafeError(
                        "TypeSafe limite le débit (429) après plusieurs "
                        "tentatives. Réessayez plus tard ou réduisez le nombre "
                        "de leads.",
                        status_code=429,
                        fatal=True,
                    )
                delay = _retry_after_seconds(response) or _backoff_seconds(attempt)
                logger.warning(
                    "TypeSafe 429 ; nouvelle tentative dans %.1f s.", delay
                )
                time.sleep(delay)
                continue

            if 500 <= status <= 599:
                if attempt >= MAX_ATTEMPTS:
                    raise TypeSafeError(
                        f"Erreur serveur TypeSafe ({status}) : "
                        f"{_error_snippet(response)}",
                        status_code=status,
                    )
                delay = _backoff_seconds(attempt)
                logger.warning(
                    "TypeSafe %d ; nouvelle tentative dans %.1f s.",
                    status,
                    delay,
                )
                time.sleep(delay)
                continue

            if status in (401, 403):
                raise TypeSafeError(
                    f"TypeSafe a refusé la clé ({status}). Vérifiez "
                    "TYPESAFE_API_KEY dans .env.",
                    status_code=status,
                    fatal=True,
                )

            if status in (400, 422):
                raise TypeSafeError(
                    f"Requête TypeSafe invalide ({status}) : "
                    f"{_error_snippet(response)}",
                    status_code=status,
                    fatal=True,
                )

            if status >= 300:
                raise TypeSafeError(
                    f"Erreur TypeSafe {status} : {_error_snippet(response)}",
                    status_code=status,
                    fatal=True,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise TypeSafeError(
                    "Réponse TypeSafe illisible (JSON invalide)."
                ) from exc
            if not isinstance(payload, dict):
                raise TypeSafeError(
                    "Format de réponse TypeSafe inattendu (objet JSON attendu)."
                )
            return payload

        raise TypeSafeError("Échec inattendu de l'appel TypeSafe.")
    finally:
        if owns_session:
            http.close()


# ---------------------------------------------------------------------------
# Exploitation de la réponse
# ---------------------------------------------------------------------------
def _required_answer(
    payload: Dict[str, Any], question_id: str
) -> Dict[str, Any]:
    """Récupère la réponse d'une question, ou lève une erreur explicite."""
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise TypeSafeError("Réponse TypeSafe sans dictionnaire « answers ».")
    answer = answers.get(question_id)
    if not isinstance(answer, dict):
        raise TypeSafeError(
            f"Réponse TypeSafe incomplète : question « {question_id} » absente."
        )
    return answer


def _probability(
    probabilities: Any, label: str, default: Optional[float]
) -> Optional[float]:
    """Extrait une probabilité bornée [0, 1] pour un libellé donné."""
    if not isinstance(probabilities, dict):
        return default
    number = _as_float(probabilities.get(label))
    if number is None:
        return default
    if not 0.0 <= number <= 1.0:
        raise TypeSafeError(
            f"Probabilité hors bornes pour « {label} » : {number}."
        )
    return number


def _confidence(value: Any) -> Optional[float]:
    """Normalise la confiance en [0, 1], ou renvoie ``None`` si absente."""
    number = _as_float(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def _format_percent(value: float) -> int:
    """Convertit une probabilité [0, 1] en pourcentage entier."""
    return int(round(value * 100))


def _build_justification(
    choice: str,
    percent: int,
    confidence: Optional[float],
    use_case: str,
    use_case_probability: Optional[float],
) -> str:
    """Compose une justification lisible à partir des décisions typées.

    Jev ne produisant pas de texte libre, cette phrase est déterministe : elle
    reprend la décision, le pourcentage, la confiance et le cas d'usage.
    """
    first = f"Jev estime « {choice} » à {percent} %"
    if confidence is not None:
        first += f" (confiance {confidence:.2f})".replace(".", ",")
    second = f"cas d'usage « {use_case} »"
    if use_case_probability is not None:
        second += f" ({_format_percent(use_case_probability)} %)"
    return f"{first} ; {second}."


def _priority(percent: int, confidence: Optional[float]) -> str:
    """Classe la priorité commerciale d'un lead.

    Une probabilité élevée mais une confiance faible est rétrogradée en
    « moyenne » : le score n'est pas assez fiable pour être traité en priorité.
    """
    if percent >= HIGH_PRIORITY_SCORE and (
        confidence is None or confidence >= HIGH_PRIORITY_MIN_CONFIDENCE
    ):
        return "haute"
    if percent >= MEDIUM_PRIORITY_SCORE:
        return "moyenne"
    return "basse"


def parse_answers(
    payload: Dict[str, Any], lead: Dict[str, str]
) -> Dict[str, Any]:
    """Transforme une réponse TypeSafe en ligne enrichie pour le CSV.

    Args:
        payload: Réponse JSON de ``/v1/systemone``.
        lead: Ligne d'origine issue de ``leads_polynesie.csv``.

    Returns:
        Une ligne contenant ``LEAD_COLUMNS`` + ``SCORE_COLUMNS``, plus des
        clés internes ``_confidence``, ``_model`` et ``_usage`` utilisées par
        l'orchestration (ignorées à l'écriture du CSV).

    Raises:
        TypeSafeError: Si la réponse ne respecte pas le contrat attendu.
    """
    buy = _required_answer(payload, QUESTION_BUY)
    choice = str(buy.get("choice") or "").strip()
    if choice not in ("Oui", "Non"):
        raise TypeSafeError(
            f"Décision d'achat inattendue renvoyée par TypeSafe : {choice!r}."
        )

    default_yes = 1.0 if choice == "Oui" else 0.0
    yes_probability = _probability(
        buy.get("probabilities"), "Oui", default_yes
    )
    if yes_probability is None:
        yes_probability = default_yes
    buy_confidence = _confidence(buy.get("confidence"))

    use_case_answer = _required_answer(payload, QUESTION_USE_CASE)
    use_case = str(use_case_answer.get("choice") or "").strip()
    if use_case not in USE_CASES:
        raise TypeSafeError(
            f"Cas d'usage inconnu renvoyé par TypeSafe : {use_case!r}."
        )
    use_case_probability = _probability(
        use_case_answer.get("probabilities"), use_case, None
    )

    percent = _format_percent(yes_probability)
    row: Dict[str, Any] = {
        column: str(lead.get(column) or "").strip() for column in LEAD_COLUMNS
    }
    row.update(
        {
            "score_achat": str(percent),
            "probabilite_achat": f"{choice} ({percent} %)",
            "cas_usage": use_case,
            "justification": _build_justification(
                choice,
                percent,
                buy_confidence,
                use_case,
                use_case_probability,
            ),
            "priorite": _priority(percent, buy_confidence),
            "_confidence": (
                buy_confidence if buy_confidence is not None else 0.0
            ),
            "_model": str(payload.get("model") or ""),
            "_usage": (
                payload.get("usage")
                if isinstance(payload.get("usage"), dict)
                else {}
            ),
        }
    )
    return row


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_error_row(lead: Dict[str, str], message: str) -> Dict[str, Any]:
    """Construit une ligne de repli lorsqu'un lead n'a pas pu être scoré.

    La ligne conserve les données du scraper et place le lead en fin de
    classement (``score_achat`` vide, priorité « erreur »).
    """
    reason = " ".join(str(message).split())[:200] or "erreur inconnue"
    row: Dict[str, Any] = {
        column: str(lead.get(column) or "").strip() for column in LEAD_COLUMNS
    }
    row.update(
        {
            "score_achat": "",
            "probabilite_achat": "Indéterminé",
            "cas_usage": "",
            "justification": f"Échec de l'évaluation : {reason}",
            "priorite": "erreur",
            "_confidence": 0.0,
            "_model": "",
        }
    )
    return row


def _sort_key(row: Dict[str, Any]) -> tuple:
    """Trie par score décroissant, puis par confiance décroissante."""
    raw_score = str(row.get("score_achat") or "").strip()
    try:
        score = int(raw_score)
    except ValueError:
        score = -1
    confidence = _as_float(row.get("_confidence")) or 0.0
    return (score, confidence)


def score_lead(
    lead: Dict[str, str],
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    session: Optional[requests.Session] = None,
    base_url: str = TYPESAFE_BASE_URL,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Score une entreprise via l'API TypeSafe.

    Args:
        lead: Ligne issue de ``leads_polynesie.csv``.
        api_key: Clé ``TYPESAFE_API_KEY``.
        model: Identifiant du modèle Jev.
        session: Session HTTP injectable (tests hors ligne).
        base_url: URL de base de l'API.
        timeout: Délai maximal d'une requête (secondes).

    Returns:
        La ligne enrichie produite par ``parse_answers``.

    Raises:
        TypeSafeError: Si l'appel ou l'exploitation de la réponse échoue.
    """
    request_body = build_request(lead, model)
    payload = call_system_one(
        api_key,
        request_body,
        session=session,
        base_url=base_url,
        timeout=timeout,
    )
    return parse_answers(payload, lead)


def score_leads(
    limit: Optional[int] = None,
    *,
    dry_run: bool = False,
    input_path: Path = LEADS_CSV,
    output_path: Path = SCORES_CSV,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Score les leads d'un CSV et écrit le classement trié.

    Les erreurs non fatales sont journalisées et produisent une ligne de repli :
    un lead en échec n'interrompt jamais le lot. Les erreurs fatales
    (authentification, requête invalide, quota persistant) lèvent une
    ``RuntimeError`` après fermeture de la session HTTP.

    Args:
        limit: Nombre maximal de leads à traiter (``None`` = tous).
        dry_run: Si vrai, affiche la requête type sans appel réseau ni écriture.
        input_path: CSV source (défaut : ``leads_polynesie.csv``).
        output_path: CSV de sortie (défaut : ``leads_scores.csv``).
        session: Session HTTP injectable (tests hors ligne).

    Returns:
        Les lignes scorées, triées par probabilité décroissante. Une liste
        vide est renvoyée en mode ``dry_run``.

    Raises:
        RuntimeError: Si aucun lead n'est disponible ou si une erreur fatale
            interrompt le scoring.
        ValueError: Si ``limit`` est inférieur à 1.
        OSError: Si l'écriture du CSV de sortie échoue.
    """
    logger = setup_logging()
    load_env()

    input_path = Path(input_path)
    output_path = Path(output_path)
    leads = read_csv_rows(input_path)
    if not leads:
        raise RuntimeError(
            f"Aucun lead à scorer : {input_path} est vide ou introuvable. "
            "Lancez d'abord « python scraper.py »."
        )
    if limit is not None:
        if limit < 1:
            raise ValueError("limit doit être supérieur ou égal à 1.")
        leads = leads[:limit]

    model = get_option("TYPESAFE_MODEL", DEFAULT_MODEL)

    if dry_run:
        logger.info("Mode --dry-run : aucun appel réseau, aucune écriture.")
        logger.info("Modèle TypeSafe : %s", model)
        logger.info(
            "Questions :\n%s",
            json.dumps(build_questions(), ensure_ascii=False, indent=2),
        )
        logger.info(
            "Exemple d'état (%s) :\n%s",
            leads[0].get("nom") or "lead 1",
            json.dumps(build_state(leads[0]), ensure_ascii=False, indent=2),
        )
        return []

    api_key = get_api_key("TYPESAFE_API_KEY")
    logger.info(
        "Scoring de %d lead(s) avec le modèle Jev (%s)…", len(leads), model
    )

    rows: List[Dict[str, Any]] = []
    total_input_tokens = 0
    total_output_tokens = 0

    owns_session = session is None
    http = session or requests.Session()
    try:
        for index, lead in enumerate(leads, start=1):
            display = str(lead.get("nom") or f"lead {index}").strip()
            try:
                row = score_lead(lead, api_key, model=model, session=http)
            except TypeSafeError as exc:
                if exc.fatal:
                    raise RuntimeError(f"Arrêt du scoring : {exc}") from exc
                logger.warning("%s : %s", display, exc)
                rows.append(build_error_row(lead, str(exc)))
                continue
            except Exception as exc:
                logger.exception(
                    "%s : erreur inattendue pendant le scoring", display
                )
                rows.append(build_error_row(lead, f"erreur inattendue : {exc}"))
                continue

            usage = row.pop("_usage", {})
            if isinstance(usage, dict):
                total_input_tokens += _int_value(usage.get("input_tokens"))
                total_output_tokens += _int_value(usage.get("output_tokens"))
            rows.append(row)
            logger.info(
                "[%d/%d] %s → %s, %s (priorité %s)",
                index,
                len(leads),
                display,
                row["probabilite_achat"],
                row["cas_usage"] or "n/a",
                row["priorite"],
            )
    finally:
        if owns_session:
            http.close()

    rows.sort(key=_sort_key, reverse=True)
    written = write_csv_atomic(
        output_path, rows, LEAD_COLUMNS + SCORE_COLUMNS
    )
    logger.info("%d lead(s) écrit(s) dans %s", written, output_path)
    if total_input_tokens or total_output_tokens:
        logger.info(
            "Tokens TypeSafe : %d en entrée, %d en sortie.",
            total_input_tokens,
            total_output_tokens,
        )

    for row in rows:
        row.pop("_confidence", None)
        row.pop("_model", None)
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``scorer.py``.

    Args:
        argv: Arguments de ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si le scoring a produit au moins un résultat, 1 sinon.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Score les leads de leads_polynesie.csv avec le modèle Jev "
            "de TypeSafe et écrit leads_scores.csv."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Nombre de leads à scorer (défaut : tous).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=LEADS_CSV,
        help="CSV source (défaut : leads_polynesie.csv).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=SCORES_CSV,
        help="CSV de sortie (défaut : leads_scores.csv).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche la requête type sans appeler l'API ni écrire de fichier.",
    )
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit doit être un entier supérieur ou égal à 1.")

    try:
        rows = score_leads(
            args.limit,
            dry_run=args.dry_run,
            input_path=args.input,
            output_path=args.output,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    if not rows:
        return 0

    scored = [row for row in rows if str(row.get("score_achat") or "").strip()]
    print(f"{len(scored)} lead(s) scoré(s) dans {args.output.name} :")
    for row in rows:
        name = row.get("nom") or "n/a"
        print(
            f"  - {name} : {row['probabilite_achat']} — "
            f"{row['cas_usage'] or 'n/a'} (priorité {row['priorite']})"
        )
    if not scored:
        print(
            "Aucun lead n'a pu être scoré ; consultez les logs pour le détail.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
