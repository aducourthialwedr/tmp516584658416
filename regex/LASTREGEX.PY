"""
Mining de regex pour références de facture — Étape 2 (clustering) + appel LLM.

Suppose que l'Étape 1 a produit un DataFrame `aligned` avec :
    contract_id, payment_description, invoice_ref,
    found (bool), match_start (int), match_end (int)
où start/end sont les offsets du span de la réf LOCALISÉE (valides si found).

Les références sont propres à chaque client et de fait aléatoires : on cluster
sur l'ENVELOPPE (mot d'ancrage + bornes + classe grossière de la réf).
- Fuite de noms d'entreprise : défense = diversité de l'échantillon + prompt.
- Portabilité : AUCUN lookahead/lookbehind (moteur cible type RE2/SQL). Python
  les compile quand même, d'où un garde explicite `has_lookaround`.
"""

import re
import ast
import random
from anthropic import Anthropic
import pandas as pd

client = Anthropic()

_SEPS = r"[\s\-/._:#°]"


# ==========================================================================
# 2a. Signature de forme (basée sur l'enveloppe ; réf traitée comme opaque)
# ==========================================================================
def _left_anchor(left: str) -> str:
    toks = re.findall(r"[A-Za-zÀ-ÿ°]+", left)
    return toks[-1].upper() if toks else ""


def _boundary_char(desc: str, i: int) -> str:
    return desc[i] if 0 <= i < len(desc) and not desc[i].isalnum() else ""


def _ref_class(span: str) -> str:
    """Classe grossière d'une réf opaque : N=chiffres, A=lettres, X=mixte."""
    has_d = any(c.isdigit() for c in span)
    has_a = any(c.isalpha() for c in span)
    return "X" if (has_d and has_a) else ("N" if has_d else "A")


def _generalize(span: str) -> str:
    """Forme fine (chiffres->N, lettres->A, séparateurs littéraux)."""
    out, i, n = [], 0, len(span)
    while i < n:
        c = span[i]
        if c.isdigit():
            out.append("N")
            while i < n and span[i].isdigit():
                i += 1
        elif c.isalpha():
            out.append("A")
            while i < n and span[i].isalpha():
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def make_signature(desc, start, end, left_window=20, right_window=8, coarse=True):
    left = desc[max(0, start - left_window):start]
    span = desc[start:end]
    anchor = _left_anchor(left)                       # 'INV', 'FAC', 'F', ''
    lbound = _boundary_char(desc, start - 1)          # collage/sép à gauche
    rbound = _boundary_char(desc, end)                # là où la réf se termine
    body = _ref_class(span) if coarse else _generalize(span)
    return f"{anchor}|{lbound}|{body}|{rbound}"


# ==========================================================================
# 2b. Clustering + cohérence par contrat
# ==========================================================================
def build_clusters(aligned: pd.DataFrame, coarse=True) -> pd.DataFrame:
    df = aligned[aligned["found"]].copy()
    df["sig"] = [
        make_signature(d, int(s), int(e), coarse=coarse)
        for d, s, e in zip(df.payment_description, df.match_start, df.match_end)
    ]
    return df


