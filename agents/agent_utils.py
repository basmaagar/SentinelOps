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
_PLACEHOLDER_MARKERS = ("<preuve", "<hypothèse", "<hypothese", "<composant",
                        "<diagnostic", "<justification")


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
    blob = json.dumps(payload, ensure_ascii=False).lower()
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