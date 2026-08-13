"""
Boucle de supervision continue — Jour 12.

C'est le chaînon qui manquait : jusqu'ici, toutes les briques existaient
(détection, agents, arbitre, garde-fou, remédiation, journalisation) mais
rien ne les reliait à une source de données réelle. Le graphe était
invoqué à la main, une fois par test.

Cette boucle :
  1. lit un échantillon de métriques dans Prometheus et les nouvelles
     lignes de app.log, à intervalle régulier ;
  2. invoque le graphe (détection -> agents -> arbitre) ;
  3. applique le garde-fou et exécute ou met en file d'attente l'action ;
  4. programme une vérification post-action différée, SANS bloquer la
     boucle (sinon un incident suivant ne serait pas détecté pendant la
     fenêtre d'observation) ;
  5. journalise chaque décision avec ses horodatages, ce qui rend le TTD
     enfin mesurable.

Le TTD ne peut être calculé qu'ici : c'est le seul point du système qui
connaisse à la fois l'instant de lecture de la donnée et l'instant de
production du verdict. Il est recoupé a posteriori avec ground_truth.jsonl
(instant d'injection réel) au Jour 13.
"""

import sys
import time
import signal
import logging
import pathlib
import threading
from dataclasses import dataclass

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _sub in ("agents", "detector", "guardrails", "remediation", "supervision"):
    sys.path.append(str(_ROOT / _sub))

from llm_client import OllamaClient                    # noqa: E402
from graph import build_graph                          # noqa: E402
from anomaly_detector import AnomalyDetector           # noqa: E402
from log_anomaly_detector import LogAnomalyDetector    # noqa: E402
from policy import evaluate                            # noqa: E402
from action_selector import select_action, resolve_and_select  # noqa: E402
from lock import RemediationLock                       # noqa: E402
from decisions_log import log_decision, append_post_action_outcome  # noqa: E402
from actions import execute_action, ActionExecutionError            # noqa: E402
from human_validation import enqueue_for_validation    # noqa: E402
from baseline_check import HealthyBaseline             # noqa: E402
from prom_client import PrometheusClient               # noqa: E402
from log_source import FileLogTailer                   # noqa: E402
from confirming_detector import ConfirmingAnomalyDetector  # noqa: E402

logger = logging.getLogger("sentinelops.supervision.loop")


# --- Configuration ---------------------------------------------------------
# Les modèles sont ici et NULLE PART ailleurs : la bascule vers les gros
# modèles (Jour 15) doit être un simple changement de paramètre, jamais
# une chasse aux occurrences codées en dur dans les agents.
@dataclass
class LoopConfig:
    metrics_model: str = "qwen2.5:1.5b"
    logs_model: str = "llama3.2:1b"
    arbiter_model: str = "qwen2.5:1.5b"
    ollama_url: str = "http://localhost:11435"
    prometheus_url: str = "http://localhost:9090"
    app_log_path: str = "./logs/app.log"
    tick_seconds: float = 5.0            # aligné sur le scrape_interval Prometheus
    observation_seconds: float = 60.0    # fenêtre avant vérification post-action
    target_container: str = "sentinelops-target-app"
    dry_run: bool = False                # True = n'exécute aucune action réelle

    # Timeout LLM. Ramené de 120 s à 45 s dans un premier temps, puis
    # remonté à 90 s après mesure : sur cette machine (inférence CPU, sans
    # GPU), un modèle 1.5B ne tient pas 45 s même une fois préchargé.
    # 90 s est un compromis assumé — il dépasse l'objectif de TTD de 60 s,
    # ce qui doit être mesuré et discuté dans le rapport plutôt que masqué.
    # Le budget de génération a par ailleurs été réduit (cf. llm_client),
    # ce qui devrait ramener la plupart des appels bien en dessous.
    llm_timeout_seconds: float = 90.0

    # Nombre de cycles consécutifs pendant lesquels une anomalie doit être
    # vue avant de solliciter les agents (cf. confirming_detector.py).
    required_consecutive: int = 2

    # Échantillons collectés avant d'activer la détection, le temps que les
    # fenêtres glissantes se remplissent.
    warmup_samples: int = 30

    # Profil de seuils du garde-fou (cf. guardrails/risk_profiles.py).
    # "production" = régime strict, "evaluation" = seuils abaissés pour
    # exercer la boucle de remédiation pendant la campagne. Le profil
    # retenu est journalisé avec chaque décision.
    risk_profile: str = "evaluation"

    # Intervalle du message périodique d'état. Sur un système sain, la
    # boucle est totalement silencieuse — ce qui est le comportement voulu,
    # mais rend impossible de distinguer "rien à signaler" de "processus
    # figé". Ce battement affiche périodiquement ce qui est réellement
    # observé, sans polluer le journal.
    heartbeat_seconds: float = 60.0