def cluster_summary(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("sig")
    summ = pd.DataFrame({
        "rows": g.size(),
        "contracts": g["contract_id"].nunique(),
        "example_desc": g["payment_description"].first(),
        "example_ref": g["invoice_ref"].first(),
    }).sort_values("rows", ascending=False)
    summ["row_share"] = summ["rows"] / len(df)
    summ["cum_share"] = summ["row_share"].cumsum()
    return summ


def contract_purity(df: pd.DataFrame) -> pd.Series:
    return df.groupby("contract_id")["sig"].agg(
        lambda s: s.value_counts(normalize=True).iloc[0]
    )


# ==========================================================================
# 2c. Échantillonnage diversifié — une ligne par contrat (défense anti-nom)
# ==========================================================================
def sample_cluster(df, sig, k=15, seed=0):
    sub = df[df.sig == sig]
    rng = random.Random(seed)
    by_contract = {cid: g.index.tolist() for cid, g in sub.groupby("contract_id")}
    contracts = list(by_contract)
    rng.shuffle(contracts)

    picks = []
    for cid in contracts:                 # au plus une ligne par contrat d'abord
        picks.append(rng.choice(by_contract[cid]))
        if len(picks) >= k:
            break
    if len(picks) < k:                    # compléter seulement si peu de contrats
        rest = [i for i in sub.index if i not in picks]
        rng.shuffle(rest)
        picks += rest[: k - len(picks)]

    return [
        {"description": r.payment_description, "ref": r.invoice_ref}
        for r in sub.loc[picks].itertuples()
    ]


# ==========================================================================
# 2d. Appel LLM + extraction robuste du motif brut
# ==========================================================================
REGEX_SYSTEM = """Tu écris UNE expression régulière qui extrait une référence de facture \
présente dans un libellé de paiement.

La référence est propre à chaque client et de fait aléatoire — ne modélise NI sa structure \
interne NI sa longueur. Ancre-toi sur la DÉCORATION autour d'elle (mots/lettres de préfixe \
comme INV, FAC, F, N°, et les séparateurs / - espace) et sur l'endroit où la référence se \
termine (délimiteur ou fin de chaîne), puis capture ce qui se trouve entre les deux comme \
un jeton opaque.

Règles de contenu :
- Une seule regex, groupe nommé (?P<ref>...) autour de la référence UNIQUEMENT — exclus la \
décoration ('INV/', un 'F' collé en tête qui n'appartient pas à la référence).
- Capture générique, ex. (?P<ref>[A-Z0-9]+), bornée par l'ancre et le délimiteur ; ne code \
jamais en dur les valeurs des exemples.
- Le groupe capturé doit être STRICTEMENT égal à la référence donnée. Différence clé : \
'F12345' -> '12345' (F = ancre) contre 'INV/FAC1234' -> 'FAC1234' (lettres = partie de la réf).
- N'utilise JAMAIS comme ancre un nom d'entreprise, un nom propre, ou une forme juridique \
(SARL, SA, SAS, GMBH, LTD, INC...). La regex doit marcher pour TOUS les clients, pas un seul.

Règles de FORMAT (sinon la regex est inutilisable) :
- Le moteur cible ne supporte NI lookahead NI lookbehind : n'emploie AUCUN (?=...), (?!...), \
(?<=...), (?<!...). Utilise uniquement des séparateurs explicites, des bornes \\b et des \
groupes de capture pour délimiter.
- Motif BRUT tel quel pour re.compile : PAS de préfixe r, PAS de guillemets, PAS \
d'échappement de littéral de chaîne (écris \\d, pas \\\\d), PAS de bloc de code.
- Déjà compilé avec IGNORECASE : n'émets AUCUN drapeau global en ligne (ni (?i), (?s), (?m)). \
Les groupes à portée locale comme (?i:FAC) restent permis.
- Évite les .* gourmands ; préfère \\b et des séparateurs explicites.

Réponds avec le motif entre <regex> et </regex>, puis une ligne entre <why> et </why>. \
Rien d'autre."""

_GLOBAL_FLAG = re.compile(r"\(\?([aiLmsux]+)\)")
_LOOKS_LITERAL = re.compile(r"""^(?:r|u|b|rb|br)?(['"]).*\1$""", re.IGNORECASE | re.DOTALL)
# (?=  (?!  (?<=  (?<!  — sans toucher (?P<...> ni (?:...) ni (?i:...)
_LOOKAROUND = re.compile(r"\(\?<?[=!]")


def has_lookaround(pattern: str) -> bool:
    """True si le motif contient un lookahead/lookbehind (interdit sur la cible).
    Utile aussi à l'Étape 4 pour re-filtrer avant déploiement."""
    return bool(_LOOKAROUND.search(pattern))


def _unwrap(pattern: str) -> str:
    """Défait une enveloppe de littéral Python : r"\\d+" ou "\\\\d+" -> \\d+."""
    p = pattern.strip()
    p = re.sub(r"^```[a-z]*\s*|\s*```$", "", p).strip()
    if _LOOKS_LITERAL.match(p):
        try:
            val = ast.literal_eval(p)
            if isinstance(val, str):
                return val
        except (ValueError, SyntaxError):
            pass
    return p


def _fix_global_flags(pattern: str) -> str:
    """Remonte les drapeaux globaux en ligne en position 0 (exigence 3.11+)."""
    groups = _GLOBAL_FLAG.findall(pattern)
    if not groups:
        return pattern
    flags = "".join(sorted(set("".join(groups))))
    body = _GLOBAL_FLAG.sub("", pattern)
    return f"(?{flags}){body}"


def _finalize(raw: str) -> str:
    pattern = _fix_global_flags(_unwrap(raw))
    re.compile(pattern, re.IGNORECASE)      # échoue bruyamment si motif cassé
    return pattern


def ask_for_regex(examples, failures=None, drop_lookaround=False, model="claude-opus-4-8"):
    ex = "\n".join(
        f"{i+1}. libellé: {e['description']!r}\n   réf: {e['ref']!r}"
        for i, e in enumerate(examples)
    )
    content = f"Exemples :\n{ex}"
    if failures:
        fx = "\n".join(
            f"- libellé: {f['description']!r}\n  attendu: {f['ref']!r}  obtenu: {f['got']!r}"
            for f in failures
        )
        content += ("\n\nTa regex précédente a mal traité ceci. Corrige-la pour que le "
                    f"groupe capturé soit égal à 'attendu' :\n{fx}")
    if drop_lookaround:
        content += ("\n\nTa regex précédente utilisait un lookahead ou lookbehind, INTERDIT "
                    "sur le moteur cible. Réécris-la sans (?=...), (?!...), (?<=...), (?<!...), "
                    "en délimitant par des séparateurs explicites, des bornes \\b et des groupes.")

    msg = client.messages.create(
        model=model, max_tokens=500, system=REGEX_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"<regex>\s*(.*?)\s*</regex>", text, re.DOTALL)
    if not m:
        raise ValueError(f"pas de bloc <regex> :\n{text}")
    why = re.search(r"<why>\s*(.*?)\s*</why>", text, re.DOTALL)
    return {"regex": _finalize(m.group(1)), "rationale": why.group(1).strip() if why else ""}


# ==========================================================================
# Validation (basée sur les labels : rappel ET précision de la capture)
# ==========================================================================
def _norm(s):
    return re.sub(_SEPS, "", str(s)).upper()


def validate_on(df_subset, pattern, collect_fails=False):
    rx = re.compile(pattern, re.IGNORECASE)
    hits = correct = 0
    fails = []
    for r in df_subset.itertuples():
        m = rx.search(r.payment_description)
        if not m:
            continue
        hits += 1
        cap = m.groupdict().get("ref")
        if cap is not None and _norm(cap) == _norm(r.invoice_ref):
            correct += 1
        elif collect_fails and len(fails) < 8:
            fails.append({"description": r.payment_description,
                          "ref": r.invoice_ref, "got": cap})
    n = len(df_subset)
    out = {"n": n, "recall": hits / n if n else 0.0,
           "precision": correct / hits if hits else 0.0}
    return (out, fails) if collect_fails else out


# ==========================================================================
# Driver : échantillonne -> demande -> valide.
# Répare si précision faible OU si lookaround détecté.
# On ne retient QUE des candidats sans lookaround ; si le modèle n'y arrive
# jamais, on renvoie le meilleur en le marquant lookaround=True (à traiter).
# `contracts` = diagnostic diversité (1–2 => risque de nom d'entreprise).
# ==========================================================================
def _score(s):
    return (s["precision"], s["recall"])


def mine_regexes(df, summ, top=15, k=15, min_precision=0.95, max_rounds=3):
    rows = []
    for sig in summ.head(top).index:
        cluster = df[df.sig == sig]
        n_contracts = cluster["contract_id"].nunique()
        examples = sample_cluster(df, sig, k=k)
        best = fallback = None          # best = sans lookaround ; fallback sinon
        failures = drop_look = None

        for _ in range(max_rounds):
            try:
                out = ask_for_regex(examples, failures=failures,
                                    drop_lookaround=bool(drop_look))
            except Exception as e:
                if best is None and fallback is None:
                    fallback = {"regex": None, "rationale": "", "error": str(e),
                                "n": len(cluster), "recall": 0.0, "precision": 0.0,
                                "lookaround": False}
                break

            stats, fails = validate_on(cluster, out["regex"], collect_fails=True)
            look = has_lookaround(out["regex"])
            cand = {"regex": out["regex"], "rationale": out["rationale"],
                    "lookaround": look, **stats}

            if look:                                     # inéligible : à réécrire
                if fallback is None or _score(stats) > _score(fallback):
                    fallback = cand
                drop_look = True
                failures = fails or None
                continue

            if best is None or _score(stats) > _score(best):
                best = cand
            if stats["precision"] >= min_precision:
                break
            drop_look = None
            failures = fails or None

        chosen = best or fallback
        rows.append({"sig": sig, "contracts": n_contracts, **chosen})
    return pd.DataFrame(rows)


# ==========================================================================
# Câblage d'exemple
# ==========================================================================
# df   = build_clusters(aligned, coarse=True)
# summ = cluster_summary(df)
# print("part des lignes top-10 :", summ.head(10)["row_share"].sum())
# candidates = mine_regexes(df, summ, top=15)
# print(candidates[["sig", "contracts", "regex", "n", "recall", "precision", "lookaround"]])
#   -> écarter lookaround==True et précision faible, puis passer les regex
#      retenues sur les 130k lignes et faire un set-cover glouton -> ~10.
