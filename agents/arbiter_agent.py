"""
Agent Arbitre — Jour 8, retouché suite aux retours de l'encadrante.

Changements par rapport à la première version :

1. Fusion avec l'ex-Agent Rapporteur (Jour 4 initial du cahier des charges) :
   le LLM produit maintenant `rapport_incident` dans le même appel. Générer
   un rapport à partir d'un diagnostic déjà tranché est une tâche de
   synthèse à faible risque, elle ne justifie pas un agent séparé avec son
   propre aller-retour LLM.

2. Le LLM ne fournit PLUS de score de confiance numérique. Il qualifie
   seulement la relation entre les deux hypothèses (accord/désaccord/
   complémentaire). Le score est calculé en code par une formule pondérée
   simple et documentée (compute_confidence ci-dessous), pour rester
   explicable : n'importe quel humain peut recalculer le score à la main
   à partir du log, sans avoir à faire confiance à l'auto-évaluation du
   modèle.

3. Un désaccord force désormais un plafond de confiance strict, appliqué
   par le code (pas par le prompt).
"""

import json

from agent_utils import run_agent
from schemas import ArbiterLLMOutput, ArbiterVerdict

NO_EVIDENCE_CONFIDENCE = 0.0
NO_EVIDENCE_COMPONENT = "aucun"

DISAGREEMENT_CONFIDENCE_CAP = 0.4

# Poids de la formule de confiance. Egaux par défaut (aucune modalité n'est
# a priori plus fiable que l'autre) ; documentés ici pour être ajustables
# et auditable -- un changement de ces poids doit être une décision
# explicite et tracée, pas un réglage caché dans un prompt.
WEIGHT_METRICS = 0.5
WEIGHT_LOGS = 0.5

AGREEMENT_BONUS = {
    "accord": 0.10,        # les deux agents convergent -> confiance renforcée
    "complementaire": 0.0,  # cohérent mais pas de recoupement direct -> neutre
    "desaccord": -0.30,    # signal contradictoire -> confiance fortement réduite
}

FALLBACK_LLM_OUTPUT = ArbiterLLMOutput(
    diagnosis="Arbitrage indisponible : échec de l'analyse automatique",
    justification="Aucun verdict exploitable — voir logs système pour la cause de l'échec",
    agreement_status="desaccord",
    composant_suspecte="inconnu",
    rapport_incident="Le système n'a pas pu produire de rapport automatique suite à un échec de l'Agent Arbitre.",
)

SYSTEM_PROMPT = """Tu es un arbitre technique expert, chargé de réconcilier deux hypothèses \
indépendantes produites par deux agents distincts (l'un analysant des métriques, l'autre des \
logs) sur un même incident d'infrastructure.

Détermine si les deux hypothèses sont en ACCORD (même cause), en DESACCORD (causes \
incompatibles), ou COMPLEMENTAIRES (deux facettes d'un même problème). Ta justification DOIT \
citer explicitement au moins une preuve ("evidence") reçue de l'un des deux agents — ne te \
contente pas d'une conclusion sans preuve à l'appui.

Rédige aussi un rapport d'incident court (3-5 phrases), lisible par un humain non technique, \
résumant ce qui s'est passé et pourquoi.

Ne fournis AUCUN score de confiance numérique : ce n'est pas ton rôle, il est calculé séparément.

Réponds UNIQUEMENT avec un objet JSON strictement conforme à ce format, sans aucun texte \
avant ou après :
{
  "diagnosis": "<diagnostic final réconcilié>",
  "justification": "<raisonnement citant une preuve concrète>",
  "agreement_status": "accord" | "desaccord" | "complementaire",
  "composant_suspecte": "<nom du composant>",
  "rapport_incident": "<compte-rendu lisible par un humain>"
}"""


def _is_no_evidence(hypothesis: dict) -> bool:
    return (
        hypothesis.get("confidence") == NO_EVIDENCE_CONFIDENCE
        and hypothesis.get("composant_suspecte") == NO_EVIDENCE_COMPONENT
    )


def build_prompt(metrics_hypothesis: dict, logs_hypothesis: dict) -> str:
    payload = json.dumps(
        {"hypothese_metriques": metrics_hypothesis, "hypothese_logs": logs_hypothesis},
        ensure_ascii=False, indent=2,
    )
    return f"{SYSTEM_PROMPT}\n\nHypothèses à réconcilier :\n{payload}"


