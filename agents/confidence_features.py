"""
Facteurs de confiance calculés en code — Jour 12.

Ce module répond à la question posée deux fois par l'encadrante :
« comment est calculé le niveau de confiance, et quelles preuves ont pesé ? »

État précédent : la confiance finale valait
    0.5 x confiance_metriques + 0.5 x confiance_logs + bonus_accord
où les deux confiances étaient les notes que les LLM s'attribuaient
eux-mêmes, et où les poids 0.5/0.5 n'étaient justifiés par rien. Le calcul
était donc traçable (on voyait l'addition) mais pas explicable (rien ne
justifiait ses termes). Aucune preuve ne pesait ; seuls des scores
déclarés pesaient.

Observé en conditions réelles, ce qui rend la correction nécessaire :
  - « Les templates de logs sont des faux. » -> confiance déclarée 0.9
  - « Le template de log est un exemple d'erreur... » -> 0.8
Deux diagnostics sans valeur, avec une confiance élevée.

Principe retenu : le modèle propose une confiance, le code la DÉCOTE en
fonction de vérifications objectives. On ne remonte jamais une confiance,
on ne fait que la réduire — une vérification ne peut pas rendre une
hypothèse meilleure que ce que son auteur prétend.

    confiance_effective = confiance_declaree x ancrage x validite_composant

Les trois facteurs ci-dessous sont tous calculables à la main à partir du
journal, sans exécuter le système : c'est le critère d'auditabilité.
"""

import re
import unicodedata

from component_inventory import (is_known_component, resolve_component,
                                 infer_component_from_events, LOG_SOURCE_COMPONENT)

# Un ancrage nul annulerait toute confiance et rendrait le score binaire.
# On conserve un plancher : une hypothèse dont aucune preuve n'est
# vérifiable garde 15 % de sa confiance déclarée — assez pour rester
# visible dans le journal, très loin du seuil d'action automatique.
GROUNDING_FLOOR = 0.15

# Idem pour un composant non reconnu : la pénalité est lourde mais non
# éliminatoire, car le garde-fou bloque déjà toute action sur un composant
# inconnu (aucune action candidate). Double barrière, pas double peine.
UNKNOWN_COMPONENT_FACTOR = 0.3

# Nombre de tokens minimum pour qu'un mot serve d'ancrage. En dessous, on
# rattacherait une preuve à des mots vides ("le", "de", "un").
MIN_TOKEN_LENGTH = 4


def _tokens(text: str) -> set[str]:
    """
    Extrait les termes significatifs, en découpant AUSSI les identifiants
    composés en leurs éléments.

    Correctif : la version initiale traitait `latence_injectee_ms` comme un
    token unique. Un agent écrivant « anomalie observée dans la latence »
    n'obtenait alors aucune correspondance, et sa preuve — pourtant juste —
    était comptée comme non ancrée. La vérification pénalisait du travail
    correct, ce qui est pire qu'inutile : elle rendait le score aveugle à
    la différence entre une preuve fondée et une hallucination.

    On indexe donc `latence_injectee_ms` sous cette forme complète ET sous
    ses composants (`latence`, `injectee`). Le critère reste discriminant :
    une preuve qui ne mentionne aucun terme des données reçues, comme
    « les templates de logs sont des faux », ne correspond toujours à rien.
    """
    normalised = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    normalised = normalised.lower()

    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9_.]+", normalised):
        if len(raw) >= MIN_TOKEN_LENGTH:
            tokens.add(raw)
        # Sous-éléments d'un identifiant composé (snake_case, points).
        for part in re.split(r"[_.]+", raw):
            if len(part) >= MIN_TOKEN_LENGTH:
                tokens.add(part)
    return tokens


def _numbers(text: str) -> set[str]:
    """Valeurs numériques citées, normalisées (200.0 et 200 sont identiques)."""
    found = set()
    for raw in re.findall(r"\d+(?:[.,]\d+)?", str(text)):
        try:
            found.add(f"{float(raw.replace(',', '.')):.2f}")
        except ValueError:
            continue
    return found


def evidence_grounding(evidence: list[str], observed_payload) -> tuple[float, dict]:
    """
    Proportion des preuves citées qui renvoient réellement à quelque chose
    de présent dans les données transmises à l'agent.

    Une preuve est considérée ancrée si elle partage avec les données reçues
    au moins un terme significatif (nom de métrique, template de log) OU une
    valeur numérique. C'est volontairement permissif : l'objectif n'est pas
    de noter la qualité rédactionnelle, mais de repérer les preuves qui ne
    renvoient à RIEN — le cas « les templates de logs sont des faux », où
    aucun terme ne correspond aux données reçues.

    Ce ratio est aussi, directement, la mesure du taux d'hallucination
    demandée par le cahier des charges : une preuve non ancrée est une
    référence à un élément inexistant. Jusqu'ici ce critère était listé
    comme objectif sans qu'aucun code ne sache le calculer.
    """
    detail = {"preuves_totales": len(evidence or []), "preuves_ancrees": 0,
              "non_ancrees": []}

    if not evidence:
        detail["raison"] = "aucune_preuve_fournie"
        return GROUNDING_FLOOR, detail

    observed_text = str(observed_payload)
    observed_tokens = _tokens(observed_text)
    observed_numbers = _numbers(observed_text)

    grounded = 0
    for item in evidence:
        item_tokens = _tokens(item)
        item_numbers = _numbers(item)
        if (item_tokens & observed_tokens) or (item_numbers & observed_numbers):
            grounded += 1
        else:
            detail["non_ancrees"].append(item[:120])

    detail["preuves_ancrees"] = grounded
    ratio = grounded / len(evidence)
    return max(GROUNDING_FLOOR, ratio), detail


