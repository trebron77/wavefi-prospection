import streamlit as st
import subprocess
import sys

st.set_page_config(page_title="Mon Agent Autonome", page_icon="🤖")
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
                sys.executable, "src/agent.py", 
                "-p", prompt, 
                "--model", "deepseek-chat",
                "--yes" 
            ]
            
            # Exécution avec gestion des erreurs d'encodage
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                encoding="utf-8",
                errors="replace"
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