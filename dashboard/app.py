"""
Console d'exploitation SentinelOps — Jour 14.

Ce n'est pas un tableau de bord de contemplation : sa raison d'être est de
faire trancher la file d'attente de validation humaine. Tout le reste est
subordonné à cette tâche.

Pourquoi ce parti pris : la campagne du Jour 13 a produit 11 décisions en
attente de validation, qu'aucune interface ne permettait de traiter. La
boucle « humain dans la boucle » était donc écrite mais jamais fermée —
le système proposait, personne ne répondait, et rien n'était appris de ces
refus ou approbations.

L'écran répond à trois questions, dans cet ordre :
  1. Qu'attend-on de moi maintenant ?
  2. Pourquoi le système n'a-t-il pas tranché seul ?
  3. Puis-je lui faire confiance sur la durée ?

L'élément central est la CHAÎNE DE CONFIANCE : la décomposition visuelle
du calcul de confiance, montrant la valeur déclarée par le modèle érodée
par chaque vérification faite en code, jusqu'au score final, avec le seuil
d'exécution automatique marqué sur la même échelle. C'est la réponse
directe à « quelles preuves ont pesé » : on voit d'un coup d'œil que la
décision est remontée à l'humain parce qu'une preuve n'était pas ancrée,
et non parce qu'un score opaque était bas.

Lancement :  streamlit run dashboard/app.py
"""

import sys
import json
import time
import pathlib

import streamlit as st

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _sub in ("remediation", "guardrails", "agents", "evaluation"):
    sys.path.append(str(_ROOT / _sub))

sys.path.append(str(pathlib.Path(__file__).resolve().parent))

from human_validation import list_pending, resolve          # noqa: E402
from timeline import render_timeline, narrate               # noqa: E402
from confrontation import render_confrontation, CONFRONTATION_CSS  # noqa: E402
import analysis as ana                                      # noqa: E402
from actions import execute_action, ActionExecutionError    # noqa: E402

DECISIONS_PATH = _ROOT / "guardrails" / "decisions_log.jsonl"
RESULTS_PATH = _ROOT / "evaluation" / "evaluation_results.json"
GROUND_TRUTH_PATH = _ROOT / "injectors" / "ground_truth.jsonl"
PROMETHEUS_URL = "http://localhost:9090"
HUMAN_FEEDBACK_PATH = _ROOT / "remediation" / "human_feedback.jsonl"

st.set_page_config(page_title="SentinelOps — console", layout="wide",
                   initial_sidebar_state="collapsed")


# ---------------------------------------------------------------------------
# Système visuel
#
# Palette : indigo profond plutôt que noir pur — un panneau d'instruments
# vu de nuit. L'ambre est réservé à ce qui attend une décision humaine et
# n'est utilisé nulle part ailleurs, de sorte qu'une tache ambre à l'écran
# signifie toujours « on attend quelque chose de toi ». Le périwinkle
# désigne une donnée mesurée et vérifiée, le rose une dégradation.
# ---------------------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

:root{
  --ground:#0B0E1A; --panel:#141A2E; --rule:#2A3454;
  --ink:#E4E8F7; --dim:#7C87AD; --faint:#4E5878;
  --signal:#7C9CF5; --decide:#F0A93B; --degraded:#E0637F; --ok:#5FD3A6;
}
.stApp{background:var(--ground);}
.block-container{padding:1.6rem 2.4rem 4rem;max-width:1500px;}
#MainMenu,footer,header{visibility:hidden;}
html,body,[class*="css"]{font-family:'IBM Plex Sans',sans-serif;color:var(--ink);}

.so-top{display:flex;align-items:baseline;gap:14px;padding-bottom:16px;
  border-bottom:1px solid var(--rule);margin-bottom:8px;flex-wrap:wrap;}
.so-brand{font-family:'Space Grotesk';font-weight:700;font-size:19px;letter-spacing:-.02em;color:var(--ink);}
.so-brand span{color:var(--signal);}
.so-tag{font-family:'IBM Plex Mono';font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);border:1px solid var(--rule);border-radius:3px;padding:3px 9px;}
.so-tag.live{color:var(--ok);border-color:#1F4A3C;}
.so-dot{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--ok);margin-right:6px;}
.so-push{margin-left:auto;}

