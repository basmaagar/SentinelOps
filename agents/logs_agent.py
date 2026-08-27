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
from metric_context import contexte_journaux, COMPOSANTS_VALIDES

SYSTEM_PROMPT = """Tu es un ingénieur SRE. Tu analyses EXCLUSIVEMENT des messages de \
journaux, transformés en templates. Tu n'as accès à AUCUNE métrique numérique.

Ta tâche n'est PAS de décrire ce qu'est un template — c'est de dire ce que ces messages \
révèlent sur l'état du système.

Réponds UNIQUEMENT avec un objet JSON, sans texte avant ni après :
{
  "hypothesis": "cause probable, formulée comme un mécanisme et non comme un constat",
  "evidence": ["citation du template et ce qu'il révèle", "seconde observation"],
  "confidence": 0.0,
  "composant_suspecte": "target-app"
}

REGLES IMPERATIVES :
1. "composant_suspecte" doit valoir EXACTEMENT l'un de : __COMPOSANTS__.
   Le contexte ci-dessous indique quel composant produit chaque type de message.
2. "hypothesis" doit décrire un MÉCANISME dans le système supervisé, et
   non commenter le message lui-même.
   Appuie-toi sur les causes listées dans le CONTEXTE ci-dessous, qui
   correspondent aux messages réellement présents. Ne reprends aucune cause
   concernant un type de message absent des données.
   MAUVAIS : "le template de log est une erreur de formatage" (parle du log, pas du système)
   MAUVAIS : "les templates de logs sont des faux" (ne dit rien du système)
3. "evidence" est une LISTE de chaînes. Chaque preuve cite un template
   RÉELLEMENT présent ci-dessous. N'invente aucun message.
4. Un template marqué "inédit" apparaît pour la première fois : c'est un
   signal fort, un type d'erreur nouveau vient d'apparaître.
5. "confidence" est un nombre décimal entre 0.0 et 1.0, sans guillemets.
6. Ne recopie aucun exemple de ce message : ce sont des illustrations."""


def build_prompt(anomaly_events: list[dict]) -> str:
    """
    Assemble le prompt : consignes, contexte métier, puis templates observés.

    Comme pour l'agent métriques, le contexte est ce qui rend la tâche
    possible : un template tel que `DependencyTimeout: call exceeded SLA`
    ne signifie rien pour un petit modèle s'il ignore ce qu'est une
    dépendance et ce qu'implique un dépassement de SLA. Sans cela, le
    modèle commentait la FORME du message au lieu d'analyser le système —
    d'où des diagnostics du type « le template est une erreur de formatage ».
    """
    # Substitution ciblée plutôt que str.format() : le prompt contient un
    # gabarit JSON, dont les accolades seraient interprétées comme des
    # champs de formatage et feraient échouer l'appel.
    consignes = SYSTEM_PROMPT.replace("__COMPOSANTS__", ", ".join(COMPOSANTS_VALIDES))
    contexte = contexte_journaux(anomaly_events)
    donnees = json.dumps(anomaly_events, ensure_ascii=False, indent=2)
    return (f"{consignes}\n\n"
            f"CONTEXTE — ce que signifient ces messages :\n{contexte}\n\n"
            f"DONNÉES — templates détectés :\n{donnees}")


def run_logs_agent(llm_client, model: str, log_anomaly_events: list[dict],
                    max_retries: int = 2) -> AgentHypothesis:
    prompt = build_prompt(log_anomaly_events)
    return run_agent(llm_client, model, prompt, agent_name="logs", max_retries=max_retries)