"""
Step 2 (shape-signature clustering) + linkage to the LLM call.

WHAT STEP 1 IS ASSUMED TO HAVE PRODUCED
---------------------------------------
A DataFrame `aligned` with at least these columns:

    contract_id          : group / consistency key
    payment_description  : the raw text of the payment
    invoice_ref          : ground-truth reference (from the link table)
    found                : bool, True if the ref was located in the description
    match_start          : int, char offset where the located span begins
    match_end            : int, char offset where it ends
                           (start/end only meaningful when found == True)

If Step 1 stored the matched substring instead of offsets, recompute
start/end once with description.find(substring) and add the columns.

PIPELINE PROVIDED HERE
----------------------
    2a  make_signature()      : collapse a located span into a shape fingerprint
    2b  build_clusters()      : attach signatures, one row per located match
        cluster_summary()     : Pareto table -> your "~10 regex" shortlist
        contract_purity()     : uses contract_id as a consistency signal
    2c  sample_cluster()      : diverse (description, ref) pairs per cluster
    2d  ask_for_regex()       : the LLM call (proposes ONE anchored regex)
    2e  validate_on()         : label-aware check (recall AND capture precision)
        mine_regexes()        : driver that ties 2c->2d->2e with a repair retry
"""

import re
import json
import random
from anthropic import Anthropic
import pandas as pd

client = Anthropic()

# separators we keep literally in signatures and strip in normalization
_SEPS = r"[\s\-/._:#°]"


# --------------------------------------------------------------------------
# 2a. Shape signature
# --------------------------------------------------------------------------
def _left_anchor(left: str) -> str:
    """Trailing alphabetic token before the ref -> the anchor word.
    'Paiement FACT ' -> 'FACT'   ;   'REF:' -> 'REF'   ;   '  ' -> '' (no anchor)
    An empty anchor is a warning sign: the ref is a bare number with no context,
    which is exactly the case where a regex will over-match dates/amounts.
    """
    toks = re.findall(r"[A-Za-zÀ-ÿ°]+", left)
    return toks[-1].upper() if toks else ""


def _boundary_char(desc: str, start: int) -> str:
    """The single non-alphanumeric char immediately before the ref (sep style)."""
    if start > 0 and not desc[start - 1].isalnum():
        return desc[start - 1]
    return ""


def _generalize(span: str) -> str:
    """Collapse a ref span into a length-agnostic shape.
    Runs of digits -> 'N', runs of letters -> 'A', separators kept literal.
    '2023-0012' -> 'N-N'  ;  'AB12' -> 'AN'  ;  '000123' -> 'N'
    Length is intentionally dropped so '0012' and '00457' cluster together.
    """
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


def make_signature(desc: str, start: int, end: int, left_window: int = 20) -> str:
    """Fingerprint = anchor word + boundary char + generalized ref shape."""
    left = desc[max(0, start - left_window):start]
    span = desc[start:end]
    anchor = _left_anchor(left)
    boundary = _boundary_char(desc, start)
    shape = _generalize(span)
    return f"{anchor}|{boundary}|{shape}"


# --------------------------------------------------------------------------
# 2b. Clustering + contract consistency
# --------------------------------------------------------------------------
def build_clusters(aligned: pd.DataFrame) -> pd.DataFrame:
    """Keep located rows, attach the shape signature to each."""
    df = aligned[aligned["found"]].copy()
    df["sig"] = [
        make_signature(d, int(s), int(e))
        for d, s, e in zip(df.payment_description, df.match_start, df.match_end)
    ]
    return df


