# Agent autonome local (inspiré de Claude Code)

Agent de développement autonome qui tourne **entièrement en local** : il reçoit une demande en
langage naturel, raisonne, puis agit sur un projet (lire/écrire des fichiers, chercher dans le
code, exécuter des commandes) en s'appuyant sur l'API d'un LLM.

> **Statut : étapes 1 à 8 terminées — agent opérationnel, testé et encadré.**
> `src/config.py` charge et valide la configuration (`.env`), `src/llm_client.py` dialogue avec
> toute API compatible OpenAI (texte, `tool_calls`, streaming, erreurs traduites),
> `src/tools/` fournit cinq outils bornés par un bac à sable, `src/agent.py` exécute la
> boucle « raisonner → agir → observer » plafonnée par `MAX_ITERATIONS`, avec journal JSONL,
> budget de tokens et confirmation interactive des commandes sensibles (`--yes` pour les
> approuver en bloc), `src/context.py` borne l'historique et amorce la session par un instantané
> du projet, et `src/ui.py` propose une **session interactive multi-tours** rendue avec `rich`
> (panneaux, tableaux, flux) et des commandes `/aide`, `/reset`, `/quit`…
> La suite `pytest` (265 validations, aucun réseau) couvre configuration, bac à sable, registre,
> outils, lecture git, client LLM, boucle agentique, gestion du contexte, interface et CLI.

## Objectifs

- **Boucle agentique** « raisonner → agir → observer » : le LLM choisit des outils, l'agent les
  exécute, réinjecte les résultats, et recommence jusqu'à la réponse finale.
- **Outils locaux** : lecture/écriture de fichiers, listing d'arborescence, recherche plein texte,
  exécution de commandes de développement validées, lecture d'un diff git (`git diff`).
- **LLM interchangeable** via une API *compatible OpenAI* (OpenAI, Groq, Mistral, Together,
  mais aussi **Ollama** ou **LM Studio** pour un fonctionnement 100 % hors ligne).
- **Garde-fous** : périmètre limité au dossier du projet (*sandbox*), confirmation des actions
  destructrices, timeouts, budget de tokens, journalisation de chaque appel d'outil.

## Structure du projet

```
local-dev-agent-01/
├── README.md          # but du projet, usage, feuille de route
├── requirements.txt   # openai + python-dotenv + rich + pytest
├── pyproject.toml     # configuration pytest (pythonpath, options)
├── .env.example       # modèle de configuration à copier vers .env
├── .gitignore         # exclut venv, __pycache__, .env (clés API) et logs/
├── logs/              # journaux JSONL générés (créé au premier lancement)
├── tests/
│   ├── conftest.py    # fixtures : projet temporaire, sandbox, registre, settings
│   ├── helpers.py     # FakeClient (LLM scripté) et arborescence de test
│   ├── test_config.py        test_sandbox.py   test_registry.py
│   ├── test_tools_files.py   test_tools_search.py   test_tools_shell.py
│   ├── test_tools_git.py     test_llm_client.py     test_agent.py
│   ├── test_ui.py            test_context.py      test_cli.py
└── src/
    ├── __init__.py    # marque src/ comme package Python (+ __version__)
    ├── main.py        # point d'entrée : bannière, ou session complète avec -p
    ├── config.py      # Settings : lecture et validation de l'environnement
    ├── llm_client.py  # LLMClient : chat, tool_calls, streaming, erreurs
    ├── agent.py       # Agent : boucle bornée, journal JSONL, CLI
    ├── context.py     # estimation de tokens, troncature, instantané du projet
    ├── ui.py          # interface interactive : REPL multi-tours + rendu rich
    └── tools/
        ├── __init__.py  # façade publique du registre
        ├── base.py      # Tool, ToolRegistry, validation des arguments
        ├── sandbox.py   # Sandbox : Path.resolve() + refus hors projet
        ├── files.py     # read_file, write_file, list_dir
        ├── search.py    # search_code (regex, secrets/binaires ignorés)
        ├── shell.py     # run_command (liste blanche, shell=False, timeout)
        ├── git.py       # read_git_diff, read_git_status (lecture seule)
        └── registry.py  # build_default_registry() : les cinq outils
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

> **Note — `requirements.txt`** : `openai` (client compatible OpenAI, utilisé par
> `src/llm_client.py`), `python-dotenv` (lecture de `.env` par `src/config.py`), `rich`
> (interface interactive de `src/ui.py`) et `pytest` (suite de tests hors ligne).
> `rich` reste **facultatif à l'exécution** : s'il est absent, l'interface retombe
> automatiquement sur un affichage texte simple.

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

### Lancer une tâche

```powershell
# Session complète : le LLM choisit les outils, l'agent les exécute
python src/agent.py -p "Résume la structure du projet et cite les fichiers de src/"

