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
PENDING_PATH = _ROOT / "remediation" / "pending_validations.jsonl"
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
    # Les lignes sont recollées avec une ESPACE et non bout à bout : un
    # texte réparti sur plusieurs lignes du source verrait sinon ses mots
    # se souder (« délaidépassé »). En HTML, une espace surnuméraire entre
    # deux balises ou dans une valeur d'attribut est sans effet, alors
    # qu'une espace manquante dans du texte est visible immédiatement.
    return " ".join(line.strip() for line in fragment.splitlines() if line.strip())


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


# Marqueurs des réponses de repli. Quand un appel au modèle échoue
# (timeout, service injoignable, sortie inexploitable après retries), les
# agents renvoient une hypothèse de repli déterministe plutôt que de faire
# planter la chaîne. C'est le bon comportement — mais le résultat n'est PAS
# un diagnostic, et il ne doit pas être présenté comme tel.
_MARQUEURS_ECHEC = ("indisponible", "échec de l'analyse", "echec de l'analyse")


def analyse_en_echec(item: dict) -> bool:
    """
    Distingue une décision réellement diagnostiquée d'une analyse qui a
    échoué faute de réponse du modèle.

    Pourquoi c'est indispensable dans l'interface : proposer « approuver le
    redémarrage » sous un diagnostic vide demande à l'opérateur d'agir sur
    une machine réelle sans la moindre information. C'est exactement la
    situation où le système doit reconnaître qu'il n'a rien à dire, plutôt
    que d'habiller un échec en décision.

    Deux signaux concordants sont exigés : une confiance nulle ET un
    diagnostic portant un marqueur de repli. Une confiance nulle seule peut
    résulter d'une décote légitime sur une hypothèse réelle mais mal
    étayée — cas différent, qui mérite d'être montré.
    """
    verdict = item.get("arbiter_verdict") or {}
    confiance = verdict.get("final_confidence")
    if confiance is None or float(confiance) > 0.0:
        return False
    texte = str(verdict.get("diagnosis", "")).lower()
    return any(marqueur in texte for marqueur in _MARQUEURS_ECHEC)


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
_tous_en_attente = sorted(list_pending(), key=lambda p: p.get("ts", 0), reverse=True)
# Les analyses en échec sont écartées de la file actionnable : elles ne
# portent aucun diagnostic, donc rien à approuver. Elles restent visibles
# dans une section distincte, car les masquer complètement reviendrait à
# dissimuler une panne du système de supervision lui-même.
pending = [p for p in _tous_en_attente if not analyse_en_echec(p)]
echecs = [p for p in _tous_en_attente if analyse_en_echec(p)]

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
  <div class="so-tag so-push">{len(decisions)} décisions · {maintenant}</div>
