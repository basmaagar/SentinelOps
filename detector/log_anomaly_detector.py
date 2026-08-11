"""
Détecteur d'anomalies sur logs — Jour 4.

Drain3 transforme les logs bruts en "templates" (ex: "DependencyTimeout:
call exceeded SLA latency_ms=<*>" au lieu d'une valeur numérique différente
à chaque ligne). On compte ensuite l'occurrence de chaque template par
fenêtre temporelle (bucket), et on réutilise le RollingZScoreDetector du
Jour 3 sur ce comptage : un template qui apparaît soudainement beaucoup
plus souvent que d'habitude est un signal d'anomalie.

Pourquoi réutiliser RollingZScoreDetector plutôt qu'écrire une nouvelle
logique ? -> Cohérence (une seule méthode statistique dans le projet,
déjà testée le Jour 3) et évite la duplication de code.

Limite connue (à documenter en Semaine 3) : seuls les buckets où un
template APPARAÎT sont envoyés au détecteur z-score ; les buckets à 0
occurrence ne sont pas comptés dans la fenêtre glissante. Cela biaise
légèrement la moyenne vers le haut pour les templates rares. Acceptable
pour un prototype, mais à mentionner explicitement dans le rapport.
"""

import sys
import pathlib
from collections import defaultdict
from dataclasses import dataclass

from drain3 import TemplateMiner

sys.path.append(str(pathlib.Path(__file__).parent))
from anomaly_detector import RollingZScoreDetector  # noqa: E402


@dataclass
class LogAnomalyEvent:
    ts: float
    template_id: int
    template: str
    count_in_bucket: int
    z_score: float | None
    severity: str
    reason: str  # "new_template" | "frequency_spike"


class LogAnomalyDetector:
    def __init__(self, bucket_seconds: float = 5.0, window_size: int = 20, z_threshold: float = 3.0):
        # TemplateMiner() sans config = mode mémoire, pas de persistance disque.
        self.miner = TemplateMiner()
        self.bucket_seconds = bucket_seconds
        self.zscore = RollingZScoreDetector(window_size=window_size, z_threshold=z_threshold)

        self._current_bucket_start: float | None = None
        self._current_bucket_counts: dict[int, int] = defaultdict(int)
        self._template_text: dict[int, str] = {}
        # Nécessaire en complément du z-score : un template totalement nouveau
        # qui apparaît à un rythme CONSTANT a un z-score de 0 (variance nulle),
        # donc ne serait jamais détecté par le z-score seul. Un incident
        # inédit (ex: un type d'erreur jamais vu) doit être signalé dès sa
        # première apparition, pas seulement s'il devient irrégulier.
        self._known_templates: set[int] = set()

    def _flush_bucket(self, bucket_ts: float) -> list[LogAnomalyEvent]:
        events = []
        for template_id, count in self._current_bucket_counts.items():
            is_new = template_id not in self._known_templates
            self._known_templates.add(template_id)

            if is_new:
                events.append(LogAnomalyEvent(
                    ts=bucket_ts,
                    template_id=template_id,
                    template=self._template_text.get(template_id, "?"),
                    count_in_bucket=count,
                    z_score=None,
                    severity="medium",
                    reason="new_template",
                ))
                continue  # pas de comparaison z-score le premier bucket : pas d'historique

            metric_name = f"log_template_{template_id}"
            zevent = self.zscore.update(metric_name, bucket_ts, count)
            if zevent.is_anomaly:
                events.append(LogAnomalyEvent(
                    ts=bucket_ts,
                    template_id=template_id,
                    template=self._template_text.get(template_id, "?"),
                    count_in_bucket=count,
                    z_score=zevent.z_score,
                    severity=zevent.severity,
                    reason="frequency_spike",
                ))
        self._current_bucket_counts = defaultdict(int)
        return events

    def process_log_line(self, ts: float, line: str) -> list[LogAnomalyEvent]:
        result = self.miner.add_log_message(line)
        template_id = result["cluster_id"]
        self._template_text[template_id] = result["template_mined"]

        if self._current_bucket_start is None:
            self._current_bucket_start = ts

        events: list[LogAnomalyEvent] = []
        while ts - self._current_bucket_start >= self.bucket_seconds:
            events.extend(self._flush_bucket(self._current_bucket_start))
            self._current_bucket_start += self.bucket_seconds

        self._current_bucket_counts[template_id] += 1
        return events