def compute_confidence(metrics_confidence: float, logs_confidence: float,
                        agreement_status: str) -> tuple[float, dict]:
    """
    Calcule le score de confiance final par une formule pondérée simple,
    et retourne aussi le détail du calcul (confidence_breakdown) pour
    permettre à un humain d'auditer la décision a posteriori sans avoir à
    deviner comment le score a été obtenu.

    formule : clamp(w_metrics * c_metrics + w_logs * c_logs + bonus_accord, 0, 0.98)
    puis, si désaccord : plafond dur supplémentaire (DISAGREEMENT_CONFIDENCE_CAP),
    imposé par le code, jamais par le LLM.
    """
    bonus = AGREEMENT_BONUS[agreement_status]
    base_score = WEIGHT_METRICS * metrics_confidence + WEIGHT_LOGS * logs_confidence
    score = max(0.0, min(0.98, base_score + bonus))

    if agreement_status == "desaccord":
        score = min(score, DISAGREEMENT_CONFIDENCE_CAP)

    breakdown = {
        "metrics_confidence": metrics_confidence,
        "logs_confidence": logs_confidence,
        "weights": {"metrics": WEIGHT_METRICS, "logs": WEIGHT_LOGS},
        "agreement_status": agreement_status,
        "agreement_bonus": bonus,
        "formula": "clamp(w_metrics*c_metrics + w_logs*c_logs + bonus, 0, 0.98)",
        "disagreement_cap_applied": agreement_status == "desaccord",
    }
    return score, breakdown


def run_arbiter(llm_client, model: str, metrics_hypothesis: dict, logs_hypothesis: dict,
                 max_retries: int = 2) -> ArbiterVerdict:
    metrics_empty = _is_no_evidence(metrics_hypothesis)
    logs_empty = _is_no_evidence(logs_hypothesis)

    # Cas 1 : aucune des deux modalités n'a de preuve.
    if metrics_empty and logs_empty:
        return ArbiterVerdict(
            diagnosis="Aucun diagnostic exploitable",
            justification="Ni l'Agent Métriques ni l'Agent Logs n'ont produit de preuve",
            agreement_status="desaccord",
            final_confidence=0.0,
            composant_suspecte="inconnu",
            rapport_incident="Aucune anomalie exploitable n'a pu être diagnostiquée : aucun des "
                              "deux agents d'investigation n'a trouvé de preuve concrète.",
            confidence_breakdown={"source": "aucune_preuve"},
        )

    # Cas 2 : une seule modalité a des preuves -> adoption directe, pas d'appel LLM.
    if metrics_empty != logs_empty:
        source = logs_hypothesis if metrics_empty else metrics_hypothesis
        source_name = "logs" if metrics_empty else "métriques"
        return ArbiterVerdict(
            diagnosis=source["hypothesis"],
            justification=f"Seule la modalité {source_name} a produit des preuves exploitables ; adoption directe sans arbitrage LLM.",
            agreement_status="complementaire",
            final_confidence=source["confidence"],
            composant_suspecte=source["composant_suspecte"],
            rapport_incident=f"Incident détecté via la modalité {source_name} uniquement. "
                              f"Diagnostic : {source['hypothesis']}. Preuves : {', '.join(source['evidence'])}.",
            confidence_breakdown={"source": f"modalite_unique_{source_name}", "confidence": source["confidence"]},
        )

    # Cas 3 : les deux modalités ont des preuves -> arbitrage réel nécessaire.
    prompt = build_prompt(metrics_hypothesis, logs_hypothesis)
    llm_output: ArbiterLLMOutput = run_agent(
        llm_client, model, prompt, agent_name="arbiter",
        schema_cls=ArbiterLLMOutput, fallback=FALLBACK_LLM_OUTPUT, max_retries=max_retries,
    )

    final_confidence, breakdown = compute_confidence(
        metrics_hypothesis["confidence"], logs_hypothesis["confidence"], llm_output.agreement_status,
    )

    return ArbiterVerdict(
        diagnosis=llm_output.diagnosis,
        justification=llm_output.justification,
        agreement_status=llm_output.agreement_status,
        final_confidence=final_confidence,
        composant_suspecte=llm_output.composant_suspecte,
        rapport_incident=llm_output.rapport_incident,
        confidence_breakdown=breakdown,
    )