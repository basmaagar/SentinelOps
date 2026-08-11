"""
Moteur de garde-fou (guardrails) — Jour 9.

Toutes les règles ici sont DÉTERMINISTES et codées en dur. Aucun LLM
n'intervient dans cette décision : c'est la garantie que le système ne
peut jamais agir en dehors du périmètre défini, quelle que soit la
confiance affichée par les agents ou l'Arbitre.

Matrice de risque (dans l'ordre d'évaluation — la première règle qui
matche l'emporte) :
1. Action hors liste blanche          -> toujours refusée.
2. agreement_status == "desaccord"    -> risque élevé forcé (peu importe
   final_confidence : un désaccord entre les deux agents d'investigation
   est en soi un signal d'incertitude que le score ne doit pas masquer).
3. final_confidence < seuil           -> risque élevé.
4. Sinon                              -> risque faible, action autorisée
   automatiquement (si la cible n'est pas déjà verrouillée, cf. lock.py).
"""

from dataclasses import dataclass, field
from typing import Literal

RiskLevel = Literal["faible", "eleve"]
Decision = Literal["autoriser_auto", "validation_humaine", "refuser"]

# Liste blanche des actions autorisées. Toute action absente de cette liste
# est refusée par construction, même si un composant amont (LLM ou logique
# de sélection) la proposait avec une confiance de 1.0.
ACTION_WHITELIST: dict[str, dict] = {
    "restart_container": {
        "description": "Redémarre un conteneur applicatif",
        "reversible": True,
    },
    "scale_replica": {
        "description": "Augmente le nombre de réplicas d'un service",
        "reversible": True,
    },
}

CONFIDENCE_THRESHOLD_AUTO = 0.75


@dataclass
class GuardrailDecision:
    action: str
    risk_level: RiskLevel
    decision: Decision
    justification: str
    reasons: list[str] = field(default_factory=list)


def evaluate(action: str, arbiter_verdict: dict) -> GuardrailDecision:
    """
    arbiter_verdict : dict issu de ArbiterVerdict.model_dump() (Jour 8) --
    doit contenir au minimum agreement_status et final_confidence.
    """
    reasons: list[str] = []

    # Règle 1 (priorité absolue) : action hors liste blanche -> refus,
    # sans même évaluer le reste. Rien ne peut contourner cette règle.
    if action not in ACTION_WHITELIST:
        return GuardrailDecision(
            action=action,
            risk_level="eleve",
            decision="refuser",
            justification=f"Action '{action}' absente de la liste blanche — refusée par construction.",
            reasons=["action_hors_liste_blanche"],
        )

    agreement_status = arbiter_verdict.get("agreement_status")
    final_confidence = arbiter_verdict.get("final_confidence", 0.0)

    # Règle 2 : désaccord entre les agents -> risque élevé FORCÉ, peu
    # importe le score de confiance calculé (qui est de toute façon déjà
    # plafonné à 0.4 par l'Arbitre, cf. Jour 8 — double sécurité ici).
    if agreement_status == "desaccord":
        reasons.append("desaccord_entre_agents")

    # Règle 3 : confiance insuffisante.
    if final_confidence < CONFIDENCE_THRESHOLD_AUTO:
        reasons.append(f"confiance_insuffisante({final_confidence:.2f}<{CONFIDENCE_THRESHOLD_AUTO})")

    if reasons:
        risk_level: RiskLevel = "eleve"
        decision: Decision = "validation_humaine"
        justification = "Validation humaine requise : " + " ; ".join(reasons)
    else:
        risk_level = "faible"
        decision = "autoriser_auto"
        justification = (
            f"Action '{action}' autorisée automatiquement : dans la liste blanche, "
            f"accord/complémentarité des agents, confiance {final_confidence:.2f} >= {CONFIDENCE_THRESHOLD_AUTO}."
        )

    return GuardrailDecision(
        action=action, risk_level=risk_level, decision=decision,
        justification=justification, reasons=reasons,
    )