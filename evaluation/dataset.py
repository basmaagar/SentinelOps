"""
Construction du jeu d'évaluation — implémentation unique et partagée.

Pourquoi ce module existe
-------------------------
Les trois analyses — métriques globales, calibration, rejeu contrefactuel —
avaient chacune leur propre logique d'appariement entre décisions et
incidents. Elles produisaient donc trois effectifs et trois taux de réussite
différents sur les mêmes données :

    compute_metrics   : 23 décisions, 61 % correctes
    calibration       : 29 décisions, 72 % correctes
    threshold_replay  : 21 décisions, 100 % correctes

Ces écarts ne venaient d'aucune différence de fond mais de trois
implémentations divergentes : sens de parcours différent, unicité de
l'appariement traitée ou non, décisions de repli incluses ou exclues.

Trois chiffres incompatibles issus des mêmes journaux discréditent
l'ensemble de l'évaluation. Ce module est donc la SEULE définition de ce
qu'est une décision évaluable, et les trois analyses l'utilisent.

Trois catégories, volontairement distinctes
-------------------------------------------
Toutes les décisions ne se valent pas, et les confondre fausse chaque
mesure. On distingue :

  DIAGNOSTIQUÉE — le système a produit un diagnostic exploitable. C'est la
                  seule catégorie sur laquelle une précision a un sens.

  EN ÉCHEC      — l'appel au modèle a échoué, le repli déterministe s'est
                  appliqué. Ce n'est pas un mauvais diagnostic, c'est
                  l'absence de diagnostic. L'inclure dans la calibration
                  reviendrait à traiter une panne d'infrastructure comme
                  une prédiction erronée.

  NON DÉTECTÉE  — aucune décision n'a été produite pour cet incident. Relève
                  de la couverture, pas de la précision.
"""

import json
import pathlib
from dataclasses import dataclass, asdict

# Fenêtre d'attribution : au-delà, un verdict n'est plus imputable à
# l'incident. Fixée large (l'inférence CPU peut être lente) mais finie,
# pour ne pas rattacher à un incident un verdict concernant le suivant.
ATTRIBUTION_WINDOW = 240

EXPECTED_COMPONENT = {
    "disk_saturation": "target-app", "saturation_disque": "target-app",
    "memory_leak": "target-app", "fuite_memoire": "target-app",
    "latency_injection": "dependency-service",
    "latence_dependance": "dependency-service",
}

# Marqueurs d'une réponse de repli — alignés sur ceux de l'arbitre et de la
# console. Un diagnostic les portant n'est pas une prédiction.
_MARQUEURS_REPLI = ("indisponible", "échec de l'analyse", "echec de l'analyse",
                    "aucun diagnostic exploitable")


@dataclass
class Decision:
    """Une décision appariée à son incident, avec sa vérité terrain."""
    incident_id: str
    type_panne: str
    ts_incident: float
    ts_decision: float
    ttd: float
    confiance: float
    composant_attendu: str
    composant_obtenu: str | None
    correct: bool
    statut_accord: str | None
    action: str | None
    risque: str
    decision_garde_fou: str | None
    action_executee: str | None
    profil: str | None
    preuves_totales: int
    preuves_ancrees: int
    en_echec: bool


def lire_jsonl(chemin: pathlib.Path) -> list[dict]:
    """
    Lit un JSONL en tolérant les encodages mixtes.

    Nécessaire parce que les journaux peuvent contenir des lignes écrites
    avant le correctif d'encodage : celles-ci sont en cp1252, les suivantes
    en UTF-8. Les perdre réduirait l'échantillon d'évaluation.
    """
    if not chemin.exists():
        return []
    sortie, ignorees = [], 0
    with chemin.open("rb") as f:
        for brut in f:
            if not brut.strip():
                continue
            try:
                ligne = brut.decode("utf-8")
            except UnicodeDecodeError:
                ligne = brut.decode("cp1252", errors="replace")
            try:
                sortie.append(json.loads(ligne))
            except json.JSONDecodeError:
                ignorees += 1
    if ignorees:
        print(f"[info] {ignorees} ligne(s) illisible(s) ignorée(s) dans {chemin.name}")
    return sortie


def _est_en_echec(verdict: dict) -> bool:
    """
    Deux signaux concordants sont exigés : confiance nulle ET marqueur de
    repli dans le diagnostic.

    La double condition compte : une confiance nulle seule peut résulter
    d'une décote légitime sur une hypothèse réelle mais mal étayée — cas
    différent, qui doit rester dans l'évaluation.
    """
    if float(verdict.get("final_confidence") or 0.0) > 0.0:
        return False
    texte = str(verdict.get("diagnosis", "")).lower()
    return any(marqueur in texte for marqueur in _MARQUEURS_REPLI)


def _compter_preuves(verdict: dict) -> tuple[int, int]:
    """Preuves citées et preuves ancrées, depuis le détail de confiance."""
    breakdown = verdict.get("confidence_breakdown") or {}
    blocs = [breakdown[cle] for cle in ("agent_metriques", "agent_logs")
             if isinstance(breakdown.get(cle), dict)]
    if isinstance(breakdown.get("facteurs"), dict):
        blocs.append(breakdown["facteurs"])

    total = ancrees = 0
    for bloc in blocs:
        detail = bloc.get("detail_ancrage") or {}
        total += int(detail.get("preuves_totales", 0) or 0)
        ancrees += int(detail.get("preuves_ancrees", 0) or 0)
    return total, ancrees


