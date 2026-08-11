"""
Schéma de sortie strict des agents d'investigation (Métriques / Logs).

Pourquoi Pydantic plutôt qu'un simple json.loads() ? -> On a besoin de
valider le TYPE et la PRESENCE de chaque champ avant de faire confiance à
la sortie du LLM. Un LLM peut produire un JSON syntaxiquement valide mais
sémantiquement incomplet (ex: confidence manquant, ou "haute" au lieu de
0.9). Pydantic rejette ces cas immédiatement, ce qui déclenche le
mécanisme de retry (cf. metrics_agent.py) au lieu de laisser une donnée
corrompue avancer dans le pipeline.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Literal


class AgentHypothesis(BaseModel):
    hypothesis: str = Field(..., min_length=5, description="Cause probable identifiée par l'agent")
    evidence: list[str] = Field(..., min_length=1, description="Preuves concrètes tirées des données pré-traitées")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Score de confiance entre 0 et 1")
    composant_suspecte: str = Field(..., min_length=1, description="Composant identifié comme source du problème")

    @field_validator("confidence")
    @classmethod
    def confidence_must_be_plausible(cls, v: float) -> float:
        # Garde-fou : un agent qui déclare une confiance de 1.0 exacte est
        # suspect (sur-confiance typique d'un LLM mal calibré). On plafonne
        # à 0.98 pour forcer une marge d'incertitude résiduelle, cohérente
        # avec la philosophie "jamais de confiance aveugle" du projet.
        return min(v, 0.98)


class ArbiterLLMOutput(BaseModel):
    """
    Ce que le LLM Arbitre doit produire — VOLONTAIREMENT sans champ de
    confiance numérique. Le LLM ne fait que qualifier la relation entre les
    deux hypothèses (accord/désaccord/complémentaire) et justifier son
    diagnostic ; le score de confiance est calculé en code (cf.
    arbiter_agent.compute_confidence) pour rester explicable et ré-auditable
    par un humain sans avoir à faire confiance à l'auto-évaluation du modèle.
    """
    diagnosis: str = Field(..., min_length=5, description="Diagnostic final réconcilié")
    justification: str = Field(..., min_length=10, description="Raisonnement citant au moins une preuve reçue")
    agreement_status: Literal["accord", "desaccord", "complementaire"] = Field(
        ..., description="Nature de la relation entre les deux hypothèses sources"
    )
    composant_suspecte: str = Field(..., min_length=1)
    rapport_incident: str = Field(
        ..., min_length=20,
        description="Compte-rendu lisible par un humain (fusion de l'ex-Agent Rapporteur : "
                    "générer un rapport à partir d'un diagnostic déjà tranché est une tâche de "
                    "synthèse à faible risque, elle ne justifie pas un agent séparé)",
    )


class ArbiterVerdict(BaseModel):
    """
    Verdict final complet : sortie LLM (ArbiterLLMOutput) + confiance
    calculée par le code + trace de calcul (confidence_breakdown), pour
    qu'un humain puisse auditer une décision a posteriori sans deviner
    comment le score a été obtenu.
    """
    diagnosis: str = Field(..., min_length=5)
    justification: str = Field(..., min_length=5)
    agreement_status: Literal["accord", "desaccord", "complementaire"]
    final_confidence: float = Field(..., ge=0.0, le=1.0)
    composant_suspecte: str = Field(..., min_length=1)
    rapport_incident: str = Field(..., min_length=5)
    confidence_breakdown: dict = Field(
        default_factory=dict,
        description="Détail traçable du calcul de final_confidence (poids, bonus, formule)",
    )

    @field_validator("final_confidence")
    @classmethod
    def confidence_must_be_plausible(cls, v: float) -> float:
        return min(v, 0.98)