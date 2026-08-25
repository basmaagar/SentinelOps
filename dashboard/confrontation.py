"""
Le face-à-face — les dépositions des deux enquêteurs, côte à côte.

Pourquoi cette vue existe
-------------------------
Un tableau de bord de supervision classique montre l'état de
l'infrastructure. Grafana le fait déjà, et mieux. Ce que SentinelOps a
d'unique n'est pas ce qu'il observe, c'est qu'il **porte des jugements, et
que ces jugements peuvent être faux**. L'objet à montrer n'est donc pas le
serveur : c'est le raisonnement.

Cette vue rend visible ce qui, jusqu'ici, n'existait que dans les journaux :

  - le CLOISONNEMENT, matérialisé par une séparation physique entre les
    deux colonnes. Chaque enquêteur n'a vu que sa modalité, et l'écran le
    montre au lieu de l'affirmer ;
  - la CONFRONTATION de chaque preuve à sa source. Une preuve citée est
    recherchée dans les données réellement transmises à l'agent, et
    affichée retrouvée ou introuvable. On voit littéralement
    l'hallucination, on ne lit pas un pourcentage qui la résume ;
  - la PONDÉRATION, dérivée de la force relative des signaux, affichée
    entre les deux colonnes.

C'est la réponse visuelle à la question posée depuis le début du projet :
l'architecture multi-agents est-elle justifiée ? Ici, l'accord ou le
désaccord de deux enquêteurs qui n'ont pas communiqué se lit d'un coup.
"""

import re
import html
import unicodedata

MIN_TOKEN = 4


def _norm(text) -> str:
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return t.lower()


def _tokens(text) -> set[str]:
    """
    Mêmes règles que `confidence_features._tokens` : les identifiants
    composés sont indexés entiers ET découpés, faute de quoi une preuve
    correcte formulée en langage naturel serait comptée comme inventée.

    La duplication est assumée : cette vue doit pouvoir être lue et
    vérifiée sans dépendre de la chaîne de décision, et une divergence
    éventuelle entre les deux implémentations serait elle-même un signal
    utile lors d'un audit.
    """
    out = set()
    for raw in re.findall(r"[a-z0-9_.]+", _norm(text)):
        if len(raw) >= MIN_TOKEN:
            out.add(raw)
        for part in re.split(r"[_.]+", raw):
            if len(part) >= MIN_TOKEN:
                out.add(part)
    return out


def _numbers(text) -> set[str]:
    found = set()
    for raw in re.findall(r"\d+(?:[.,]\d+)?", str(text)):
        try:
            found.add(f"{float(raw.replace(',', '.')):.2f}")
        except ValueError:
            continue
    return found


def check_evidence(evidence: list[str], observed) -> list[dict]:
    """
    Confronte chaque preuve aux données réellement reçues par l'agent, et
    retourne pour chacune les termes qui l'ancrent.

    Rendre visibles les termes de correspondance est le point important :
    dire « preuve non ancrée » demande de faire confiance à la
    vérification ; montrer *quels* mots ont été retrouvés permet de la
    contester. C'est la différence entre une note et une justification.
    """
    obs_text = str(observed)
    obs_tokens, obs_numbers = _tokens(obs_text), _numbers(obs_text)

    resultats = []
    for item in evidence or []:
        communs = sorted((_tokens(item) & obs_tokens) | (_numbers(item) & obs_numbers))
        resultats.append({
            "texte": str(item),
            "ancree": bool(communs),
            "correspondances": communs[:4],
        })
    return resultats


def _observed_summary(observed, modalite: str) -> list[str]:
    """Ce que l'agent a effectivement reçu, en une ligne par signal."""
    lignes = []
    for ev in (observed or [])[:5]:
        if not isinstance(ev, dict):
            lignes.append(str(ev)[:80])
            continue
        if modalite == "metriques":
            nom = ev.get("metric", "?")
            z = ev.get("z_score")
            val = ev.get("value")
            morceaux = [nom]
            if val is not None:
                morceaux.append(f"= {float(val):.1f}")
            if z is not None:
                morceaux.append(f"· z {float(z):.1f}")
            lignes.append(" ".join(morceaux))
        else:
            tpl = str(ev.get("template", "?"))[:64]
            n = ev.get("count_in_bucket")
            neuf = " · inédit" if ev.get("reason") == "new_template" else ""
            lignes.append(f"{tpl}{f' ×{n}' if n else ''}{neuf}")
    return lignes


# Marqueurs des hypothèses de repli — un agent dont l'appel au modèle a
# échoué. Ils doivent rester alignés avec ceux de l'arbitre.
_MARQUEURS_REPLI = ("indisponible", "échec de l'analyse", "echec de l'analyse",
                    "aucune preuve exploitable")


