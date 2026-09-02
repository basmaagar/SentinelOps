"""
Exécution des actions correctives.

Deux principes gouvernent ce module :

DÉFENSE EN PROFONDEUR. `execute_action` refuse toute action absente de
`ACTION_EXECUTORS`, alors même que le garde-fou a déjà filtré en amont.
C'est volontairement redondant : si le garde-fou était contourné par un
bug, l'exécuteur bloquerait quand même. Le point d'entrée d'une action sur
un système réel ne doit pas dépendre de la correction d'un composant amont.

PÉRIMÈTRE RESTREINT ET AUDITABLE. Deux actions seulement, toutes deux
réversibles, toutes deux journalisées.

--- Correctif Jour 14 : scale_replica devient exécutable ---

La première implémentation utilisait `docker_client.services.scale()`, qui
relève de l'API Docker **Swarm**. En environnement `docker compose`
classique, tout appel échouait avec « This node is not a swarm manager ».
Le garde-fou autorisait donc correctement l'action, mais elle n'aboutissait
jamais : la branche « action à risque faible » de la politique de risque
restait purement théorique.

Deux conditions ont été réunies pour la rendre réelle :

1. `dependency-service` est devenu un vrai conteneur, sans port publié et
   sans nom de conteneur figé — les deux obstacles qui empêchent Compose
   de mettre un service à l'échelle.
2. La mise à l'échelle passe par la commande `docker compose up -d
   --scale`, qui est le mécanisme natif de Compose, plutôt que par l'API
   Swarm.

Pourquoi une commande externe plutôt que le SDK Python : le SDK Docker
n'expose pas la notion de service Compose, seulement des conteneurs
individuels. Créer une réplique « à la main » supposerait de reproduire le
réseau, les variables d'environnement et les étiquettes que Compose gère —
et toute divergence produirait une réplique subtilement différente des
autres. Déléguer à Compose garantit que les répliques sont identiques.
"""

import os
import shutil
import logging
import pathlib
import subprocess

logger = logging.getLogger("sentinelops.remediation.actions")


class ActionExecutionError(Exception):
    """Échec d'exécution d'une action pourtant autorisée par le garde-fou."""


# Racine du projet : c'est depuis ce dossier que `docker compose` doit être
# lancé, sinon il ne trouve pas le fichier de composition et échoue.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Nom du SERVICE Compose, distinct du nom du conteneur. La mise à l'échelle
# porte sur le service ; le redémarrage porte sur un conteneur.
SCALABLE_SERVICE = os.getenv("SENTINELOPS_SCALABLE_SERVICE", "dependency-service")

# Bornes dures sur le nombre de répliques. Une action automatique ne doit
# pas pouvoir consommer les ressources de la machine, même en cas de boucle
# de décision imprévue : c'est la même logique de bornage que celle
# appliquée aux injecteurs de pannes.
REPLICAS_MIN = 1
REPLICAS_MAX = 3

SCALE_TIMEOUT_SECONDS = 90


def _compose_command() -> list[str]:
    """
    Détermine la forme disponible de la commande Compose.

    `docker compose` (plugin v2) est la forme moderne ; `docker-compose`
    (v1, binaire séparé) subsiste sur des installations plus anciennes. On
    teste plutôt que de supposer, faute de quoi l'échec surviendrait au
    pire moment — pendant une remédiation.
    """
    if shutil.which("docker"):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise ActionExecutionError(
        "ni « docker compose » ni « docker-compose » n'est disponible sur cet hôte")


