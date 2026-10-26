"""Configuration de l'agent autonome local.

Toutes les valeurs viennent de l'environnement, chargé depuis le fichier `.env`
situé à la racine du projet (modèle fourni : `.env.example`). Aucune clé API
n'est jamais écrite en dur dans le code ni committée.

Utilisation :
    from config import load_settings

    settings = load_settings()
    print(settings.model, settings.masked_api_key())

Test rapide : ``python src/config.py``
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse

try:  # dépendance déclarée dans requirements.txt
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - secours si le paquet est absent
    load_dotenv = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Hôtes considérés comme locaux : aucune clé API n'y est exigée.
LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
)

# Jeton factice accepté par le SDK pour les serveurs locaux sans authentification.
NO_KEY_PLACEHOLDER = "local-no-key"

# Valeurs permettant d'omettre volontairement un paramètre numérique.
NONE_VALUES = frozenset({"none", "off", "null", "vide"})


class ConfigError(RuntimeError):
    """Variable d'environnement manquante ou invalide."""


def load_dotenv_file(path: Optional[Path] = None) -> bool:
    """Charge le fichier `.env` dans ``os.environ`` (sans écraser l'existant).

    Args:
        path: chemin du fichier ; par défaut ``<racine du projet>/.env``.

    Returns:
        ``True`` si un fichier a été lu, ``False`` sinon (fichier absent ou
        ``python-dotenv`` non installé — les variables système restent utilisées).
    """
    if load_dotenv is None:
        return False
    return bool(load_dotenv(dotenv_path=path or DEFAULT_ENV_FILE, override=False))


def _lire_texte(env: Mapping[str, str], name: str) -> Optional[str]:
    """Retourne la variable nettoyée, ou ``None`` si elle est absente/vide."""
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _lire_entier(
    env: Mapping[str, str], name: str, default: int, minimum: Optional[int] = None
) -> int:
    """Lit un entier, avec message d'erreur explicite en cas de valeur invalide."""
    raw = _lire_texte(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{name} doit être un entier (reçu : {raw!r}). Corrigez votre .env."
        ) from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} doit être >= {minimum} (reçu : {value}).")
    return value


def _parse_decimal(
    name: str, raw: str, minimum: Optional[float], maximum: Optional[float]
) -> float:
    try:
        # La virgule décimale française est tolérée (0,7).
        value = float(raw.replace(",", "."))
    except ValueError:
        raise ConfigError(
            f"{name} doit être un nombre décimal (reçu : {raw!r}). Corrigez votre .env."
        ) from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} doit être >= {minimum} (reçu : {value}).")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} doit être <= {maximum} (reçu : {value}).")
    return value


def _lire_decimal(
    env: Mapping[str, str],
    name: str,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = _lire_texte(env, name)
    if raw is None:
        return default
    return _parse_decimal(name, raw, minimum, maximum)


def _lire_decimal_optionnel(
    env: Mapping[str, str],
    name: str,
    default: Optional[float],
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> Optional[float]:
    """Comme :func:`_lire_decimal`, mais accepte ``none``/``off`` pour omettre."""
    raw = _lire_texte(env, name)
    if raw is None:
        return default
    if raw.lower() in NONE_VALUES:
        return None
    return _parse_decimal(name, raw, minimum, maximum)


@dataclass(frozen=True, repr=False)
class Settings:
    """Paramètres de l'agent, immuables une fois chargés."""

    api_key: Optional[str] = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: Optional[float] = 0.0
    max_tokens: int = 4096
    timeout: float = 60.0
    max_retries: int = 3
    max_iterations: int = 15

    @classmethod
    def from_environment(
        cls,
        env: Optional[Mapping[str, str]] = None,
        env_file: Optional[Path] = None,
        load_file: bool = True,
    ) -> "Settings":
        """Construit les paramètres depuis l'environnement.

        Args:
            env: mapping à lire (par défaut ``os.environ``).
            env_file: fichier `.env` alternatif.
            load_file: charger le fichier `.env` avant lecture.

        Returns:
            Une instance immuable de :class:`Settings`.

        Raises:
            ConfigError: si une variable est invalide (type ou bornes).
        """
        if env is None:
            if load_file:
                load_dotenv_file(env_file)
            env = os.environ
        return cls(
            api_key=_lire_texte(env, "LLM_API_KEY"),
            base_url=_lire_texte(env, "LLM_BASE_URL") or DEFAULT_BASE_URL,
            model=_lire_texte(env, "LLM_MODEL") or DEFAULT_MODEL,
            temperature=_lire_decimal_optionnel(
                env, "LLM_TEMPERATURE", 0.0, minimum=0.0, maximum=2.0
            ),
            max_tokens=_lire_entier(env, "LLM_MAX_TOKENS", 4096, minimum=1),
            timeout=_lire_decimal(env, "LLM_TIMEOUT", 60.0, minimum=1.0),
            max_retries=_lire_entier(env, "LLM_MAX_RETRIES", 3, minimum=0),
            max_iterations=_lire_entier(env, "MAX_ITERATIONS", 15, minimum=1),
        )

    @property
    def is_local_base_url(self) -> bool:
        """Vrai si l'API ciblée tourne en local (aucune clé requise)."""
        host = (urlparse(self.base_url).hostname or "").lower()
        return host in LOCAL_HOSTS

    @property
    def has_api_key(self) -> bool:
        """Vrai si une clé API est renseignée."""
        return bool(self.api_key)

    @property
    def effective_api_key(self) -> str:
        """Clé à transmettre au SDK.

        Les serveurs locaux (Ollama, LM Studio) n'authentifient pas : le SDK
        exige malgré tout une valeur, on lui fournit donc un jeton factice.

        Raises:
            ConfigError: aucune clé pour une API distante.
        """
        if self.api_key:
            return self.api_key
        if self.is_local_base_url:
            return NO_KEY_PLACEHOLDER
        raise ConfigError(
            "LLM_API_KEY est absente : renseignez-la dans .env (copie de "
            ".env.example), ou pointez LLM_BASE_URL vers un serveur local "
            "(Ollama, LM Studio)."
        )

    def masked_api_key(self) -> str:
        """Version tronquée de la clé, sûre pour les logs et les erreurs."""
        if not self.api_key:
            return "(non configurée)"
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"

    def __repr__(self) -> str:  # évite toute fuite de clé dans les traces
        return (
            f"Settings(model={self.model!r}, base_url={self.base_url!r}, "
            f"api_key={self.masked_api_key()}, temperature={self.temperature}, "
            f"max_tokens={self.max_tokens}, timeout={self.timeout}, "
            f"max_retries={self.max_retries}, max_iterations={self.max_iterations})"
        )


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    env_file: Optional[Path] = None,
    load_file: bool = True,
) -> Settings:
    """Raccourci vers :meth:`Settings.from_environment`."""
    return Settings.from_environment(env=env, env_file=env_file, load_file=load_file)


if __name__ == "__main__":
    print(load_settings())
