"""Rédige une séquence de trois emails de prospection avec DeepSeek.

Le script lit ``leads_scores.csv`` (produit par ``scorer.py``), traite tous les
leads (ou un lot limité avec ``--limit``) puis demande à l'API DeepSeek —
compatible avec le SDK OpenAI — une séquence personnalisée de trois emails par
lead. Le résultat est exporté dans ``campagne_wifi_polynesie.csv`` avec les
colonnes ``nom``, ``email_1``, ``email_2`` et ``email_3``.

Preuve technique (données de l'étape 6)
---------------------------------------
Les colonnes ``fabricant_routeur`` et ``securite_wifi`` produites par
``enricher_netanalyzer.py`` sont relues dans ``campagne_wifi_ultime.csv`` et
jointes aux leads par nom normalisé (accents, casse et ponctuation ignorés).
Quand un fabricant est connu, le prompt demande une allusion discrète dans le
premier email (« J'ai remarqué que votre établissement utilise une
infrastructure Ruckus Wireless... ») ; quand la colonne est vide, le modèle
reçoit l'interdiction de parler de technique.

Le fichier de sortie reste ``campagne_wifi_polynesie.csv`` : y écrire les
emails effacerait ``email_contact`` et les colonnes Wi-Fi ; les étapes 4 à 6
(``enricher.py``, ``enricher_hunter.py``, ``enricher_netanalyzer.py``) sont
donc relancées après une régénération complète.

Utilisation
-----------
    python emailer.py                 # tous les leads, appel réel
    python emailer.py --limit 5       # 5 premiers leads
    python emailer.py --dry-run       # aperçu des prompts, sans réseau ni écriture
    python emailer.py --no-wifi       # sans les données de scan Wi-Fi
    python emailer.py --wifi scans.csv  # autre CSV de scans Wi-Fi

La clé ``DEEPSEEK_API_KEY`` doit être renseignée dans ``.env``. Le modèle et
l'URL de base peuvent être surchargés avec ``DEEPSEEK_MODEL`` et
``DEEPSEEK_BASE_URL``.

Après génération, les placeholders de signature sont remplacés par les
coordonnées WaveFi (Norbert, WaveFi, +689 87 07 70 19) et les lignes
``contact@wavefi.tech`` / ``www.wavefi.tech`` sont ajoutées à la fin.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from config import (
    CAMPAIGN_CSV,
    EMAIL_COLUMNS,
    NETWORK_COLUMNS,
    SCORES_CSV,
    ULTIMATE_CSV,
    get_api_key,
    get_option,
    load_env,
    read_csv_rows,
    setup_logging,
    write_csv_atomic,
)


#: Modèle DeepSeek utilisé par défaut.
DEFAULT_MODEL = "deepseek-chat"

#: URL de base de l'API DeepSeek (compatible OpenAI).
DEFAULT_BASE_URL = "https://api.deepseek.com"

#: Délai maximal d'un appel à DeepSeek (secondes).
REQUEST_TIMEOUT_SECONDS = 90

#: Nombre total de tentatives par lead (appel réseau ou réponse illisible).
MAX_ATTEMPTS = 2

#: Délai de base entre deux tentatives (secondes).
RETRY_BACKOFF_SECONDS = 2.0

#: Budget de génération pour les trois emails.
MAX_TOKENS = 1800

#: Colonne du CSV de campagne portant le nom de l'entreprise.
CAMPAIGN_NAME_COLUMN = "nom"

#: Matériel relevé par le scan Wi-Fi (noms de colonnes fournis par ``config``).
ROUTER_COLUMN = NETWORK_COLUMNS[0]
SECURITY_COLUMN = NETWORK_COLUMNS[1]

#: Mots génériques retirés des raisons sociales de fabricants afin de citer une
#: marque courte dans les emails : « Edgecore » plutôt que « Edgecore Networks
#: Corporation », « Hewlett Packard » plutôt que « Hewlett Packard Enterprise ».
VENDOR_NOISE_WORDS: FrozenSet[str] = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "enterprise",
        "enterprises",
        "gmbh",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "network",
        "networks",
        "sa",
        "sarl",
        "sas",
        "system",
        "systems",
        "technologies",
        "technology",
    }
)


SYSTEM_PROMPT = """Tu es un rédacteur B2B spécialisé dans les établissements \
d'accueil en Polynésie française. Tu rédiges pour un prestataire qui propose \
une solution de Wi-Fi marketing aux établissements d'accueil polynésiens : \
portail captif personnalisé, collecte d'adresses e-mail avec consentement, \
demandes d'avis Google automatisées, fidélisation des clients, promotion \
locale et analyse d'audience.