def _est_repli(hypothese: dict) -> bool:
    if float(hypothese.get("confidence") or 0.0) > 0.0:
        return False
    texte = " ".join([str(hypothese.get("hypothesis", "")),
                      " ".join(str(e) for e in (hypothese.get("evidence") or []))]).lower()
    return any(m in texte for m in _MARQUEURS_REPLI)


def _column(titre: str, hypothese: dict, observed, modalite: str,
            facteurs: dict | None, poids: float | None) -> str:
    conf_declaree = hypothese.get("confidence")
    verif = check_evidence(hypothese.get("evidence", []), observed)
    signaux = _observed_summary(observed, modalite)
    muet = not signaux

    # Un agent en repli n'a pas produit d'hypothèse : il n'a pas pu
    # travailler. L'afficher comme un diagnostic — fût-il à confiance nulle
    # — laisserait croire qu'il a analysé les données et conclu, alors
    # qu'il n'a rien vu du tout.
    if _est_repli(hypothese):
        return f"""<div class="fc-col fc-mute">
          <div class="fc-role">{titre}</div>
          <div class="fc-silent">Cet agent n'a pas répondu.<br>
          <span>Délai dépassé ou sortie inexploitable. Le diagnostic ne
          repose donc que sur l'autre modalité, et sa confiance est
          plafonnée en conséquence — ce n'est pas un désaccord.</span>
          </div></div>"""

    if muet:
        return f"""<div class="fc-col fc-mute">
          <div class="fc-role">{titre}</div>
          <div class="fc-silent">Aucun signal dans cette modalité.<br>
          <span>L'agent n'a pas été interrogé — pas d'appel au modèle,
          donc aucune hypothèse à confronter.</span></div></div>"""

    lignes_signal = "".join(
        f'<div class="fc-sig">{html.escape(s)}</div>' for s in signaux)

    lignes_preuve = ""
    for v in verif:
        if v["ancree"]:
            match = " ".join(f'<em>{html.escape(m)}</em>' for m in v["correspondances"])
            lignes_preuve += (f'<div class="fc-ev ok"><span class="fc-mark">✓</span>'
                              f'<div>{html.escape(v["texte"][:150])}'
                              f'<div class="fc-trace">retrouvé : {match}</div></div></div>')
        else:
            lignes_preuve += (f'<div class="fc-ev ko"><span class="fc-mark">✕</span>'
                              f'<div>{html.escape(v["texte"][:150])}'
                              f'<div class="fc-trace">aucun terme de cette preuve '
                              f'n\'apparaît dans les données reçues</div></div></div>')
    if not lignes_preuve:
        lignes_preuve = '<div class="fc-ev"><span class="fc-mark">—</span><div>Aucune preuve citée.</div></div>'

    bandeau_poids = ""
    if poids is not None:
        bandeau_poids = (f'<div class="fc-weight"><div class="fc-wbar" '
                         f'style="width:{poids * 100:.0f}%"></div>'
                         f'<span>poids {poids:.0%}</span></div>')

    decote = ""
    if facteurs:
        eff = facteurs.get("confiance_effective")
        anc = facteurs.get("ancrage_preuves")
        if eff is not None and conf_declaree is not None:
            decote = (f'<div class="fc-decote">annonçait <b>{float(conf_declaree):.2f}</b>'
                      f' · retenu <b>{float(eff):.2f}</b>'
                      f'{f" (ancrage {float(anc):.2f})" if anc is not None else ""}</div>')

    return f"""<div class="fc-col">
      <div class="fc-role">{titre}</div>
      <div class="fc-claim">{html.escape(str(hypothese.get('hypothesis', '—')))}</div>
      {decote}
      <div class="fc-lbl">Ce qu'il a reçu</div>{lignes_signal}
      <div class="fc-lbl">Ce qu'il en tire</div>{lignes_preuve}
      {bandeau_poids}
    </div>"""


