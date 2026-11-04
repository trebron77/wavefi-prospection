"""Interface Streamlit de l'agent autonome local.

L'interface ne fait pas tourner la boucle agentique dans le processus du serveur :
elle lance `src/agent.py` dans un sous-processus. Trois précautions en découlent :

- `python_executable()` vise l'interpréteur du venv décrit par `sys.prefix` ;
- `injecter_secrets()` recopie les secrets Streamlit dans l'environnement hérité ;
- le `PATH` transmis commence par le dossier de `sys.executable`, afin que les
  commandes `python ...` lancées par l'agent utilisent le Python du venv.
"""

import os
import subprocess
import sys
from typing import List

import streamlit as st

# Correspondances de noms de secrets acceptées : la famille `DEEPSEEK_*` (celle
# de `prospection_wifi/`) est traduite vers les noms lus par `src/config.py`.
# Les noms `LLM_*` restent prioritaires, et rien n'est écrasé.
ALIAS_SECRETS = {
    "DEEPSEEK_API_KEY": "LLM_API_KEY",
    "DEEPSEEK_BASE_URL": "LLM_BASE_URL",
    "DEEPSEEK_MODEL": "LLM_MODEL",
}


def python_executable() -> str:
    """Interpréteur Python à utiliser pour lancer l'agent en sous-processus.

    Le sous-processus a besoin des dépendances du projet (`openai`,
    `python-dotenv`...) : il faut donc viser l'environnement virtuel. Le dossier
    de cet interpréteur est également placé en tête du `PATH` du sous-processus
    (voir le bloc `subprocess.run` plus bas) : sur Streamlit Cloud,
    `sys.executable` vaut bien `/home/adminuser/venv/bin/python`, mais le `PATH`
    du serveur place `/usr/local/bin` avant le venv, si bien qu'un
    `python ...` lancé par l'agent (outil `shell`) tombe sur le Python système,
    qui ne connaît pas `openai`.

    L'interpréteur visé est construit à partir de `sys.prefix`
    (`/home/adminuser/venv`), à condition qu'il existe réellement sur le disque.

    `VIRTUAL_ENV` n'est pas utilisé : cette variable peut être absente ou décrire
    un autre environnement que celui du processus courant.

    Returns:
        `<sys.prefix>/bin/python` (Linux/macOS) ou `<sys.prefix>/Scripts/python.exe`
        (Windows) si ce fichier existe, sinon `sys.executable`.
    """
    if os.name == "nt":  # Windows : <prefix>\Scripts\python.exe
        candidat = os.path.join(sys.prefix, "Scripts", "python.exe")
    else:  # Linux / macOS : <prefix>/bin/python
        candidat = os.path.join(sys.prefix, "bin", "python")
    if os.path.isfile(candidat):
        return candidat
    return sys.executable


def injecter_secrets() -> List[str]:
    """Recopie les *secrets* Streamlit dans ``os.environ`` pour le sous-processus.

    Les secrets d'une application déployée ne vivent pas dans les variables
    d'environnement du serveur mais dans ``st.secrets`` (tableau de bord
    Streamlit Cloud, ou ``.streamlit/secrets.toml`` en local). Or l'agent lit
    ``os.environ`` (voir ``src/config.py``) et le sous-processus n'hérite que de
    cet environnement : les clés doivent donc y être recopiées avant le
    lancement.

    Sans ``secrets.toml``, ``st.secrets`` est vide et sa lecture lève
    ``FileNotFoundError`` : la fonction ne fait alors rien (lancement local
    inchangé). Les variables déjà présentes dans l'environnement ne sont pas
    écrasées, et les sections TOML (valeurs imbriquées) sont ignorées, faute de
    pouvoir représenter un tableau dans une variable d'environnement.

    Un secret ``DEEPSEEK_API_KEY`` (nom utilisé par ``prospection_wifi/``)
    alimente aussi ``LLM_API_KEY``, le nom lu par ``src/config.py`` : voir
    :data:`ALIAS_SECRETS`. La clé ``DEEPSEEK_API_KEY`` elle-même reste dans
    l'environnement, donc les deux conventions cohabitent.

    Returns:
        Liste des variables effectivement ajoutées (vide si l'environnement
        contenait déjà tout, ou si ``st.secrets`` est vide). Les valeurs ne sont
        jamais affichées.
    """
    try:
        secrets = dict(st.secrets)
    except FileNotFoundError:  # aucun secrets.toml : rien à injecter
        return []

    injectees: List[str] = []
    for cle, valeur in secrets.items():
        if not isinstance(valeur, (str, int, float)):
            continue  # section TOML ([section]) : pas une variable d'environnement
        if cle not in os.environ:
            os.environ[cle] = str(valeur)
            injectees.append(cle)

    # Correspondances de noms : `DEEPSEEK_API_KEY` alimente `LLM_API_KEY` si
    # celle-ci n'est pas déjà connue (ni par l'environnement, ni par un secret).
    for source, cible in ALIAS_SECRETS.items():
        if cible in os.environ:
            continue
        valeur = secrets.get(source)
        if isinstance(valeur, (str, int, float)) and str(valeur).strip():
            os.environ[cible] = str(valeur)
            injectees.append(cible)

    return injectees


st.set_page_config(page_title="KAI", page_icon="🤖")
st.title("🤖 Mon Agent Autonome")
st.caption("Interface graphique propulsée par Streamlit et DeepSeek.")

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Que dois-je faire ?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("L'agent réfléchit..."):
            cmd = [
                python_executable(), "src/agent.py", 
                "-p", prompt, 
                "--model", "deepseek-chat",
                "--yes" 
            ]
            
            # Les secrets Streamlit (clé API, endpoint...) doivent figurer dans
            # l'environnement hérité par le sous-processus.
            injecter_secrets()

            # Environnement du sous-processus : copie de celui du serveur, dont le
            # `PATH` est réécrit pour placer le dossier de l'interpréteur courant
            # en tête. Sur Streamlit Cloud, le `PATH` du serveur met le Python
            # système (`/usr/local/bin`) avant celui du venv
            # (`/home/adminuser/venv/bin`) : les commandes `python ...` lancées par
            # l'agent (outil `shell`) tomberaient sinon sur un Python sans `openai`.
            environ = os.environ.copy()
            dossier_python = os.path.dirname(sys.executable)
            elements = []
            for element in [dossier_python] + environ.get("PATH", "").split(os.pathsep):
                if element and element not in elements:
                    elements.append(element)
            environ["PATH"] = os.pathsep.join(elements)

            # Exécution avec gestion des erreurs d'encodage.
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environ,
            )
            
            if result.returncode == 0:
                reponse = result.stdout
                st.markdown(reponse)
                st.session_state.messages.append({"role": "assistant", "content": reponse})
            else:
                st.error("L'agent a rencontré une erreur :")
                st.code(result.stderr, language="bash")
                if result.stdout:
                    st.text("Sortie standard :")
                    st.code(result.stdout, language="bash")