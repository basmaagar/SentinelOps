"""
Agent Métriques — Jour 5 (refactorisé Jour 6 pour utiliser agent_utils).

Persona : analyse EXCLUSIVEMENT les métriques temporelles agrégées.
Anti-collusion : ignore volontairement toute mention de logs.
"""

import json

from agent_utils import run_agent, FALLBACK_HYPOTHESIS  # noqa: F401 (ré-exporté pour compat)
from schemas import AgentHypothesis  # noqa: F401 (ré-exporté pour compat)
from metric_context import contexte_metriques, COMPOSANTS_VALIDES

SYSTEM_PROMPT = """Tu es un ingénieur SRE. Tu analyses EXCLUSIVEMENT des métriques \
numériques. Tu n'as accès à AUCUN journal applicatif.

Ta tâche n'est PAS de constater qu'il y a une anomalie — le système de détection l'a déjà \
fait. Ta tâche est d'expliquer CE QUI la provoque, en t'appuyant sur le contexte fourni.

Réponds UNIQUEMENT avec un objet JSON, sans texte avant ni après :
{
  "hypothesis": "cause probable, formulée comme un mécanisme et non comme un constat",
  "evidence": ["metrique = valeur, et ce que cette valeur indique", "seconde observation"],
  "confidence": 0.0,
  "composant_suspecte": "target-app"
}

REGLES IMPERATIVES :
1. "composant_suspecte" doit valoir EXACTEMENT l'un de : __COMPOSANTS__.
   N'invente jamais de nom, n'extrais jamais un mot d'un nom de métrique.
   Le contexte ci-dessous indique à quel composant chaque métrique se rattache.
2. "hypothesis" doit décrire un MÉCANISME, pas répéter l'observation.
   MAUVAIS : "la métrique de latence est anormale" (constat, sans valeur)
   MAUVAIS : "le composant injectee est responsable" (nom inventé)
   BON     : "la dépendance externe sature et ne répond plus dans son délai"
   BON     : "des fichiers temporaires s'accumulent et remplissent le volume"
3. "evidence" est une LISTE de chaînes. Chaque preuve cite une métrique
   RÉELLEMENT présente ci-dessous avec sa valeur. N'invente aucune valeur.
4. "confidence" est un nombre décimal entre 0.0 et 1.0, sans guillemets.
   Sois honnête : une seule métrique en écart justifie rarement plus de 0.7.
5. Ne recopie aucun exemple de ce message : ce sont des illustrations."""


def build_prompt(anomaly_events: list[dict]) -> str:
    """
    Assemble le prompt : consignes, contexte métier, puis données brutes.

    Le CONTEXTE est la partie ajoutée au Jour 15. Sans lui, l'agent recevait
    des noms de métriques dépourvus de sens pour lui — il ne pouvait ni
    savoir ce que mesurait `latence_injectee_ms`, ni ce qu'était une valeur
    normale, ni quelle cause invoquer. Il se contentait donc de reformuler
    la question. Le contexte n'est pas une aide à la rédaction : c'est
    l'information sans laquelle la tâche demandée est impossible.

    Seules les métriques en anomalie sont documentées : chaque token coûte
    du temps d'inférence, et la latence est une contrainte du projet.
    """
    # Substitution ciblée plutôt que str.format() : le prompt contient un
    # gabarit JSON, dont les accolades seraient interprétées comme des
    # champs de formatage et feraient échouer l'appel.
    consignes = SYSTEM_PROMPT.replace("__COMPOSANTS__", ", ".join(COMPOSANTS_VALIDES))
    contexte = contexte_metriques(anomaly_events)
    donnees = json.dumps(anomaly_events, ensure_ascii=False, indent=2)
    return (f"{consignes}\n\n"
            f"CONTEXTE — ce que mesurent ces métriques :\n{contexte}\n\n"
            f"DONNÉES — anomalies détectées :\n{donnees}")


def run_metrics_agent(llm_client, model: str, anomaly_events: list[dict],
                       max_retries: int = 2) -> AgentHypothesis:
    prompt = build_prompt(anomaly_events)
    return run_agent(llm_client, model, prompt, agent_name="metrics", max_retries=max_retries)