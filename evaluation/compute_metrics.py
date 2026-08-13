"""
Calcul des métriques d'évaluation — Jour 13.

Rapproche `injectors/ground_truth.jsonl` (ce qui a réellement été injecté)
de `guardrails/decisions_log.jsonl` (ce que le système a conclu), puis
calcule les critères définis dans le cahier des charges.

Le rapprochement se fait par FENÊTRE TEMPORELLE : une décision est
attribuée à l'incident dont l'intervalle d'injection la précède le plus
récemment, dans une limite donnée. C'est la seule jointure possible — la
boucle de supervision ignore l'existence des incidents injectés, et c'est
précisément ce qui rend l'évaluation honnête : le système ne sait pas
qu'il est évalué, et n'a aucun moyen de « connaître » la bonne réponse.

Métriques produites
-------------------
  - précision du diagnostic : proportion d'incidents dont le composant
    identifié correspond au composant réellement touché ;
  - TTD (temps de détection) : délai entre le début de l'injection et la
    production du verdict ;
  - taux d'hallucination : proportion de preuves citées par les agents qui
    ne correspondent à rien dans les données qu'ils ont reçues. C'est la
    mesure rendue possible par `evidence_grounding` ;
  - couverture : proportion d'incidents ayant produit un diagnostic. Un
    incident non détecté n'est ni un succès ni un échec de diagnostic : le
    confondre avec une erreur de diagnostic fausserait la précision.
"""

import sys
import json
import pathlib
import statistics
from dataclasses import dataclass, field, asdict

_ROOT = pathlib.Path(__file__).resolve().parent.parent

GROUND_TRUTH_PATH = _ROOT / "injectors" / "ground_truth.jsonl"
DECISIONS_PATH = _ROOT / "guardrails" / "decisions_log.jsonl"

# Un verdict produit plus de N secondes après le début d'une injection
# n'est plus attribué à cet incident. Fixé large (le diagnostic peut être
# lent sur inférence CPU) mais fini, pour ne pas rattacher à un incident
# un verdict qui concerne en réalité le suivant.
ATTRIBUTION_WINDOW_SECONDS = 240

# Correspondance type de panne -> composant attendu. Doit rester alignée
# avec les scénarios de run_campaign.py.
EXPECTED_COMPONENT = {
    "disk_saturation": "target-app",
    "saturation_disque": "target-app",
    "memory_leak": "target-app",
    "fuite_memoire": "target-app",
    "latency_injection": "dependency-service",
    "latence_dependance": "dependency-service",
}


