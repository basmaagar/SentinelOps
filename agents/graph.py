"""
Orchestration LangGraph — Jour 7.

Graphe : detect -> [agent_metrics, agent_logs] en parallèle -> END.

Pourquoi un routage conditionnel après `detect` plutôt qu'appeler les
agents systématiquement ? -> Contrainte de latence du cahier des charges :
un appel LLM local coûte 5-20s. Si la couche statistique ne détecte
AUCUNE anomalie, invoquer les agents serait un gaspillage pur (et casserait
l'objectif TTD < 60s sur un run de supervision continue). Le graphe ne
bascule vers les agents QUE si `detect` a confirmé une anomalie.

Pourquoi les deux agents en parallèle (retour d'une LISTE de nœuds dans la
fonction de routage) plutôt qu'un enchaînement séquentiel agent_metrics
puis agent_logs ? -> C'est la justification technique du "multi-agents"
par opposition à un script séquentiel (cf. cahier des charges, section
Architecture) : les deux hypothèses doivent être produites indépendamment,
sans que l'une influence l'autre.
"""

import sys
import time
import pathlib
from typing import TypedDict

sys.path.append(str(pathlib.Path(__file__).parent))
sys.path.append(str(pathlib.Path(__file__).parent.parent / "detector"))

from langgraph.graph import StateGraph, END

from metrics_agent import run_metrics_agent
from logs_agent import run_logs_agent
from arbiter_agent import run_arbiter
from anomaly_detector import AnomalyDetector
from log_anomaly_detector import LogAnomalyDetector


class SentinelState(TypedDict, total=False):
    ts: float
    metrics_sample: dict
    log_lines: list[str]
    anomaly_metrics_events: list[dict]
    anomaly_log_events: list[dict]
    anomaly_detected: bool
    metrics_hypothesis: dict
    logs_hypothesis: dict
    arbiter_verdict: dict


def _metric_event_to_dict(event) -> dict:
    return {
        "metric": event.metric,
        "value": event.value,
        "z_score": event.z_score,
        "severity": event.severity,
    }


def _log_event_to_dict(event) -> dict:
    return {
        "template": event.template,
        "count_in_bucket": event.count_in_bucket,
        "reason": event.reason,
        "severity": event.severity,
    }


def make_detect_node(metric_detector: AnomalyDetector, log_detector: LogAnomalyDetector):
    def detect_node(state: SentinelState) -> dict:
        ts = state.get("ts", time.time())
        metrics_sample = state.get("metrics_sample", {})
        log_lines = state.get("log_lines", [])

        metric_events = []
        if metrics_sample:
            for event in metric_detector.process_sample(ts, metrics_sample):
                if event.is_anomaly:
                    metric_events.append(_metric_event_to_dict(event))

        log_events = []
        for line in log_lines:
            for event in log_detector.process_log_line(ts, line):
                log_events.append(_log_event_to_dict(event))

        return {
            "anomaly_metrics_events": metric_events,
            "anomaly_log_events": log_events,
            "anomaly_detected": bool(metric_events or log_events),
        }
    return detect_node


def make_agent_metrics_node(llm_client, model: str):
    def node(state: SentinelState) -> dict:
        events = state.get("anomaly_metrics_events", [])
        if not events:
            # Cette modalité n'a rien détecté (l'anomalie vient de l'autre
            # agent) : inutile d'invoquer le LLM, et cela évite de lui
            # laisser halluciner un diagnostic sans preuve disponible.
            return {"metrics_hypothesis": _no_evidence_hypothesis()}
        hypothesis = run_metrics_agent(llm_client, model, events)
        return {"metrics_hypothesis": hypothesis.model_dump()}
    return node


def make_agent_logs_node(llm_client, model: str):
    def node(state: SentinelState) -> dict:
        events = state.get("anomaly_log_events", [])
        if not events:
            return {"logs_hypothesis": _no_evidence_hypothesis()}
        hypothesis = run_logs_agent(llm_client, model, events)
        return {"logs_hypothesis": hypothesis.model_dump()}
    return node


def _no_evidence_hypothesis() -> dict:
    return {
        "hypothesis": "Aucune anomalie corrélée dans cette modalité",
        "evidence": ["Aucun événement détecté dans la fenêtre courante pour cette modalité"],
        "confidence": 0.0,
        "composant_suspecte": "aucun",
    }


def make_arbiter_node(llm_client, model: str):
    def node(state: SentinelState) -> dict:
        metrics_hyp = state.get("metrics_hypothesis") or _no_evidence_hypothesis()
        logs_hyp = state.get("logs_hypothesis") or _no_evidence_hypothesis()

        # Les données réellement transmises aux agents sont passées à
        # l'arbitre (Jour 12). C'est indispensable pour vérifier en code
        # que les preuves citées existent bien dans ces données : sans la
        # source, l'ancrage des preuves ne serait pas calculable et la
        # confiance resterait une simple auto-évaluation du modèle.
        metrics_events = state.get("anomaly_metrics_events", [])
        log_events = state.get("anomaly_log_events", [])

        verdict = run_arbiter(
            llm_client, model, metrics_hyp, logs_hyp,
            metrics_observed=metrics_events, logs_observed=log_events,
            metrics_events=metrics_events, logs_events=log_events,
        )
        return {"arbiter_verdict": verdict.model_dump()}
    return node


def route_after_detect(state: SentinelState):
    if state.get("anomaly_detected"):
        # Retourner une liste de noms de nœuds déclenche un fan-out parallèle
        # natif de LangGraph : les deux agents s'exécutent indépendamment.
        return ["agent_metrics", "agent_logs"]
    return END


def build_graph(metrics_client, metrics_model: str, logs_client, logs_model: str,
                 arbiter_client=None, arbiter_model: str | None = None,
                 metric_detector: AnomalyDetector | None = None,
                 log_detector: LogAnomalyDetector | None = None):
    """
    Construit et compile le graphe. Les détecteurs sont injectables (sinon
    créés avec les paramètres par défaut) pour permettre de contrôler
    précisément l'état de la fenêtre glissante dans les tests.

    arbiter_client/arbiter_model: par défaut, réutilise le client/modèle
    des métriques si non fournis (un 3e modèle distinct n'apporte pas de
    garantie supplémentaire ici : l'anti-collusion concerne les DEUX
    agents d'INVESTIGATION, pas l'arbitre qui n'a par nature accès qu'à
    leurs conclusions déjà indépendantes).
    """
    metric_detector = metric_detector or AnomalyDetector()
    log_detector = log_detector or LogAnomalyDetector()
    arbiter_client = arbiter_client or metrics_client
    arbiter_model = arbiter_model or metrics_model

    builder = StateGraph(SentinelState)
    builder.add_node("detect", make_detect_node(metric_detector, log_detector))
    builder.add_node("agent_metrics", make_agent_metrics_node(metrics_client, metrics_model))
    builder.add_node("agent_logs", make_agent_logs_node(logs_client, logs_model))
    builder.add_node("arbiter", make_arbiter_node(arbiter_client, arbiter_model))

    builder.set_entry_point("detect")
    builder.add_conditional_edges("detect", route_after_detect)
    # Les deux arêtes convergent vers "arbiter" : LangGraph attend que les
    # DEUX agents aient terminé avant d'exécuter ce nœud (jointure native).
    builder.add_edge("agent_metrics", "arbiter")
    builder.add_edge("agent_logs", "arbiter")
    builder.add_edge("arbiter", END)

    return builder.compile()