.so-sec{display:flex;align-items:baseline;gap:12px;margin:30px 0 12px;}
.so-sec h2{font-family:'Space Grotesk';font-size:12.5px;font-weight:600;letter-spacing:.16em;
  text-transform:uppercase;color:var(--dim);margin:0;}
.so-sec .n{font-family:'IBM Plex Mono';font-size:12px;color:var(--decide);}
.so-sec .ln{flex:1;height:1px;background:var(--rule);}

.so-card{background:var(--panel);border:1px solid var(--rule);border-left:2px solid var(--decide);
  border-radius:6px;padding:15px 19px 4px;margin-bottom:2px;}
.so-card.calm{border-left-color:var(--rule);}
.so-hrow{display:flex;align-items:flex-start;gap:16px;}
.so-act{font-family:'Space Grotesk';font-size:17px;font-weight:600;letter-spacing:-.01em;}
.so-act em{font-style:normal;color:var(--signal);}
.so-diag{color:var(--dim);font-size:13px;margin-top:3px;max-width:78ch;}
.so-when{font-family:'IBM Plex Mono';font-size:11px;color:var(--faint);white-space:nowrap;margin-left:auto;}

.so-clbl{font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--faint);margin:16px 0 8px;}
.so-track{position:relative;height:24px;background:#0D1226;border-radius:3px;
  border:1px solid #1A2140;margin-bottom:6px;}
.so-kept{position:absolute;top:0;bottom:0;left:0;border-radius:2px 0 0 2px;
  background:linear-gradient(90deg,#2C4590,#4668C9);}
.so-lost{position:absolute;top:0;bottom:0;
  background:repeating-linear-gradient(115deg,#232B4D,#232B4D 3px,#141A2E 3px,#141A2E 7px);}
.so-thr{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--decide);}
.so-endlbl{position:absolute;top:2px;font-family:'IBM Plex Mono';font-size:12px;font-weight:600;
  color:#CFE0FF;transform:translateX(-100%);padding-right:8px;}
.so-thrlbl{position:absolute;top:-17px;font-family:'IBM Plex Mono';font-size:9.5px;
  color:var(--decide);white-space:nowrap;padding-left:5px;}
.so-fx{font-family:'IBM Plex Mono';font-size:11px;color:var(--dim);margin-bottom:12px;}
.so-fx b{color:var(--ink);font-weight:500;}
.so-fx .cut{color:var(--degraded);}
.so-fx .op{color:var(--faint);padding:0 7px;}

.so-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;}
.so-chip{font-family:'IBM Plex Mono';font-size:11px;padding:3px 9px;border-radius:3px;
  background:#101731;border:1px solid var(--rule);color:var(--dim);}
.so-chip.anch{border-color:#2B4A7A;color:#A8C0F0;}
.so-chip.float{border-color:#4A2739;color:#D89AAB;}
.so-meta{font-family:'IBM Plex Mono';font-size:10.5px;color:var(--faint);margin-bottom:2px;}

/* boutons Streamlit */
.stButton>button{font-family:'Space Grotesk'!important;font-weight:600!important;font-size:13px!important;
  border-radius:4px!important;padding:.42rem 1.05rem!important;width:100%;transition:all .15s ease!important;}
.stButton>button[kind="primary"]{background:var(--decide)!important;border:1px solid var(--decide)!important;color:#1A1204!important;}
.stButton>button[kind="primary"]:hover{background:#FFC15C!important;border-color:#FFC15C!important;}
.stButton>button[kind="secondary"]{background:transparent!important;border:1px solid var(--rule)!important;color:var(--dim)!important;}
.stButton>button[kind="secondary"]:hover{border-color:var(--degraded)!important;color:var(--degraded)!important;}

.so-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--rule);
  border:1px solid var(--rule);border-radius:6px;overflow:hidden;}
.so-cell{background:var(--panel);padding:15px 18px;}
.so-k{font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);}
.so-v{font-family:'Space Grotesk';font-size:27px;font-weight:600;margin-top:5px;letter-spacing:-.02em;}
.so-d{font-family:'IBM Plex Mono';font-size:11px;margin-top:3px;}
.up{color:var(--ok);} .dn{color:var(--degraded);} .flat{color:var(--faint);}

.so-empty{border:1px dashed var(--rule);border-radius:6px;padding:34px;text-align:center;}
.so-empty .t{font-family:'Space Grotesk';font-size:16px;font-weight:600;color:var(--dim);}
.so-empty .s{font-size:13px;color:var(--faint);margin-top:5px;}

.so-row{display:flex;gap:14px;font-family:'IBM Plex Mono';font-size:11.5px;
  padding:9px 14px;border-bottom:1px solid #1C2340;align-items:center;}
.so-row:hover{background:#111730;}
/* Le pouls suit le rythme réel de la boucle (5 s). Une console de
   supervision doit donner un signe de vie : sans cela, « rien à signaler »
   et « processus figé » se ressemblent exactement. */
@keyframes so-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.82)}}
.so-dot{animation:so-pulse 5s ease-in-out infinite;}
@keyframes so-enter{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.so-card{animation:so-enter .32s ease-out;}
@media (prefers-reduced-motion:reduce){.so-dot,.so-card{animation:none;}}

.so-mk{cursor:pointer;}
.so-mk:hover line{stroke-opacity:1!important;stroke-width:2;}
.so-story{color:var(--dim);font-size:13.5px;margin:2px 0 13px;}
.so-story b{color:var(--ink);font-weight:500;}
.so-frame{background:var(--panel);border:1px solid var(--rule);border-radius:6px;
  padding:9px 14px 2px;}
.so-key{display:flex;gap:18px;margin-top:9px;font-family:'IBM Plex Mono';
  font-size:10px;color:var(--faint);flex-wrap:wrap;}

/* Onglets : trois questions distinctes, pas trois pages de widgets.
   Maintenant → qu'attend-on de moi. Le dossier → que s'est-il passé.
   Le système → puis-je lui faire confiance. */
.stTabs [data-baseweb="tab-list"]{gap:2px;border-bottom:1px solid var(--rule);}
.stTabs [data-baseweb="tab"]{font-family:'Space Grotesk'!important;font-size:13px!important;
  font-weight:600!important;color:var(--faint)!important;background:transparent!important;
  padding:8px 16px!important;}
.stTabs [aria-selected="true"]{color:var(--ink)!important;
  border-bottom:2px solid var(--signal)!important;}
.so-panel{background:var(--panel);border:1px solid var(--rule);border-radius:6px;
  padding:14px 17px;}
.so-ptitle{font-family:'Space Grotesk';font-size:14px;font-weight:600;color:var(--ink);}
.so-psub{font-size:12px;color:var(--dim);margin:3px 0 10px;line-height:1.45;}
.so-psub b{color:var(--ink);font-weight:500;}
.stSlider label{font-family:'IBM Plex Mono'!important;font-size:10.5px!important;
  letter-spacing:.1em;text-transform:uppercase;color:var(--faint)!important;}

.so-badge{font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;padding:2px 7px;
  border-radius:2px;border:1px solid;}
.b-auto{color:var(--ok);border-color:#1F4A3C;}
.b-human{color:var(--decide);border-color:#4A3A1A;}
.b-none{color:var(--faint);border-color:var(--rule);}
</style>
""" + "<style>" + CONFRONTATION_CSS + "</style>"
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Lecture des données
# ---------------------------------------------------------------------------
def html_block(fragment: str) -> str:
    """
    Prépare un fragment HTML pour st.markdown.

    Streamlit passe le contenu dans un moteur Markdown avant de l'insérer :
    toute ligne indentée de quatre espaces ou plus y est interprétée comme
    un bloc de code et affichée littéralement. Le HTML généré ici étant
    indenté pour rester lisible dans le source, une partie des cartes
    s'affichait telle quelle à l'écran, balises comprises.

    On retire donc l'indentation de chaque ligne et on recolle le tout sur
    une seule ligne : le HTML n'a pas besoin des sauts de ligne, et cela
    supprime toute ambiguïté avec la syntaxe Markdown.
    """
    return "".join(line.strip() for line in fragment.splitlines())


def read_jsonl(path: pathlib.Path) -> list[dict]:
    """Tolère les encodages mixtes (journaux antérieurs au correctif UTF-8)."""
    if not path.exists():
        return []
    out = []
    with path.open("rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                line = raw.decode("cp1252", errors="replace")
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def since(ts: float) -> str:
    d = max(0, time.time() - float(ts or 0))
    if d < 60:
        return f"il y a {int(d)} s"
    if d < 3600:
        return f"il y a {int(d // 60)} min"
    if d < 86400:
        return f"il y a {int(d // 3600)} h"
    return f"il y a {int(d // 86400)} j"


def extract_factors(verdict: dict) -> dict:
    """
    Extrait de quoi tracer la chaîne de confiance, quelle que soit la forme
    du breakdown (mono-modalité ou arbitrage complet).
    """
    br = verdict.get("confidence_breakdown") or {}
    final = float(verdict.get("final_confidence") or 0.0)

    if isinstance(br.get("facteurs"), dict):          # mono-modalité
        f = br["facteurs"]
        return {
            "mode": "modalité unique",
            "declaree": f.get("confiance_declaree", final),
            "termes": [("ancrage", f.get("ancrage_preuves"), False),
                       ("composant", f.get("validite_composant"), True)],
            "final": final,
            "detail_ancrage": f.get("detail_ancrage") or {},
        }

    m = br.get("agent_metriques") or {}
    l = br.get("agent_logs") or {}
    if m or l:
        declaree = max(float(m.get("confiance_declaree") or 0),
                       float(l.get("confiance_declaree") or 0))
        termes = []
        if m:
            termes.append(("métriques", m.get("confiance_effective"), False))
        if l:
            termes.append(("journaux", l.get("confiance_effective"), False))
        if br.get("disagreement_cap_applied"):
            termes.append(("désaccord → plafond", 0.40, True))
        detail = {}
        for bloc in (m, l):
            d = bloc.get("detail_ancrage") or {}
            detail["preuves_totales"] = detail.get("preuves_totales", 0) + int(d.get("preuves_totales", 0) or 0)
            detail["preuves_ancrees"] = detail.get("preuves_ancrees", 0) + int(d.get("preuves_ancrees", 0) or 0)
            detail.setdefault("non_ancrees", []).extend(d.get("non_ancrees", []) or [])
        return {"mode": "arbitrage", "declaree": declaree, "termes": termes,
                "final": final, "detail_ancrage": detail}

    return {"mode": "indisponible", "declaree": final, "termes": [],
            "final": final, "detail_ancrage": {}}


def confidence_chain_html(factors: dict, seuil: float) -> str:
    """
    L'élément signature : la confiance déclarée par le modèle, puis ce que
    les vérifications en code lui ont retiré, sur une échelle commune avec
    le seuil d'exécution automatique.

    Lecture : le segment plein est la confiance retenue, la zone hachurée
    est ce qui a été retiré, le trait ambre est le seuil qu'il fallait
    atteindre. Quand le hachuré recouvre le seuil, on voit immédiatement
    que la décision est remontée à cause d'une vérification échouée.
    """
    declaree = float(factors["declaree"] or 0)
    final = float(factors["final"] or 0)
    kept = max(0.0, min(1.0, final)) * 100
    lost_to = max(kept, min(1.0, declaree) * 100)
    thr = max(0.0, min(1.0, seuil)) * 100

    parts = [f'<span>déclarée <b>{declaree:.2f}</b></span>']
    for nom, valeur, penalisant in factors["termes"]:
        if valeur is None:
            continue
        cls = ' class="cut"' if penalisant and float(valeur) < 1.0 else ""
        parts.append(f'<span class="op">×</span><span{cls}>{nom} <b>{float(valeur):.2f}</b></span>')
    parts.append(f'<span class="op">→</span><span>retenue <b>{final:.2f}</b></span>')

    return f"""
    <div class="so-clbl">Chaîne de confiance · {factors['mode']}</div>
    <div class="so-track">
      <div class="so-lost" style="left:{kept:.1f}%;width:{max(0, lost_to - kept):.1f}%"></div>
      <div class="so-kept" style="width:{kept:.1f}%"></div>
      <div class="so-endlbl" style="left:{max(kept, 8):.1f}%">{final:.2f}</div>
      <div class="so-thr" style="left:{thr:.1f}%"></div>
      <div class="so-thrlbl" style="left:{thr:.1f}%">seuil {seuil:.2f}</div>
    </div>
    <div class="so-fx">{''.join(parts)}</div>
    """


def evidence_chips(verdict: dict, factors: dict) -> str:
    detail = factors.get("detail_ancrage") or {}
    total = int(detail.get("preuves_totales", 0) or 0)
    ancrees = int(detail.get("preuves_ancrees", 0) or 0)
    flottantes = [t for t in (detail.get("non_ancrees") or []) if t]

    chips = []
    for texte in flottantes[:3]:
        chips.append(f'<span class="so-chip float">✕ {texte[:70]}</span>')
    reste = max(0, ancrees)
    if reste:
        chips.append(f'<span class="so-chip anch">✓ {reste} preuve(s) retrouvée(s) dans les données</span>')
    if not chips and total == 0:
        chips.append('<span class="so-chip">aucune preuve citée</span>')
    return f'<div class="so-chips">{"".join(chips)}</div>'


def log_human_feedback(record: dict) -> None:
    """
    Journalise la décision humaine séparément, en append-only.

    Ces enregistrements sont la seule trace de ce qu'un opérateur pense du
    jugement du système. C'est aussi la matière première d'un recalibrage
    ultérieur des seuils : sans eux, le système répéterait indéfiniment les
    mêmes erreurs sans jamais rien en apprendre.
    """
    with HUMAN_FEEDBACK_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# En-tête
# ---------------------------------------------------------------------------
maintenant = time.strftime("%H:%M:%S")
decisions = read_jsonl(DECISIONS_PATH)
pending = sorted(list_pending(), key=lambda p: p.get("ts", 0), reverse=True)

derniere = max((d.get("ts", 0) for d in decisions), default=0)
boucle_active = (time.time() - derniere) < 300 if derniere else False
profil = "—"
for d in reversed(decisions):
    p = (d.get("guardrail_decision") or {}).get("profile")
    if p:
        profil = p
        break

st.markdown(html_block(f"""
<div class="so-top">
  <div class="so-brand">Sentinel<span>Ops</span></div>
  <div class="so-tag {'live' if boucle_active else ''}">
    {'<span class="so-dot"></span>boucle active · tick 5 s' if boucle_active
      else 'boucle inactive · dernier signe ' + (since(derniere) if derniere else 'jamais')}
  </div>
  <div class="so-tag">profil {profil}</div>
  <div class="so-tag so-push">{len(decisions)} décisions · rafraîchi {maintenant}</div>
</div>
"""), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Trois onglets, trois questions
#
# L'écran ne cherche pas à tout montrer en même temps. Chaque onglet répond
# à une question qu'un opérateur se pose à un moment distinct :
#   Maintenant  — qu'attend-on de moi ?
#   Le dossier  — que s'est-il réellement passé ?
#   Le système  — puis-je lui faire confiance sur la durée ?
# ---------------------------------------------------------------------------
incidents = read_jsonl(GROUND_TRUTH_PATH)
onglet_now, onglet_dossier, onglet_systeme = st.tabs(
    ["Maintenant", "Le dossier", "Le système"])


# ===========================================================================
# MAINTENANT — la frise, puis la file à trancher
# ===========================================================================
with onglet_now:
    st.markdown(html_block("""<div class="so-sec"><h2>La dernière heure</h2>
    <span class="ln"></span></div>"""), unsafe_allow_html=True)
    st.markdown(html_block(f'<div class="so-story">{narrate(decisions, incidents)}</div>'),
                unsafe_allow_html=True)

    try:
        frise = render_timeline(PROMETHEUS_URL, decisions, incidents)
        st.markdown(html_block(f'<div class="so-frame">{frise}</div>'), unsafe_allow_html=True)
        st.markdown(html_block("""<div class="so-key">
          <span><span style="color:var(--ok)">&#9679;</span> exécuté seul</span>
          <span><span style="color:var(--decide)">&#9670;</span> vous attend</span>
          <span><span style="color:var(--degraded)">&#10005;</span> refusé</span>
          <span><span style="background:#1B2340;padding:0 9px">&nbsp;</span> panne injectée</span>
          <span style="margin-left:auto">échelle verticale propre à chaque série —
            on compare des formes, pas des grandeurs</span></div>"""),
                    unsafe_allow_html=True)
    except Exception:  # noqa: BLE001
        st.markdown(html_block("""<div class="so-empty"><div class="t">Frise indisponible</div>
        <div class="s">Prometheus ne répond pas sur localhost:9090.</div></div>"""),
                    unsafe_allow_html=True)

    st.markdown(html_block(f"""<div class="so-sec"><h2>En attente de votre décision</h2>
    <span class="n">{len(pending)}</span><span class="ln"></span></div>"""),
                unsafe_allow_html=True)

    if not pending:
        st.markdown(html_block("""<div class="so-empty">
          <div class="t">Rien à trancher</div>
          <div class="s">Le système exécute seul ce qu'il juge sûr et remonte ici tout le reste.</div>
        </div>"""), unsafe_allow_html=True)
    else:
        par_id = {d.get("decision_id"): d for d in decisions}
        for item in pending:
            verdict = item.get("arbiter_verdict") or {}
            guardrail = item.get("guardrail_decision") or {}
            factors = extract_factors(verdict)
            seuil = float(guardrail.get("confidence_threshold") or 0.75)
            action_fr = {"restart_container": "Redémarrer",
                         "scale_replica": "Mettre à l'échelle"}.get(
                             item.get("action", ""), "Agir sur")
            raisons = ", ".join(guardrail.get("reasons", [])) or "seuil non atteint"

            st.markdown(html_block(f"""<div class="so-card">
              <div class="so-hrow"><div style="flex:1">
                <div class="so-act">{action_fr} <em>{item.get('target', '?')}</em></div>
                <div class="so-diag">{verdict.get('diagnosis', 'Diagnostic indisponible')}</div>
              </div><div class="so-when">{since(item.get('ts', 0))}</div></div>
              {confidence_chain_html(factors, seuil)}
              <div class="so-meta">{raisons} · {item.get('validation_id', '')[:8]}</div>
            </div>"""), unsafe_allow_html=True)

            # Le face-à-face est la pièce qui permet de DÉCIDER : il montre
            # ce que chaque enquêteur a vu et si ses preuves tiennent.
            source = par_id.get(item.get("decision_id")) or item
            st.markdown(html_block(render_confrontation(source)), unsafe_allow_html=True)

            c1, c2, _ = st.columns([1.3, 1, 5.2])
            vid = item["validation_id"]
            if c1.button(f"Approuver — {action_fr.lower()}", key=f"ok_{vid}", type="primary"):
                resolve(vid, approved=True)
                try:
                    import docker
                    execute_action(docker.from_env(), item["action"], item["target"])
                    statut, message = "executee", f"{action_fr} exécuté sur {item['target']}"
                except Exception as exc:  # noqa: BLE001
                    statut, message = "echec_execution", f"Approuvé, mais échec : {exc}"
                log_human_feedback({
                    "validation_id": vid, "decision_id": item.get("decision_id"),
                    "decision_humaine": "approuvee", "statut_execution": statut,
                    "action": item["action"], "target": item["target"],
                    "confiance_systeme": verdict.get("final_confidence"),
                    "seuil_applique": seuil, "ts": time.time()})
                (st.success if statut == "executee" else st.error)(message)
                time.sleep(1.2)
                st.rerun()

            if c2.button("Refuser", key=f"no_{vid}", type="secondary"):
                resolve(vid, approved=False)
                log_human_feedback({
                    "validation_id": vid, "decision_id": item.get("decision_id"),
                    "decision_humaine": "refusee", "statut_execution": "non_executee",
                    "action": item["action"], "target": item["target"],
                    "confiance_systeme": verdict.get("final_confidence"),
                    "seuil_applique": seuil, "ts": time.time()})
                st.info("Refus enregistré — aucune action exécutée.")
                time.sleep(1.0)
                st.rerun()
            st.write("")


# ===========================================================================
# LE DOSSIER — l'instruction complète d'un incident
# ===========================================================================
with onglet_dossier:
    st.markdown(html_block("""<div class="so-sec"><h2>Instruction d'un incident</h2>
    <span class="ln"></span></div>"""), unsafe_allow_html=True)

    recentes = sorted(decisions, key=lambda d: d.get("ts", 0), reverse=True)[:40]
    if not recentes:
        st.markdown(html_block("""<div class="so-empty"><div class="t">Journal vide</div>
        <div class="s">Démarrez la boucle : <code>python supervision/orchestrator.py</code></div>
        </div>"""), unsafe_allow_html=True)
    else:
        def etiquette(d):
            v = d.get("arbiter_verdict") or {}
            g = d.get("guardrail_decision") or {}
            c = v.get("final_confidence")
            return (f"{since(d.get('ts', 0)):>12} · {v.get('composant_suspecte', '—'):<20} "
                    f"· {f'{c:.2f}' if c is not None else '—'} · {g.get('decision', '—')}")

        choix = st.selectbox("Décision à instruire", recentes, format_func=etiquette,
                             label_visibility="collapsed")
        v = choix.get("arbiter_verdict") or {}
        g = choix.get("guardrail_decision") or {}

        st.markdown(html_block(f"""<div class="so-card calm">
          <div class="so-hrow"><div style="flex:1">
            <div class="so-act">{v.get('diagnosis', '—')}</div>
            <div class="so-diag">{v.get('justification', '')}</div>
          </div><div class="so-when">{since(choix.get('ts', 0))}</div></div>
          {confidence_chain_html(extract_factors(v),
                                 float(g.get('confidence_threshold') or 0.75))}
          <div class="so-meta">{g.get('decision', '—')} · profil {g.get('profile', '—')}
          · {choix.get('decision_id', '')[:8]}</div></div>"""), unsafe_allow_html=True)

        st.markdown(html_block(render_confrontation(choix)), unsafe_allow_html=True)

        colg, cold = st.columns(2)
        with colg:
            st.markdown(html_block(f"""<div class="so-panel">
              <div class="so-ptitle">Règles appliquées</div>
              <div class="so-psub">Ce que le garde-fou a vérifié, dans l'ordre.</div>
              <div style="font-family:'IBM Plex Mono';font-size:11.5px;color:var(--dim);
                line-height:1.9">
                action <b style="color:var(--ink)">{g.get('action', '—')}</b><br>
                risque intrinsèque <b style="color:var(--ink)">{g.get('intrinsic_risk', '—')}</b><br>
                seuil exigé <b style="color:var(--ink)">{g.get('confidence_threshold', '—')}</b><br>
                motifs <b style="color:var(--degraded)">{', '.join(g.get('reasons', [])) or 'aucun'}</b>
              </div></div>"""), unsafe_allow_html=True)
        with cold:
            po = choix.get("post_action_outcome") or {}
            if po:
                classe = po.get("classification", "—")
                couleur = "var(--ok)" if classe == "positive" else "var(--degraded)"
                contenu = (f'<div style="font-family:\'IBM Plex Mono\';font-size:11.5px;'
                           f'color:var(--dim);line-height:1.9">issue '
                           f'<b style="color:{couleur}">{classe}</b><br>'
                           f'{po.get("raison", "")}<br>rollback '
                           f'<b>{po.get("rollback_performed", False)}</b></div>')
            else:
                contenu = ('<div style="font-size:12px;color:var(--faint)">'
                           'Aucune vérification post-action : soit rien n\'a été exécuté, '
                           'soit la fenêtre d\'observation court encore.</div>')
            st.markdown(html_block(f"""<div class="so-panel">
              <div class="so-ptitle">Après l'action</div>
              <div class="so-psub">Comparaison à la ligne de base saine.</div>
              {contenu}</div>"""), unsafe_allow_html=True)

        with st.expander("Enregistrement brut"):
            st.json(choix, expanded=False)


# ===========================================================================
# LE SYSTÈME — peut-on lui faire confiance ?
# ===========================================================================
with onglet_systeme:
    st.markdown(html_block("""<div class="so-sec"><h2>Le système, jugé par ses propres critères</h2>
    <span class="ln"></span></div>"""), unsafe_allow_html=True)

    dataset = ana.build_dataset(decisions, incidents)

    if len(dataset) < 3:
        st.markdown(html_block("""<div class="so-empty">
          <div class="t">Pas assez de décisions appariées</div>
          <div class="s">Ces analyses croisent les décisions avec la vérité terrain.
          Lancez une campagne : <code>python evaluation/run_campaign.py</code></div></div>"""),
                    unsafe_allow_html=True)
    else:
        brier = ana.brier_score(dataset)
        gauche, droite = st.columns(2)

        with gauche:
            st.markdown(html_block(f"""<div class="so-ptitle">Calibration</div>
            <div class="so-psub">Quand il annonce 0.70, a-t-il raison 70 % du temps ?
            Un point sous la diagonale signale un système <b>trop sûr de lui</b> — et c'est
            cette situation qui déclenche des actions.<br>
            Score de Brier <b>{brier:.3f}</b> · 0.25 correspondrait à une absence totale
            d'information.</div>"""), unsafe_allow_html=True)
            st.markdown(html_block(f'<div class="so-panel">{ana.render_calibration(dataset)}</div>'),
                        unsafe_allow_html=True)

        with droite:
            seuil = st.slider("Seuil de confiance pour une action de risque modéré",
                              0.40, 1.00, 0.65, 0.05)
            r = ana.replay(dataset, max(0.0, seuil - 0.15), seuil)
            precision = f"{r['precision_auto']:.0%}" if r["precision_auto"] is not None else "—"
            st.markdown(html_block(f"""<div class="so-psub">
            Rejeu des <b>{r['total']} décisions</b> déjà prises, sous ce seuil. Aucune
            inférence n'est refaite : seule la frontière de décision se déplace, donc
            ce n'est pas une estimation mais ce qui <b>se serait produit</b>.<br>
            <b>{r['auto']} actions automatiques</b> dont {r['auto_faux']} erronées
            (précision {precision}) · {r['humain']} renvoyées à un humain, dont
            <b>{r['humain_correct']}</b> auraient pourtant été correctes.</div>"""),
                        unsafe_allow_html=True)
            st.markdown(html_block(
                f'<div class="so-panel">{ana.render_tradeoff(dataset, seuil)}</div>'),
                unsafe_allow_html=True)

        sc = ana.agent_scorecard(decisions, incidents)
        acc, des = sc["accord"], sc["desaccord"]
        taux = lambda b: f"{b['correct'] / b['total']:.0%}" if b["total"] else "—"
        st.write("")
        st.markdown(html_block(f"""<div class="so-strip">
          <div class="so-cell"><div class="so-k">Agent métriques</div>
            <div class="so-v">{sc['metriques']['interroge']}</div>
            <div class="so-d flat">fois interrogé</div></div>
          <div class="so-cell"><div class="so-k">Agent journaux</div>
            <div class="so-v">{sc['logs']['interroge']}</div>
            <div class="so-d flat">fois interrogé</div></div>
          <div class="so-cell"><div class="so-k">Quand ils sont d'accord</div>
            <div class="so-v" style="color:var(--ok)">{taux(acc)}</div>
            <div class="so-d flat">de diagnostics corrects · {acc['total']} cas</div></div>
          <div class="so-cell"><div class="so-k">Quand ils divergent</div>
            <div class="so-v" style="color:var(--degraded)">{taux(des)}</div>
            <div class="so-d flat">de diagnostics corrects · {des['total']} cas</div></div>
        </div>"""), unsafe_allow_html=True)
        st.caption("L'écart entre ces deux dernières colonnes chiffre ce que l'architecture "
                   "multi-agents apporte : si l'accord ne vaut pas mieux que le désaccord, "
                   "le cloisonnement n'apporte rien.")

    if RESULTS_PATH.exists():
        res = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        st.write("")
        st.markdown(html_block("""<div class="so-sec"><h2>Dernière campagne</h2>
        <span class="ln"></span></div>"""), unsafe_allow_html=True)
        prec, ttd = res.get("precision_diagnostic"), res.get("ttd_median_s")
        hall, sous60 = res.get("taux_hallucination"), res.get("ttd_sous_60s")
        ecart = (prec - 0.70) * 100 if prec is not None else 0
        st.markdown(html_block(f"""<div class="so-strip">
          <div class="so-cell"><div class="so-k">Cause racine correcte</div>
            <div class="so-v">{prec:.1%}</div>
            <div class="so-d {'up' if ecart >= 0 else 'dn'}">
              {'▲' if ecart >= 0 else '▼'} {abs(ecart):.1f} pts vs cible 70 %</div></div>
          <div class="so-cell"><div class="so-k">Diagnostic — médiane</div>
            <div class="so-v">{ttd:.1f} s</div>
            <div class="so-d flat">{sous60:.0%} des cas sous 60 s</div></div>
          <div class="so-cell"><div class="so-k">Preuves sans ancrage</div>
            <div class="so-v" style="color:var(--degraded)">{hall:.1%}</div>
            <div class="so-d dn">{res.get('preuves_citees', 0) - res.get('preuves_ancrees', 0)}
              des {res.get('preuves_citees', 0)} preuves citées</div></div>
          <div class="so-cell"><div class="so-k">Actions autonomes</div>
            <div class="so-v">{res.get('actions_executees', 0)}</div>
            <div class="so-d flat">{res.get('validations_humaines', 0)} renvoyées</div></div>
        </div>"""), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Rafraîchissement — explicite plutôt qu'automatique : une page qui se
# recharge pendant qu'on lit une décision fait cliquer à côté.
# ---------------------------------------------------------------------------
st.divider()
gauche, droite = st.columns([1, 6])
if gauche.button("Actualiser", type="secondary"):
    st.rerun()
droite.caption(f"Dernière lecture à {maintenant} · "
               f"la boucle écrit en continu dans decisions_log.jsonl")