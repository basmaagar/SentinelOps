"""
Agent Logs — Jour 6.

Persona : analyse EXCLUSIVEMENT les templates de logs pré-extraits par
Drain3 (Jour 4). Anti-collusion : ignore volontairement toute mention de
métriques numériques.

Pourquoi un persona aussi strictement séparé de l'Agent Métriques (même
structure de prompt, mais périmètre de données totalement disjoint) ?
-> C'est la condition technique du multi-agents "collaboratif et non
collusif" exigée par le cahier des charges : si les deux agents avaient
accès aux mêmes données, leur "accord" ne prouverait rien (ils
convergeraient trivialement). En les isolant, un accord entre les deux
devient un signal fort de confiance pour l'Arbitre (Jour 8).
"""

import json

from agent_utils import run_agent
from schemas import AgentHypothesis

SYSTEM_PROMPT = """Tu es un ingénieur SRE expert, spécialisé EXCLUSIVEMENT dans l'analyse de \
journaux applicatifs structurés (templates de logs extraits automatiquement, avec leur \
fréquence d'apparition). Tu n'as accès à AUCUNE métrique numérique (CPU, mémoire, latence) : \
base uniquement ton raisonnement sur les templates de logs fournis.

Réponds UNIQUEMENT avec un objet JSON strictement conforme à ce format, sans aucun texte \
avant ou après :
{
  "hypothesis": "<description courte de la cause probable>",
  "evidence": ["<preuve 1 tirée des templates de logs>", "<preuve 2>"],
  "confidence": <nombre entre 0.0 et 1.0>,
  "composant_suspecte": "<nom du composant>"
}"""


def build_prompt(log_anomaly_events: list[dict]) -> str:
    """
    log_anomaly_events: sortie pré-agrégée du détecteur Drain3 (Jour 4),
    jamais de lignes de logs brutes en volume. Exemple d'élément :
    {"template": "DependencyTimeout: call exceeded SLA <*>", "count_in_bucket": 8,
     "reason": "new_template", "severity": "medium"}
    """
    events_json = json.dumps(log_anomaly_events, ensure_ascii=False, indent=2)
    return f"{SYSTEM_PROMPT}\n\nTemplates de logs anormaux détectés :\n{events_json}"


def run_logs_agent(llm_client, model: str, log_anomaly_events: list[dict],
                    max_retries: int = 2) -> AgentHypothesis:
    prompt = build_prompt(log_anomaly_events)
    return run_agent(llm_client, model, prompt, agent_name="logs", max_retries=max_retries)