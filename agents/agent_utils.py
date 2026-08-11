"""
Logique partagée entre tous les agents (Métriques, Logs, Arbitre).

Factorisée ici après le Jour 5 : dupliquer le mécanisme de retry/parsing
dans chaque agent créerait un risque de divergence silencieuse (ex: un
correctif de bug appliqué à un agent mais oublié dans l'autre).

Généralisée au Jour 8 (paramètre schema_cls/fallback) pour être réutilisée
par l'Agent Arbitre, dont le schéma de sortie (ArbiterVerdict) diffère de
celui des agents d'investigation (AgentHypothesis).
"""

import json
import logging

from pydantic import BaseModel, ValidationError

from schemas import AgentHypothesis

FALLBACK_HYPOTHESIS = AgentHypothesis(
    hypothesis="Diagnostic indisponible : échec de l'analyse automatique",
    evidence=["Aucune preuve exploitable — voir logs système pour la cause de l'échec"],
    confidence=0.0,
    composant_suspecte="inconnu",
)


def extract_json(raw_text: str) -> dict:
    """Tolère les blocs ```json ... ``` et le texte parasite autour du JSON."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise json.JSONDecodeError("Aucun objet JSON trouvé", text, 0)
    return json.loads(text[start:end + 1])


def run_agent(llm_client, model: str, prompt: str, agent_name: str,
              schema_cls: type[BaseModel] = AgentHypothesis,
              fallback: BaseModel = FALLBACK_HYPOTHESIS,
              max_retries: int = 2) -> BaseModel:
    """
    Exécute un agent avec retry borné (max_retries+1 tentatives au total),
    générique sur le schéma de sortie attendu.

    - JSON malformé / schema invalide -> on retente (erreur probablement
      liée au prompt/modèle, corrigible par une nouvelle génération).
    - Erreur réseau/timeout -> fallback immédiat, pas de retry (une
      indisponibilité réseau ne se corrige pas en réessayant le même appel).
    """
    logger = logging.getLogger(f"sentinelops.agents.{agent_name}")
    last_error: Exception | None = None
    attempt = 0

    for attempt in range(1, max_retries + 2):
        try:
            raw_output = llm_client.generate(model=model, prompt=prompt)
            parsed = extract_json(raw_output)
            result = schema_cls(**parsed)
            logger.info(f"succès à la tentative {attempt}")
            return result
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            last_error = exc
            logger.warning(f"tentative {attempt} invalide ({exc})")
        except Exception as exc:
            last_error = exc
            logger.error(f"erreur d'appel LLM à la tentative {attempt} ({exc})")
            break

    logger.error(f"échec définitif après {attempt} tentative(s), fallback appliqué. Dernière erreur: {last_error}")
    return fallback