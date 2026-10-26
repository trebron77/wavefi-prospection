"""Bout en bout : vrai SDK openai + faux serveur HTTP local (aucun réseau externe)."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "src")

REQUETES = []
REPONSES = [
    {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "src/config.py", "max_lines": 2}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    },
    {
        "id": "chatcmpl-2",
        "object": "chat.completion",
        "created": 2,
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Le fichier commence par une docstring."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 80, "completion_tokens": 8, "total_tokens": 88},
    },
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        REQUETES.append(json.loads(self.rfile.read(length).decode("utf-8")))
        corps = json.dumps(REPONSES[min(len(REQUETES) - 1, len(REPONSES) - 1)]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *args):
        pass


serveur = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=serveur.serve_forever, daemon=True).start()
port = serveur.server_address[1]

os.environ.update(
    {
        "LLM_API_KEY": "sk-test-local",
        "LLM_BASE_URL": "http://127.0.0.1:%d/v1" % port,
        "LLM_MODEL": "fake-model",
        "LLM_TEMPERATURE": "0",
        "LLM_MAX_TOKENS": "512",
        "MAX_ITERATIONS": "4",
    }
)

from agent import main  # noqa: E402

code = main(["-p", "Lis le début de src/config.py", "--no-log", "--quiet"])
serveur.shutdown()

RESULTATS = []


def check(label, condition, detail=""):
    suffixe = (" | " + str(detail)) if (detail and not condition) else ""
    RESULTATS.append(("OK " if condition else "KO ") + label + suffixe)


check("e2e code sortie 0", code == 0, code)
check("e2e deux appels", len(REQUETES) == 2, len(REQUETES))
premier = REQUETES[0]
check("e2e modele transmis", premier.get("model") == "fake-model", premier.get("model"))
check("e2e 5 outils transmis", len(premier.get("tools") or []) == 5, [t["function"]["name"] for t in premier.get("tools") or []])
check("e2e tool_choice auto", premier.get("tool_choice") == "auto", premier.get("tool_choice"))
check("e2e parametres generation", premier.get("temperature") == 0 and premier.get("max_tokens") == 512, premier.get("temperature"))
second = REQUETES[1]
roles = [m["role"] for m in second["messages"]]
check("e2e historique renvoye", roles == ["system", "user", "assistant", "tool"], roles)
outil = [m for m in second["messages"] if m["role"] == "tool"][0]
check("e2e resultat outil reel", "Configuration de l'agent" in outil["content"] and outil["tool_call_id"] == "call_1", outil["content"][:120])
check("e2e plage de lignes respectee", outil["content"].count("\n") <= 3, outil["content"][:200])

print("\n".join(RESULTATS))
print("TOTAL=%d KO=%d" % (len(RESULTATS), sum(1 for ligne in RESULTATS if ligne.startswith("KO"))))
