"""
Exécuteurs d'actions de remédiation — Jour 10.

Seules les 2 actions de la liste blanche (guardrails/policy.py) ont un
exécuteur ici. Le client Docker est injectable pour permettre les tests
sans Docker réel (cf. tests avec FakeDockerClient plus bas dans le repo
de tests) -- le code de production utilise `docker.from_env()`.
"""

import logging

logger = logging.getLogger("sentinelops.remediation")


class ActionExecutionError(Exception):
    pass


def restart_container(docker_client, container_name: str) -> dict:
    """
    Redémarre un conteneur. Action jugée réversible : un restart ramène
    le conteneur à un état sain connu (image + config inchangées).
    """
    try:
        container = docker_client.containers.get(container_name)
        container.restart(timeout=10)
        logger.info(f"restart_container: {container_name} redémarré")
        return {"status": "success", "action": "restart_container", "target": container_name}
    except Exception as exc:
        logger.error(f"restart_container: échec sur {container_name} ({exc})")
        raise ActionExecutionError(f"Échec du restart de {container_name}: {exc}") from exc


def scale_replica(docker_client, service_name: str, replicas: int = 2) -> dict:
    """
    Augmente le nombre de réplicas d'un service. Action réversible :
    un scale-down ultérieur ramène au nombre initial (cf. rollback dans
    post_action_check.py).
    """
    try:
        service = docker_client.services.get(service_name)
        service.scale(replicas)
        logger.info(f"scale_replica: {service_name} mis à l'échelle à {replicas} réplicas")
        return {"status": "success", "action": "scale_replica", "target": service_name, "replicas": replicas}
    except Exception as exc:
        logger.error(f"scale_replica: échec sur {service_name} ({exc})")
        raise ActionExecutionError(f"Échec du scale de {service_name}: {exc}") from exc


ACTION_EXECUTORS = {
    "restart_container": restart_container,
    "scale_replica": scale_replica,
}


def execute_action(docker_client, action: str, target: str, **kwargs) -> dict:
    """
    Point d'entrée unique d'exécution. Refuse explicitement toute action
    absente de ACTION_EXECUTORS -- double sécurité en plus du guardrail
    (défense en profondeur : même si guardrails/policy.py était contourné
    par erreur ailleurs dans le code, cette fonction refuserait quand même).
    """
    executor = ACTION_EXECUTORS.get(action)
    if executor is None:
        raise ActionExecutionError(f"Action '{action}' non exécutable : absente de ACTION_EXECUTORS.")
    return executor(docker_client, target, **kwargs)