"""Package principal de l'agent autonome local.

Modules :
    - ``main``       : point d'entrée (bannière seule, ou session complète avec une tâche)
    - ``config``     : configuration et secrets (.env)
    - ``llm_client`` : client d'API compatible OpenAI
    - ``agent``      : boucle agentique bornée (raisonner / agir / observer)
    - ``tools``      : outils locaux (fichiers, recherche, commandes) et bac à sable
"""

__version__ = "0.3.0"