# Même chose depuis le point d'entrée principal
python src/main.py -p "Ajoute une fonction somme(a, b) dans src/main.py puis vérifie-la"

# Aucun appel réseau : configuration, outils et prompt système
python src/agent.py --dry-run

# Sans -p dans un terminal : session interactive multi-tours (REPL)
python src/agent.py

# Sans -p avec une entrée redirigée : la tâche est lue sur stdin
Get-Content demande.txt | python src/agent.py

# Approuver d'avance les commandes sensibles et forcer un modèle
python src/agent.py -p "Lance les tests et corrige les échecs" --yes --model gpt-4o

# Version installée
python src/agent.py --version
```

Aperçu d'une session :

```text
[agent] modèle=gpt-4o-mini | outils=5 | itérations max=15
[1/15] appel du modèle (2 messages)…
  ← réponse reçue : 0 caractère(s), outils demandés : read_file
  → read_file({"max_lines": 60, "path": "src/main.py"})
  ← read_file : ok (412 caractères)
[2/15] appel du modèle (5 messages)…
  ← réponse reçue : 187 caractère(s), outils demandés : aucun
[agent] final_answer | 2 itération(s) | 1 outil(s) | 1 234 tokens
```

Options : `--model`, `--max-iterations`, `--token-budget`, `--context-tokens`, `--no-context`,
`-y/--yes`, `--repl`, `--log-dir`, `--no-log`, `--stream`, `--dry-run`, `-q`, `--version`.
Codes de sortie : `0` réponse finale, `1` erreur de configuration, `2` borne atteinte
(`max_iterations` ou `token_budget`), `130` interruption.

Avant chaque `run_command`, l'agent demande une confirmation (`o`/`n`).
`-y/--yes` approuve d'avance ; sans terminal interactif (pipe, CI), l'appel est **refusé**
par défaut avec un rappel de `--yes` : aucune commande destructrice ne part en silence.

Chaque session écrit `logs/session-AAAAMMJJ-HHMMSS.jsonl` : un événement JSON par ligne
(`session_start`, `llm_request`, `llm_response`, `tool_call`, `tool_result`, `tool_approved`,
`tool_refused`, `git_diff`, `context_injected`, `context_trimmed`, `error`, `session_end`).

### Session interactive (REPL)

Dans un terminal, l'absence de `-p` ouvre une session multi-tours : l'historique est
conservé d'une demande à l'autre, donc le modèle se souvient de ce qu'il a déjà lu, écrit
ou exécuté.

```powershell
# Session interactive (équivalent à --repl)
python src/agent.py

