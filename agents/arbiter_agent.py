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
from confidence_features import effective_confidence
from component_inventory import (resolve_component, infer_component_from_events,
                                 UNKNOWN_COMPONENT, LOG_SOURCE_COMPONENT)

NO_EVIDENCE_CONFIDENCE = 0.0
NO_EVIDENCE_COMPONENT = "aucun"

DISAGREEMENT_CONFIDENCE_CAP = 0.4

# Poids de repli, utilisés UNIQUEMENT lorsque aucune des deux modalités ne
# présente de signal mesurable (cas dégénéré). En fonctionnement normal,
# les poids sont désormais DÉRIVÉS de la force relative des signaux (cf.
# compute_confidence) et non plus fixés à l'avance.
#
# C'était le point le plus difficile à défendre de la version précédente :
# à la question « pourquoi 0.5 et pas 0.6 ? », il n'existait aucune réponse
# autre que « c'est un choix ». Un poids dérivé d'une mesure n'a pas ce
# problème — il n'y a plus de constante à justifier.
WEIGHT_METRICS_FALLBACK = 0.5
WEIGHT_LOGS_FALLBACK = 0.5

# Plafond de confiance pour un diagnostic reposant sur une seule modalité.
#
# Correctif d'un défaut majeur : le cas mono-modalité adoptait telle quelle
# la confiance auto-déclarée par UN SEUL modèle, sans arbitrage, sans
# recoupement et sans plafond — alors que le seuil d'action automatique est
# à 0.75. Les journaux d'exécution réelle montrent que c'est le cas le plus
# FRÉQUENT, pas un cas rare : un pic métrique et un burst de logs coïncident
# rarement au même cycle. Le chemin le moins vérifié était donc le chemin
# nominal.
#
# Une seule modalité signifie qu'aucune corroboration indépendante n'existe.
# C'est précisément ce que l'architecture multi-agents est censée apporter :
# quand elle fait défaut, la confiance doit en tenir compte.
SINGLE_MODALITY_CAP = 0.70

# Budget de génération propre à l'Arbitre. Il produit cinq champs, dont un
# rapport d'incident rédigé, là où un agent d'investigation en produit
# quatre, tous courts. Avec le budget standard (220 tokens), sa réponse
# était systématiquement coupée avant l'accolade fermante : le JSON était
# invalide, les trois tentatives échouaient de façon identique, et le
# système retombait sur un fallback "arbitrage indisponible" alors que le
# modèle avait correctement travaillé.
ARBITER_OUTPUT_BUDGET = 600

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

Rédige aussi un rapport d'incident très court (2 phrases maximum), lisible par un humain non technique, \
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


# Marqueurs des hypothèses de repli, produites lorsqu'un appel au modèle
# échoue (délai dépassé, service injoignable, sortie inexploitable après
# plusieurs tentatives).
_MARQUEURS_REPLI = ("indisponible", "échec de l'analyse", "echec de l'analyse",
                    "aucune preuve exploitable")


def _is_fallback(hypothesis: dict) -> bool:
    """
    Reconnaît une hypothèse de REPLI, c'est-à-dire l'aveu qu'un agent n'a
    pas pu travailler — à distinguer d'une hypothèse réelle mais faible.

    Correctif important. Le repli porte une confiance de 0.0 mais un
    composant « inconnu » et une preuve textuelle non vide : il ne
    satisfaisait donc pas `_is_no_evidence`, qui exige le composant
    « aucun ». L'arbitre le traitait par conséquent comme une hypothèse
    CONCURRENTE, la comparait à l'hypothèse valide de l'autre agent, et
    concluait à un désaccord.

    Conséquence observée en fonctionnement : un diagnostic logs
    parfaitement ancré, annoncé à 0.90, était ramené à 0.28 par un « faux
    désaccord » avec un agent qui n'avait tout simplement pas répondu. Le
    système se pénalisait lui-même pour une panne d'infrastructure.

    Un agent en repli doit être traité comme ABSENT. On retombe alors sur
    le cas mono-modalité : l'hypothèse survivante est adoptée, vérifiée
    normalement, puis plafonnée faute de corroboration.
    """
    if float(hypothesis.get("confidence") or 0.0) > 0.0:
        return False
    texte = " ".join([
        str(hypothesis.get("hypothesis", "")),
        " ".join(str(e) for e in (hypothesis.get("evidence") or [])),
    ]).lower()
    return any(marqueur in texte for marqueur in _MARQUEURS_REPLI)


def _is_no_evidence(hypothesis: dict) -> bool:
    """
    Vrai si l'agent n'a rien à dire — soit parce que sa modalité ne
    contenait aucune preuve (court-circuit voulu), soit parce que son
    analyse a échoué (repli). Les deux situations appellent le même
    traitement : ne pas faire arbitrer une hypothèse contre du vide.
    """
    if not hypothesis:
        return True
    sans_preuve = (
        hypothesis.get("confidence") == NO_EVIDENCE_CONFIDENCE
        and hypothesis.get("composant_suspecte") == NO_EVIDENCE_COMPONENT
    )
    return sans_preuve or _is_fallback(hypothesis)


