"""
Contexte métier des signaux supervisés — Jour 15.

Le problème que ce module résout
--------------------------------
Les agents recevaient une liste brute d'événements : un nom de métrique,
une valeur, un z-score. Rien d'autre. Un modèle de 1 à 1,5 milliard de
paramètres n'a alors aucun moyen de savoir ce que `latence_injectee_ms`
mesure, quelle est sa valeur normale, ni ce qui provoque typiquement un
écart. Faute de pouvoir interpréter, il paraphrase son entrée.

Le diagnostic observé en conditions réelles l'illustre exactement :

    « Le composant injectee semble être responsable de l'anomalie
      observée dans la latence. »

Deux échecs simultanés. Le modèle a découpé `latence_injectee_ms` et pris
le fragment « injectee » pour un nom de composant. Et sa phrase ne fait que
reformuler la question posée — elle n'apporte aucune information que le
détecteur n'avait déjà.

Ce n'est pas une faute du modèle : on lui demandait d'interpréter des
données sans lui fournir de quoi les interpréter.

Ce que ce module apporte
------------------------
Pour chaque signal : ce qu'il mesure, sa plage normale, son unité, les
causes habituelles d'un écart, et le composant auquel il se rattache. Le
contexte est injecté dans le prompt UNIQUEMENT pour les métriques
réellement en anomalie — un prompt court est un prompt rapide, et la
latence d'inférence est une contrainte du projet.

Ce catalogue est de la connaissance d'infrastructure, pas de
l'interprétation : il est écrit une fois, vérifiable, et ne dépend
d'aucun modèle.
"""

METRIC_CONTEXT: dict[str, dict] = {
    "disque_injecte_mb": {
        "mesure": "volume de données écrites dans le stockage de l'application",
        "unite": "Mo",
        "normal": "0 Mo en fonctionnement nominal",
        "composant": "target-app",
        "causes": [
            "accumulation de fichiers temporaires non purgés",
            "journaux applicatifs non tournés",
            "export ou dump volumineux laissé sur le disque",
        ],
        "consequence": "saturation du volume, puis échec des écritures",
    },
    "memoire_injectee_mb": {
        "mesure": "mémoire retenue par l'application au-delà de son usage nominal",
        "unite": "Mo",
        "normal": "0 Mo en fonctionnement nominal",
        "composant": "target-app",
        "causes": [
            "fuite mémoire — objets référencés sans être libérés",
            "cache applicatif sans limite de taille",
            "accumulation de connexions ou de sessions non fermées",
        ],
        "consequence": "pression mémoire croissante, puis arrêt du processus",
    },
    "latence_injectee_ms": {
        "mesure": "délai supplémentaire sur les appels vers la dépendance externe",
        "unite": "ms",
        "normal": "0 ms — la dépendance répond normalement en 20 à 50 ms",
        "composant": "dependency-service",
        "causes": [
            "dépendance externe saturée ou sous-dimensionnée",
            "contention réseau vers le service appelé",
            "dépassement de la capacité de traitement de la dépendance",
        ],
        "consequence": "dépassement du SLA, effet de file d'attente en amont",
    },
    "latence_p95_ms": {
        "mesure": "latence applicative au 95e centile",
        "unite": "ms",
        "normal": "moins de 100 ms",
        "composant": "dependency-service",
        "causes": [
            "lenteur d'une dépendance appelée par l'application",
            "contention sur une ressource partagée",
        ],
        "consequence": "dégradation perçue par les utilisateurs finaux",
    },
    "cpu_simule_pct": {
        "mesure": "charge processeur applicative",
        "unite": "%",
        "normal": "environ 5 % au repos",
        "composant": "target-app",
        "causes": [
            "traitement coûteux déclenché par une requête",
            "boucle de calcul non bornée",
            "pic de trafic entrant",
        ],
        "consequence": "allongement des temps de réponse, saturation du processeur",
    },
    "cpu_conteneur_pct": {
        "mesure": "charge processeur réelle du conteneur",
        "unite": "% d'un cœur",
        "normal": "quelques pourcents au repos",
        "composant": "target-app",
        "causes": ["charge applicative soutenue", "processus annexe consommateur"],
        "consequence": "étranglement du conteneur par sa limite de ressources",
    },
    "memoire_conteneur_mb": {
        "mesure": "mémoire résidente réelle du conteneur",
        "unite": "Mo",
        "normal": "stable en fonctionnement nominal",
        "composant": "target-app",
        "causes": ["fuite mémoire applicative", "cache du runtime en croissance"],
        "consequence": "arrêt du conteneur par dépassement de sa limite mémoire",
    },
}

