# Agent autonome local (inspiré de Claude Code)

Agent de développement autonome qui tourne **entièrement en local** : il reçoit une demande en
langage naturel, raisonne, puis agit sur un projet (lire/écrire des fichiers, chercher dans le
code, exécuter des commandes) en s'appuyant sur l'API d'un LLM.

> **Statut : étapes 1 et 2 terminées — squelette + client LLM.**
> `src/config.py` charge et valide la configuration (`.env`), `src/llm_client.py` dialogue avec
> toute API compatible OpenAI (texte, `tool_calls`, streaming, erreurs traduites). Il reste à
> écrire la boucle agentique et les outils
> (voir [Prochaines étapes](#prochaines-étapes--connecter-une-api-de-llm)).

## Objectifs

- **Boucle agentique** « raisonner → agir → observer » : le LLM choisit des outils, l'agent les
  exécute, réinjecte les résultats, et recommence jusqu'à la réponse finale.
- **Outils locaux** : lecture/écriture de fichiers, listing d'arborescence, recherche plein texte,
  exécution de commandes shell, lecture d'un diff git.
- **LLM interchangeable** via une API *compatible OpenAI* (OpenAI, Groq, Mistral, Together,
  mais aussi **Ollama** ou **LM Studio** pour un fonctionnement 100 % hors ligne).
- **Garde-fous** : périmètre limité au dossier du projet (*sandbox*), confirmation des actions
  destructrices, timeouts, budget de tokens, journalisation de chaque appel d'outil.

## Structure du projet

```
local-dev-agent-01/
├── README.md          # but du projet, usage, feuille de route
├── requirements.txt   # openai + python-dotenv
├── .env.example       # modèle de configuration à copier vers .env
├── .gitignore         # exclut venv, __pycache__ et .env (clés API)
└── src/
    ├── __init__.py    # marque src/ comme package Python
    ├── main.py        # point d'entrée : affiche le message d'initialisation
    ├── config.py      # Settings : lecture et validation de l'environnement
    └── llm_client.py  # LLMClient : chat, tool_calls, streaming, erreurs
```

## Prérequis

- **Python 3.9+** (développé et vérifié avec Python 3.9.7 sous Windows).
- Un terminal PowerShell (ou n'importe quel shell).

## Installation

```powershell
# 1. Se placer dans le dossier du projet
cd local-dev-agent-01

# 2. Créer et activer un environnement virtuel
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Installer les dépendances (voir la note ci-dessous)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> **Note — `requirements.txt`** : deux dépendances seulement — `openai` (client compatible
> OpenAI, utilisé par `src/llm_client.py`) et `python-dotenv` (lecture de `.env` par
> `src/config.py`). `rich` et `pytest` restent en commentaires pour l'étape 3.

## Configuration

```powershell
Copy-Item .env.example .env
# puis éditez .env : LLM_API_KEY, et éventuellement LLM_BASE_URL / LLM_MODEL
```

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `LLM_API_KEY` | — | clé du fournisseur (inutile pour Ollama / LM Studio) |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | endpoint compatible OpenAI |
| `LLM_MODEL` | `gpt-4o-mini` | modèle utilisé pour raisonner et appeler les outils |
| `LLM_TEMPERATURE` | `0` | `0` = déterministe ; `none` pour omettre le paramètre |
| `LLM_MAX_TOKENS` | `4096` | longueur maximale de la réponse |
| `LLM_TIMEOUT` | `60` | délai maximal d'un appel, en secondes |
| `LLM_MAX_RETRIES` | `3` | tentatives supplémentaires gérées par le SDK |
| `MAX_ITERATIONS` | `15` | itérations maximales de la boucle agentique |

`src/config.py` valide tout au chargement : une valeur mal typée lève `ConfigError` en nommant
la variable fautive. `Settings` est immuable et sa clé API est **masquée** dans les logs
(`masked_api_key()`).

## Utilisation

```powershell
python src/main.py
```

Sortie attendue :

```
Agent initialisé et prêt à travailler
```

Le script expose deux fonctions dans `src/main.py` :

- `afficher_banniere(message=...)` — affiche le message d'initialisation et le renvoie ;
- `main()` — point d'entrée, renvoie un code de sortie (`0` = succès).

### Vérifier la connexion au LLM

```powershell
python src/llm_client.py
```

Sortie attendue (exemple) :

```
Modèle   : gpt-4o-mini
Endpoint : https://api.openai.com/v1
Clé API  : sk-a...WXYZ
[OK] Connexion établie. Réponse du modèle : 'OK'
```

### Utiliser le client depuis le code

```python
from src.config import load_settings
from src.llm_client import LLMClient

client = LLMClient(load_settings())

# 1. Réponse simple
reponse = client.chat([{"role": "user", "content": "Explique `pathlib` en une phrase."}])
print(reponse.text, reponse.total_tokens)

# 2. Appel d'outils (schéma au format OpenAI)
outils = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Lit un fichier texte du projet",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}]
reponse = client.chat([{"role": "user", "content": "Lis le README"}], tools=outils)
for appel in reponse.tool_calls:
    print(appel.name, appel.arguments, appel.arguments_error)

# 3. Streaming
client.chat([{"role": "user", "content": "Compte de 1 à 5"}], stream=True, on_token=print)
```

> Depuis un script placé **dans** `src/`, utilisez plutôt `from config import ...` et
> `from llm_client import ...` : les deux formes sont supportées (`src/llm_client.py` gère
> l'import relatif et l'import direct).

## Prochaines étapes — connecter une API de LLM

### 1. Choisir le fournisseur et le modèle

| Usage | Fournisseur | `LLM_BASE_URL` | Remarque |
| --- | --- | --- | --- |
| Qualité maximale | OpenAI | `https://api.openai.com/v1` | clé API payante |
| Hors ligne / privé | **Ollama** | `http://localhost:11434/v1` | `ollama pull qwen2.5-coder` |
| Hors ligne / GUI | LM Studio | `http://localhost:1234/v1` | serveur local à démarrer |
| Rapide & bon marché | Groq / Mistral | endpoints compatibles OpenAI | clé API |

Tous ces fournisseurs parlent le même protocole, donc **un seul client `openai` suffit** : le
changement de modèle se fait par variables d'environnement, sans toucher au code.

### 2. Installer les dépendances — ✅ fait (`openai`, `python-dotenv`)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 3. Gérer la configuration et les secrets — ✅ fait (`src/config.py`)

- `Settings` (dataclass gelée) lue depuis l'environnement via `python-dotenv`, avec validation
  typée et bornes : `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_TEMPERATURE`,
  `LLM_MAX_TOKENS`, `LLM_TIMEOUT`, `LLM_MAX_RETRIES`, `MAX_ITERATIONS`.
- `ConfigError` nomme toujours la variable fautive ; la clé API n'apparaît jamais en clair
  (`masked_api_key()` est utilisée dans `__repr__` et dans les messages d'erreur).
- Une API locale (Ollama, LM Studio) fonctionne **sans clé** : un jeton factice est fourni au SDK.
- `.env` reste exclu par git, `.env.example` documente chaque variable.

### 4. Client LLM — ✅ fait (`src/llm_client.py`)

- `chat(messages, tools=None, stream=False)` → `LLMResponse` (`text`, `tool_calls`, `model`,
  `finish_reason`, `prompt_tokens`, `completion_tokens`, `total_tokens`).
- `ToolCall` parse les arguments JSON ; un JSON invalide est **conservé** dans
  `arguments_error` au lieu de lever une exception, pour que l'agent renvoie l'erreur au modèle.
- Streaming avec callback `on_token` (texte et `tool_calls` reconstitués) et `extra_params` pour
  les options spécifiques (`max_completion_tokens`, `stream_options`…).
- Retry avec *backoff* exponentiel délégué au SDK (`max_retries`) ; erreurs 401/403/404/429/5xx
  et réseau traduites en messages actionnables.
- `python src/llm_client.py` valide la configuration de bout en bout.

### 5. Modéliser les outils (`src/tools/`)

Un registre où chaque outil déclare son nom, sa description, son schéma JSON (format
`function calling`) et son *handler* Python :

```python
TOOLS = {
    "read_file":   Tool(name="read_file",   schema={...}, handler=read_file),
    "write_file":  Tool(name="write_file",  schema={...}, handler=write_file),
    "list_dir":    Tool(name="list_dir",    schema={...}, handler=list_dir),
    "search_code": Tool(name="search_code", schema={...}, handler=search_code),
    "run_command": Tool(name="run_command", schema={...}, handler=run_command),
}
```

Le schéma JSON est envoyé au LLM ; le *handler* n'est exécuté **que** si le nom demandé existe
dans le registre (aucune exécution dynamique arbitraire).

### 6. Implémenter la boucle agentique (`src/agent.py`)

```text
messages = [system, user]
répéter jusqu'à réponse finale ou MAX_ITERATIONS :
    réponse = llm.chat(messages, tools=TOOLS.schemas)
    si réponse.tool_calls :
        pour chaque appel : résultat = TOOLS[appel.nom].handler(**appel.arguments)
        messages += [réponse assistant, résultats des outils]
    sinon :
        retourner réponse.texte
```

Prévoir : arrêt propre sur `MAX_ITERATIONS`, remontée d'erreur d'outil **au LLM** (pour qu'il
puisse se corriger) plutôt qu'un crash, et journalisation de chaque étape.

### 7. Sécurité et garde-fous (à faire dès la première version)

- **Sandbox** : résoudre chaque chemin avec `pathlib.Path.resolve()` et refuser tout chemin hors
  du dossier du projet (protection contre `..` et les liens symboliques).
- **Commandes shell** : liste blanche de commandes, `shell=False`, timeout, refus des commandes
  destructrices sans confirmation interactive explicite.
- **Écritures** : sauvegarde ou `git diff` avant modification, refus d'écraser un fichier hors
  périmètre.
- **Budget** : plafond de tokens/coût par session et nombre maximal d'itérations.
- **Journal** : trace JSONL horodatée de chaque appel d'outil (audit et débogage).

### 8. Confort et qualité

- CLI avec `argparse` (ou `rich` pour l'affichage) : `--prompt`, `--model`, `--dry-run`,
  `--yes` pour les confirmations.
- Tests `pytest` sur le registre d'outils et la boucle agentique, avec un **client LLM factice**
  (aucun appel réseau dans les tests).
- Gestion du contexte : troncature/résumé de l'historique, injection de l'arborescence et du
  `README` au démarrage, prise en compte du `git status` / `git diff`.
