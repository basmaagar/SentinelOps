"""
Logique partagée entre tous les agents (Métriques, Logs, Arbitre).

Factorisée ici après le Jour 5 : dupliquer le mécanisme de retry/parsing
dans chaque agent créerait un risque de divergence silencieuse (ex: un
correctif de bug appliqué à un agent mais oublié dans l'autre).

Généralisée au Jour 8 (paramètre schema_cls/fallback) pour être réutilisée
par l'Agent Arbitre, dont le schéma de sortie (ArbiterVerdict) diffère de
celui des agents d'investigation (AgentHypothesis).
"""

import json
import unicodedata
import logging

from pydantic import BaseModel, ValidationError

from schemas import AgentHypothesis

FALLBACK_HYPOTHESIS = AgentHypothesis(
    hypothesis="Diagnostic indisponible : échec de l'analyse automatique",
    evidence=["Aucune preuve exploitable — voir logs système pour la cause de l'échec"],
    confidence=0.0,
    composant_suspecte="inconnu",
)


def extract_json(raw_text: str) -> dict:
    """Tolère les blocs ```json ... ``` et le texte parasite autour du JSON."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("Aucun objet JSON trouvé", text, 0)

    end = text.rfind("}")
    if end != -1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass  # JSON présent mais malformé -> on tente la réparation

    return _repair_truncated_json(text[start:])


def _repair_truncated_json(fragment: str) -> dict:
    """
    Tente de récupérer un objet JSON tronqué en cours de génération.

    Cas réel : l'Agent Arbitre atteignait son budget de tokens au milieu
    de son rapport d'incident. La réponse contenait quatre champs sur cinq,
    parfaitement valides, mais l'accolade fermante manquait — donc tout
    était rejeté, trois fois de suite, pour finir sur un fallback. Jeter
    une réponse presque complète parce qu'il manque un caractère de
    fermeture est un gaspillage pur.

    La réparation est volontairement conservatrice : on ferme la chaîne en
    cours si nécessaire, on retire une éventuelle paire clé/valeur
    incomplète, puis on ferme les structures ouvertes. On n'invente aucune
    valeur — un champ manquant restera manquant et sera signalé par la
    validation Pydantic, exactement comme avant.
    """
    text = fragment
    in_string = False
    escaped = False
    stack: list[str] = []
    last_safe = None  # position juste après la dernière valeur complète

    for i, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack:
                stack.pop()
        elif char == "," and len(stack) == 1:
            last_safe = i  # fin d'une paire clé/valeur de premier niveau

    candidate = text
    if in_string:
        # Chaîne coupée en plein milieu : on la ferme.
        candidate += '"'
    closing = "".join(reversed(stack))

    for attempt in (candidate + closing,
                    (text[:last_safe] + closing) if last_safe else None):
        if not attempt:
            continue
        try:
            parsed = json.loads(attempt)
            logging.getLogger("sentinelops.agents").warning(
                "réponse JSON tronquée réparée — envisager d'augmenter num_predict"
            )
            return parsed
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("JSON tronqué non réparable", text, 0)


# Marqueurs du gabarit présent dans les prompts. Les petits modèles
# (llama3.2:1b en particulier) recopient parfois l'exemple au lieu de le
# remplir : on obtient alors un JSON parfaitement valide contenant
# "<preuve 1 tirée des templates de logs>". C'est le pire cas possible —
# une hypothèse d'apparence structurée, sans aucun contenu réel — et il
# passerait sans encombre la validation Pydantic.
_PLACEHOLDER_MARKERS = (
    # Gabarits à chevrons de la première version des prompts.
    "<preuve", "<hypothèse", "<hypothese", "<composant",
    "<diagnostic", "<justification",
    # Exemples concrets de la seconde version. Les chevrons ayant été
    # retirés pour cesser d'inviter à la recopie, les petits modèles se
    # sont mis à recopier l'EXEMPLE lui-même — mot pour mot. Le problème
    # n'était donc pas la forme du gabarit mais le fait qu'un modèle
    # faible, faute de savoir répondre, restitue ce qu'il a sous les yeux.
    # Il faut donc reconnaître chaque formulation d'exemple introduite.
    "citation exacte d une metrique", "citation exacte d un template",
    "seconde citation", "phrase courte decrivant la cause",
    "nom du composant", "citation exacte",
    # Exemples d'hypothèses « correctes » de la troisième version des
    # prompts. Ils ont été retirés des consignes, mais le filet reste :
    # les modèles recopiaient ces phrases telles quelles, y compris quand
    # l'anomalie portait sur une tout autre métrique — produisant un
    # diagnostic parfaitement formulé et sans aucun rapport avec les
    # données observées.
    #
    # C'est le troisième gabarit successivement recopié : chevrons, puis
    # exemples de format, puis exemples d'hypothèses. La leçon est stable —
    # tout ce qu'on montre à un petit modèle est susceptible d'être
    # restitué, et seule une vérification en aval s'en protège.
    "la dependance externe sature et ne repond plus dans son delai",
    "des fichiers temporaires s accumulent et remplissent le volume",
    "les appels vers la dependance externe depassent leur delai",
    "l application signale un remplissage anormal de son volume",
)


def normalise_payload(payload: dict) -> dict:
    """
    Absorbe les variations de FORME produites par les petits modèles, sans
    jamais inventer de contenu.

    Constaté en conditions réelles avec llama3.2:1b : le champ `evidence`
    revient tantôt comme liste (attendu), tantôt comme dictionnaire
    ({"preuve_1": "...", "preuve_2": null}), tantôt comme simple chaîne.
    Ces trois formes portent la même information ; seule la structure
    diffère. Rejeter la réponse pour cette seule raison coûtait trois
    nouvelles générations d'environ 50 secondes chacune, pour finir sur un
    fallback — soit deux minutes et demie perdues par incident.

    On normalise donc la forme. On ne comble aucun manque : un champ
    absent reste absent et sera signalé par Pydantic, comme avant.
    """
    payload = dict(payload)

    # Réponse imbriquée d'un niveau.
    #
    # Constaté avec llama3.2:1b : le modèle place l'objet complet DANS le
    # champ `hypothesis`, au lieu d'y mettre une phrase :
    #     {"hypothesis": {"hypothesis": "...", "confidence": 0.5, ...}}
    #
    # Le contenu est correct, seule la profondeur est fausse. Rejeter la
    # réponse coûtait trois générations d'une quinzaine de secondes chacune
    # pour finir sur un repli, alors que l'information demandée était bien
    # présente. On remonte donc l'objet imbriqué d'un niveau.
    #
    # La condition est stricte : on ne remonte que si l'objet interne porte
    # au moins deux des champs attendus. Un dictionnaire quelconque trouvé
    # dans `hypothesis` n'est pas une réponse mal emboîtée, et sera traité
    # plus bas comme une valeur à sérialiser.
    _CHAMPS = {"hypothesis", "evidence", "confidence", "composant_suspecte",
               "diagnosis", "justification", "agreement_status"}
    for cle in ("hypothesis", "diagnosis"):
        interne = payload.get(cle)
        if isinstance(interne, dict) and len(_CHAMPS & set(interne)) >= 2:
            fusion = dict(interne)
            # Les champs du niveau supérieur, s'ils existent déjà, priment :
            # ils ont été produits explicitement, pas par emboîtement.
            for k, v in payload.items():
                if k != cle and v not in (None, "", [], {}):
                    fusion[k] = v
            payload = fusion
            break

    # Alias de clés. Les petits modèles produisent régulièrement une
    # variante du nom attendu ("component", "composant"...). Renommer une
    # clé n'invente aucune information, contrairement à en fabriquer une.
    _ALIASES = {
        "composant": "composant_suspecte",
        "component": "composant_suspecte",
        "suspected_component": "composant_suspecte",
        "composant_suspect": "composant_suspecte",
        "hypothese": "hypothesis",
        "hypothèse": "hypothesis",
        "preuves": "evidence",
        "confiance": "confidence",
    }
    for source, target in _ALIASES.items():
        if source in payload and target not in payload:
            payload[target] = payload.pop(source)

    # Champ `hypothesis` manquant alors que des preuves sont présentes.
    #
    # Constaté avec llama3.2:1b : la réponse contient `composant_suspecte`,
    # `evidence` et `confidence`, mais pas la phrase d'hypothèse. L'agent a
    # donc bien travaillé — ses preuves citent des données réelles — mais il
    # n'a pas formulé de cause.
    #
    # Rejeter la réponse coûtait trois générations d'une douzaine de
    # secondes pour finir sur un repli qui perd AUSSI les preuves. On
    # conserve donc ce qui a été produit, en signalant explicitement
    # l'absence de formulation plutôt qu'en inventant une phrase.
    #
    # La confiance déclarée est réduite de moitié : un agent qui énumère
    # des observations sans en tirer de cause n'a accompli que la moitié de
    # sa tâche, et son score doit le refléter. Ce n'est pas une pénalité
    # arbitraire, c'est la traduction d'une réponse incomplète.
    if not payload.get("hypothesis") and payload.get("evidence"):
        payload["hypothesis"] = ("Cause non formulée par l'agent — seules des "
                                 "observations ont été produites")
        confiance_partielle = payload.get("confidence")
        if isinstance(confiance_partielle, (int, float)):
            payload["confidence"] = float(confiance_partielle) * 0.5

    # Champ composant manquant : constaté systématiquement avec
    # llama3.2:1b, qui produit hypothesis + evidence + confidence mais
    # omet le composant. Trois tentatives échouaient sur cette seule
    # absence, pour finir sur un fallback qui perdait AUSSI l'hypothèse —
    # alors que celle-ci était exploitable.
    #
    # On renseigne donc "inconnu" plutôt que de tout jeter. Ce n'est pas
    # une invention : c'est l'aveu explicite que le modèle n'a pas nommé
    # de composant. Le garde-fou traite déjà ce cas — un composant inconnu
    # ne correspond à aucune action de la liste blanche, donc aucune action
    # automatique ne peut en découler.
    if "hypothesis" in payload and not payload.get("composant_suspecte"):
        payload["composant_suspecte"] = "inconnu"

    evidence = payload.get("evidence")

    if isinstance(evidence, dict):
        # Ordre des clés préservé (Python conserve l'ordre d'insertion,
        # donc preuve_1 avant preuve_2).
        evidence = list(evidence.values())
    elif isinstance(evidence, str):
        evidence = [evidence]

    if isinstance(evidence, list):
        cleaned = []
        for item in evidence:
            if item is None:
                continue  # "preuve_2": null -> pas une preuve
            if isinstance(item, (dict, list)):
                item = json.dumps(item, ensure_ascii=False)
            elif not isinstance(item, str):
                item = str(item)
            if item.strip():
                cleaned.append(item.strip())
        payload["evidence"] = cleaned

    # La confiance revient parfois en texte ("0.85") ou en pourcentage (85).
    confidence = payload.get("confidence")
    if isinstance(confidence, str):
        try:
            confidence = float(confidence.strip().rstrip("%"))
        except ValueError:
            confidence = None
    if isinstance(confidence, (int, float)) and confidence > 1.0:
        confidence = confidence / 100.0
    if confidence is not None:
        payload["confidence"] = confidence

    return payload


def contains_placeholder(payload: dict) -> bool:
    """
    Détecte une réponse qui recopie le gabarit du prompt au lieu de
    l'instancier. Une telle réponse est syntaxiquement valide mais
    sémantiquement vide : la laisser passer produirait un diagnostic
    fabriqué de toutes pièces, exactement ce que le projet cherche à
    éviter. On la traite donc comme une réponse invalide, ce qui déclenche
    une nouvelle tentative.
    """
    # Les accents sont retirés avant comparaison. Le modèle produit
    # « la dépendance externe sature », tandis que les marqueurs sont
    # écrits sans accents : une comparaison littérale échouerait
    # silencieusement sur le cas même qu'elle doit attraper.
    brut = json.dumps(payload, ensure_ascii=False).lower()
    blob = unicodedata.normalize("NFKD", brut).encode("ascii", "ignore").decode()
    return any(marker in blob for marker in _PLACEHOLDER_MARKERS)


def run_agent(llm_client, model: str, prompt: str, agent_name: str,
              schema_cls: type[BaseModel] = AgentHypothesis,
              fallback: BaseModel = FALLBACK_HYPOTHESIS,
              max_retries: int = 2,
              num_predict: int | None = None) -> BaseModel:
    """
    Exécute un agent avec retry borné (max_retries+1 tentatives au total),
    générique sur le schéma de sortie attendu.

    - JSON malformé / schema invalide -> on retente (erreur probablement
      liée au prompt/modèle, corrigible par une nouvelle génération).
    - Erreur réseau/timeout -> fallback immédiat, pas de retry (une
      indisponibilité réseau ne se corrige pas en réessayant le même appel).
    """
    logger = logging.getLogger(f"sentinelops.agents.{agent_name}")
    last_error: Exception | None = None
    attempt = 0

    for attempt in range(1, max_retries + 2):
        try:
            # Budget de sortie ajustable : un agent produisant plusieurs
            # champs rédigés (l'Arbitre) a besoin de plus qu'un agent
            # d'investigation. On reste compatible avec un client qui ne
            # connaîtrait pas ce paramètre (mocks des tests unitaires).
            try:
                raw_output = llm_client.generate(model=model, prompt=prompt,
                                                 num_predict=num_predict)
            except TypeError:
                raw_output = llm_client.generate(model=model, prompt=prompt)
            parsed = normalise_payload(extract_json(raw_output))
            if contains_placeholder(parsed):
                raise ValueError(
                    "le modèle a recopié le gabarit du prompt au lieu de le remplir"
                )
            result = schema_cls(**parsed)
            logger.info(f"succès à la tentative {attempt}")
            return result
        except (json.JSONDecodeError, ValidationError, KeyError, ValueError) as exc:
            last_error = exc
            logger.warning(f"tentative {attempt} invalide ({exc})")
        except Exception as exc:
            last_error = exc
            logger.error(f"erreur d'appel LLM à la tentative {attempt} ({exc})")
            break

    logger.error(f"échec définitif après {attempt} tentative(s), fallback appliqué. Dernière erreur: {last_error}")
    return fallback