Rédige une séquence de prospection de trois emails en français :
1. Premier contact : une accroche personnalisée, le bénéfice principal lié au \
cas d'usage recommandé et une proposition d'échange simple.
2. Relance : un rappel courtois du premier message, un bénéfice concret \
supplémentaire et une nouvelle invitation à échanger.
3. Dernier contact : un message bref et respectueux qui propose un appel de \
15 minutes, sans pression ni reproche.

Règles de rédaction :
- Chaque email commence exactement par « Ia ora na, » (avec la virgule), puis \
une ligne vide. N'utilisez jamais « Bonjour », « Bonsoir », « Salut » ou une \
autre formule d'ouverture.
- Vouvoiement systématique : utilisez « vous », jamais « tu », « ton » ou « ta ».
- Ton professionnel, chaleureux, simple et respectueux des usages polynésiens.
- Ciblez uniquement la catégorie indiquée dans le prompt utilisateur. Si c'est \
un hôtel, ne parlez que d'hôtels et de l'expérience de séjour ; si c'est un \
restaurant, ne parlez que de restaurants et de l'expérience des convives. \
N'évoquez jamais l'autre catégorie, même en exemple ou en comparaison.
- Respectez l'orthographe et la grammaire françaises, en particulier \
l'élision : écrivez « de l'Hôtel... », « de l'établissement... » ou \
« de l'auberge... », mais « du Restaurant... » ou « de Maru Maru ». \
N'écrivez jamais « de Hôtel » ni « de Restaurant ».
- Restez concret et crédible : n'inventez aucun chiffre, client, partenariat, \
témoignage ou caractéristique de l'entreprise.
- Ne parlez de technique (matériel Wi-Fi, marque de routeur, sécurité du \
réseau) que si le prompt utilisateur indique un équipement relevé sur place, et \
uniquement dans le premier email, en une allusion discrète qui montre votre \
intérêt pour l'installation existante. Sans donnée fournie, n'évoquez aucun \
équipement et n'inventez ni marque ni modèle.
- Personnalisez avec le nom de l'entreprise, sa catégorie, sa ville et le cas \
d'usage recommandé fournis. Ne mentionnez jamais le score, la probabilité ou \
la justification interne dans le texte de l'email.
- Chaque email doit être court (environ 90 à 150 mots), avec un seul appel à \
l'action clair.
- L'objet doit faire moins de 70 caractères et ne pas être racoleur.
- Terminez chaque email par une signature sur cinq lignes :
  [Votre prénom]
  [Votre entreprise]
  [Votre téléphone]
  contact@wavefi.tech
  www.wavefi.tech
- N'utilisez ni markdown, ni listes à puces, ni emoji.