def component_validity(composant_suspecte: str,
                       composant_deduit: str | None = None) -> tuple[float, dict]:
    """
    Vérifie que le composant en cause est bien un composant supervisé.

    Correctif important. La version initiale ne regardait QUE le nom fourni
    par le modèle : un modèle répondant « inconnu » écrasait la confiance
    d'un facteur 0.3, y compris lorsque le code avait déduit le composant
    avec certitude à partir de la métrique en anomalie. On pénalisait donc
    le score pour une information qu'on possédait — un double comptage qui
    maintenait mécaniquement toutes les confiances sous les seuils
    d'action, quelle que soit la qualité réelle du diagnostic.

    Le facteur porte désormais sur le composant qui FAIT AUTORITÉ : celui
    déduit en code quand il existe, celui du modèle sinon. Le désaccord
    entre les deux reste tracé — c'est une mesure utile de la qualité du
    modèle — mais il ne réduit plus la confiance, puisqu'il ne réduit pas
    la certitude que l'on a sur le composant.
    """
    resolu_modele = resolve_component(composant_suspecte)
    autorite = composant_deduit if composant_deduit and composant_deduit != "inconnu" \
        else resolu_modele
    valide = autorite != "inconnu" and is_known_component(autorite)

    return (1.0 if valide else UNKNOWN_COMPONENT_FACTOR), {
        "composant_declare": composant_suspecte,
        "composant_declare_resolu": resolu_modele,
        "composant_deduit_en_code": composant_deduit,
        "composant_faisant_autorite": autorite,
        "reconnu": valide,
        "concordance_modele_code": (resolu_modele == autorite),
    }


def signal_strength(anomaly_events: list[dict] | None) -> tuple[float, dict]:
    """
    Force objective du signal détecté dans une modalité, sur [0, 1].

    Sert à PONDÉRER les deux hypothèses l'une par rapport à l'autre, en
    remplacement des poids fixes 0.5/0.5. Ces poids arbitraires étaient le
    point le plus difficile à défendre de l'ancienne formule : impossible
    d'expliquer pourquoi 0.5 plutôt que 0.6. Ici, il n'y a plus de constante
    à justifier — le poids est dérivé de l'amplitude mesurée.

    Métriques : z-score maximal, normalisé (z=3 est le seuil de détection,
    z=10 ou plus est un signal massif).
    Logs : un template inédit vaut un signal fort ; sinon on se base sur le
    z-score de fréquence.
    """
    detail = {"evenements": len(anomaly_events or [])}
    if not anomaly_events:
        detail["raison"] = "aucun_evenement"
        return 0.0, detail

    best = 0.0
    for event in anomaly_events:
        if event.get("reason") == "new_template":
            best = max(best, 1.0)
            detail["template_inedit"] = True
            continue
        z = event.get("z_score")
        if z is None:
            continue
        # z=3 (seuil) -> 0.3 ; z=10 -> 1.0. Bornée, pour qu'une valeur
        # aberrante ne saature pas la pondération à elle seule.
        best = max(best, min(1.0, abs(float(z)) / 10.0))

    detail["force"] = round(best, 3)
    return best, detail


def effective_confidence(hypothesis: dict, observed_payload,
                         anomaly_events: list[dict] | None,
                         composant_deduit: str | None = None) -> dict:
    """
    Assemble les trois facteurs pour une hypothèse d'agent.

    Retourne un dictionnaire directement journalisable : chaque terme du
    calcul y figure, ce qui permet à un humain de refaire l'opération à la
    main six mois plus tard, à partir du seul journal.
    """
    declared = float(hypothesis.get("confidence", 0.0) or 0.0)
    grounding, grounding_detail = evidence_grounding(
        hypothesis.get("evidence", []), observed_payload)
    # Le composant déduit en code fait autorité (cf. component_validity).
    # S'il n'est pas fourni par l'appelant, on le calcule ici à partir des
    # mêmes événements d'anomalie, pour que la décote reste cohérente avec
    # le composant que le verdict désignera effectivement.
    if composant_deduit is None:
        composant_deduit, _ = infer_component_from_events(
            anomaly_events, default_owner=LOG_SOURCE_COMPONENT)
    validity, validity_detail = component_validity(
        hypothesis.get("composant_suspecte", ""), composant_deduit)
    strength, strength_detail = signal_strength(anomaly_events)

    effective = round(declared * grounding * validity, 4)

    return {
        "confiance_declaree": round(declared, 4),
        "ancrage_preuves": round(grounding, 4),
        "validite_composant": round(validity, 4),
        "confiance_effective": effective,
        "force_signal": round(strength, 4),
        "formule": "confiance_effective = declaree x ancrage x validite_composant",
        "detail_ancrage": grounding_detail,
        "detail_composant": validity_detail,
        "detail_signal": strength_detail,
    }