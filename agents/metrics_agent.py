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
  "hypothesis": "phrase courte decrivant la cause probable",
  "evidence": ["citation exacte d une metrique et de sa valeur", "seconde citation"],
  "confidence": 0.0,
  "composant_suspecte": "nom du composant"
}

REGLES IMPERATIVES :
- "evidence" doit etre une LISTE de chaines de caracteres, jamais un objet.
- Chaque preuve doit citer une metrique REELLEMENT presente dans les donnees
  fournies ci-dessous, avec sa valeur. N invente jamais de metrique ni de valeur.
- Ne recopie PAS l exemple ci-dessus : remplace chaque valeur par ton analyse.
- "confidence" est un nombre decimal entre 0.0 et 1.0, sans guillemets."""


def build_prompt(anomaly_events: list[dict]) -> str:
    events_json = json.dumps(anomaly_events, ensure_ascii=False, indent=2)
    return f"{SYSTEM_PROMPT}\n\nAnomalies détectées :\n{events_json}"


def run_metrics_agent(llm_client, model: str, anomaly_events: list[dict],
                       max_retries: int = 2) -> AgentHypothesis:
    prompt = build_prompt(anomaly_events)
    return run_agent(llm_client, model, prompt, agent_name="metrics", max_retries=max_retries)