Réponds uniquement avec un objet JSON valide, sans texte avant ou après et sans \
balises de code, selon exactement ce schéma :
{"emails": [{"objet": "objet du premier email", "corps": "corps du premier email"}, \
{"objet": "objet du deuxième email", "corps": "corps du deuxième email"}, \
{"objet": "objet du troisième email", "corps": "corps du troisième email"}]}
"""


class EmailGenerationError(RuntimeError):
    """Erreur de génération ou de lecture de la réponse DeepSeek."""


def _text(value: Any) -> str:
    """Convertit une valeur CSV en texte nettoyé (jamais ``None``)."""
    if value is None:
        return ""
    return str(value).strip()


#: Formule d'ouverture imposée à chaque email.
GREETING = "Ia ora na,"

#: Coordonnées substituées aux placeholders de la signature générée.
SENDER_REPLACEMENTS: Dict[str, str] = {
    "[Votre prénom]": "Norbert",
    "[Votre entreprise]": "WaveFi",
    "[Votre téléphone]": "+689 87 07 70 19",
}

#: Lignes ajoutées à la fin de la signature (sans doublon).
SENDER_CONTACT_LINES: Tuple[str, ...] = (
    "contact@wavefi.tech",
    "www.wavefi.tech",
)

_GREETING_PREFIX = re.compile(
    r"^\s*(?:ia\s+ora\s+na|bonjour|bonsoir|salut|hello)"
    r"(?:\s+à\s+vous)?\s*[,!.]?\s*",
    re.IGNORECASE,
)
_GREETING_LINE = re.compile(
    r"^\s*(?:ia\s+ora\s+na|bonjour|bonsoir|salut|hello)"
    r"(?:\s+à\s+vous)?\s*[,!.]?\s*\n+",
    re.IGNORECASE,
)


def ensure_polynesian_greeting(body: str) -> str:
    """Force le corps de l'email à commencer par « Ia ora na, ».

    Cette normalisation déterministe complète la consigne du prompt : même si
    le modèle produit une autre formule d'ouverture, le CSV final reste
    conforme.

    Args:
        body: Corps brut renvoyé par DeepSeek.

    Returns:
        Le corps commençant exactement par ``Ia ora na,`` suivi d'une ligne
        vide et du contenu d'origine.
    """
    text = (body or "").strip()
    if not text:
        return GREETING

    text = _GREETING_LINE.sub("", text, count=1)
    text = _GREETING_PREFIX.sub("", text, count=1)
    text = text.lstrip()

    return f"{GREETING}\n\n{text}" if text else GREETING


def replace_sender_placeholders(text: str) -> str:
    """Remplace les placeholders de signature par les coordonnées WaveFi.

    Args:
        text: Objet ou corps brut renvoyé par DeepSeek.

    Returns:
        Le texte avec ``[Votre prénom]``, ``[Votre entreprise]`` et
        ``[Votre téléphone]`` remplacés. Les autres caractères sont conservés.
    """
    result = text or ""
    for placeholder, replacement in SENDER_REPLACEMENTS.items():
        result = result.replace(placeholder, replacement)
    return result


def ensure_contact_signature(body: str) -> str:
    """Ajoute l'e-mail et le site WaveFi à la fin de la signature.

    La fonction est idempotente : si une ligne de contact est déjà présente
    (par exemple parce que le modèle l'a écrite spontanément), elle n'est pas
    dupliquée.

    Args:
        body: Corps de l'email après remplacement des placeholders.

    Returns:
        Le corps nettoyé, terminé par ``contact@wavefi.tech`` puis
        ``www.wavefi.tech``.
    """
    text = (body or "").rstrip()
    if not text:
        return text

    missing = [
        line
        for line in SENDER_CONTACT_LINES
        if line.casefold() not in text.casefold()
    ]
    if missing:
        text = f"{text}\n" + "\n".join(missing)
    return text


def _normalize_for_match(value: str) -> str:
    """Retire les accents et passe le texte en minuscules pour les tests."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).lower()


def _build_category_instruction(category: str) -> str:
    """Construit la consigne de ciblage stricte selon la catégorie du lead.

    Un hôtel ne doit jamais être comparé à un restaurant, et inversement.

    Args:
        category: Catégorie issue du CSV (ex. ``"Hôtel"``).

    Returns:
        La consigne à insérer dans le prompt utilisateur.
    """
    normalized = _normalize_for_match(category)
    if any(
        token in normalized
        for token in ("hotel", "pension", "resort", "auberge", "hebergement")
    ):
        return (
            "Cette entreprise est un hôtel. Dans la phrase d'introduction et "
            "dans tous les exemples, ne parlez que d'hôtels : séjour, chambres, "
            "réception, arrivées et départs, clients en séjour. Ne mentionnez "
            "jamais les restaurants, les convives, les repas ou le service en "
            "salle."
        )
    if any(
        token in normalized
        for token in (
            "restaurant",
            "restauration",
            "brasserie",
            "bistrot",
            "cafe",
            "snack",
            "roulotte",
        )
    ):
        return (
            "Cette entreprise est un restaurant. Dans la phrase d'introduction "
            "et dans tous les exemples, ne parlez que de restaurants : convives, "
            "service, repas, réservations, clients de passage et habitués. Ne "
            "mentionnez jamais les hôtels, les chambres, la réception ou les "
            "clients en séjour."
        )
    return (
        f"Cette entreprise appartient à la catégorie « {category} ». Utilisez "
        "un vocabulaire adapté à cette catégorie et ne mélangez pas les "
        "univers hôtelier et restauration."
    )


def _starts_with_vowel_sound(company: str) -> bool:
    """Indique si un nom d'entreprise appelle l'élision ``de l'``."""
    cleaned = company.strip()
    if not cleaned:
        return False
    first = cleaned[0].lower()
    if first in "aàâäeéèêëiîïoôöuùûüyÿœæ":
        return True
    return cleaned.lower().startswith(
        ("hôtel", "hotel", "hôtellerie", "hotellerie", "auberge")
    )


def _build_elision_instruction(company: str) -> str:
    """Construit la consigne d'élision adaptée au nom de l'entreprise.

    Args:
        company: Nom de l'entreprise.

    Returns:
        La consigne à insérer dans le prompt utilisateur.
    """
    if _starts_with_vowel_sound(company):
        return (
            f"Le nom « {company} » commence par une voyelle ou un h muet : "
            f"utilisez l'élision, par exemple « de l'{company} » ou "
            f"« à l'{company} », et n'écrivez jamais « de {company} »."
        )
    return (
        f"Le nom « {company} » commence par une consonne : n'ajoutez pas "
        f"d'élision artificielle. Écrivez par exemple « de {company} » et "
        "n'ajoutez pas de « l' » devant le nom."
    )


def normalize_name_key(value: str) -> str:
    """Normalise un nom d'entreprise pour la jointure avec les scans Wi-Fi.

    La règle est celle de ``enricher_netanalyzer.py`` : accents, casse et
    ponctuation sont retirés, seuls les lettres et les chiffres sont conservés.
    ``Hôtel Kon Tiki`` et ``Hotel  Kon-Tiki`` produisent ainsi la même clé.

    Args:
        value: Nom de l'entreprise tel qu'il figure dans un CSV.

    Returns:
        La clé de jointure, éventuellement vide.
    """
    normalized = _normalize_for_match(value or "")
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def shorten_vendor(fabricant: str) -> str:
    """Réduit une liste de fabricants à une marque courte et citable.

    NetAnalyzer renvoie des raisons sociales complètes
    (``Edgecore Networks Corporation, Ruckus Wireless, AVM``). Seule la
    première marque est retenue, débarrassée de ses suffixes juridiques, pour
    obtenir une formulation naturelle dans un email.

    Args:
        fabricant: Valeur brute de la colonne ``fabricant_routeur``.

    Returns:
        La marque à citer (``"Edgecore"``, ``"Hewlett Packard"``), ou une
        chaîne vide si la valeur est vide.
    """
    first = _text(fabricant).split(",")[0].strip()
    words = first.split()
    while words and words[-1].casefold().strip(".") in VENDOR_NOISE_WORDS:
        words.pop()
    return " ".join(words) or first


def read_wifi_rows(path: Path = ULTIMATE_CSV) -> Dict[str, Dict[str, str]]:
    """Indexe les données Wi-Fi de la campagne par nom normalisé.

    Le fichier lu est ``campagne_wifi_ultime.csv`` (sortie de
    ``enricher_netanalyzer.py``). Un fichier absent, une colonne manquante ou
    une ligne sans scan sont ignorés : la génération d'emails reste possible
    sans données techniques.

    Args:
        path: CSV enrichi par NetAnalyzer.

    Returns:
        Un dictionnaire ``clé normalisée -> {fabricant_routeur,
        securite_wifi}`` limité aux lignes qui portent au moins une
        information Wi-Fi.
    """
    index: Dict[str, Dict[str, str]] = {}
    for row in read_csv_rows(Path(path)):
        key = normalize_name_key(_text(row.get(CAMPAIGN_NAME_COLUMN)))
        router = _text(row.get(ROUTER_COLUMN))
        security = _text(row.get(SECURITY_COLUMN))
        if not key or (not router and not security):
            continue
        index.setdefault(
            key, {ROUTER_COLUMN: router, SECURITY_COLUMN: security}
        )
    return index


def attach_wifi_data(
    lead: Mapping[str, str],
    wifi_index: Mapping[str, Mapping[str, str]],
) -> Dict[str, str]:
    """Renvoie une copie du lead complétée par son matériel Wi-Fi éventuel.

    Args:
        lead: Ligne de ``leads_scores.csv``.
        wifi_index: Index produit par :func:`read_wifi_rows`.

    Returns:
        Une copie du lead ; ``fabricant_routeur`` et ``securite_wifi`` ne sont
        ajoutés que si un scan a été trouvé pour ce nom.
    """
    enriched = dict(lead)
    key = normalize_name_key(_text(lead.get(CAMPAIGN_NAME_COLUMN)))
    info = wifi_index.get(key)
    if info:
        enriched.update(info)
    return enriched


def has_wifi_data(lead: Mapping[str, str]) -> bool:
    """Indique si un scan Wi-Fi exploitable est associé au lead."""
    return bool(
        _text(lead.get(ROUTER_COLUMN)) or _text(lead.get(SECURITY_COLUMN))
    )


def _build_network_instruction(lead: Mapping[str, str]) -> str:
    """Construit la consigne « preuve technique » du prompt utilisateur.

    Args:
        lead: Ligne de campagne, éventuellement complétée par les scans Wi-Fi.

    Returns:
        Une consigne d'allusion discrète quand un fabricant est connu, sinon
        une interdiction explicite de parler de technique.
    """
    router = _text(lead.get(ROUTER_COLUMN))
    security = _text(lead.get(SECURITY_COLUMN))
    if not router and not security:
        return (
            "Aucune donnée technique n'est disponible pour cet "
            "établissement : ne parlez ni de matériel Wi-Fi, ni de marque de "
            "routeur, ni de sécurité du réseau, et n'inventez aucun "
            "équipement."
        )

    details: List[str] = []
    if router:
        details.append(f"fabricant du routeur : {router}")
    if security:
        details.append(f"sécurité du réseau : {security}")
    brand = shorten_vendor(router) or router
    return (
        "Le scan Wi-Fi de l'établissement a relevé "
        f"{' ; '.join(details)}. Dans l'Email 1 uniquement, intégrez cette "
        "information de façon subtile pour montrer que vous vous êtes "
        "renseigné sur son installation technique, par exemple : « J'ai "
        f"remarqué que votre établissement utilise une infrastructure {brand} "
        "... ». Une seule phrase, sans jargon, sans conseil technique et sans "
        "critique du matériel existant. N'ajoutez aucune autre marque, aucun "
        "modèle et aucun débit. Les Emails 2 et 3 ne parlent pas de technique."
    )


def build_lead_prompt(lead: Mapping[str, str]) -> str:
    """Construit le prompt utilisateur personnalisé pour un lead.

    Args:
        lead: Ligne issue de ``leads_scores.csv``, éventuellement complétée par
            les colonnes Wi-Fi de ``campagne_wifi_ultime.csv``.

    Returns:
        Le prompt contenant l'entreprise, sa catégorie, son score, sa
        probabilité, son cas d'usage recommandé, son contexte local et, le cas
        échéant, une consigne de preuve technique fondée sur le scan Wi-Fi.
    """
    name = _text(lead.get("nom"))
    company = _text(lead.get("entreprise")) or name or "Entreprise non renseignée"
    category = _text(lead.get("categorie")) or "Non renseignée"
    probability = _text(lead.get("probabilite_achat")) or "Non renseignée"
    use_case = _text(lead.get("cas_usage")) or "Non renseigné"

    score = _text(lead.get("score_achat"))
    if not score:
        match = re.search(r"\d+(?:[.,]\d+)?", probability)
        score = match.group(0) if match else "Non renseigné"

    category_instruction = _build_category_instruction(category)
    elision_instruction = _build_elision_instruction(company)

    context: List[str] = []
    city = _text(lead.get("ville"))
    if city:
        context.append(f"- Ville : {city}")
    island = _text(lead.get("ile"))
    if island:
        context.append(f"- Île : {island}")
    rating = _text(lead.get("note_google"))
    if rating:
        context.append(f"- Note Google : {rating}")
    reviews = _text(lead.get("nb_avis"))
    if reviews:
        context.append(f"- Nombre d'avis Google : {reviews}")
    source = _text(lead.get("source"))
    if source:
        context.append(f"- Source du lead : {source}")
    job = _text(lead.get("poste"))
    if job:
        context.append(f"- Poste du contact : {job}")
    router = _text(lead.get(ROUTER_COLUMN))
    if router:
        context.append(f"- Fabricant du routeur (scan Wi-Fi) : {router}")
    security = _text(lead.get(SECURITY_COLUMN))
    if security:
        context.append(f"- Sécurité Wi-Fi relevée : {security}")

    network_instruction = _build_network_instruction(lead)

    context_block = "\n".join(context) if context else "- Contexte local : Polynésie française"
    return f"""Rédige la séquence de trois emails de prospection pour le lead suivant.

- Nom / enseigne : {name or company}
- Entreprise : {company}
- Catégorie : {category}
- Score d'achat (0-100) : {score}
- Cas d'usage recommandé : {use_case}
{context_block}

La séquence doit être adaptée exclusivement à la catégorie « {category} » en \
Polynésie française, en vouvoiement, avec un ton professionnel et chaleureux.
{category_instruction}
{elision_instruction}
{network_instruction}
Chaque email doit commencer exactement par « Ia ora na, » (avec la virgule).
Utilise le cas d'usage recommandé comme fil conducteur sans le présenter comme \
une évidence acquise. Ne cite aucun score ni aucune donnée interne dans les \
emails.
"""


def _extract_json_object(content: str) -> Dict[str, Any]:
    """Extrait un objet JSON d'une réponse modèle, même entourée de texte."""
    text = (content or "").strip()
    if not text:
        raise EmailGenerationError("DeepSeek a renvoyé une réponse vide.")

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload

    snippet = " ".join(text.split())[:220]
    raise EmailGenerationError(
        f"Réponse JSON illisible de DeepSeek : {snippet!r}"
    )


def parse_emails(content: str) -> List[Dict[str, str]]:
    """Valide la réponse DeepSeek et renvoie exactement trois emails.

    Args:
        content: Contenu texte renvoyé par le modèle.

    Returns:
        Une liste de trois dictionnaires ``{"objet": ..., "corps": ...}``.

    Raises:
        EmailGenerationError: Si le JSON est invalide, incomplet ou vide.
    """
    payload = _extract_json_object(content)
    raw_emails = payload.get("emails")
    if not isinstance(raw_emails, list):
        raise EmailGenerationError(
            "La réponse DeepSeek ne contient pas la liste « emails »."
        )
    if len(raw_emails) != 3:
        raise EmailGenerationError(
            f"DeepSeek devait produire 3 emails, il en a produit {len(raw_emails)}."
        )

    emails: List[Dict[str, str]] = []
    for index, raw_email in enumerate(raw_emails, start=1):
        if not isinstance(raw_email, dict):
            raise EmailGenerationError(
                f"L'email {index} n'est pas un objet JSON."
            )
        subject = _text(raw_email.get("objet") or raw_email.get("subject"))
        body = _text(raw_email.get("corps") or raw_email.get("body"))
        if not subject:
            raise EmailGenerationError(f"L'email {index} n'a pas d'objet.")
        if not body:
            raise EmailGenerationError(f"L'email {index} n'a pas de corps.")
        subject = replace_sender_placeholders(subject)
        body = replace_sender_placeholders(body)
        body = ensure_polynesian_greeting(body)
        body = ensure_contact_signature(body)
        emails.append({"objet": subject, "corps": body})
    return emails


def format_email(email: Mapping[str, str]) -> str:
    """Assemble un email complet pour une cellule CSV.

    Args:
        email: Dictionnaire contenant ``objet`` et ``corps``.

    Returns:
        Le texte ``Objet : ...`` suivi d'une ligne vide puis du corps.
    """
    return f"Objet : {_text(email.get('objet'))}\n\n{_text(email.get('corps'))}"


def _build_client(api_key: str, base_url: str) -> Any:
    """Instancie le client OpenAI configuré pour l'API DeepSeek.

    Args:
        api_key: Clé DeepSeek.
        base_url: URL de base compatible OpenAI.

    Returns:
        Un client ``openai.OpenAI`` prêt à l'emploi.

    Raises:
        RuntimeError: Si le paquet ``openai`` manque ou si le client ne peut
            pas être initialisé.
    """
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - dépend de l'install
        raise RuntimeError(
            "Le paquet « openai » n'est pas installé. "
            "Lancez : python -m pip install -r requirements.txt"
        ) from exc

    try:
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
    except Exception as exc:  # pragma: no cover - dépend du SDK
        raise RuntimeError(f"Initialisation du client DeepSeek impossible : {exc}") from exc


def _usage_counts(response: Any) -> Dict[str, int]:
    """Extrait les compteurs de tokens d'une réponse OpenAI/DeepSeek."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    counts: Dict[str, int] = {}
    for name, attribute in (
        ("prompt", "prompt_tokens"),
        ("completion", "completion_tokens"),
        ("total", "total_tokens"),
    ):
        value = getattr(usage, attribute, None)
        if isinstance(value, (int, float)):
            counts[name] = int(value)
    return counts


def _generate_sequence(
    client: Any,
    model: str,
    lead: Mapping[str, str],
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Demande à DeepSeek trois emails pour un lead, avec une relance.

    Args:
        client: Client compatible OpenAI.
        model: Nom du modèle DeepSeek.
        lead: Ligne issue du CSV de scores.

    Returns:
        Le triplet d'emails validé et les compteurs de tokens éventuels.

    Raises:
        EmailGenerationError: Si les tentatives échouent ou si la réponse est
            invalide.
    """
    logger = setup_logging()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_lead_prompt(lead)},
    ]
    last_error: Optional[BaseException] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
                max_tokens=MAX_TOKENS,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Appel DeepSeek en échec (%d/%d) : %s", attempt, MAX_ATTEMPTS, exc
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        try:
            content = response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError) as exc:
            last_error = exc
            logger.warning(
                "Réponse DeepSeek inexploitable (%d/%d) : %s",
                attempt,
                MAX_ATTEMPTS,
                exc,
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        try:
            emails = parse_emails(content)
        except EmailGenerationError as exc:
            last_error = exc
            logger.warning(
                "Réponse DeepSeek invalide (%d/%d) : %s",
                attempt,
                MAX_ATTEMPTS,
                exc,
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        return emails, _usage_counts(response)

    raise EmailGenerationError(
        f"Génération impossible après {MAX_ATTEMPTS} tentative(s) : {last_error}"
    )


def generate_campaign(
    limit: Optional[int] = None,
    *,
    dry_run: bool = False,
    input_path: Path = SCORES_CSV,
    output_path: Path = CAMPAIGN_CSV,
    wifi_path: Path = ULTIMATE_CSV,
    use_wifi: bool = True,
    client: Optional[Any] = None,
) -> List[Dict[str, str]]:
    """Génère et exporte la séquence d'emails des leads sélectionnés.

    Args:
        limit: Nombre maximal de leads à traiter (``None`` = tous).
        dry_run: Si vrai, journalise les prompts sans appel réseau ni écriture.
        input_path: CSV de scores produit par ``scorer.py``.
        output_path: CSV de campagne à écrire.
        wifi_path: CSV enrichi par ``enricher_netanalyzer.py`` ; ses colonnes
            ``fabricant_routeur`` et ``securite_wifi`` nourrissent la preuve
            technique du premier email.
        use_wifi: Si faux, aucune donnée Wi-Fi n'est relue ni mentionnée.
        client: Client compatible OpenAI injectable (tests hors ligne). S'il
            est fourni, aucune clé DeepSeek n'est lue.

    Returns:
        Les lignes écrites, ou une liste vide en mode ``--dry-run``.

    Raises:
        RuntimeError: Si le CSV est vide, si la clé manque ou si DeepSeek
            échoue.
        ValueError: Si ``limit`` est inférieur à 1.
        OSError: Si l'écriture du CSV échoue.
    """
    logger = setup_logging()
    load_env()

    if limit is not None and limit < 1:
        raise ValueError("limit doit être supérieur ou égal à 1.")

    leads = read_csv_rows(Path(input_path))
    if not leads:
        raise RuntimeError(
            f"Aucun lead dans {input_path}. "
            "Lancez d'abord « python scorer.py » pour produire les scores."
        )

    selected = leads if limit is None else leads[:limit]
    if limit is not None and len(selected) < limit:
        logger.warning(
            "Seulement %d lead(s) disponible(s) sur les %d demandés.",
            len(selected),
            limit,
        )

    wifi_index: Dict[str, Dict[str, str]] = {}
    if use_wifi:
        wifi_index = read_wifi_rows(wifi_path)
        if wifi_index:
            logger.info(
                "%d fiche(s) de scan Wi-Fi relue(s) dans %s",
                len(wifi_index),
                Path(wifi_path).name,
            )
        else:
            logger.warning(
                "Aucune donnée Wi-Fi exploitable dans %s : les emails ne "
                "parleront pas de technique. Lancez d'abord "
                "« python enricher_netanalyzer.py ».",
                wifi_path,
            )

    prepared = [attach_wifi_data(lead, wifi_index) for lead in selected]
    if use_wifi:
        with_wifi = sum(1 for lead in prepared if has_wifi_data(lead))
        logger.info(
            "%d lead(s) sur %d disposent d'un scan Wi-Fi exploitable.",
            with_wifi,
            len(prepared),
        )

    if dry_run:
        logger.info("Mode --dry-run : aucun appel réseau, aucune écriture.")
        for index, lead in enumerate(prepared, start=1):
            display = _text(lead.get("nom")) or f"lead {index}"
            logger.info(
                "Prompt %d/%d pour %s :\n%s",
                index,
                len(prepared),
                display,
                build_lead_prompt(lead),
            )
        return []

    base_url = get_option("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    model = get_option("DEEPSEEK_MODEL", DEFAULT_MODEL)

    http_client = client
    if http_client is None:
        api_key = get_api_key("DEEPSEEK_API_KEY")
        http_client = _build_client(api_key, base_url)

    logger.info(
        "Génération de %d séquence(s) de 3 emails avec %s (%s)…",
        len(prepared),
        model,
        base_url,
    )

    rows: List[Dict[str, str]] = []
    for index, lead in enumerate(prepared, start=1):
        display = _text(lead.get("nom")) or f"lead {index}"
        try:
            emails, usage = _generate_sequence(http_client, model, lead)
        except EmailGenerationError as exc:
            raise RuntimeError(f"{display} : {exc}") from exc

        row: Dict[str, str] = {"nom": display}
        for email_index, email in enumerate(emails, start=1):
            row[f"email_{email_index}"] = format_email(email)
        rows.append(row)
        logger.info(
            "[%d/%d] %s → 3 emails générés%s",
            index,
            len(prepared),
            display,
            (
                f" ({usage['total']} tokens)"
                if usage.get("total")
                else ""
            ),
        )

    written = write_csv_atomic(output_path, rows, EMAIL_COLUMNS)
    logger.info(
        "%d lead(s) et %d email(s) écrit(s) dans %s",
        written,
        written * 3,
        output_path,
    )
    return rows


def _configure_console_encoding() -> None:
    """Évite les erreurs d'encodage des consoles Windows héritées.

    Certains noms polynésiens contiennent des caractères absents de cp1252
    (par exemple ``Ī``). Le CSV reste écrit en UTF-8 ; on demande simplement à
    la console de remplacer les caractères qu'elle ne sait pas afficher au lieu
    d'interrompre le script.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    """Point d'entrée CLI de ``emailer.py``.

    Args:
        argv: Arguments de ligne de commande (``None`` = ``sys.argv[1:]``).

    Returns:
        0 si les emails ont été générés (ou simulés), 1 sinon.
    """
    _configure_console_encoding()
    parser = argparse.ArgumentParser(
        description=(
            "Génère trois emails de prospection par lead avec DeepSeek, à "
            "partir des scores et des scans Wi-Fi, et écrit "
            "campagne_wifi_polynesie.csv."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Nombre de leads à traiter (défaut : tous).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=SCORES_CSV,
        help="CSV de scores source (défaut : leads_scores.csv).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=CAMPAIGN_CSV,
        help="CSV de campagne à écrire (défaut : campagne_wifi_polynesie.csv).",
    )
    parser.add_argument(
        "--wifi",
        type=Path,
        default=ULTIMATE_CSV,
        help=(
            "CSV enrichi par enricher_netanalyzer.py "
            "(défaut : campagne_wifi_ultime.csv)."
        ),
    )
    parser.add_argument(
        "--no-wifi",
        action="store_true",
        help="Ignore les données de scan Wi-Fi (aucune allusion technique).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les prompts sans appeler DeepSeek ni écrire de fichier.",
    )
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit doit être un entier supérieur ou égal à 1.")

    try:
        rows = generate_campaign(
            args.limit,
            dry_run=args.dry_run,
            input_path=args.input,
            output_path=args.output,
            wifi_path=args.wifi,
            use_wifi=not args.no_wifi,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    if not rows:
        return 0

    print(f"{len(rows)} lead(s) et {len(rows) * 3} email(s) dans {args.output} :")
    for row in rows:
        print(f"  - {row.get('nom') or 'n/a'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
