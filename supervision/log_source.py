"""
Source de logs pour la boucle de supervision — Jour 12.

L'Agent Logs recevait jusqu'ici des listes de lignes fournies à la main
dans les tests. Ce module lit réellement `app.log` au fil de l'eau.

Contrainte d'infrastructure : `app.log` est écrit À L'INTÉRIEUR du
conteneur `target-app`. Pour que la boucle (qui tourne sur l'hôte) puisse
le lire, il faut monter un volume — voir la note en bas de ce fichier.

Choix : lecture par position (offset) plutôt que `subprocess tail -f`.
Raison : pas de processus externe à surveiller, comportement identique
sur Windows et Linux (la machine de dev est sous Windows), et la position
est un état simple à raisonner et à tester.
"""

import re
import html
import logging
import pathlib

logger = logging.getLogger("sentinelops.supervision.logs")

# Les journaux applicatifs peuvent contenir des balises de colorisation
# (`<span style='color: red'>WARNING</span>`). Drain3 les conserve telles
# quelles dans ses templates, et l'Agent Journaux les cite ensuite comme
# preuves — ce qui produit des diagnostics illisibles et pollue la
# correspondance d'ancrage avec des mots de balisage.
#
# On nettoie donc à la SOURCE, avant toute extraction de motifs : c'est le
# seul endroit où la correction s'applique une fois pour toutes, quel que
# soit le consommateur en aval.
_BALISES = re.compile(r"<[^>]{1,200}>")
_ESPACES = re.compile(r"\s{2,}")


def nettoyer_ligne(ligne: str) -> str:
    """Retire le balisage HTML et normalise les espaces."""
    sans_balises = _BALISES.sub("", ligne)
    return _ESPACES.sub(" ", html.unescape(sans_balises)).strip()




class FileLogTailer:
    """
    Lit les nouvelles lignes ajoutées à un fichier depuis le dernier appel.

    Gère trois cas que la lecture naïve rate :
      - le fichier n'existe pas encore (app pas démarrée) ;
      - le fichier a été tronqué ou remplacé (rotation, redémarrage du
        conteneur) : la taille devient inférieure à la position mémorisée,
        on repart de zéro plutôt que de ne plus jamais rien lire ;
      - une ligne partiellement écrite au moment de la lecture : on ne
        consomme que jusqu'au dernier saut de ligne complet.
    """

    def __init__(self, path: str, from_start: bool = False):
        self.path = pathlib.Path(path)
        self._position = 0
        self._initialised = False
        self._from_start = from_start

    def _initialise(self) -> None:
        # Par défaut on démarre à la FIN du fichier : au lancement de la
        # boucle, on ne veut pas rejouer des heures de logs historiques
        # comme s'ils venaient d'arriver (ce qui déclencherait une rafale
        # de faux "new_template" et des appels LLM inutiles).
        if self._from_start or not self.path.exists():
            self._position = 0
        else:
            self._position = self.path.stat().st_size
        self._initialised = True

    def read_new_lines(self) -> list[str]:
        if not self.path.exists():
            return []

        if not self._initialised:
            self._initialise()

        size = self.path.stat().st_size
        if size < self._position:
            logger.info("fichier de logs tronqué ou remplacé — relecture depuis le début")
            self._position = 0
        if size == self._position:
            return []

        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._position)
            chunk = handle.read(size - self._position)

        # On ne consomme que les lignes complètes : le reliquat éventuel
        # sera relu au prochain tick, une fois terminé par l'application.
        last_newline = chunk.rfind("\n")
        if last_newline == -1:
            return []
        consumed = chunk[: last_newline + 1]
        self._position += len(consumed.encode("utf-8"))

        lignes = (nettoyer_ligne(line) for line in consumed.splitlines())
        return [line for line in lignes if line]


# ---------------------------------------------------------------------------
# NOTE D'INFRASTRUCTURE — modification requise dans docker-compose.yml
#
# Ajouter un volume sur le service target-app pour exposer app.log à l'hôte :
#
#   target-app:
#     build: ./app
#     volumes:
#       - ./logs:/app/logs
#
# et dans app/main.py, écrire dans ce dossier :
#
#   os.makedirs("logs", exist_ok=True)
#   _handler = logging.FileHandler("logs/app.log")
#
# La boucle lit alors ./logs/app.log côté hôte.
# ---------------------------------------------------------------------------