def _run_compose(args: list[str]) -> str:
    commande = _compose_command() + args
    logger.info(f"exécution : {' '.join(commande)}")
    try:
        resultat = subprocess.run(
            commande, cwd=str(PROJECT_ROOT), capture_output=True,
            text=True, timeout=SCALE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionExecutionError(
            f"la commande Compose a dépassé {SCALE_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ActionExecutionError(f"impossible de lancer Compose : {exc}") from exc

    if resultat.returncode != 0:
        # La sortie d'erreur de Compose est verbeuse ; on la tronque pour
        # que le journal de décisions reste lisible, tout en gardant assez
        # de contexte pour diagnostiquer.
        detail = (resultat.stderr or resultat.stdout or "").strip()[:400]
        raise ActionExecutionError(f"Compose a échoué (code {resultat.returncode}) : {detail}")
    return (resultat.stdout or "").strip()


def count_replicas(service: str = SCALABLE_SERVICE) -> int:
    """
    Nombre de répliques actuellement en fonctionnement.

    Utilisé avant une mise à l'échelle pour connaître l'état de départ, et
    après un échec pour pouvoir revenir exactement à cet état. Sans cette
    lecture, un rollback devinerait au lieu de restaurer.
    """
    sortie = _run_compose(["ps", "-q", service])
    return len([ligne for ligne in sortie.splitlines() if ligne.strip()])


def scale_replica(docker_client, target: str, replicas: int | None = None) -> str:
    """
    Ajoute une réplique au service, dans les bornes autorisées.

    `docker_client` est ignoré : la mise à l'échelle passe par Compose et
    non par le SDK. Le paramètre est conservé pour que tous les exécuteurs
    partagent la même signature, ce qui permet au code appelant de les
    traiter uniformément.

    `target` est ici le nom du SERVICE Compose. Si l'appelant transmet un
    nom de conteneur — ce que fait la boucle de supervision, qui raisonne
    en conteneurs — on retombe sur le service scalable configuré.
    """
    service = target if target == SCALABLE_SERVICE else SCALABLE_SERVICE

    actuel = count_replicas(service)
    if replicas is None:
        replicas = actuel + 1
    replicas = max(REPLICAS_MIN, min(REPLICAS_MAX, replicas))

    if replicas == actuel:
        return (f"{service} déjà à {actuel} réplique(s) — "
                f"plafond de {REPLICAS_MAX} atteint, aucune modification")

    # `--no-recreate` est essentiel : sans lui, Compose recrée les
    # conteneurs existants, ce qui provoquerait une interruption de service
    # et ferait de `scale_replica` une action à risque modéré au lieu de
    # faible. Toute la justification de son seuil de confiance abaissé
    # repose sur l'absence d'interruption.
    _run_compose(["up", "-d", "--no-deps", "--no-recreate",
                  "--scale", f"{service}={replicas}", service])

    verifie = count_replicas(service)
    if verifie != replicas:
        raise ActionExecutionError(
            f"mise à l'échelle demandée à {replicas}, mais {verifie} réplique(s) "
            f"observée(s) après exécution")

    logger.info(f"{service} : {actuel} -> {replicas} répliques")
    return f"{service} mis à l'échelle de {actuel} à {replicas} répliques"


def unscale_replica(docker_client, target: str, replicas: int = REPLICAS_MIN) -> str:
    """
    Annulation exacte d'une mise à l'échelle.

    C'est cette opération qui justifie le classement de `scale_replica` en
    risque faible : l'état antérieur est restaurable à l'identique, ce qui
    n'est pas le cas d'un redémarrage.
    """
    service = target if target == SCALABLE_SERVICE else SCALABLE_SERVICE
    replicas = max(REPLICAS_MIN, min(REPLICAS_MAX, replicas))
    _run_compose(["up", "-d", "--no-deps", "--no-recreate",
                  "--scale", f"{service}={replicas}", service])
    return f"{service} ramené à {replicas} réplique(s)"


def restart_container(docker_client, target: str) -> str:
    """
    Redémarre un conteneur applicatif.

    Passe par le SDK Docker et non par Compose : l'opération porte sur un
    conteneur précis, pas sur un service, et le SDK est plus direct.

    Nuance à garder à l'esprit : le redémarrage est réversible au sens où
    l'état antérieur revient de lui-même, mais il n'a pas d'inverse — on ne
    peut défaire ni la coupure de service ni les requêtes perdues. C'est la
    raison pour laquelle un échec constaté après un redémarrage déclenche
    une escalade humaine plutôt qu'un rollback automatique.
    """
    if docker_client is None:
        raise ActionExecutionError("client Docker indisponible")
    try:
        conteneur = docker_client.containers.get(target)
        conteneur.restart(timeout=10)
    except Exception as exc:  # noqa: BLE001
        raise ActionExecutionError(f"échec du redémarrage de {target} : {exc}") from exc
    return f"conteneur {target} redémarré"


# Liste blanche d'exécution. Toute action absente d'ici est refusée, quelle
# que soit la décision du garde-fou.
ACTION_EXECUTORS = {
    "restart_container": restart_container,
    "scale_replica": scale_replica,
}


def execute_action(docker_client, action: str, target: str) -> str:
    if action not in ACTION_EXECUTORS:
        raise ActionExecutionError(
            f"action '{action}' non exécutable : absente de la liste blanche d'exécution")
    return ACTION_EXECUTORS[action](docker_client, target)