# Indices lexicaux permettant de rattacher un template de journal à un
# composant. Les journaux n'ont pas de nom de métrique : c'est leur contenu
# qui renseigne, et l'agent doit savoir quoi y chercher.
LOG_CONTEXT: list[dict] = [
    {
        "motifs": ["dependencytimeout", "sla", "timeout", "dependency"],
        "signification": "un appel vers la dépendance externe a dépassé son délai",
        "composant": "dependency-service",
        "causes": ["dépendance saturée", "capacité de traitement insuffisante"],
    },
    {
        "motifs": ["disk", "disque", "volume", "storage"],
        "signification": "le stockage de l'application se remplit anormalement",
        "composant": "target-app",
        "causes": ["fichiers temporaires non purgés", "journaux non tournés"],
    },
    {
        "motifs": ["memory", "memoire", "heap", "oom"],
        "signification": "la consommation mémoire de l'application augmente",
        "composant": "target-app",
        "causes": ["fuite mémoire", "cache sans limite"],
    },
    {
        "motifs": ["cpu", "load", "charge"],
        "signification": "la charge processeur de l'application augmente",
        "composant": "target-app",
        "causes": ["traitement coûteux", "pic de trafic"],
    },
]

# Noms de composants valides. Les fournir explicitement évite que le modèle
# invente un nom à partir d'un fragment de métrique — c'est précisément
# l'erreur qui a produit le composant « injectee ».
COMPOSANTS_VALIDES = ["target-app", "dependency-service"]


def contexte_metriques(anomaly_events: list[dict]) -> str:
    """
    Fiche de contexte pour les seules métriques en anomalie.

    On n'injecte pas tout le catalogue : chaque token de prompt coûte du
    temps d'inférence, et la latence est une contrainte mesurée du projet.
    Seul ce qui concerne l'incident courant est transmis.
    """
    vus, lignes = set(), []
    for event in anomaly_events or []:
        nom = event.get("metric")
        if not nom or nom in vus or nom == "__multivariate__":
            continue
        vus.add(nom)
        info = METRIC_CONTEXT.get(nom)
        if not info:
            continue
        lignes.append(
            f"- {nom} ({info['unite']}) : {info['mesure']}.\n"
            f"  Valeur normale : {info['normal']}.\n"
            f"  Causes habituelles d'un écart : {' ; '.join(info['causes'])}.\n"
            f"  Conséquence si non traité : {info['consequence']}.\n"
            f"  Rattachée au composant : {info['composant']}."
        )
    if not lignes:
        return "Aucun contexte disponible pour ces métriques."
    return "\n".join(lignes)


def contexte_journaux(anomaly_events: list[dict]) -> str:
    """Fiche de contexte pour les types de messages effectivement présents."""
    blob = " ".join(str(e.get("template", "")) for e in (anomaly_events or [])).lower()
    lignes = []
    for entree in LOG_CONTEXT:
        if any(motif in blob for motif in entree["motifs"]):
            lignes.append(
                f"- Messages évoquant {', '.join(entree['motifs'][:3])} :\n"
                f"  Signification : {entree['signification']}.\n"
                f"  Causes habituelles : {' ; '.join(entree['causes'])}.\n"
                f"  Rattachés au composant : {entree['composant']}."
            )
    if not lignes:
        return ("Aucun motif connu reconnu dans ces messages. Décris ce que tu "
                "observes sans supposer une cause que les messages n'indiquent pas.")
    return "\n".join(lignes)