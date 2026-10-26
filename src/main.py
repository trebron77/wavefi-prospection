"""Point d'entrée de l'agent autonome local.

Étape 1 : l'agent se contente de s'initialiser et d'afficher un message.
Aucun appel réseau n'est effectué à ce stade.

Utilisation :
    python src/main.py
"""

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


def main() -> int:
    """Démarre l'agent.

    Returns:
        Code de sortie du processus (0 = succès).
    """
    afficher_banniere()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
