"""Point d'entrée de l'agent autonome local.

Sans argument, le script affiche la bannière d'initialisation (étape 1).
Avec une tâche, il délègue à :mod:`agent` (boucle agentique + outils).

Utilisation :
    python src/main.py
    python src/main.py -p "Résume la structure du projet"
    python src/main.py --dry-run
"""

import sys
from typing import Optional, Sequence

MESSAGE_INITIALISATION = "Agent initialisé et prêt à travailler"


def afficher_banniere(message: str = MESSAGE_INITIALISATION) -> str:
    """Affiche le message d'initialisation de l'agent.

    Args:
        message: texte à afficher (par défaut le message d'initialisation).

    Returns:
        Le message affiché, ce qui facilite les tests et la réutilisation.
    """
    print(message)
    return message


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Démarre l'agent (bannière seule, ou session complète avec une tâche).

    Args:
        argv: arguments de la ligne de commande (par défaut ``sys.argv[1:]``).

    Returns:
        Code de sortie du processus (0 = succès).
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        afficher_banniere()
        print('Utilisez « python src/main.py -p "votre tâche" » pour lancer une session.')
        return 0

    try:  # exécution en package : python -m src.main
        from .agent import main as agent_main
    except ImportError:  # exécution directe : python src/main.py
        from agent import main as agent_main

    return agent_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())

