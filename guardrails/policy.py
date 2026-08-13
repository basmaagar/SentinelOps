"""
Moteur de garde-fou (guardrails) — Jour 9, étendu au Jour 12.

Toutes les règles ici sont DÉTERMINISTES et codées en dur. Aucun LLM
n'intervient dans cette décision : c'est la garantie que le système ne
peut jamais agir en dehors du périmètre défini, quelle que soit la
confiance affichée par les agents ou l'Arbitre.

--- Ce qui change au Jour 12 : une vraie matrice à deux axes ---

La version précédente comparait la confiance à UN seuil unique (0.75),
identique pour toutes les actions. Or "risque" et "confiance" sont deux
dimensions indépendantes :

  - le RISQUE INTRINSÈQUE dépend de l'action, pas du diagnostic. Il est
    connu à l'avance et ne varie jamais : redémarrer un conteneur coupe le
    service quelques secondes et n'a pas d'inverse (on ne "dé-redémarre"
    pas), tandis qu'ajouter une réplique est sans interruption et
    s'annule exactement.

  - la CONFIANCE dépend du diagnostic, pas de l'action. Elle varie à
    chaque incident.

Un seuil unique revient à traiter ces deux actions comme équivalentes, ce
qu'elles ne sont pas. La matrice ci-dessous exige une confiance d'autant
plus élevée que l'action est risquée — c'est la formulation habituelle
d'une politique de risque, et elle se justifie sans recourir à une
constante arbitraire globale.

Ordre d'évaluation (la première règle qui s'applique l'emporte) :
1. Action hors liste blanche       -> toujours refusée.
2. Action irréversible             -> jamais automatique, quelle que soit
   la confiance (règle absolue, indépendante du score).
3. agreement_status == "desaccord" -> risque élevé forcé.
4. Confiance < seuil PROPRE À L'ACTION -> validation humaine.
5. Sinon                           -> action autorisée automatiquement.
"""

from dataclasses import dataclass, field
from typing import Literal

from risk_profiles import PROFILES, PRODUCTION

RiskLevel = Literal["faible", "modere", "eleve"]
Decision = Literal["autoriser_auto", "validation_humaine", "refuser"]

# Liste blanche des actions autorisées. Toute action absente de cette liste
# est refusée par construction, même si un composant amont (LLM ou logique
# de sélection) la proposait avec une confiance de 1.0.
#
# `risque_intrinseque` est une propriété de l'action elle-même, fixée à
# l'avance et indépendante de tout diagnostic.
ACTION_WHITELIST: dict[str, dict] = {
    "scale_replica": {
        "description": "Augmente le nombre de réplicas d'un service",
        "reversible": True,
        "risque_intrinseque": "faible",
        # Sans interruption de service, et exactement annulable en
        # revenant au nombre de réplicas précédent.
        "justification_risque": "sans interruption, annulable exactement",
    },
    "restart_container": {
        "description": "Redémarre un conteneur applicatif",
        "reversible": True,
        "risque_intrinseque": "modere",
        # Nuance importante et volontairement explicite : le redémarrage
        # est "réversible" au sens où l'état antérieur revient de lui-même,
        # mais il n'a pas d'inverse — une fois le conteneur redémarré, on
        # ne peut pas défaire l'interruption de service ni les requêtes
        # perdues. C'est pour cette raison qu'un échec post-action sur un
        # restart déclenche une escalade humaine et non un rollback.
        "justification_risque": "coupure de service brève, aucun inverse possible",
    },
}

# Seuil de confiance requis pour une exécution automatique, PAR NIVEAU de
# risque intrinsèque. Plus l'action est risquée, plus la preuve exigée est
# forte.
CONFIDENCE_THRESHOLD_BY_RISK: dict[str, float] = {
    "faible": 0.60,
    "modere": 0.80,
    "eleve": 1.01,  # > 1 : jamais atteignable, donc jamais automatique
}

# Conservé pour compatibilité avec les tests existants du Jour 9.
CONFIDENCE_THRESHOLD_AUTO = 0.75


@dataclass
class GuardrailDecision:
    action: str
    risk_level: RiskLevel
    decision: Decision
    justification: str
    reasons: list[str] = field(default_factory=list)
    confidence_threshold: float = 0.0
    intrinsic_risk: str = ""
    profile: str = "production"


def evaluate(action: str, arbiter_verdict: dict,
             profile: str = "production") -> GuardrailDecision:
    """
    arbiter_verdict : dict issu de ArbiterVerdict.model_dump() (Jour 8) --
    doit contenir au minimum agreement_status et final_confidence.

    profile : nom du profil de seuils appliqué (cf. risk_profiles.py). Le
    profil est journalisé avec la décision, de sorte qu'on sache toujours
    sous quel régime une décision a été prise. Le calcul de confiance ne
    dépend PAS du profil : seule la décision qu'on en tire change.
    """
    reasons: list[str] = []
    active_profile = PROFILES.get(profile, PRODUCTION)

    # Règle 1 (priorité absolue) : action hors liste blanche -> refus,
    # sans même évaluer le reste. Rien ne peut contourner cette règle.
    if action not in ACTION_WHITELIST:
        return GuardrailDecision(
            action=action,
            risk_level="eleve",
            decision="refuser",
            justification=f"Action '{action}' absente de la liste blanche — refusée par construction.",
            reasons=["action_hors_liste_blanche"],
            profile=profile,
        )

    spec = ACTION_WHITELIST[action]
    intrinsic_risk = spec["risque_intrinseque"]
    threshold = active_profile.seuils_par_risque[intrinsic_risk]

    agreement_status = arbiter_verdict.get("agreement_status")
    final_confidence = float(arbiter_verdict.get("final_confidence", 0.0) or 0.0)

    # Règle 2 : une action irréversible n'est jamais automatique, quelle
    # que soit la confiance. Règle absolue, volontairement placée avant
    # toute évaluation de score : aucun niveau de confiance ne doit pouvoir
    # autoriser une action qu'on ne saurait pas défaire.
    if not spec.get("reversible", False):
        reasons.append("action_irreversible")

    # Règle 3 : désaccord entre les agents -> risque élevé FORCÉ, peu
    # importe le score de confiance calculé (qui est de toute façon déjà
    # plafonné à 0.4 par l'Arbitre — double sécurité ici).
    if agreement_status == "desaccord":
        reasons.append("desaccord_entre_agents")

    # Règle 4 : confiance insuffisante AU REGARD DU RISQUE DE CETTE ACTION.
    if final_confidence < threshold:
        reasons.append(
            f"confiance_insuffisante_pour_risque_{intrinsic_risk}"
            f"({final_confidence:.2f}<{threshold:.2f})"
        )

    if reasons:
        risk_level: RiskLevel = "eleve"
        decision: Decision = "validation_humaine"
        justification = (
            f"Validation humaine requise pour '{action}' "
            f"(risque intrinsèque {intrinsic_risk} : {spec['justification_risque']}) : "
            + " ; ".join(reasons)
        )
    else:
        risk_level = intrinsic_risk  # type: ignore[assignment]
        decision = "autoriser_auto"
        justification = (
            f"Action '{action}' autorisée automatiquement : dans la liste blanche, "
            f"réversible, accord ou complémentarité des agents, et confiance "
            f"{final_confidence:.2f} >= {threshold:.2f} (seuil exigé pour une action "
            f"de risque intrinsèque {intrinsic_risk})."
        )

    return GuardrailDecision(
        action=action, risk_level=risk_level, decision=decision,
        justification=justification, reasons=reasons,
        confidence_threshold=threshold, intrinsic_risk=intrinsic_risk,
        profile=active_profile.nom,
    )