def build_prompt(metrics_hypothesis: dict, logs_hypothesis: dict) -> str:
    payload = json.dumps(
        {"hypothese_metriques": metrics_hypothesis, "hypothese_logs": logs_hypothesis},
        ensure_ascii=False, indent=2,
    )
    return f"{SYSTEM_PROMPT}\n\nHypothèses à réconcilier :\n{payload}"


def compute_confidence(metrics_hypothesis: dict, logs_hypothesis: dict,
                       agreement_status: str,
                       metrics_observed=None, logs_observed=None,
                       metrics_events=None, logs_events=None) -> tuple[float, dict]:
    """
    Calcule le score de confiance final, et retourne le détail complet du
    calcul pour permettre un audit humain a posteriori.

    Trois différences par rapport à la version précédente :

    1. Les confiances des agents sont DÉCOTÉES avant d'être combinées, en
       fonction de vérifications faites par le code : les preuves citées
       existent-elles dans les données reçues, le composant nommé existe-t-il.
       Auparavant, les auto-évaluations des modèles entraient telles quelles
       dans le calcul.

    2. Les poids ne sont plus fixés à 0.5/0.5 mais dérivés de la force
       relative des signaux détectés dans chaque modalité. Un z-score
       métrique trois fois plus fort que le signal logs donne un poids trois
       fois supérieur à l'hypothèse métrique.

    3. Le détail retourné contient chaque terme intermédiaire, de sorte que
       le score soit recalculable à la main depuis le seul journal.

    formule : clamp(w_m x c_eff_m + w_l x c_eff_l + bonus_accord, 0, 0.98)
      avec c_eff = c_declaree x ancrage_preuves x validite_composant
      et   w_m   = force_m / (force_m + force_l)
    """
    metrics_factors = effective_confidence(metrics_hypothesis, metrics_observed, metrics_events)
    logs_factors = effective_confidence(logs_hypothesis, logs_observed, logs_events)

    strength_m = metrics_factors["force_signal"]
    strength_l = logs_factors["force_signal"]
    total_strength = strength_m + strength_l

    if total_strength > 0:
        weight_m = strength_m / total_strength
        weight_l = strength_l / total_strength
        weight_origin = "derive_de_la_force_des_signaux"
    else:
        # Aucun signal mesurable des deux côtés : on ne peut rien dériver,
        # on retombe sur des poids égaux, en le signalant explicitement.
        weight_m, weight_l = WEIGHT_METRICS_FALLBACK, WEIGHT_LOGS_FALLBACK
        weight_origin = "repli_poids_egaux_faute_de_signal_mesurable"

    bonus = AGREEMENT_BONUS[agreement_status]
    base_score = (weight_m * metrics_factors["confiance_effective"]
                  + weight_l * logs_factors["confiance_effective"])
    score = max(0.0, min(0.98, base_score + bonus))

    if agreement_status == "desaccord":
        score = min(score, DISAGREEMENT_CONFIDENCE_CAP)

    breakdown = {
        "agent_metriques": metrics_factors,
        "agent_logs": logs_factors,
        "poids": {"metriques": round(weight_m, 4), "logs": round(weight_l, 4),
                  "origine": weight_origin},
        "agreement_status": agreement_status,
        "agreement_bonus": bonus,
        "score_avant_bonus": round(base_score, 4),
        "formule": ("clamp(w_m*c_eff_m + w_l*c_eff_l + bonus, 0, 0.98) ; "
                    "c_eff = declaree x ancrage x validite_composant ; "
                    "w = force_signal / somme_forces"),
        "disagreement_cap_applied": agreement_status == "desaccord",
        "score_final": round(score, 4),
    }
    # Arrondi : sans lui, la formule produit des valeurs du type
    # 0.9500000000000001 qui apparaissent telles quelles dans les journaux
    # et les rapports.
    return round(score, 4), breakdown


def _authoritative_component(declared: str | None,
                             events: list[dict] | None,
                             default_owner: str | None = None) -> tuple[str, dict]:
    """
    Détermine le composant retenu, en privilégiant la déduction faite en
    code à partir des métriques en anomalie.

    Le composant proposé par le modèle n'est pas ignoré : il est conservé
    dans la trace, et l'accord ou le désaccord entre les deux sources est
    journalisé. C'est une information de qualité utile — un modèle qui
    nomme systématiquement le bon composant est plus fiable qu'un modèle
    qui répond "inconnu" — mais elle ne pilote aucune décision.
    """
    inferred, detail = infer_component_from_events(events, default_owner=default_owner)
    resolved_declared = resolve_component(declared)

    detail["composant_declare_par_le_modele"] = declared
    detail["composant_declare_resolu"] = resolved_declared

    if inferred != UNKNOWN_COMPONENT:
        detail["retenu_final"] = inferred
        detail["origine"] = "deduit_des_metriques_en_anomalie"
        detail["concordance_avec_le_modele"] = (resolved_declared == inferred)
        return inferred, detail

    # Aucune déduction possible (métrique ambiguë ou non rattachable) :
    # on retombe sur le composant du modèle, s'il est reconnu.
    detail["retenu_final"] = resolved_declared
    detail["origine"] = ("repli_sur_le_modele" if resolved_declared != UNKNOWN_COMPONENT
                         else "aucun_composant_identifiable")
    return resolved_declared, detail


