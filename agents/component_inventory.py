"""
Inventaire des composants supervisés — Jour 12.

Raison d'être : les agents nomment le composant en texte libre. Observé en
conditions réelles avec les petits modèles : "Application", "inconnu",
"target-app", "l'application cible"... Deux conséquences graves :

1. `select_action` faisait une correspondance exacte sur ce texte libre.
   Résultat : "aucune_action_candidate" sur TOUS les incidents réels, donc
   toute la partie remédiation du système ne s'exécutait jamais.

2. Rien ne vérifiait que le composant nommé EXISTE. Un modèle inventant un
   composant produisait un diagnostic d'apparence normale, et sa confiance
   déclarée passait telle quelle dans la décision du garde-fou.

Ce module est la référence unique : il sert à la fois à normaliser un nom
libre vers un identifiant canonique, et à valider qu'un composant existe.
Les deux usages doivent partager la même source, sinon ils divergeront.
"""

import re
import unicodedata

# Composants réellement supervisés dans le périmètre du prototype.
# Chaque entrée porte ses alias : formulations effectivement observées dans
# les sorties des modèles, plus les variantes prévisibles.
COMPONENT_INVENTORY: dict[str, dict] = {
    "target-app": {
        "libelle": "Application cible (conteneur FastAPI instrumenté)",
        "aliases": [
            "target-app", "targetapp", "target app", "sentinelops-target-app",
            "application", "application cible", "app", "webapp", "web app",
            "service applicatif", "conteneur applicatif", "fastapi",
            "container", "conteneur", "serveur applicatif",
            # Noms de RESSOURCES effectivement retournés par les modèles à
            # la place d'un composant ("stockage", "mémoire"...). Dans le
            # périmètre du prototype, une seule application porte ces
            # ressources, donc le rattachement est sans ambiguïté. Ce
            # raccourci devra être revu si plusieurs applications sont
            # supervisées — c'est noté comme limite.
            "stockage", "disque", "disk", "storage", "systeme de fichiers",
            "memoire", "mémoire", "memory", "ram", "tas", "heap",
            "cpu", "processeur", "charge cpu",
        ],
        # Métriques rattachées à ce composant. Sert à vérifier qu'une
        # hypothèse s'appuie bien sur des signaux qui le concernent, et à
        # déduire le composant en cause à partir de la métrique anormale.
        #
        # Note : `latence_injectee_ms` et `latence_p95_ms` ne figurent PAS
        # ici. Elles mesurent la lenteur d'un appel vers la dépendance
        # externe simulée, pas un problème de l'application elle-même —
        # elles appartiennent donc à `dependency-service`. Les laisser
        # dans les deux inventaires rendait ces métriques ambiguës, donc
        # inexploitables pour la déduction : une injection de latence ne
        # désignait aucun composant, et aucune action ne pouvait suivre.
        "metriques": [
            "cpu_simule_pct", "disque_injecte_mb", "memoire_injectee_mb",
            "cpu_conteneur_pct", "memoire_conteneur_mb",
        ],
        # Termes caractéristiques apparaissant dans les templates de logs
        # produits par ce composant. Permet d'attribuer une anomalie
        # détectée UNIQUEMENT dans les journaux, cas où aucune métrique
        # n'est disponible pour trancher.
        "indices_logs": ["disk", "disque", "memory", "memoire", "heap", "cpu"],
    },
    "dependency-service": {
        "libelle": "Dépendance externe simulée",
        "aliases": [
            "dependency-service", "dependency service", "dependance",
            "dépendance", "service externe", "dependance externe",
            "dépendance externe", "external dependency", "api externe",
            "downstream", "service tiers",
            "latence", "latency", "temps de reponse",
        ],
        "metriques": ["latence_injectee_ms", "latence_p95_ms",
                      "latence_dependance_ms", "appels_en_attente"],
        "indices_logs": ["dependency", "dependance", "latency", "latence",
                         "timeout", "sla", "slow"],
    },
}

# Valeur canonique signifiant "le modèle n'a pas su nommer de composant".
# Volontairement présente et explicite : c'est une information utile, pas
# une absence à masquer. Aucune action n'y est rattachée.
UNKNOWN_COMPONENT = "inconnu"

