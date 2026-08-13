"""
Profils de politique de risque — Jour 12, préparation de la campagne.

Pourquoi ce module existe
-------------------------
Les seuils de la politique de production sont volontairement stricts. En
les appliquant tels quels pendant la campagne d'évaluation, un pan entier
du système ne serait jamais exercé : aucune action automatique ne se
déclenchant, l'exécution, le verrou anti-double-action, la vérification
post-action et le rollback resteraient sans mesure. On ne pourrait ni
chiffrer leur fiabilité, ni démontrer qu'ils fonctionnent.

Abaisser discrètement les seuils pour « faire marcher la démo » serait
malhonnête. Les déclarer dans un profil nommé, documenté et journalisé
avec chaque décision ne l'est pas : le lecteur du rapport sait exactement
sous quel régime chaque chiffre a été obtenu.

La campagne mesure donc les DEUX régimes sur les mêmes incidents :
  - `production` : ce que le système ferait sur une vraie infrastructure ;
  - `evaluation` : seuils abaissés, permettant d'exercer la boucle de
    remédiation complète et d'en mesurer le taux de réussite.

L'écart entre les deux est lui-même un résultat : il quantifie ce que
coûte la prudence, en actions correctes non exécutées.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskProfile:
    nom: str
    seuils_par_risque: dict[str, float]
    plafond_modalite_unique: float
    description: str


# Profil par défaut. Aucune action de risque modéré n'est exécutable sans
# corroboration par les deux modalités : le plafond mono-modalité (0.70)
# est inférieur au seuil exigé (0.80). C'est une contrainte voulue, et non
# un effet de bord — elle traduit l'idée que couper un service demande
# davantage qu'une seule source de preuve.
PRODUCTION = RiskProfile(
    nom="production",
    seuils_par_risque={"faible": 0.60, "modere": 0.80, "eleve": 1.01},
    plafond_modalite_unique=0.70,
    description=("Régime strict. Une action de risque modéré exige une "
                 "corroboration par les deux modalités indépendantes."),
)

# Profil d'évaluation. Les seuils sont abaissés d'environ 0.15 afin que le
# chemin mono-modalité — de loin le plus fréquent en pratique, les deux
# modalités coïncidant rarement au même cycle — puisse déclencher une
# action et donc exercer toute la chaîne de remédiation.
#
# Le plafond mono-modalité est inchangé : on abaisse l'exigence, on ne
# gonfle pas la confiance. La distinction est importante — le score reste
# calculé exactement de la même façon dans les deux profils, seule la
# décision qu'on en tire change.
EVALUATION = RiskProfile(
    nom="evaluation",
    seuils_par_risque={"faible": 0.45, "modere": 0.65, "eleve": 1.01},
    plafond_modalite_unique=0.70,
    description=("Régime d'évaluation, seuils abaissés pour exercer la boucle "
                 "de remédiation complète. Ne doit pas être utilisé en "
                 "production. Le calcul de confiance est identique à celui du "
                 "profil production ; seule la décision diffère."),
)

PROFILES: dict[str, RiskProfile] = {p.nom: p for p in (PRODUCTION, EVALUATION)}

# Le risque "eleve" reste à 1.01 dans les deux profils : un seuil
# supérieur à 1 est inatteignable par construction, donc une action jugée
# à risque élevé n'est JAMAIS automatique, quel que soit le profil. Cette
# garantie ne doit dépendre d'aucun réglage.