def render_confrontation(decision: dict) -> str:
    """Assemble les deux colonnes et le verdict central."""
    verdict = decision.get("arbiter_verdict") or {}
    br = verdict.get("confidence_breakdown") or {}

    m_hyp = decision.get("metrics_hypothesis") or {}
    l_hyp = decision.get("logs_hypothesis") or {}
    m_obs = decision.get("metrics_observed") or []
    l_obs = decision.get("logs_observed") or []

    poids = br.get("poids") or {}
    p_m, p_l = poids.get("metriques"), poids.get("logs")
    f_m, f_l = br.get("agent_metriques"), br.get("agent_logs")

    if br.get("source", "").startswith("modalite_unique"):
        # Adoption directe : une seule modalité avait des preuves, donc
        # aucun arbitrage n'a eu lieu. Le signaler explicitement évite de
        # laisser croire à une corroboration qui n'a pas existé.
        #
        # Le test porte sur la présence de « logs », non de « metriques » :
        # le libellé de source est accentué (« modalite_unique_métriques »),
        # et une comparaison naïve sur une chaîne sans accent échouait
        # silencieusement, attribuant un poids nul à la seule modalité qui
        # portait la décision.
        est_logs = "log" in _norm(br["source"])
        if est_logs:
            f_l, p_m, p_l = br.get("facteurs"), 0.0, 1.0
        else:
            f_m, p_m, p_l = br.get("facteurs"), 1.0, 0.0

    statut = verdict.get("agreement_status", "—")
    couleur = {"accord": "ok", "desaccord": "ko", "complementaire": "mid"}.get(statut, "mid")
    conf = verdict.get("final_confidence")

    note = {
        "accord": "Deux enquêteurs cloisonnés ont convergé.",
        "desaccord": "Divergence : au moins l'un des deux se trompe.",
        "complementaire": "Une seule modalité portait des preuves.",
    }.get(statut, "")

    return f"""<div class="fc">
      {_column("Agent Métriques", m_hyp, m_obs, "metriques", f_m, p_m)}
      <div class="fc-mid">
        <div class="fc-gap"></div>
        <div class="fc-verdict {couleur}">
          <div class="fc-vstat">{html.escape(statut)}</div>
          <div class="fc-vconf">{f'{float(conf):.2f}' if conf is not None else '—'}</div>
          <div class="fc-vnote">{note}</div>
        </div>
        <div class="fc-gap"></div>
      </div>
      {_column("Agent Journaux", l_hyp, l_obs, "logs", f_l, p_l)}
    </div>"""


# Styles propres à cette vue. Le fossé central n'est pas décoratif : c'est
# la représentation du cloisonnement, et il doit rester visible même quand
# les deux colonnes disent la même chose.
CONFRONTATION_CSS = """
.fc{display:grid;grid-template-columns:1fr 150px 1fr;gap:0;align-items:stretch;
  background:#101731;border:1px solid var(--rule);border-radius:6px;overflow:hidden;}
.fc-col{padding:15px 18px;min-width:0;}
.fc-col:last-child{border-left:1px solid var(--rule);}
.fc-col:first-child{border-right:1px solid var(--rule);}
.fc-mute{opacity:.55;}
.fc-silent{font-size:13px;color:var(--dim);line-height:1.5;margin-top:10px;}
.fc-silent span{font-size:11.5px;color:var(--faint);}
.fc-role{font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--faint);margin-bottom:7px;}
.fc-claim{font-family:'Space Grotesk';font-size:14.5px;font-weight:600;
  color:var(--ink);line-height:1.35;}
.fc-decote{font-family:'IBM Plex Mono';font-size:10.5px;color:var(--dim);margin-top:5px;}
.fc-decote b{color:var(--ink);}
.fc-lbl{font-family:'IBM Plex Mono';font-size:9px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint);margin:14px 0 6px;}
.fc-sig{font-family:'IBM Plex Mono';font-size:11px;color:#A8C0F0;background:#0D1226;
  border-left:2px solid var(--signal);padding:3px 8px;margin-bottom:3px;border-radius:0 2px 2px 0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.fc-ev{display:flex;gap:8px;font-size:12px;color:var(--dim);margin-bottom:7px;line-height:1.4;}
.fc-mark{font-family:'IBM Plex Mono';flex-shrink:0;}
.fc-ev.ok .fc-mark{color:var(--signal);}
.fc-ev.ko .fc-mark{color:var(--degraded);}
.fc-ev.ko>div{color:#B0839A;}
.fc-trace{font-family:'IBM Plex Mono';font-size:9.5px;color:var(--faint);margin-top:2px;}
.fc-trace em{font-style:normal;color:var(--signal);background:#16255C;
  padding:0 4px;border-radius:2px;}
.fc-weight{position:relative;height:14px;background:#0D1226;border-radius:2px;
  margin-top:14px;font-family:'IBM Plex Mono';font-size:9px;}
.fc-wbar{position:absolute;top:0;bottom:0;left:0;background:#2C4590;border-radius:2px;}
.fc-weight span{position:absolute;right:6px;top:2px;color:var(--dim);}

.fc-mid{display:flex;flex-direction:column;align-items:center;justify-content:center;
  background:#0B0E1A;}
.fc-gap{flex:1;width:1px;background:repeating-linear-gradient(180deg,
  var(--rule),var(--rule) 3px,transparent 3px,transparent 7px);}
.fc-verdict{text-align:center;padding:11px 8px;width:100%;}
.fc-vstat{font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.12em;
  text-transform:uppercase;}
.fc-verdict.ok .fc-vstat{color:var(--ok);}
.fc-verdict.ko .fc-vstat{color:var(--degraded);}
.fc-verdict.mid .fc-vstat{color:var(--dim);}
.fc-vconf{font-family:'Space Grotesk';font-size:25px;font-weight:600;color:var(--ink);
  margin:3px 0;letter-spacing:-.02em;}
.fc-vnote{font-size:10px;color:var(--faint);line-height:1.35;}
"""