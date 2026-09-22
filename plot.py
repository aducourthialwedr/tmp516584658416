# -*- coding: utf-8 -*-
"""
Waterfall Chart (Plotly) : groupes d'algorithmes + decomposition en sous-algos
+ taux de remplacement ML affiches quand ils sont non nuls.
"""
import plotly.graph_objects as go

# -- DONNEES ------------------------------------------------------------
# Modifiez librement. "ml" = taux de remplacement ML en % (0 => non affiche).
groupes = [
    {"nom": "Groupe A", "couleur": "#7FB3D5", "sous_algos": [   # bleu clair
        {"nom": "A1", "valeur": 12.0, "ml": 20},
        {"nom": "A2", "valeur": 18.0, "ml": 50},
    ]},
    {"nom": "Groupe C", "couleur": "#C9B458", "sous_algos": [   # kaki
        {"nom": "C1", "valeur": 12.5, "ml": 40},
        {"nom": "C2", "valeur": 7.5,  "ml": 0},
    ]},
    {"nom": "Groupe D", "couleur": "#E8998D", "sous_algos": [   # corail
        {"nom": "D1", "valeur": 25.0, "ml": 30},
    ]},
]
non_traite    = {"nom": "Non traite", "couleur": "#F5CBA7", "valeur": 25.0}  # saumon
couleur_total = "#87CEEB"                                                     # bleu ciel
LARGEUR = 0.55

# -- CALCULS ------------------------------------------------------------
for g in groupes:
    g["total"] = sum(s["valeur"] for s in g["sous_algos"])
total = sum(g["total"] for g in groupes) + non_traite["valeur"]

noms   = ["Total"] + [g["nom"] for g in groupes] + [non_traite["nom"]]
mesure = ["total"] + ["relative"] * len(groupes) + ["relative"]
vals   = [total]   + [-g["total"] for g in groupes] + [-non_traite["valeur"]]

fig = go.Figure()

# -- 1. Squelette waterfall (algos principaux negatifs) -----------------
fig.add_trace(go.Waterfall(
    orientation="v", measure=mesure, x=noms, y=vals, width=LARGEUR,
    connector={"line": {"color": "rgba(120,120,120,0.45)", "width": 1}},
    increasing={"marker": {"color": "rgba(0,0,0,0)"}},
    decreasing={"marker": {"color": "rgba(0,0,0,0)"}},
    totals={"marker": {"color": couleur_total}},
    text=[f"{total:g}"] + [f"{g['total']:g}" for g in groupes] + [f"{non_traite['valeur']:g}"],
    textposition="outside", showlegend=False, hoverinfo="skip",
))

# -- 2. Remplissage des marches par sous-algos empiles (positifs) -------
annotations = []
plancher = total

def nuance(couleur_hex, i, n):
    r, v, b = (int(couleur_hex[j:j + 2], 16) for j in (1, 3, 5))
    f = 0.0 if n <= 1 else 0.30 * (i / (n - 1))
    r, v, b = (int(c + (255 - c) * f) for c in (r, v, b))
    return f"rgb({r},{v},{b})"

for g in groupes:
    bas = plancher - g["total"]
    fig.add_trace(go.Bar(x=[g["nom"]], y=[bas], width=LARGEUR,
                         marker={"color": "rgba(0,0,0,0)"},
                         showlegend=False, hoverinfo="skip"))
    cur = bas
    n = len(g["sous_algos"])
    for i, s in enumerate(g["sous_algos"]):
        fig.add_trace(go.Bar(
            name=s["nom"], x=[g["nom"]], y=[s["valeur"]], width=LARGEUR,
            marker={"color": nuance(g["couleur"], i, n),
                    "line": {"color": "white", "width": 1.5}},
            text=[f"{s['nom']} - {s['valeur']:g}"], textposition="inside",
            insidetextanchor="middle",
            hovertemplate=f"{s['nom']} : {s['valeur']:g}"
                          + (f" - ML {s['ml']}%" if s["ml"] else "") + "<extra></extra>",
            showlegend=False,
        ))
        if s["ml"]:
            annotations.append(dict(
                x=g["nom"], y=cur + s["valeur"] / 2, xref="x", yref="y",
                xshift=int(LARGEUR * 130), text=f"ML {s['ml']}%",
                showarrow=False, align="left",
                font=dict(size=11, color="#333"),
                bgcolor="rgba(255,255,255,0.75)",
            ))
        cur += s["valeur"]
    plancher = bas

fig.add_trace(go.Bar(
    x=[non_traite["nom"]], y=[non_traite["valeur"]], width=LARGEUR,
    marker={"color": non_traite["couleur"], "line": {"color": "white", "width": 1.5}},
    text=[f"{non_traite['valeur']:g}"], textposition="inside",
    insidetextanchor="middle", hoverinfo="skip", showlegend=False,
))

# -- 3. Mise en page ----------------------------------------------------
fig.update_layout(
    title="Repartition des algorithmes & taux de remplacement ML",
    barmode="stack", bargap=0.35, yaxis_title="Items",
    plot_bgcolor="white", annotations=annotations,
    margin=dict(l=60, r=100, t=70, b=40), height=580, width=920,
    font=dict(family="Segoe UI, Arial, sans-serif"),
)
fig.update_yaxes(gridcolor="rgba(0,0,0,0.08)", zeroline=False, rangemode="tozero")

fig.write_html("/home/claude/waterfall.html", include_plotlyjs="cdn")
try:
    fig.write_image("/home/claude/waterfall.png", scale=2)
except Exception as e:
    print("PNG non genere (kaleido/Chrome absent) :", type(e).__name__)

plancher = total
for g in groupes:
    bas = plancher - g["total"]
    line = f"{g['nom']:9s} marche [{bas:6.1f} , {plancher:6.1f}]  "
    c = bas
    for s in g["sous_algos"]:
        line += f"{s['nom']}[{c:.1f}->{c+s['valeur']:.1f}] "
        c += s["valeur"]
    print(line)
    plancher = bas
print(f"Non traite marche [   0.0 , {plancher:6.1f}]   total={total:g}")
