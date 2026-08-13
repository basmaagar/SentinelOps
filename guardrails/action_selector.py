"""
Sélection de l'action candidate — Jour 9, corrigé au Jour 12.

Volontairement déterministe (mapping composant -> action), PAS un appel
LLM : la sélection d'action est un point de sécurité critique, elle doit
rester prévisible et auditable. Un LLM pourrait halluciner une action
plausible mais absente de la liste blanche ; ici, une action hors mapping
retombe simplement sur "aucune action candidate" (jamais d'invention).

--- Correctif Jour 12 ---

Défaut constaté en conditions réelles : la correspondance se faisait par
égalité stricte sur le nom de composant produit par le LLM, en texte libre.
Les modèles renvoyant "Application", "inconnu" ou "l'application cible",
AUCUN incident réel ne trouvait d'action candidate. Toute la partie
remédiation du système — garde-fou, exécution, verrou, vérification
post-action — n'était donc jamais atteinte en fonctionnement réel, alors
que les tests unitaires (qui passaient "target-app" en dur) réussissaient
tous. Le défaut était invisible en test et bloquant en production.

Le nom est désormais résolu via l'inventaire partagé (component_inventory),
qui gère les alias. La résolution reste stricte : en cas de doute, on
retourne "inconnu" et aucune action n'est proposée. On ne devine jamais une
cible d'action.

Limite assumée (à documenter) : ce mapping est volontairement simple pour
le périmètre du prototype (3 types de pannes). Une version production
devrait probablement lier l'action au TYPE d'incident plutôt qu'au seul
nom de composant.
"""

from component_inventory import resolve_component, UNKNOWN_COMPONENT

COMPONENT_ACTION_MAP: dict[str, str] = {
    "target-app": "restart_container",
    # Correctif Jour 13. `scale_replica` était initialement rattaché ici,
    # mais il s'appuie sur l'API Docker Swarm (`services.scale`), absente
    # d'un environnement `docker compose` classique : toute tentative
    # échouait avec « This node is not a swarm manager ». Le garde-fou
    # autorisait donc correctement l'action, mais elle n'était jamais
    # exécutée — la boucle de remédiation restait non démontrée.
    #
    # Le redémarrage du conteneur applicatif est une remédiation réelle
    # pour une dégradation de dépendance : il réinitialise l'état de
    # connexion du client (pool de connexions, disjoncteur), ce qui est une
    # action d'exploitation courante face à une dépendance lente. Elle est
    # de surcroît exécutable dans l'environnement du prototype.
    #
    # `scale_replica` reste dans la liste blanche du garde-fou : la
    # politique de risque associée est écrite et testée, seule son
    # exécution dépend d'un environnement Swarm. À mentionner comme limite
    # d'environnement, et non comme fonctionnalité absente.
    "dependency-service": "restart_container",
}


def select_action(composant_suspecte: str) -> str | None:
    """
    Retourne l'action candidate pour un composant, ou None.

    None signifie "aucune action candidate" et non "erreur" : c'est un
    résultat légitime et fréquent, notamment quand le modèle n'a pas su
    nommer de composant.
    """
    canonical = resolve_component(composant_suspecte)
    if canonical == UNKNOWN_COMPONENT:
        return None
    return COMPONENT_ACTION_MAP.get(canonical)


def resolve_and_select(composant_suspecte: str) -> tuple[str, str | None]:
    """
    Variante retournant aussi le nom canonique, pour la journalisation :
    il est utile de tracer que "Application" a été résolu en "target-app",
    sans quoi l'audit d'une décision serait incompréhensible.
    """
    canonical = resolve_component(composant_suspecte)
    if canonical == UNKNOWN_COMPONENT:
        return canonical, None
    return canonical, COMPONENT_ACTION_MAP.get(canonical)