</div>
"""), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Veille automatique
#
# Une console de supervision qu'il faut recharger à la main n'est pas une
# console : on ne peut pas savoir si rien ne se passe ou si l'écran est
# figé sur un état ancien.
#
# Le mécanisme ne recharge PAS la page toutes les 30 secondes. Il compare
# une empreinte des fichiers de données (taille + date de modification) et
# ne déclenche un rechargement que si quelque chose a réellement changé.
# La différence est importante en usage : recharger pendant qu'un opérateur
# lit une décision réinitialise sa position et lui fait perdre le fil, pour
# rien la plupart du temps.
#
# `st.fragment(run_every=...)` n'existe qu'à partir de Streamlit 1.37 ;
# on retombe silencieusement sur le bouton manuel si la version est plus
# ancienne, plutôt que de faire échouer toute la page.
# ---------------------------------------------------------------------------
INTERVALLE_VEILLE_SECONDES = 30


def empreinte_donnees() -> tuple:
    """Signature bon marché des sources : évite de relire les fichiers."""
    signature = []
    for chemin in (DECISIONS_PATH, PENDING_PATH, GROUND_TRUTH_PATH):
        try:
            st_ = chemin.stat()
            signature.append((chemin.name, st_.st_size, int(st_.st_mtime)))
        except OSError:
            signature.append((chemin.name, -1, -1))
    return tuple(signature)


if hasattr(st, "fragment"):
    @st.fragment(run_every=INTERVALLE_VEILLE_SECONDES)
    def _veille():
        actuelle = empreinte_donnees()
        precedente = st.session_state.get("empreinte")
        if precedente is None:
            st.session_state["empreinte"] = actuelle
            return
        if actuelle != precedente:
            st.session_state["empreinte"] = actuelle
            st.session_state["nouveaute"] = True
            st.rerun(scope="app")

    _veille()
    VEILLE_ACTIVE = True
else:
    VEILLE_ACTIVE = False


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
# L'onglet « Le système » (calibration, rejeu contrefactuel) est
# temporairement retiré : ses analyses croisent les décisions avec la vérité
# terrain, et n'ont de sens qu'une fois une campagne complète effectuée sur
# une chaîne pleinement fonctionnelle. Le réintroduire avec des données
# partielles afficherait des courbes vides ou trompeuses.
onglet_now, onglet_dossier = st.tabs(["Maintenant", "Le dossier"])


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
        # Pagination.
        #
        # Chaque décision dépliée occupe une hauteur d'écran entière : le
        # face-à-face montre deux colonnes de preuves, et c'est justement ce
        # qui permet de trancher. Empiler onze décisions en attente produit
        # une page de plusieurs mètres où l'on ne trouve plus rien.
        #
        # On en affiche donc trois à la fois — assez pour comparer des cas
        # voisins, assez peu pour rester lisible — avec une navigation
        # explicite. Les plus récentes d'abord : une panne en cours importe
        # davantage qu'une décision vieille d'une heure.
        PAR_PAGE = 3
        pages = max(1, -(-len(pending) // PAR_PAGE))
        page = min(st.session_state.get("page_attente", 0), pages - 1)

        if pages > 1:
            nav_g, nav_c, nav_d, _ = st.columns([1, 1, 1, 4])
            if nav_g.button("← Précédentes", disabled=(page == 0),
                            key="prev_att", type="secondary"):
                st.session_state["page_attente"] = page - 1
                st.rerun()
            nav_c.markdown(
                html_block(f'<div style="text-align:center;font-family:\'IBM Plex Mono\';'
                           f'font-size:11px;color:var(--dim);padding-top:9px">'
                           f'{page * PAR_PAGE + 1}–{min((page + 1) * PAR_PAGE, len(pending))}'
                           f' sur {len(pending)}</div>'), unsafe_allow_html=True)
            if nav_d.button("Suivantes →", disabled=(page >= pages - 1),
                            key="next_att", type="secondary"):
                st.session_state["page_attente"] = page + 1
                st.rerun()

        par_id = {d.get("decision_id"): d for d in decisions}
        for item in pending[page * PAR_PAGE:(page + 1) * PAR_PAGE]:
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

    # -----------------------------------------------------------------------
    # Analyses en échec
    #
    # Ces incidents ont bien été détectés, mais aucun diagnostic n'a pu être
    # produit : le modèle n'a pas répondu dans le délai imparti, ou sa
    # sortie est restée inexploitable après plusieurs tentatives. Le repli
    # déterministe a joué son rôle — le système n'a rien inventé et n'a rien
    # exécuté.
    #
    # On les présente à part, sans bouton d'action : il n'y a rien à
    # approuver. Les afficher comme des décisions ordinaires demanderait à
    # l'opérateur d'agir à l'aveugle ; les masquer dissimulerait une panne
    # de la supervision elle-même.
    # -----------------------------------------------------------------------
    if echecs:
        st.markdown(html_block(f"""<div class="so-sec">
        <h2>Analyses n'ayant pas abouti</h2><span class="n">{len(echecs)}</span>
        <span class="ln"></span></div>"""), unsafe_allow_html=True)

        st.markdown(html_block("""<div class="so-story">
        Une anomalie a été détectée, mais le modèle n'a pas répondu — délai
        dépassé ou sortie inexploitable. <b>Aucune action n'a été exécutée
        et aucun diagnostic n'est proposé</b> : le repli déterministe a
        fonctionné comme prévu. Vérifiez qu'Ollama répond, puis écartez ces
        entrées.</div>"""), unsafe_allow_html=True)

        lignes = []
        for e in echecs[:6]:
            v = e.get("arbiter_verdict") or {}
            lignes.append(f"""<div class="so-row">
              <span class="so-badge b-none">non conclu</span>
              <span style="color:var(--faint);width:86px">{since(e.get('ts', 0))}</span>
              <span style="color:var(--dim);width:150px">{e.get('target', '—')}</span>
              <span style="color:var(--faint);flex:1;overflow:hidden;
                text-overflow:ellipsis;white-space:nowrap">
                {v.get('diagnosis', 'aucun diagnostic')}</span>
              <span style="color:var(--faint)">{e.get('validation_id', '')[:8]}</span>
            </div>""")
        st.markdown(html_block(f'<div style="border:1px solid var(--rule);border-radius:6px;'
                               f'overflow:hidden;background:var(--panel);opacity:.72">'
                               f'{"".join(lignes)}</div>'), unsafe_allow_html=True)

        col_e, _ = st.columns([1.4, 5])
        if col_e.button(f"Écarter {len(echecs)} entrée{'s' if len(echecs) > 1 else ''}", key="purge_echecs",
                        type="secondary"):
            for e in echecs:
                resolve(e["validation_id"], approved=False)
                log_human_feedback({
                    "validation_id": e["validation_id"],
                    "decision_id": e.get("decision_id"),
                    "decision_humaine": "ecartee_analyse_en_echec",
                    "statut_execution": "non_executee",
                    "action": e.get("action"), "target": e.get("target"),
                    "confiance_systeme": 0.0, "seuil_applique": None,
                    "ts": time.time()})
            n_ec = len(echecs)
            st.info(f"{n_ec} analyse{'s' if n_ec > 1 else ''} en échec écartée{'s' if n_ec > 1 else ''}.")
            time.sleep(1.0)
            st.rerun()
        st.write("")

    # -----------------------------------------------------------------------
    # Les cinq dernières décisions, en résumé
    #
    # Elles ne demandent rien à l'opérateur, mais leur absence poserait une
    # question à chaque coup d'œil : le système a-t-il fait quelque chose
    # depuis tout à l'heure ? Une ligne par décision suffit à répondre.
    # L'instruction complète reste accessible dans l'onglet « Le dossier ».
    # -----------------------------------------------------------------------
    recentes = sorted(decisions, key=lambda d: d.get("ts", 0), reverse=True)[:5]
    if recentes:
        st.markdown(html_block("""<div class="so-sec"><h2>Ce que le système a fait</h2>
        <span class="ln"></span></div>"""), unsafe_allow_html=True)

        lignes = []
        for d in recentes:
            v = d.get("arbiter_verdict") or {}
            g = d.get("guardrail_decision") or {}
            badge = {"autoriser_auto": ("b-auto", "auto"),
                     "validation_humaine": ("b-human", "humain"),
                     "refuser": ("b-none", "refusé")}.get(g.get("decision", ""),
                                                          ("b-none", g.get("decision", "—")))
            conf = v.get("final_confidence")
            lignes.append(f"""<div class="so-row">
              <span class="so-badge {badge[0]}">{badge[1]}</span>
              <span style="color:var(--faint);width:86px">{since(d.get('ts', 0))}</span>
              <span style="color:var(--signal);width:150px">{v.get('composant_suspecte', '—')}</span>
              <span style="width:52px;color:var(--ink)">{f'{conf:.2f}' if conf is not None else '—'}</span>
              <span style="color:var(--dim);flex:1;overflow:hidden;text-overflow:ellipsis;
                white-space:nowrap">{v.get('diagnosis', '')[:100]}</span>
              <span style="color:var(--faint)">{d.get('action_executed') or '—'}</span>
            </div>""")
        st.markdown(html_block(f'<div style="border:1px solid var(--rule);border-radius:6px;'
                               f'overflow:hidden;background:var(--panel)">{"".join(lignes)}</div>'),
                    unsafe_allow_html=True)
        st.caption("Instruction complète d'une décision dans l'onglet « Le dossier ».")


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


# ---------------------------------------------------------------------------
# Pied de page
# ---------------------------------------------------------------------------
st.divider()
gauche, droite = st.columns([1, 6])
if gauche.button("Actualiser", type="secondary"):
    st.session_state.pop("empreinte", None)
    st.rerun()

if VEILLE_ACTIVE:
    droite.caption(
        f"Lecture à {maintenant} · la page se met à jour d'elle-même "
        f"lorsqu'une décision nouvelle apparaît (vérification toutes les "
        f"{INTERVALLE_VEILLE_SECONDES} s)")
else:
    droite.caption(f"Lecture à {maintenant} · veille automatique indisponible "
                   f"(Streamlit < 1.37) — utilisez le bouton")