@dataclass
class IncidentResult:
    incident_id: str
    type: str
    composant_attendu: str
    detecte: bool = False
    composant_identifie: str | None = None
    correct: bool | None = None
    ttd_seconds: float | None = None
    confiance: float | None = None
    decision_garde_fou: str | None = None
    action_executee: str | None = None
    profil_risque: str | None = None
    preuves_totales: int = 0
    preuves_ancrees: int = 0
    decision_id: str | None = None


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    """
    Lit un fichier JSONL en tolérant les encodages mixtes.

    Nécessaire parce que les journaux contiennent des lignes écrites avant
    le correctif d'encodage du Jour 12 : celles-ci sont en cp1252 (défaut
    de Windows), les suivantes en UTF-8. Un même fichier peut donc mêler
    les deux, ce qui fait échouer une lecture UTF-8 stricte sur la
    première ligne ancienne rencontrée.

    On décode ligne par ligne, avec repli sur cp1252, plutôt que d'imposer
    de repartir d'un journal vide : ces lignes contiennent de vraies
    décisions, et les perdre réduirait d'autant l'échantillon d'évaluation.
    """
    if not path.exists():
        return []

    records = []
    ignorees = 0
    with path.open("rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Ligne antérieure au correctif d'encodage.
                line = raw.decode("cp1252", errors="replace")
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                ignorees += 1  # ligne partielle (écriture interrompue)

    if ignorees:
        print(f"[info] {ignorees} ligne(s) illisible(s) ignorée(s) dans {path.name}")
    return records


def _decision_timestamp(decision: dict) -> float | None:
    verdict = decision.get("arbiter_verdict") or {}
    timing = verdict.get("timing") or {}
    return timing.get("ts_verdict") or decision.get("ts")


def _count_evidence(decision: dict) -> tuple[int, int]:
    """
    Compte les preuves citées et les preuves ancrées, depuis le détail de
    confiance produit par l'arbitre. Ce comptage est la base du taux
    d'hallucination : une preuve non ancrée référence un élément absent
    des données transmises à l'agent.
    """
    verdict = decision.get("arbiter_verdict") or {}
    breakdown = verdict.get("confidence_breakdown") or {}

    blocs = []
    for cle in ("agent_metriques", "agent_logs"):
        if isinstance(breakdown.get(cle), dict):
            blocs.append(breakdown[cle])
    if isinstance(breakdown.get("facteurs"), dict):
        blocs.append(breakdown["facteurs"])

    total = ancrees = 0
    for bloc in blocs:
        detail = bloc.get("detail_ancrage") or {}
        total += int(detail.get("preuves_totales", 0) or 0)
        ancrees += int(detail.get("preuves_ancrees", 0) or 0)
    return total, ancrees


def evaluate(ground_truth_path=None, decisions_path=None,
             window_seconds: int = ATTRIBUTION_WINDOW_SECONDS) -> dict:
    incidents = _read_jsonl(ground_truth_path or GROUND_TRUTH_PATH)
    decisions = _read_jsonl(decisions_path or DECISIONS_PATH)

    dated_decisions = sorted(
        ((_decision_timestamp(d), d) for d in decisions if _decision_timestamp(d)),
        key=lambda pair: pair[0],
    )

    results: list[IncidentResult] = []
    used_decisions: set[int] = set()

    for incident in sorted(incidents, key=lambda i: i.get("start_ts", 0)):
        expected = EXPECTED_COMPONENT.get(
            incident.get("type", ""), incident.get("composant_cible", "inconnu"))
        result = IncidentResult(
            incident_id=incident.get("incident_id", "?"),
            type=incident.get("type", "?"),
            composant_attendu=expected,
        )
        start = incident.get("start_ts", 0)

        # Premier verdict postérieur au début de l'injection, non déjà
        # attribué à un incident précédent. L'unicité évite qu'un même
        # verdict compte deux fois si deux injections se chevauchent.
        for position, (ts, decision) in enumerate(dated_decisions):
            if position in used_decisions or ts < start:
                continue
            if ts - start > window_seconds:
                break

            verdict = decision.get("arbiter_verdict") or {}
            guardrail = decision.get("guardrail_decision") or {}
            total, ancrees = _count_evidence(decision)

            result.detecte = True
            result.decision_id = decision.get("decision_id")
            result.composant_identifie = verdict.get("composant_suspecte")
            result.correct = (result.composant_identifie == expected)
            result.ttd_seconds = round(ts - start, 2)
            result.confiance = verdict.get("final_confidence")
            result.decision_garde_fou = guardrail.get("decision")
            result.profil_risque = guardrail.get("profile")
            result.action_executee = decision.get("action_executed")
            result.preuves_totales = total
            result.preuves_ancrees = ancrees
            used_decisions.add(position)
            break

        results.append(result)

    return _summarise(results)


def _summarise(results: list[IncidentResult]) -> dict:
    total = len(results)
    detectes = [r for r in results if r.detecte]
    corrects = [r for r in detectes if r.correct]
    ttds = [r.ttd_seconds for r in detectes if r.ttd_seconds is not None]

    preuves_totales = sum(r.preuves_totales for r in detectes)
    preuves_ancrees = sum(r.preuves_ancrees for r in detectes)

    # Précision calculée sur les incidents DÉTECTÉS. Un incident non
    # détecté relève de la couverture, pas d'une erreur de diagnostic :
    # les mélanger produirait un chiffre qui ne mesure ni l'un ni l'autre.
    # Les deux valeurs sont donc rapportées séparément.
    precision = len(corrects) / len(detectes) if detectes else None
    couverture = len(detectes) / total if total else None
    taux_hallucination = (
        1 - (preuves_ancrees / preuves_totales) if preuves_totales else None
    )

    par_type: dict[str, dict] = {}
    for r in results:
        bucket = par_type.setdefault(r.type, {"total": 0, "detectes": 0, "corrects": 0})
        bucket["total"] += 1
        bucket["detectes"] += int(r.detecte)
        bucket["corrects"] += int(bool(r.correct))

    return {
        "incidents_injectes": total,
        "incidents_detectes": len(detectes),
        "couverture": round(couverture, 4) if couverture is not None else None,
        "precision_diagnostic": round(precision, 4) if precision is not None else None,
        "objectif_precision": 0.70,
        "ttd_median_s": round(statistics.median(ttds), 2) if ttds else None,
        "ttd_moyen_s": round(statistics.fmean(ttds), 2) if ttds else None,
        "ttd_max_s": round(max(ttds), 2) if ttds else None,
        "ttd_sous_60s": (round(sum(1 for t in ttds if t < 60) / len(ttds), 4)
                         if ttds else None),
        "objectif_ttd_s": 60,
        "preuves_citees": preuves_totales,
        "preuves_ancrees": preuves_ancrees,
        "taux_hallucination": (round(taux_hallucination, 4)
                               if taux_hallucination is not None else None),
        "actions_executees": sum(1 for r in detectes if r.action_executee),
        "validations_humaines": sum(1 for r in detectes
                                    if r.decision_garde_fou == "validation_humaine"),
        "par_type": par_type,
        "detail": [asdict(r) for r in results],
    }


def main() -> None:
    resume = evaluate()
    detail = resume.pop("detail")

    print(json.dumps(resume, indent=2, ensure_ascii=False))

    sortie = pathlib.Path(__file__).parent / "evaluation_results.json"
    with sortie.open("w", encoding="utf-8") as f:
        json.dump({**resume, "detail": detail}, f, indent=2, ensure_ascii=False)
    print(f"\nDétail complet écrit dans {sortie}")


if __name__ == "__main__":
    main()