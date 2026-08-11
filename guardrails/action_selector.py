"""
Sélection de l'action candidate — Jour 9.

Volontairement déterministe (mapping composant -> action), PAS un appel
LLM : la sélection d'action est un point de sécurité critique, elle doit
rester prévisible et auditable. Un LLM pourrait halluciner une action
plausible mais absente de la liste blanche ; ici, une action hors mapping
retombe simplement sur "aucune action candidate" (jamais d'invention).

Limite assumée (à documenter) : ce mapping est volontairement simple pour
le périmètre du prototype (3 types de pannes). Une version production
devrait probablement lier l'action au TYPE d'incident plutôt qu'au seul
nom de composant.
"""

COMPONENT_ACTION_MAP: dict[str, str] = {
    "target-app": "restart_container",
    "dependency-service": "scale_replica",
}


def select_action(composant_suspecte: str) -> str | None:
    return COMPONENT_ACTION_MAP.get(composant_suspecte)