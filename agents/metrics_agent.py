"""
Agent Métriques — Jour 5 (refactorisé Jour 6 pour utiliser agent_utils).

Persona : analyse EXCLUSIVEMENT les métriques temporelles agrégées.
Anti-collusion : ignore volontairement toute mention de logs.
"""

import json

from agent_utils import run_agent, FALLBACK_HYPOTHESIS  # noqa: F401 (ré-exporté pour compat)
from schemas import AgentHypothesis  # noqa: F401 (ré-exporté pour compat)

SYSTEM_PROMPT = """Tu es un ingénieur SRE expert, spécialisé EXCLUSIVEMENT dans l'analyse de \
métriques temporelles agrégées (CPU, mémoire, disque, latence). Tu n'as accès à AUCUN journal \
applicatif : base uniquement ton raisonnement sur les métriques fournies.

Réponds UNIQUEMENT avec un objet JSON strictement conforme à ce format, sans aucun texte \
avant ou après :
{
  "hypothesis": "<description courte de la cause probable>",
  "evidence": ["<preuve 1 tirée des métriques>", "<preuve 2>"],
  "confidence": <nombre entre 0.0 et 1.0>,
  "composant_suspecte": "<nom du composant>"
}"""


def build_prompt(anomaly_events: list[dict]) -> str:
    events_json = json.dumps(anomaly_events, ensure_ascii=False, indent=2)
    return f"{SYSTEM_PROMPT}\n\nAnomalies détectées :\n{events_json}"


def run_metrics_agent(llm_client, model: str, anomaly_events: list[dict],
                       max_retries: int = 2) -> AgentHypothesis:
    prompt = build_prompt(anomaly_events)
    return run_agent(llm_client, model, prompt, agent_name="metrics", max_retries=max_retries)