def construire(decisions_brutes: list[dict],
               incidents_bruts: list[dict]) -> tuple[list[Decision], list[dict]]:
    """
    Apparie chaque incident à la première décision qui le suit.

    Règles d'appariement, fixées une fois pour toutes :

    1. Sens INCIDENT -> DÉCISION. On part de ce qui a été injecté et on
       cherche ce que le système en a dit. Le sens inverse laisserait de
       côté les incidents non détectés, qui sont pourtant l'information
       principale de la couverture.

    2. PREMIÈRE décision postérieure au début de l'incident, dans la
       fenêtre d'attribution. La première et non la dernière : c'est elle
       qui détermine le temps de diagnostic.

    3. UNICITÉ. Une décision déjà attribuée ne peut pas l'être une seconde
       fois. Sans cette règle, deux injections rapprochées se partageraient
       le même verdict et le compte serait gonflé.

    Retourne les décisions appariées, et la liste des incidents non
    détectés — ces derniers ne sont pas une erreur de diagnostic mais un
    défaut de couverture, et les deux doivent être rapportés séparément.
    """
    datees = sorted(
        ((float(d.get("ts", 0) or 0), d) for d in decisions_brutes
         if float(d.get("ts", 0) or 0) > 0),
        key=lambda paire: paire[0],
    )
    utilisees: set[int] = set()

    appariees: list[Decision] = []
    non_detectes: list[dict] = []

    ordonnes = sorted(incidents_bruts, key=lambda i: float(i.get("start_ts", 0) or 0))

    for index, incident in enumerate(ordonnes):
        debut = float(incident.get("start_ts", 0) or 0)
        if not debut:
            continue
        attendu = EXPECTED_COMPONENT.get(incident.get("type", ""),
                                         incident.get("composant_cible", ""))

        # Borne haute de l'attribution : le plus proche entre la fenêtre
        # nominale et le DÉBUT DE L'INCIDENT SUIVANT.
        #
        # Correctif d'un défaut sérieux. La fenêtre était fixée à 240 s,
        # alors qu'une campagne enchaîne les injections toutes les ~165 s
        # (45 s de panne + 120 s de retour au calme). Un incident pouvait
        # donc capturer la décision appartenant au suivant : le temps de
        # diagnostic mesuré devenait l'intervalle entre deux injections
        # plutôt que la latence réelle du système, et la précision
        # s'effondrait puisque les diagnostics étaient comparés à la mauvaise
        # vérité terrain.
        #
        # Une décision postérieure au début de l'incident suivant appartient
        # à celui-ci, quelle que soit la fenêtre nominale.
        limite = debut + ATTRIBUTION_WINDOW
        if index + 1 < len(ordonnes):
            debut_suivant = float(ordonnes[index + 1].get("start_ts", 0) or 0)
            if debut_suivant > debut:
                limite = min(limite, debut_suivant)

        trouvee = None
        for position, (ts, decision) in enumerate(datees):
            if position in utilisees or ts < debut:
                continue
            if ts > limite:
                break
            utilisees.add(position)
            trouvee = (ts, decision)
            break

        if trouvee is None:
            non_detectes.append(incident)
            continue

        ts, decision = trouvee
        verdict = decision.get("arbiter_verdict") or {}
        garde_fou = decision.get("guardrail_decision") or {}
        total, ancrees = _compter_preuves(verdict)
        obtenu = verdict.get("composant_suspecte")

        appariees.append(Decision(
            incident_id=incident.get("incident_id", "?"),
            type_panne=incident.get("type", "?"),
            ts_incident=debut,
            ts_decision=ts,
            ttd=round(ts - debut, 2),
            confiance=float(verdict.get("final_confidence") or 0.0),
            composant_attendu=attendu,
            composant_obtenu=obtenu,
            correct=(obtenu == attendu),
            statut_accord=verdict.get("agreement_status"),
            action=garde_fou.get("action"),
            risque=garde_fou.get("intrinsic_risk", "modere"),
            decision_garde_fou=garde_fou.get("decision"),
            action_executee=decision.get("action_executed"),
            profil=garde_fou.get("profile"),
            preuves_totales=total,
            preuves_ancrees=ancrees,
            en_echec=_est_en_echec(verdict),
        ))

    return appariees, non_detectes


def diagnostiquees(appariees: list[Decision]) -> list[Decision]:
    """
    Décisions portant un diagnostic réel — les analyses de qualité ne
    portent que sur elles.

    Une analyse en échec n'est pas un mauvais diagnostic : c'est l'absence
    de diagnostic. La compter comme une prédiction erronée reviendrait à
    imputer au modèle une panne d'infrastructure.
    """
    return [d for d in appariees if not d.en_echec]


def resume_effectifs(appariees: list[Decision], non_detectes: list[dict]) -> dict:
    """
    Les trois catégories, toujours rapportées ensemble.

    C'est ce résumé qui garantit la cohérence entre les analyses : chacune
    doit repartir des mêmes effectifs, et les afficher.
    """
    utiles = diagnostiquees(appariees)
    echecs = [d for d in appariees if d.en_echec]
    return {
        "incidents_injectes": len(appariees) + len(non_detectes),
        "diagnostiquees": len(utiles),
        "analyses_en_echec": len(echecs),
        "non_detectes": len(non_detectes),
    }


def charger(decisions_path: pathlib.Path,
            ground_truth_path: pathlib.Path) -> tuple[list[Decision], list[dict]]:
    return construire(lire_jsonl(decisions_path), lire_jsonl(ground_truth_path))


def en_dict(decision: Decision) -> dict:
    return asdict(decision)