@dataclass
class PendingCheck:
    decision_id: str
    component: str
    action: str
    target: str
    due_ts: float


class SupervisionLoop:
    def __init__(self, config: LoopConfig, docker_client=None):
        self.config = config
        self.docker_client = docker_client

        self.prometheus = PrometheusClient(config.prometheus_url)
        self.tailer = FileLogTailer(config.app_log_path)

        llm = OllamaClient(base_url=config.ollama_url,
                           timeout_seconds=config.llm_timeout_seconds)
        self.llm = llm

        # Le détecteur brut est enveloppé par le détecteur à confirmation :
        # c'est lui qui filtre les faux positifs isolés d'Isolation Forest.
        self.metric_detector = ConfirmingAnomalyDetector(
            AnomalyDetector(),
            required_consecutive=config.required_consecutive,
            warmup_samples=config.warmup_samples,
        )
        self.log_detector = LogAnomalyDetector()
        self.graph = build_graph(
            metrics_client=llm, metrics_model=config.metrics_model,
            logs_client=llm, logs_model=config.logs_model,
            arbiter_client=llm, arbiter_model=config.arbiter_model,
            metric_detector=self.metric_detector,
            log_detector=self.log_detector,
        )

        self.lock = RemediationLock()
        self.baseline = HealthyBaseline()
        self._pending_checks: list[PendingCheck] = []
        self._running = False

        # Un seul diagnostic à la fois. Le graphe partage des détecteurs à
        # état (fenêtres glissantes, Drain3) qui ne sont pas conçus pour un
        # accès concurrent : deux diagnostics en parallèle corrompraient
        # ces fenêtres. Les anomalies survenant pendant un diagnostic en
        # cours sont donc ignorées — c'est acceptable, une panne persiste
        # et sera reprise au cycle suivant.
        self._diagnosis_lock = threading.Lock()
        self._diagnosis_thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._ticks = 0
        self._analyses = 0     # cycles où le graphe a tourné (détection)
        self._diagnoses = 0    # cycles ayant réellement sollicité les agents

    # -- Boucle principale --------------------------------------------------

    def run(self) -> None:
        self._running = True
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

        if not self.prometheus.is_up():
            raise RuntimeError(
                f"Prometheus injoignable sur {self.config.prometheus_url} — "
                "lancer `docker compose up -d` avant la boucle."
            )

        # Une requête PromQL sans correspondance renvoie un résultat vide,
        # pas une erreur : on signale donc explicitement les métriques
        # muettes, sans quoi elles disparaissent de la supervision en
        # silence (cf. prom_client.report_missing).
        missing = self.prometheus.report_missing()
        if missing:
            logger.warning(
                f"métriques sans données : {missing} — "
                "vérifier les noms de métriques et les étiquettes cAdvisor"
            )

        # Préchargement des modèles AVANT la première anomalie. Ollama
        # décharge un modèle après quelques minutes d'inactivité ; sans ce
        # préchargement, le premier diagnostic paierait le rechargement
        # complet depuis le disque et dépasserait forcément le timeout.
        self.llm.prewarm([
            self.config.metrics_model,
            self.config.logs_model,
            self.config.arbiter_model,
        ])

        logger.info(
            f"boucle démarrée (tick={self.config.tick_seconds}s, "
            f"modèles={self.config.metrics_model}/{self.config.logs_model}, "
            f"profil de risque={self.config.risk_profile})"
        )
        while self._running:
            started = time.perf_counter()
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                # Une boucle de supervision qui meurt sur une exception
                # ponctuelle est pire qu'inutile : elle donne l'illusion
                # d'être surveillé. On journalise et on continue.
                logger.exception(f"erreur non fatale dans le tick : {exc}")

            elapsed = time.perf_counter() - started
            time.sleep(max(0.0, self.config.tick_seconds - elapsed))

    def _stop(self, *_args) -> None:
        logger.info("arrêt demandé — sortie après le tick courant")
        self._running = False

    def tick(self) -> None:
        ts_read = time.time()
        metrics_sample = self.prometheus.sample()
        log_lines = self.tailer.read_new_lines()
        self._ticks += 1
        self._heartbeat(ts_read, metrics_sample, log_lines)

        # Ces deux opérations sont rapides et doivent continuer même
        # pendant un diagnostic : c'est ce qui garantit qu'une vérification
        # post-action arrive bien à l'heure.
        self._process_due_checks(ts_read, metrics_sample)

        if not metrics_sample and not log_lines:
            return

        # Un diagnostic est déjà en cours : le graphe et ses détecteurs
        # sont à état partagé (fenêtres glissantes, Drain3), donc on ne
        # peut pas en lancer un second en parallèle sans corrompre ces
        # fenêtres. On saute ce cycle de diagnostic. Une vraie panne
        # persiste plusieurs dizaines de secondes et sera reprise ensuite.
        if self._diagnosis_lock.locked():
            logger.debug("diagnostic déjà en cours — cycle sauté")
            return

        # Le diagnostic part en arrière-plan. C'est le correctif du défaut
        # majeur observé au premier lancement : un appel LLM de 90 à 150 s
        # exécuté dans la boucle bloquait TOUTE la collecte pendant ce
        # temps — la boucle prétendait tourner à 5 s alors qu'elle ne
        # produisait un échantillon que toutes les 2 à 3 minutes.
        self._diagnosis_thread = threading.Thread(
            target=self._run_diagnosis,
            args=(ts_read, metrics_sample, log_lines),
            daemon=True,
        )
        self._diagnosis_thread.start()

    def _heartbeat(self, now: float, metrics_sample: dict,
                   log_lines: list[str]) -> None:
        if now - self._last_heartbeat < self.config.heartbeat_seconds:
            return
        self._last_heartbeat = now

        # On affiche les valeurs réellement lues : c'est aussi le moyen le
        # plus simple de vérifier que les noms de métriques PromQL
        # correspondent bien à ce que l'application expose. Un échantillon
        # vide signale une requête qui ne renvoie rien, pas une panne.
        apercu = ", ".join(f"{k}={v:.1f}" for k, v in sorted(metrics_sample.items())[:4])
        etat = "diagnostic en cours" if self._diagnosis_lock.locked() else "surveillance"
        logger.info(
            f"[{etat}] {self._ticks} cycles · {len(metrics_sample)} métriques "
            f"({apercu or 'aucune'}) · {len(log_lines)} nouvelles lignes de log · "
            f"{self._analyses} analyses, {self._diagnoses} diagnostics IA · "
            f"{len(self._pending_checks)} vérifications en attente"
        )

    def _run_diagnosis(self, ts_read: float, metrics_sample: dict,
                       log_lines: list[str]) -> None:
        with self._diagnosis_lock:
            self._analyses += 1
            try:
                state = self.graph.invoke({
                    "ts": ts_read,
                    "metrics_sample": metrics_sample,
                    "log_lines": log_lines,
                })
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"échec du diagnostic : {exc}")
                return

            if not state.get("anomaly_detected"):
                # Période saine : on enrichit la ligne de base de référence.
                # C'est ici, et seulement ici, que la baseline se met à jour.
                self.baseline.observe(metrics_sample)
                return

            ts_verdict = time.time()
            self._diagnoses += 1
            verdict = state.get("arbiter_verdict")
            if verdict is None:
                logger.warning("anomalie détectée mais aucun verdict d'arbitre produit")
                return

            self._handle_verdict(state, verdict, ts_read, ts_verdict, metrics_sample)

    # -- Décision et action -------------------------------------------------

    def _handle_verdict(self, state: dict, verdict: dict,
                        ts_read: float, ts_verdict: float,
                        metrics_sample: dict) -> None:
        component = verdict.get("composant_suspecte", "inconnu")
        canonical, action = resolve_and_select(component)
        if canonical != component:
            logger.info(f"composant '{component}' résolu en '{canonical}'")
        component = canonical

        timing = {
            "ts_lecture_donnees": ts_read,
            "ts_verdict": ts_verdict,
            "latence_diagnostic_s": round(ts_verdict - ts_read, 3),
        }

        # Cas 1 : aucune action candidate pour ce composant.
        if action is None:
            self._log(state, verdict,
                      {"decision": "aucune_action_candidate", "composant": component},
                      None, timing)
            return

        # Cas 2 : une remédiation est déjà en observation sur cette cible.
        # Verrou vérifié AVANT le garde-fou : inutile d'évaluer le risque
        # d'une action qu'on ne déclenchera de toute façon pas.
        if self.lock.is_locked(component):
            self._log(state, verdict,
                      {"decision": "ignoree_verrou_actif", "composant": component,
                       "action_candidate": action},
                      None, timing)
            logger.info(f"action ignorée : {component} déjà en observation")
            return

        decision = evaluate(action, verdict, profile=self.config.risk_profile)
        decision_dict = decision.__dict__

        if decision.decision == "autoriser_auto":
            self._execute(state, verdict, decision_dict, action, component, timing)
        elif decision.decision == "validation_humaine":
            decision_id = self._log(state, verdict, decision_dict, None, timing)
            enqueue_for_validation(
                decision_id=decision_id, action=action,
                target=self.config.target_container,
                arbiter_verdict=verdict, guardrail_decision=decision_dict,
            )
        else:  # "refuser"
            self._log(state, verdict, decision_dict, None, timing)

    def _execute(self, state: dict, verdict: dict, decision_dict: dict,
                 action: str, component: str, timing: dict) -> None:
        target = self.config.target_container

        if self.config.dry_run:
            decision_id = self._log(state, verdict,
                                    {**decision_dict, "dry_run": True}, action, timing)
            logger.info(f"[dry-run] action {action} sur {target} non exécutée")
            self._schedule_check(decision_id, component, action, target)
            return

        self.lock.acquire(component, timeout_seconds=self.config.observation_seconds * 2)
        try:
            result = execute_action(self.docker_client, action, target)
            logger.info(f"action exécutée : {result}")
            executed = action
        except ActionExecutionError as exc:
            logger.error(f"échec d'exécution de {action} : {exc}")
            decision_dict = {**decision_dict, "erreur_execution": str(exc)}
            executed = None
            self.lock.release(component)

        decision_id = self._log(state, verdict, decision_dict, executed, timing)
        if executed:
            self._schedule_check(decision_id, component, action, target)

    # -- Vérification post-action différée ----------------------------------

    def _schedule_check(self, decision_id: str, component: str,
                        action: str, target: str) -> None:
        """
        Programme la vérification SANS bloquer : la boucle continue de
        détecter pendant la fenêtre d'observation. Un `time.sleep(60)`
        ici rendrait le système aveugle pendant une minute — exactement
        au moment le plus critique, juste après une action automatique.
        """
        self._pending_checks.append(PendingCheck(
            decision_id=decision_id, component=component, action=action,
            target=target, due_ts=time.time() + self.config.observation_seconds,
        ))

    def _process_due_checks(self, now: float, metrics_sample: dict) -> None:
        still_pending: list[PendingCheck] = []
        for check in self._pending_checks:
            if now < check.due_ts:
                still_pending.append(check)
                continue

            # Comparaison à la ligne de base SAINE, en lecture seule
            # (cf. baseline_check.py — correctif du défaut du Jour 10).
            comparison = self.baseline.compare(metrics_sample)
            classification = "negative" if comparison.is_anomalous else "positive"

            outcome = {
                "classification": classification,
                "action": check.action,
                "target": check.target,
                "metrics_sample_after": metrics_sample,
                "ecarts_vs_baseline": comparison.deviations,
                "raison": comparison.reason,
                "rollback_performed": False,
                "escalade": False,
            }

            if classification == "negative":
                outcome.update(self._handle_failed_action(check))

            append_post_action_outcome(check.decision_id, outcome)
            self.lock.release(check.component)
            logger.info(f"vérification post-action {check.decision_id} : {classification}")

        self._pending_checks = still_pending

    def _handle_failed_action(self, check: PendingCheck) -> dict:
        """
        L'action n'a pas amélioré la situation. Deux cas :
          - scale_replica : réversible, on annule ;
          - restart_container : un redémarrage n'a pas d'inverse, on
            escalade vers un humain plutôt que de prétendre avoir résolu.
        """
        if check.action == "scale_replica" and self.docker_client and not self.config.dry_run:
            try:
                service = self.docker_client.services.get(check.target)
                service.scale(1)
                return {"rollback_performed": True, "rollback_detail": "retour à 1 réplica"}
            except Exception as exc:  # noqa: BLE001
                return {"rollback_performed": False, "escalade": True,
                        "rollback_erreur": str(exc)}
        return {
            "rollback_performed": False,
            "escalade": True,
            "raison_escalade": "aucun rollback automatique possible pour cette action",
        }

    # -- Journalisation -----------------------------------------------------

    def _log(self, state: dict, verdict: dict, decision_dict: dict,
             action_executed: str | None, timing: dict) -> str:
        decision_id = log_decision(
            metrics_hypothesis=state.get("metrics_hypothesis", {}),
            logs_hypothesis=state.get("logs_hypothesis", {}),
            arbiter_verdict={**verdict, "timing": timing},
            guardrail_decision=decision_dict,
            action_executed=action_executed,
        )
        # Trace lisible du résultat. Sans elle, la partie la plus
        # intéressante du pipeline — le verdict et la décision du
        # garde-fou — n'apparaissait nulle part à l'écran : tout partait
        # directement dans le fichier JSONL. C'est aussi ce qu'on veut
        # pouvoir montrer pendant la démonstration.
        logger.info(
            f"VERDICT [{decision_id}] {verdict.get('diagnosis', '?')} "
            f"· composant={verdict.get('composant_suspecte', '?')} "
            f"· accord={verdict.get('agreement_status', '?')} "
            f"· confiance={verdict.get('final_confidence', '?')} "
            f"· garde-fou={decision_dict.get('decision', '?')} "
            f"· action={action_executed or 'aucune'} "
            f"· latence={timing.get('latence_diagnostic_s', '?')}s"
        )
        return decision_id


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    config = LoopConfig()

    docker_client = None
    if not config.dry_run:
        try:
            import docker
            docker_client = docker.from_env()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Docker indisponible ({exc}) — passage en dry_run")
            config.dry_run = True

    SupervisionLoop(config, docker_client).run()


if __name__ == "__main__":
    main()