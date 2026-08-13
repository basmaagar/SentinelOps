"""
Détecteur à confirmation — correctif Jour 12.

PROBLÈME OBSERVÉ au premier lancement en conditions réelles : sur un
système parfaitement au repos, sans aucune injection de panne, les agents
étaient sollicités toutes les 1 à 5 minutes. Diagnostic de la cause :

1. `MultivariateAnomalyDetector` utilise `contamination=0.05`. Ce paramètre
   ne décrit pas ce que le modèle observe : il lui IMPOSE de classer 5 %
   des échantillons comme aberrants, quoi qu'il arrive. À un tick de 5 s,
   cela fait mécaniquement ~36 fausses alertes par heure sur un système
   sain. Ce n'est pas un réglage mal choisi, c'est le fonctionnement
   nominal d'Isolation Forest en mode non supervisé.

2. Le z-score a aussi un régime de démarrage fragile (dès 5 échantillons),
   où l'écart-type estimé est très instable.

Conséquence directe : chaque faux positif déclenche un appel LLM de 90 à
150 secondes, ce qui saturait la boucle en pure perte.

CORRECTIF : exiger qu'une anomalie soit CONFIRMÉE sur plusieurs cycles
consécutifs avant d'être transmise aux agents. Une vraie panne (saturation
disque, fuite mémoire, latence) persiste par nature sur plusieurs dizaines
de secondes ; un faux positif d'Isolation Forest est ponctuel et isolé.

Le compromis est explicite et mesurable : on ajoute (N-1) × tick secondes
au TTD. Avec N=2 et tick=5s, cela coûte 5 secondes sur un objectif de 60 —
et supprime l'essentiel des appels LLM inutiles.

Ce wrapper respecte l'interface d'`AnomalyDetector` et s'injecte tel quel
dans `build_graph(metric_detector=...)`, sans modifier le détecteur d'origine
(dont les tests du Jour 3 restent valides).
"""

import logging

logger = logging.getLogger("sentinelops.supervision.confirming")


class ConfirmingAnomalyDetector:
    def __init__(self, inner, required_consecutive: int = 2,
                 warmup_samples: int = 30,
                 ignore_multivariate_alone: bool = True):
        """
        inner : instance d'AnomalyDetector (composition, pas héritage —
                on ne veut pas dépendre de ses détails internes).

        required_consecutive : nombre de cycles consécutifs pendant
                lesquels la même métrique doit être signalée.

        warmup_samples : pendant les N premiers échantillons, on alimente
                les fenêtres glissantes SANS jamais déclencher d'alerte.
                Sans cela, les toutes premières minutes après le démarrage
                produisent des alertes sur des statistiques calculées sur
                une poignée de points.

        ignore_multivariate_alone : un signalement d'Isolation Forest SEUL,
                sans aucun z-score anormal, est écarté. C'est précisément
                le cas produit par le paramètre `contamination`. Si une
                anomalie est réelle, au moins une métrique doit aussi
                s'écarter de sa propre distribution.
        """
        self.inner = inner
        self.required_consecutive = required_consecutive
        self.warmup_samples = warmup_samples
        self.ignore_multivariate_alone = ignore_multivariate_alone
        self._streaks: dict[str, int] = {}
        self._samples_seen = 0

    def process_sample(self, ts: float, metrics: dict[str, float]) -> list:
        self._samples_seen += 1
        events = self.inner.process_sample(ts, metrics)

        # Phase d'apprentissage : on nourrit les fenêtres, on ne conclut pas.
        if self._samples_seen <= self.warmup_samples:
            if self._samples_seen == self.warmup_samples:
                logger.info(
                    f"fin de la phase d'apprentissage ({self.warmup_samples} échantillons) "
                    "— détection active"
                )
            return [self._silence(e) for e in events]

        flagged = [e for e in events if e.is_anomaly]
        univariate_flagged = [e for e in flagged if e.metric != "__multivariate__"]

        # Isolation Forest isolé : écarté (cf. explication en tête de module).
        if self.ignore_multivariate_alone and flagged and not univariate_flagged:
            self._streaks.clear()
            return [self._silence(e) for e in events]

        current = {e.metric for e in univariate_flagged}
        for metric in list(self._streaks):
            if metric not in current:
                del self._streaks[metric]
        for metric in current:
            self._streaks[metric] = self._streaks.get(metric, 0) + 1

        confirmed = {m for m, n in self._streaks.items() if n >= self.required_consecutive}

        output = []
        for event in events:
            if event.is_anomaly and event.metric not in confirmed:
                output.append(self._silence(event))
            else:
                output.append(event)

        if confirmed:
            logger.info(f"anomalie confirmée sur {sorted(confirmed)} — agents sollicités")
        return output

    @staticmethod
    def _silence(event):
        """Neutralise le drapeau sans détruire l'événement (les valeurs et
        z-scores restent disponibles pour le journal et le dashboard)."""
        if event.is_anomaly:
            event.is_anomaly = False
        return event