def cluster_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Pareto table over signatures. cum_share tells you how few regex you need."""
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
    """For each contract, the share of its rows in its single dominant signature.
    Values near 1.0 confirm 'this debtor reuses one format' -> trustworthy pattern.
    """
    return df.groupby("contract_id")["sig"].agg(
        lambda s: s.value_counts(normalize=True).iloc[0]
    )


# --------------------------------------------------------------------------
# 2c. Diverse sampling per cluster (variety across contracts, not near-dupes)
# --------------------------------------------------------------------------
def sample_cluster(df: pd.DataFrame, sig: str, k: int = 15, seed: int = 0) -> list[dict]:
    sub = df[df.sig == sig]
    rng = random.Random(seed)
    by_contract = {cid: g.index.tolist() for cid, g in sub.groupby("contract_id")}
    contracts = list(by_contract)
    rng.shuffle(contracts)

    picks = []
    for cid in contracts:                       # one per contract first
        picks.append(rng.choice(by_contract[cid]))
        if len(picks) >= k:
            break
    if len(picks) < k:                          # top up if few contracts
        rest = [i for i in sub.index if i not in picks]
        rng.shuffle(rest)
        picks += rest[: k - len(picks)]

    return [
        {"description": r.payment_description, "ref": r.invoice_ref}
        for r in sub.loc[picks].itertuples()
    ]


# --------------------------------------------------------------------------
# 2d. The LLM call: propose ONE anchored regex for a cluster
# --------------------------------------------------------------------------
REGEX_SYSTEM = """You write ONE Python regular expression that extracts an invoice \
reference from a payment description.

Rules:
- Output a single regex with a named capture group (?P<ref>...) around the reference itself.
- The regex MUST anchor on surrounding context (prefix words like FACT, INV, REF, N°, and \
their separators) so it does NOT match bare numbers such as dates, amounts, or IBAN fragments.
- Prefer \\b word boundaries and explicit separators over greedy .* .
- It must match EVERY example below, and the captured group must equal the given ref exactly.
- Assume re.IGNORECASE is applied. Never hard-code the specific numeric values from the examples.

Respond with ONLY a JSON object: {"regex": "<pattern>", "rationale": "<one line>"}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    return json.loads(text)


def ask_for_regex(examples: list[dict], failures: list[dict] | None = None,
                  model: str = "claude-opus-4-8") -> dict:
    ex = "\n".join(
        f"{i+1}. description: {e['description']!r}\n   ref: {e['ref']!r}"
        for i, e in enumerate(examples)
    )
    content = f"Examples:\n{ex}"
    if failures:  # repair pass: show what the previous regex got wrong
        fx = "\n".join(
            f"- description: {f['description']!r}\n  expected: {f['ref']!r}  got: {f['got']!r}"
            for f in failures
        )
        content += (
            "\n\nYour previous regex mis-handled these. Fix it so the captured "
            f"group equals 'expected':\n{fx}"
        )
    msg = client.messages.create(
        model=model, max_tokens=500, system=REGEX_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    return _extract_json("".join(b.text for b in msg.content if b.type == "text"))


# --------------------------------------------------------------------------
# 2e. Label-aware validation + driver with a repair retry
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(_SEPS, "", str(s)).upper()


def validate_on(df_subset: pd.DataFrame, pattern: str, collect_fails: bool = False):
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


def mine_regexes(df: pd.DataFrame, summ: pd.DataFrame, top: int = 15,
                 k: int = 15, min_precision: float = 0.95) -> pd.DataFrame:
    """For each top signature: sample -> ask -> validate on the cluster ->
    one repair pass if capture precision is low. Cheap check before you run
    the survivors over all 130k rows (your Step 4) and do greedy set-cover."""
    rows = []
    for sig in summ.head(top).index:
        cluster = df[df.sig == sig]
        examples = sample_cluster(df, sig, k=k)
        try:
            out = ask_for_regex(examples)
            stats, fails = validate_on(cluster, out["regex"], collect_fails=True)
            if stats["precision"] < min_precision and fails:   # one repair loop
                out = ask_for_regex(examples, failures=fails)
                stats = validate_on(cluster, out["regex"])
            rows.append({"sig": sig, "regex": out["regex"],
                         "rationale": out.get("rationale", ""), **stats})
        except Exception as e:                                 # bad JSON / bad regex
            rows.append({"sig": sig, "error": str(e), "n": len(cluster)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Example wiring (uncomment once `aligned` from Step 1 exists)
# --------------------------------------------------------------------------
# df   = build_clusters(aligned)
# summ = cluster_summary(df)
# print(summ.head(20)[["rows", "contracts", "cum_share", "example_desc", "example_ref"]])
# print("mean contract purity:", contract_purity(df).mean())
# candidates = mine_regexes(df, summ, top=15)
# print(candidates.sort_values("rows", ascending=False))
#   -> feed `candidates` (regex + cluster stats) into your Step 4 full-data
#      validation and greedy set-cover to land on the final ~10.
