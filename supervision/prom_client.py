"""
Client de lecture Prometheus — Jour 12.

Jusqu'ici, le graphe était invoqué à la main avec des dictionnaires de
métriques codés en dur (`{"cpu": 95.0}`). C'était suffisant pour valider
la logique des agents, mais cela rendait impossible toute mesure de TTD :
sans source réelle, il n'y a pas de délai entre "la panne apparaît" et
"le système la voit".

Ce module interroge l'API HTTP de Prometheus (/api/v1/query) en PromQL.
On ne lit JAMAIS directement la TSDB sur disque : c'est un format interne
non documenté, et l'API est le contrat stable.

Choix : requêtes instantanées (instant query) et non `query_range`. On
veut l'état courant à chaque tick de la boucle, pas un historique — la
fenêtre glissante est déjà tenue en mémoire par le détecteur (Jour 3).
"""

import logging

import requests

logger = logging.getLogger("sentinelops.supervision.prometheus")

# Nom du conteneur cible tel que déclaré dans docker-compose.yml.
#
# On interroge cAdvisor avec un motif (=~) et non une égalité stricte :
# selon la version de cAdvisor et la plateforme (Docker Desktop sous
# Windows notamment), l'étiquette `name` peut être préfixée d'un "/" ou
# porter un suffixe. Une égalité stricte renvoyait silencieusement zéro
# série — les métriques conteneur étaient donc simplement absentes de
# chaque échantillon, sans le moindre message d'erreur.
TARGET_CONTAINER = "sentinelops-target-app"
TARGET_CONTAINER_PATTERN = f".*{TARGET_CONTAINER}.*"

# Les métriques effectivement collectées à chaque tick.
#
# Deux sources volontairement mêlées :
#  - app_* : ce que l'application expose via prometheus_client (sémantique
#    métier : charge simulée, injections actives, latence applicative).
#  - container_* : ce que cAdvisor mesure réellement au niveau conteneur.
#
# Aucune des deux ne suffit seule : l'app ne connaît pas sa consommation
# réelle, et cAdvisor ne connaît pas le sens métier de ce qui se passe.
DEFAULT_QUERIES: dict[str, str] = {
    "cpu_simule_pct": "app_simulated_cpu_load_percent",
    "disque_injecte_mb": "app_disk_injection_active_mb",
    "memoire_injectee_mb": "app_memory_injection_active_mb",
    "latence_injectee_ms": "app_latency_injection_active_ms",
    # Latence applicative observée (p95 sur 30s) — la vraie métrique de
    # dégradation, indépendante du fait qu'une injection soit déclarée.
    #
    # ATTENTION : sur un système au repos, cette métrique est très
    # instable. Avec très peu de requêtes dans la fenêtre, le quantile est
    # calculé sur une poignée d'échantillons et saute brutalement dès
    # qu'une requête isolée arrive — ce qui a produit une fausse alerte
    # `latence_p95_ms` lors du premier lancement, sans aucune injection.
    #
    # Le garde-fou : on ne renvoie le quantile que si le trafic est
    # suffisant (au moins ~0.2 req/s sur la fenêtre). En dessous, la
    # requête ne renvoie aucune série, la métrique est simplement absente
    # de l'échantillon, et la fenêtre glissante n'est pas polluée.
    "latence_p95_ms": (
        "1000 * histogram_quantile(0.95, "
        "sum(rate(app_request_latency_seconds_bucket[30s])) by (le)) "
        "and on() (sum(rate(app_request_latency_seconds_count[30s])) > 0.2)"
    ),
    # CPU réel du conteneur, en pourcentage d'un cœur.
    "cpu_conteneur_pct": (
        "100 * sum(rate(container_cpu_usage_seconds_total"
        f'{{name=~"{TARGET_CONTAINER_PATTERN}"}}[30s]))'
    ),
    # Mémoire réelle du conteneur, en Mo.
    "memoire_conteneur_mb": (
        f'sum(container_memory_usage_bytes{{name=~"{TARGET_CONTAINER_PATTERN}"}}) '
        "/ 1024 / 1024"
    ),
}


class PrometheusClient:
    def __init__(self, base_url: str = "http://localhost:9090", timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def instant_query(self, promql: str) -> float | None:
        """
        Retourne la valeur scalaire courante, ou None si la requête échoue
        ou ne renvoie aucune série.

        On retourne None plutôt que de lever : une métrique absente à un
        instant donné (ex: aucune requête HTTP depuis 30s, donc pas de
        quantile de latence) est un cas NORMAL, pas une erreur. La faire
        remonter en exception ferait planter la boucle de supervision sur
        un non-événement.
        """
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/query",
                params={"query": promql},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning(f"requête Prometheus échouée ({promql[:40]}...) : {exc}")
            return None

        if payload.get("status") != "success":
            logger.warning(f"Prometheus a répondu status={payload.get('status')}")
            return None

        results = payload.get("data", {}).get("result", [])
        if not results:
            return None

        try:
            value = float(results[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

        # Prometheus renvoie "NaN" en texte pour un quantile non calculable.
        if value != value:  # test NaN
            return None
        return value

    def sample(self, queries: dict[str, str] | None = None) -> dict[str, float]:
        """
        Collecte un échantillon complet. Les métriques indisponibles sont
        simplement absentes du dictionnaire retourné — le détecteur ne
        traite que les clés présentes, donc une métrique manquante ne
        casse jamais rien et ne pollue pas la fenêtre glissante avec un
        zéro artificiel (ce qui créerait un faux pic au retour).
        """
        queries = queries or DEFAULT_QUERIES
        collected: dict[str, float] = {}
        for name, promql in queries.items():
            value = self.instant_query(promql)
            if value is not None:
                collected[name] = value
        return collected

    def is_up(self) -> bool:
        """Vérifie que Prometheus répond, avant de démarrer la boucle."""
        return self.instant_query("up") is not None

    def report_missing(self, queries: dict[str, str] | None = None) -> list[str]:
        """
        Retourne la liste des métriques qui ne renvoient rien.

        Une requête PromQL qui ne correspond à aucune série ne produit
        aucune erreur : elle renvoie un résultat vide. Une métrique mal
        nommée disparaît donc silencieusement de la supervision, sans
        aucun signal — c'est exactement ce qui s'est produit avec les
        métriques cAdvisor. On vérifie donc explicitement au démarrage.
        """
        queries = queries or DEFAULT_QUERIES
        return [name for name, promql in queries.items()
                if self.instant_query(promql) is None]