def run_arbiter(llm_client, model: str, metrics_hypothesis: dict, logs_hypothesis: dict,
                 max_retries: int = 2,
                 metrics_observed=None, logs_observed=None,
                 metrics_events=None, logs_events=None) -> ArbiterVerdict:
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

    # Cas 2 : une seule modalité a des preuves -> adoption directe, sans
    # appel LLM (inutile de faire arbitrer une hypothèse unique, l'arbitre
    # inventerait un désaccord avec du vide).
    #
    # La confiance n'est PLUS reprise telle quelle : elle subit les mêmes
    # vérifications que dans le cas nominal (ancrage des preuves, validité
    # du composant), puis un plafond propre à l'absence de corroboration.
    if metrics_empty != logs_empty:
        source = logs_hypothesis if metrics_empty else metrics_hypothesis
        source_name = "logs" if metrics_empty else "métriques"
        observed = logs_observed if metrics_empty else metrics_observed
        events = logs_events if metrics_empty else metrics_events

        factors = effective_confidence(source, observed, events)
        confidence = min(factors["confiance_effective"], SINGLE_MODALITY_CAP)
        confidence = round(confidence, 4)

        # Pour la modalité logs, le propriétaire du fichier de journaux
        # sert de rattachement par défaut (cf. LOG_SOURCE_COMPONENT).
        canonical, component_detail = _authoritative_component(
            source.get("composant_suspecte"), events,
            default_owner=LOG_SOURCE_COMPONENT if metrics_empty else None)

        return ArbiterVerdict(
            diagnosis=source["hypothesis"],
            justification=(
                f"Seule la modalité {source_name} a produit des preuves exploitables ; "
                f"adoption directe sans arbitrage LLM. Confiance déclarée "
                f"{factors['confiance_declaree']} décotée à {factors['confiance_effective']} "
                f"(ancrage des preuves {factors['ancrage_preuves']}, "
                f"validité du composant {factors['validite_composant']}), "
                f"puis plafonnée à {SINGLE_MODALITY_CAP} faute de corroboration "
                f"par la seconde modalité."
            ),
            agreement_status="complementaire",
            final_confidence=confidence,
            composant_suspecte=canonical,
            rapport_incident=f"Incident détecté via la modalité {source_name} uniquement. "
                              f"Diagnostic : {source['hypothesis']}. "
                              f"Preuves : {', '.join(source.get('evidence', []) or ['aucune'])}.",
            confidence_breakdown={
                "source": f"modalite_unique_{source_name}",
                "facteurs": factors,
                "composant": component_detail,
                "plafond_modalite_unique": SINGLE_MODALITY_CAP,
                "formule": ("min(declaree x ancrage x validite_composant, "
                            f"{SINGLE_MODALITY_CAP})"),
                "score_final": confidence,
            },
        )

    # Cas 3 : les deux modalités ont des preuves -> arbitrage réel nécessaire.
    prompt = build_prompt(metrics_hypothesis, logs_hypothesis)
    llm_output: ArbiterLLMOutput = run_agent(
        llm_client, model, prompt, agent_name="arbiter",
        schema_cls=ArbiterLLMOutput, fallback=FALLBACK_LLM_OUTPUT, max_retries=max_retries,
        num_predict=ARBITER_OUTPUT_BUDGET,
    )

    final_confidence, breakdown = compute_confidence(
        metrics_hypothesis, logs_hypothesis, llm_output.agreement_status,
        metrics_observed=metrics_observed, logs_observed=logs_observed,
        metrics_events=metrics_events, logs_events=logs_events,
    )

    # Le composant est déduit des métriques en anomalie des DEUX modalités :
    # c'est un fait d'infrastructure, pas une interprétation.
    canonical, component_detail = _authoritative_component(
        llm_output.composant_suspecte, (metrics_events or []) + (logs_events or []),
        default_owner=LOG_SOURCE_COMPONENT)
    breakdown["composant"] = component_detail

    return ArbiterVerdict(
        diagnosis=llm_output.diagnosis,
        justification=llm_output.justification,
        agreement_status=llm_output.agreement_status,
        final_confidence=final_confidence,
        composant_suspecte=canonical,
        rapport_incident=llm_output.rapport_incident,
        confidence_breakdown=breakdown,
    )