# Forcer la session même avec une entrée redirigée (script, CI)
python src/agent.py --repl < demandes.txt
```

| Commande | Effet |
|---|---|
| `/aide` | liste des commandes |
| `/outils` | outils disponibles et leur rôle |
| `/reset` | oublie l'historique (repart d'une conversation vierge) |
| `/stats` | tokens consommés, messages et estimation du contexte |
| `/journal` | chemin du journal JSONL de la session |
| `/oui` | bascule l'approbation automatique des outils sensibles |
| `/quit` | quitte (`/exit`, `quit`, `exit`, `bye`, ou Ctrl+D) |

`Ctrl+C` pendant une tâche annule **la tâche en cours** sans quitter la session ; au prompt,
il efface la ligne en cours. `--yes` démarre en approbation automatique (la bascule `/oui`
fonctionne dans les deux sens). `--repl` et `-p` sont incompatibles.

Avec `rich`, la bannière, la progression, la réponse (Markdown) et les statistiques sont
rendues en panneaux et tableaux. Sans `rich` — ou si la console refuse l'Unicode — l'interface
repasse d'elle-même en texte simple : une session ne s'interrompt jamais pour un caractère.

### Gestion du contexte

Deux mécanismes bornent ce qui part au modèle à chaque tour (`src/context.py`) :

- **Instantané du projet** — au premier tour, un second message système décrit l'arborescence
  (60 entrées, profondeur 2), un extrait du `README` (1 200 caractères) et le `git status`
  (800 caractères). Les dossiers techniques (`.git`, `.venv`, `node_modules`, `__pycache__`) et
  les fichiers cachés — dont `.env`, qui contient la clé API — sont exclus. Le texte est calculé
  une seule fois puis mis en cache ; `--no-context` le désactive, et un échec de collecte
  n'interrompt jamais la session.
- **Budget d'historique** — `--context-tokens` (16 000 par défaut, `0` = illimité) estime la
  conversation à ≈ 4 caractères par token. Au-delà, les échanges les plus anciens sont retirés
  **par blocs complets** : un message `assistant` porteur de `tool_calls` n'est jamais séparé de
  ses messages `tool`. Le prompt système et le dernier échange sont toujours conservés, et une
  note `[contexte] …` indique au modèle ce qui a été omis.

```powershell
python src/agent.py -p "..." --context-tokens 8000   # historique plus court
python src/agent.py -p "..." --context-tokens 0      # aucune troncature
python src/agent.py -p "..." --no-context            # sans instantané du projet
```

`/stats` dans le REPL affiche les tokens consommés, le nombre de messages en mémoire et
l'estimation courante du contexte ; `/reset` vide l'historique (l'instantané reste en cache et
est réinjecté à la demande suivante). Les événements `context_injected` et `context_trimmed`
apparaissent aussi dans le journal JSONL.

### Bannière seule

```powershell
python src/main.py
```

Sortie attendue :

```text
Agent initialisé et prêt à travailler
Utilisez « python src/main.py -p "votre tâche" » pour lancer une session.
```

`src/main.py` expose `afficher_banniere(message=...)` et `main(argv=None)` ; sans argument elle
affiche la bannière, avec une tâche elle délègue à `src/agent.py`.

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

### Lancer les tests

```powershell
# Suite complète (hors ligne, aucun appel réseau, aucun fichier du projet modifié)
.\\.venv\\Scripts\\python.exe -m pytest -q

# Un module en particulier
.\\.venv\\Scripts\\python.exe -m pytest tests/test_agent.py -v
```

Les tests s'appuient sur `tests/conftest.py` (projet temporaire, `Sandbox`, registre) et sur
`tests/helpers.py` (`FakeClient`, qui rejoue des réponses scriptées au lieu d'appeler l'API).
`pyproject.toml` déclare `pythonpath = [".", "tests"]`, ce qui permet d'écrire `from src...`
comme dans le reste du projet.

Les anciens scripts de vérification manuels (`tests/legacy/_chk_tools.py`, `_chk_agent.py`,
`_e2e_agent.py`) ont été portés en `pytest` puis supprimés : la suite ci-dessus est désormais
l'unique filet de sécurité.

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

## Feuille de route

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

### 5. Modéliser les outils — ✅ fait (`src/tools/`)

`Tool` associe un nom, une description, un schéma JSON (`function calling`) et un *handler*.
`ToolRegistry` refuse un nom inconnu et valide les arguments contre le schéma (types, champs
obligatoires, arguments inconnus) avant d'appeler le handler :

```python
from src.tools import build_default_registry

registre = build_default_registry()          # racine = dossier du projet
for schema in registre.schemas:
    print(schema["function"]["name"])        # list_dir, read_file, run_command…

resultat = registre.execute("read_file", {"path": "README.md", "max_lines": 20})
print(resultat.ok, resultat.content)         # une erreur d'outil ne lève jamais

# chemin hors projet -> resultat.ok = False et message explicite
registre.execute("read_file", {"path": "../secret.txt"})
```

| Outil | Rôle | Garde-fous |
| --- | --- | --- |
| `read_file` | lit un fichier texte, par plages de lignes | refuse `.env`, les binaires, les chemins hors projet |
| `write_file` | crée ou remplace un fichier (UTF-8, LF) | refuse `.git`, `.venv`, `.env*` et plus de 512 Ko |
| `list_dir` | arborescence bornée (profondeur 1–5) | ignore `.git`, `.venv`, `__pycache__`, `node_modules` |
| `search_code` | recherche regex, résultat `chemin:ligne: texte` | ignore secrets, binaires et fichiers de plus de 5 Mo |
| `run_command` | commande de dev (`git`, `python`, `pytest`, `npm`…) | liste blanche, `shell=False`, timeout 1–300 s, refus de `git push`, `pip install`, des tubes et des `&&` |

Le *handler* n'est exécuté **que** si le nom demandé existe dans le registre : aucune exécution
dynamique arbitraire.

### 6. Implémenter la boucle agentique — ✅ fait (`src/agent.py`)

```python
from src.agent import Agent
from src.config import load_settings