# Composant qui produit le fichier de journaux supervisé (logs/app.log).
#
# Dans le périmètre du prototype, une seule application écrit dans ce
# fichier : toute anomalie détectée dans les logs lui est donc imputable
# avec certitude. L'attribution repose sur la PROVENANCE de la donnée, ce
# qui est un fait vérifiable, et non sur l'interprétation de son contenu.
#
# Limite à documenter : dès que plusieurs composants produiraient des
# journaux, il faudrait une source de logs distincte par composant plutôt
# que cette constante.
LOG_SOURCE_COMPONENT = "target-app"


def _normalise_text(value: str) -> str:
    """Minuscules, sans accents, ponctuation réduite à des espaces."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    text = text.lower().strip()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# Index alias normalisé -> identifiant canonique, construit une seule fois.
_ALIAS_INDEX: dict[str, str] = {}
for _canonical, _entry in COMPONENT_INVENTORY.items():
    _ALIAS_INDEX[_normalise_text(_canonical)] = _canonical
    for _alias in _entry["aliases"]:
        _ALIAS_INDEX[_normalise_text(_alias)] = _canonical


def resolve_component(raw_name: str | None) -> str:
    """
    Ramène un nom libre à un identifiant canonique, ou UNKNOWN_COMPONENT.

    Trois niveaux, du plus strict au plus permissif :
      1. correspondance exacte sur un alias connu ;
      2. un alias apparaît comme mot entier dans le texte (couvre les
         formulations du type "le conteneur target-app est saturé") ;
      3. sinon, inconnu — on ne devine pas.

    On ne fait délibérément AUCUNE correspondance approximative par
    similarité de chaînes : rattacher à tort un composant inventé à une
    cible réelle autoriserait une action sur la mauvaise machine. En cas
    de doute, "inconnu" est la bonne réponse.
    """
    if not raw_name:
        return UNKNOWN_COMPONENT

    normalised = _normalise_text(raw_name)
    if not normalised:
        return UNKNOWN_COMPONENT

    if normalised in _ALIAS_INDEX:
        return _ALIAS_INDEX[normalised]

    # Recherche par mot entier, en privilégiant les alias les plus longs :
    # "target app" doit primer sur "app" si les deux apparaissent.
    for alias in sorted(_ALIAS_INDEX, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", normalised):
            return _ALIAS_INDEX[alias]

    return UNKNOWN_COMPONENT


def is_known_component(raw_name: str | None) -> bool:
    return resolve_component(raw_name) != UNKNOWN_COMPONENT


def known_metrics(canonical_component: str) -> list[str]:
    entry = COMPONENT_INVENTORY.get(canonical_component)
    return list(entry["metriques"]) if entry else []


def all_known_metrics() -> set[str]:
    metrics: set[str] = set()
    for entry in COMPONENT_INVENTORY.values():
        metrics.update(entry["metriques"])
    return metrics


# Index inverse métrique -> composant, construit depuis l'inventaire.
# Une métrique rattachée à plusieurs composants (la latence, par exemple)
# est volontairement exclue de cet index : elle ne permet pas de trancher
# à elle seule, et deviner serait pire que ne rien conclure.
_METRIC_OWNER: dict[str, str | None] = {}
for _canonical, _entry in COMPONENT_INVENTORY.items():
    for _metric in _entry["metriques"]:
        if _metric in _METRIC_OWNER and _METRIC_OWNER[_metric] != _canonical:
            _METRIC_OWNER[_metric] = None  # ambiguë
        else:
            _METRIC_OWNER.setdefault(_metric, _canonical)


def infer_component_from_events(anomaly_events: list[dict] | None,
                                default_owner: str | None = None) -> tuple[str, dict]:
    """
    Déduit le composant concerné à partir des métriques en anomalie.

    `default_owner` : composant à retenir lorsque les événements ne portent
    pas de nom de métrique rattachable — cas des anomalies de LOGS, qui
    portent un template et non une métrique. Voir LOG_SOURCE_COMPONENT.

    Pourquoi en code plutôt que par le LLM : le rattachement d'une métrique
    à un composant est un fait d'infrastructure, connu à l'avance et
    invariant. Il n'y a rien à interpréter. Le confier à un modèle de
    langage revient à transformer une donnée certaine en une prédiction
    incertaine.

    Constat qui a motivé ce choix : interrogés sur le composant suspect,
    les modèles répondaient par le nom de la RESSOURCE en cause
    ("stockage", "mémoire", "inconnu") et non par celui d'un composant
    supervisé. Ce n'est d'ailleurs pas une faute de leur part — la question
    était mal posée, puisque l'information ne se trouvait pas dans les
    données qu'on leur transmettait.

    Le composant déduit ici fait autorité. Celui proposé par le modèle est
    conservé, mais sert uniquement de recoupement : une divergence est un
    signal utile, pas une source de décision.
    """
    detail: dict = {"metriques_anormales": [], "source": "deduction_code"}
    if not anomaly_events:
        detail["raison"] = "aucun_evenement"
        return UNKNOWN_COMPONENT, detail

    owners: dict[str, int] = {}
    templates: list[str] = []
    for event in anomaly_events:
        metric = event.get("metric")
        if metric and metric != "__multivariate__":
            detail["metriques_anormales"].append(metric)
            owner = _METRIC_OWNER.get(metric)
            if owner:
                owners[owner] = owners.get(owner, 0) + 1
        elif event.get("template"):
            templates.append(str(event["template"]))

    if not owners and templates:
        # Anomalie détectée uniquement dans les journaux. On examine
        # d'abord le CONTENU des templates : un message mentionnant un
        # dépassement de SLA sur un appel de dépendance désigne un autre
        # composant qu'un message de saturation disque, même si les deux
        # sont écrits dans le même fichier.
        #
        # Correctif : sans cette étape, toute anomalie de logs était
        # attribuée au propriétaire du fichier, ce qui rendait
        # systématiquement fausse l'attribution des incidents de latence.
        blob = _normalise_text(" ".join(templates))
        indices: dict[str, int] = {}
        for canonical, entry in COMPONENT_INVENTORY.items():
            for hint in entry.get("indices_logs", []):
                if re.search(rf"\b{re.escape(_normalise_text(hint))}\b", blob):
                    indices[canonical] = indices.get(canonical, 0) + 1

        if len(indices) == 1:
            retenu = next(iter(indices))
            detail.update({"evenements_logs": len(templates), "retenu": retenu,
                           "indices_trouves": indices,
                           "origine_deduction": "indices_dans_les_templates"})
            return retenu, detail
        if len(indices) > 1:
            # Plusieurs composants évoqués : on prend le plus cité, en
            # traçant l'ambiguïté pour que l'audit puisse la constater.
            retenu = max(indices, key=indices.get)
            detail.update({"evenements_logs": len(templates), "retenu": retenu,
                           "indices_trouves": indices, "ambigu": True,
                           "origine_deduction": "indices_dans_les_templates"})
            return retenu, detail

    if not owners and templates and default_owner:
        # Anomalies de logs : un template ne se rattache pas à un composant
        # par son nom. En revanche, le FICHIER dont il provient, lui, a un
        # propriétaire connu — dans le périmètre du prototype, app.log est
        # produit par une seule application supervisée. L'attribution est
        # donc certaine, et vient de la provenance de la donnée, pas de son
        # contenu. (À revoir si plusieurs applications écrivaient dans des
        # journaux distincts : il faudrait alors une source par composant.)
        detail["evenements_logs"] = len(templates)
        detail["retenu"] = default_owner
        detail["origine_deduction"] = "proprietaire_du_fichier_de_logs"
        return default_owner, detail

    if not owners:
        detail["raison"] = "aucune_metrique_rattachable"
        return UNKNOWN_COMPONENT, detail

    # Composant le plus souvent mis en cause par les métriques anormales.
    best = max(owners, key=owners.get)
    detail["candidats"] = owners
    detail["retenu"] = best
    detail["origine_deduction"] = "metrique_en_anomalie"
    return best, detail