"""Package principal de l'agent autonome local.

Modules :
    - ``main``       : point d'entrée (bannière seule, ou session complète avec une tâche)
    - ``config``     : configuration et secrets (.env)
    - ``llm_client`` : client d'API compatible OpenAI
    - ``agent``      : boucle agentique bornée (raisonner / agir / observer)
    - ``context``    : instantané du projet et budget d'historique
    - ``ui``         : interface interactive (REPL multi-tours, rendu rich)
    - ``tools``      : outils locaux (fichiers, recherche, commandes) et bac à sable
"""

__version__ = "0.8.0"