agent = Agent(load_settings())            # verbose=False pour un usage scripté
resultat = agent.run("Ajoute une fonction somme(a, b) dans src/main.py")

print(resultat.answer)                    # réponse finale
print(resultat.summary())                 # final_answer | 2 itération(s) | 3 outil(s) | 1 234 tokens
print(resultat.stopped_reason)            # final_answer, max_iterations, token_budget, error
```

Le déroulé effectif :

```text
messages = [system, user]
répéter jusqu'à réponse finale ou MAX_ITERATIONS :
    réponse = client.chat(messages, tools=registre.schemas)
    si réponse.tool_calls :
        pour chaque appel : résultat = registre.execute(appel.name, appel.arguments)
        messages += [réponse assistant, messages « tool »]
    sinon : retourner réponse.texte
```

Points implémentés : arrêt propre sur `MAX_ITERATIONS` et sur `--token-budget`, erreurs d'outil et
JSON invalide **renvoyés au modèle** (aucun crash), protection anti-boucle (un appel strictement
identique n'est pas réexécuté), journal JSONL de chaque étape, callbacks `on_event` (interface,
tests) et `confirm` (outils sensibles, voir §7).

### 7. Sécurité et garde-fous — ✅ fait

- **Sandbox** — ✅ `src/tools/sandbox.py` : chaque chemin passe par `Path.resolve()` puis doit être
  le dossier du projet ou l'un de ses descendants. `..`, chemins absolus externes et liens
  symboliques sortants sont refusés avant tout accès disque.
- **Commandes shell** — ✅ liste blanche d'exécutables, `shell=False`, `stdin` fermé, timeout
  (1–300 s), refus des motifs destructeurs (`git push`, `git reset --hard`, `pip install`, `npx`…)
  et des tubes/redirections/`&&`.
- **Écritures** — ✅ refus de `.git`, `.venv`, `node_modules` et de tout `.env*` (secrets) ;
  réécriture UTF-8 avec fins de ligne LF ; avant chaque `write_file`, le `git diff` du fichier est
  consigné (événement `git_diff`) pour garder une trace de l'état antérieur.
- **Budget** — ✅ `MAX_ITERATIONS` (défaut 15) et `--token-budget` optionnel.
- **Confirmation** — ✅ callback `confirm(name, arguments)` consulté avant tout outil
  `dangerous` (`run_command`) : `o`/`n` en terminal, refus hors terminal, `-y/--yes` pour
  approuver d'avance. Un refus devient un message `ACTION REFUSÉE` renvoyé au modèle.
- **Journal** — ✅ `logs/session-*.jsonl` : un événement horodaté par ligne, contenus tronqués à
  2 000 caractères (`tool_approved`, `tool_refused`, `git_diff` inclus).

### 8. Confort, contexte et qualité — ✅ fait

- CLI `argparse` — ✅ `-p/--prompt`, `--model`, `--max-iterations`, `--token-budget`,
  `--context-tokens`, `--no-context`, `-y/--yes`, `--repl`, `--log-dir`, `--no-log`, `--stream`,
  `--dry-run`, `-q`, `--version`.
- Tests `pytest` — ✅ `tests/` : 265 validations hors ligne (1 test ignoré sous Windows faute de
  privilèges pour les liens symboliques). Couverture : configuration, bac à sable, registre,
  cinq outils, lecture git, client LLM (texte, outils, flux, erreurs), boucle agentique (bornes,
  cache anti-boucle, journal, confirmation, diff avant écriture), gestion du contexte
  (estimation, troncature par échanges complets, instantané injecté une seule fois puis mis en
  cache, échec toléré), interface (`rich` et repli texte, commandes du REPL) et CLI (codes de
  sortie, `--dry-run`, `--no-log`, stdin, `--model`, `--yes`, `--context-tokens`, `--no-context`,
  `--version`).
- Gestion du contexte — ✅ `src/context.py` : estimation ≈ 4 caractères/token,
  `trim_messages` qui préserve le prompt système et les derniers échanges entiers (paires
  `assistant(tool_calls)`/`tool` jamais coupées) avec note `[contexte]`, et
  `build_project_context` (arborescence bornée, extrait du `README`, `git status`) injecté au
  premier tour. Voir [Gestion du contexte](#gestion-du-contexte).
