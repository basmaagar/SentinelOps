"""
Vérification post-action et rollback — Jour 10.

Réponse à la remarque de l'encadrante : "un garde-fou qui autorise une
action automatique doit aussi savoir comment annuler si l'action aggrave
la situation [...] même une action jugée sûre peut mal tourner".

Version heuristique volontairement assumée (PAS un modèle ML entraîné) :
on réutilise le détecteur statistique du Jour 3 sur un échantillon
post-action et on classe le résultat "positive" (l'anomalie a disparu) ou
"negative" (elle persiste). Un vrai classifieur supervisé demanderait des
centaines d'exemples labellisés que ce prototype ne peut pas produire en
3 semaines (cf. discussion — limite de VOLUME DE DONNÉES, pas de temps de
développement). Ce module pose néanmoins les fondations exactes dont un
futur classifieur aurait besoin : chaque vérification est journalisée
avec son échantillon "avant/après" et sa classification, exploitable
telle quelle comme jeu de données d'entraînement futur.
"""

from dataclasses import dataclass

from actions import execute_action, ActionExecutionError


@dataclass
class PostActionOutcome:
    classification: str  # "positive" | "negative"
    metrics_sample_after: dict
    rollback_performed: bool
    rollback_result: dict | None


def _rollback_scale_replica(docker_client, service_name: str, original_replicas: int) -> dict:
    service = docker_client.services.get(service_name)
    service.scale(original_replicas)
    return {"status": "rolled_back", "action": "scale_replica", "target": service_name, "replicas": original_replicas}


def _escalate_no_auto_rollback(target: str) -> dict:
    # Un restart de conteneur n'a pas d'action "inverse" significative :
    # on ne peut pas annuler un redémarrage. On signale explicitement
    # qu'aucun rollback automatique n'est possible et qu'une escalade
    # humaine est nécessaire, plutôt que de prétendre avoir résolu le problème.
    return {"status": "escalation_required", "action": "restart_container", "target": target,
            "reason": "aucun rollback automatique possible pour un redémarrage"}


def verify_and_rollback(metric_detector, ts: float, metrics_sample_after: dict,
                         action: str, target: str, docker_client=None,
                         original_replicas: int | None = None) -> PostActionOutcome:
    """
    A appeler après un délai d'observation suite à l'exécution d'une action
    automatique (cf. roadmap : fenêtre de vérification, ex. 60s).

    metrics_sample_after : nouvel échantillon de métriques observé sur la
    cible, APRES l'action, à faire passer dans le même détecteur que celui
    qui a initialement repéré l'anomalie (cohérence de la comparaison).
    """
    events = metric_detector.process_sample(ts, metrics_sample_after)
    still_anomalous = any(event.is_anomaly for event in events)
    classification = "negative" if still_anomalous else "positive"

    rollback_performed = False
    rollback_result = None

    if classification == "negative":
        if action == "scale_replica" and original_replicas is not None and docker_client is not None:
            rollback_result = _rollback_scale_replica(docker_client, target, original_replicas)
            rollback_performed = True
        else:
            rollback_result = _escalate_no_auto_rollback(target)
            rollback_performed = False

    return PostActionOutcome(
        classification=classification,
        metrics_sample_after=metrics_sample_after,
        rollback_performed=rollback_performed,
        rollback_result=rollback_result,
    )