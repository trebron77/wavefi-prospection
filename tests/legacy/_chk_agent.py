"""Vérification hors ligne de la boucle agentique (client LLM simulé)."""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from agent import Agent, EventLogger, STOP_ERROR, STOP_FINAL, STOP_MAX_ITERATIONS, STOP_TOKEN_BUDGET
from config import Settings
from llm_client import LLMError, LLMResponse, ToolCall
from tools import Tool, ToolRegistry, build_default_registry

RESULTATS = []


def check(label, condition, detail=""):
    suffixe = (" | " + str(detail)) if (detail and not condition) else ""
    RESULTATS.append(("OK " if condition else "KO ") + label + suffixe)


class FakeClient:
    """Client LLM scripté : aucune requête réseau."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.tokens_used = 0

    def chat(self, messages, tools=None, stream=False, on_token=None, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "stream": stream})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        self.tokens_used += item.total_tokens
        return item


settings = Settings(
    api_key="sk-test",
    model="fake-model",
    max_iterations=5,
    temperature=0.0,
    base_url="https://api.openai.com/v1",
)

root = Path(tempfile.mkdtemp(prefix="agent-check-"))
try:
    (root / "src").mkdir()
    (root / "src" / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    registry = build_default_registry(root=root)

    log_path = root / "logs" / "session.jsonl"
    logger = EventLogger(log_path)
    events = []
    fake = FakeClient(
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "src/hello.py"})], prompt_tokens=6, completion_tokens=4, total_tokens=10),
            LLMResponse(text="Le fichier affiche hi.", prompt_tokens=8, completion_tokens=4, total_tokens=12),
        ]
    )
    agent = Agent(settings=settings, client=fake, registry=registry, logger=logger, verbose=False, on_event=lambda nom, champs: events.append(nom))
    resultat = agent.run("Que fait src/hello.py ?")
    check("1 boucle outil puis reponse", resultat.success and resultat.stopped_reason == STOP_FINAL and resultat.answer == "Le fichier affiche hi.", resultat)
    check("1 compteurs", resultat.iterations == 2 and resultat.tool_calls == 1 and resultat.total_tokens == 22, resultat.summary())
    roles = [m["role"] for m in resultat.messages]
    check("1 roles historiques", roles == ["system", "user", "assistant", "tool", "assistant"], roles)
    outil = [m for m in resultat.messages if m["role"] == "tool"][0]
    check("1 resultat outil transmis", "print('hi')" in outil["content"] and outil["tool_call_id"] == "c1", outil)
    check("1 schemas envoyes", len(fake.calls[0]["tools"]) == 5 and fake.calls[0]["tools"][0]["type"] == "function")
    check("1 system prompt present", resultat.messages[0]["role"] == "system" and "agent" in resultat.messages[0]["content"].lower())
    check("1 evenements on_event", "session_start" in events and "tool_call" in events and "session_end" in events, events)
    logger.close()
    lignes = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    noms = [ligne["event"] for ligne in lignes]
    check("1 journal JSONL", "session_start" in noms and "tool_result" in noms and "session_end" in noms, noms)
    check("1 journal log_path", resultat.log_path == str(log_path), resultat.log_path)
    check("1 historique conserve", len(agent.history) == 5)

    # --- 2) écriture réelle via un appel d'outil ---------------------------
    fake = FakeClient(
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="w1", name="write_file", arguments={"path": "src/nouveau.py", "content": "X = 1\n"})], total_tokens=5),
            LLMResponse(text="Fichier créé.", total_tokens=5),
        ]
    )
    agent = Agent(settings=settings, client=fake, registry=registry, verbose=False)
    resultat = agent.run("Cree src/nouveau.py")
    check("2 write_file via agent", (root / "src" / "nouveau.py").read_text(encoding="utf-8") == "X = 1\n", resultat.answer)

    # --- 3) borne max_iterations ------------------------------------------
    appels = [
        LLMResponse(text="", tool_calls=[ToolCall(id="t%d" % i, name="list_dir", arguments={"path": "src", "max_depth": i % 2 + 1})], total_tokens=3)
        for i in range(5)
    ]
    fake = FakeClient(appels)
    agent = Agent(settings=settings, client=fake, registry=registry, max_iterations=2, verbose=False)
    resultat = agent.run("Explore")
    check("3 borne iterations", resultat.stopped_reason == STOP_MAX_ITERATIONS and resultat.iterations == 2 and len(fake.calls) == 2, resultat.summary())
    check("3 message borne", "Limite de 2 itérations" in resultat.answer, resultat.answer)

    # --- 4) arguments JSON invalides --------------------------------------
    fake = FakeClient(
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="bad", name="read_file", arguments={}, arguments_error="Expecting value")], total_tokens=2),
            LLMResponse(text="ok", total_tokens=2),
        ]
    )
    agent = Agent(settings=settings, client=fake, registry=registry, verbose=False)
    resultat = agent.run("Lis")
    message = [m for m in resultat.messages if m["role"] == "tool"][0]["content"]
    check("4 JSON invalide signale", "invalides" in message and "Expecting value" in message, message)

    # --- 5) appel répété : pas de ré-exécution -----------------------------
    compteur = {"n": 0}

    def compter():
        compteur["n"] += 1
        return "resultat stable"

    registre_perso = ToolRegistry(
        [Tool("ping", "Test", {"type": "object", "properties": {}, "required": []}, compter)]
    )
    fake = FakeClient(
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="p1", name="ping")], total_tokens=1),
            LLMResponse(text="", tool_calls=[ToolCall(id="p2", name="ping")], total_tokens=1),
            LLMResponse(text="fini", total_tokens=1),
        ]
    )
    agent = Agent(settings=settings, client=fake, registry=registre_perso, verbose=False)
    resultat = agent.run("Ping")
    check("5 anti-boucle", compteur["n"] == 1 and "déjà exécuté" in resultat.messages[-2]["content"], (compteur["n"], resultat.messages[-2]["content"]))

    # --- 6) outil inconnu --------------------------------------------------
    fake = FakeClient(
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="u1", name="rm_rf", arguments={"path": "/"})], total_tokens=1),
            LLMResponse(text="Je ne peux pas.", total_tokens=1),
        ]
    )
    agent = Agent(settings=settings, client=fake, registry=registry, verbose=False)
    resultat = agent.run("Supprime tout")
    message = [m for m in resultat.messages if m["role"] == "tool"][0]["content"]
    check("6 outil inconnu", "Outil inconnu" in message and "read_file" in message, message)

    # --- 7) budget de tokens ----------------------------------------------
    budget = [
        LLMResponse(text="", tool_calls=[ToolCall(id="b%d" % i, name="list_dir", arguments={"path": "src", "max_depth": i % 2 + 1})], total_tokens=100)
        for i in range(5)
    ]
    fake = FakeClient(budget)
    agent = Agent(settings=settings, client=fake, registry=registry, token_budget=50, verbose=False)
    resultat = agent.run("Explore")
    check("7 budget tokens", resultat.stopped_reason == STOP_TOKEN_BUDGET and len(fake.calls) == 1, resultat.summary())

    # --- 8) erreur LLM -----------------------------------------------------
    fake = FakeClient([LLMError("quota depasse")])
    agent = Agent(settings=settings, client=fake, registry=registry, verbose=False)
    resultat = agent.run("Bonjour")
    check("8 erreur LLM", resultat.stopped_reason == STOP_ERROR and "quota depasse" in resultat.answer, resultat.answer)

    # --- 9) tâche vide -----------------------------------------------------
    try:
        agent.run("   ")
        check("9 tache vide", False, "aucune erreur")
    except ValueError:
        check("9 tache vide", True)

    # --- 10) validations de construction ----------------------------------
    try:
        Agent(settings=settings, client=fake, registry=registry, max_iterations=0)
        check("10 max_iterations invalide", False, "aucune erreur")
    except ValueError:
        check("10 max_iterations invalide", True)
    try:
        Agent(settings=settings, client=fake, registry=registry, token_budget=0)
        check("10 token_budget invalide", False, "aucune erreur")
    except ValueError:
        check("10 token_budget invalide", True)
    check("10 capabilities", "run_command" in Agent(settings=settings, client=fake, registry=registry, verbose=False).capabilities())
finally:
    shutil.rmtree(root, ignore_errors=True)

print("\n".join(RESULTATS))
print("TOTAL=%d KO=%d" % (len(RESULTATS), sum(1 for ligne in RESULTATS if ligne.startswith("KO"))))
