"""
invoice_regex_miner.py
======================

Génération, validation et sélection de regex permettant d'extraire les numéros
de facture depuis les libellés de paiement (réconciliation paiement <-> facture).

Pipeline :
    1. Segmentation par contrat
    2. Mining heuristique de patterns (à partir des numéros réellement présents
       dans les libellés) -> "graines" pour le LLM
    3. Génération de regex par le LLM (par contrat, avec exemples libellé/numéro)
    4. Validation des regex sur les données réelles du contrat (precision/recall)
    5. (option) Round de raffinement LLM sur les cas d'échec
    6. Consolidation globale : agrégation par fréquence, généralisation LLM,
       puis sélection gloutonne d'un portefeuille minimal de regex

Le module fonctionne SANS LLM (llm=None) : il se rabat alors sur les seules
regex heuristiques, ce qui permet de tester le pipeline à coût nul.

Usage minimal :
    from invoice_regex_miner import RegexDiscoveryPipeline, PipelineConfig, AnthropicLLM

    result = RegexDiscoveryPipeline(PipelineConfig(), llm=AnthropicLLM()).run(df_regex)
    result.per_contract_df      # regex validées par contrat
    result.global_df            # candidats globaux scorés
    result.portfolio_df         # portefeuille final sélectionné
    result.apply(df_regex)      # extraction sur des libellés
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import pandas as pd

# =============================================================================
# 1. CONFIGURATION — NOMS DE COLONNES (à modifier ici uniquement)
# =============================================================================

COL_CONTRACT_ID = "contract_id"        # facture : identifiant du contrat
COL_PAYMENT_ID = "payment_id"          # paiement : identifiant du paiement
COL_PAYMENT_LABEL = "payment_label"    # paiement : libellé (texte libre)
COL_INVOICE_NUMBER = "invoice_number"  # facture : numéro de facture (la cible)
COL_INVOICE_ID = "invoice_id"          # facture : id technique (optionnel)

# Colonnes optionnelles utilisées seulement si présentes dans le df
COL_PAYMENT_DATE = "payment_date"      # optionnel, pour l'échantillonnage
OPTIONAL_COLUMNS = [COL_INVOICE_ID, COL_PAYMENT_DATE]

# =============================================================================
# 2. PARAMÈTRES DU PIPELINE
# =============================================================================


@dataclass
class PipelineConfig:
    # --- normalisation utilisée pour comparer un extrait au numéro attendu ---
    normalize_case: bool = True
    normalize_strip_separators: bool = True   # "F-2024/001" == "F2024001"
    normalize_strip_leading_zeros: bool = False  # risqué : peut fusionner 2 factures

    # --- segmentation ---
    min_payments_per_contract: int = 3    # en dessous : contrat traité en "pool résiduel"
    max_contracts: Optional[int] = None   # None = tous ; sinon échantillon (debug/coût)

    # --- mining heuristique ---
    heuristic_max_candidates_per_contract: int = 12
    heuristic_prefix_window: int = 25     # nb de caractères de contexte gauche analysés

    # --- LLM ---
    llm_examples_per_contract: int = 25   # nb d'exemples (libellé, numéros) envoyés
    llm_max_regex_per_contract: int = 6
    llm_refinement_rounds: int = 1        # 0 = pas de round de correction
    llm_global_candidates: int = 30       # top-N patterns envoyés à la consolidation

    # --- validation par contrat ---
    min_precision_contract: float = 0.80
    min_recall_contract: float = 0.30

    # --- sélection globale ---
    min_precision_global: float = 0.85
    min_marginal_gain: float = 0.005      # gain de recall minimal pour garder une regex
    max_portfolio_size: int = 12

    # --- garde-fous regex ---
    regex_timeout_ms: int = 200           # si le module `regex` est installé
    max_label_length: int = 500           # tronque les libellés monstrueux
    forbid_trivial_patterns: bool = True  # rejette \d+ , .* , etc.

    # --- divers ---
    random_state: int = 0
    verbose: bool = True


LOGGER = logging.getLogger("invoice_regex_miner")

# =============================================================================
# 3. OUTILS REGEX (compilation sûre + extraction)
# =============================================================================

try:  # `regex` supporte un timeout -> protège du catastrophic backtracking
    import regex as _re_engine

    _HAS_TIMEOUT = True
except ImportError:  # pragma: no cover
    _re_engine = re
    _HAS_TIMEOUT = False

_TRIVIAL_PATTERNS = {
    r"\d+", r"\d*", r".*", r".+", r"\w+", r"[0-9]+", r"\S+", r"\b\d+\b",
}

# Nom du groupe de capture attendu dans les regex générées
CAPTURE_GROUP = "num"


def compile_regex(pattern: str, cfg: PipelineConfig) -> Optional[Any]:
    """Compile une regex en refusant les patterns invalides ou triviaux."""
    if not pattern or not isinstance(pattern, str):
        return None
    if cfg.forbid_trivial_patterns and pattern.strip() in _TRIVIAL_PATTERNS:
        return None
    try:
        return _re_engine.compile(pattern)
    except Exception:
        return None


def extract_matches(compiled: Any, text: str, cfg: PipelineConfig) -> list[str]:
    """Retourne les chaînes extraites par la regex (groupe `num`, sinon groupe 1,
    sinon match complet)."""
    if not text:
        return []
    text = text[: cfg.max_label_length]
    out: list[str] = []
    try:
        kwargs = {"timeout": cfg.regex_timeout_ms / 1000} if _HAS_TIMEOUT else {}
        for m in compiled.finditer(text, **kwargs):
            val = None
            try:
                gd = m.groupdict()
                if CAPTURE_GROUP in gd and gd[CAPTURE_GROUP]:
                    val = gd[CAPTURE_GROUP]
            except Exception:
                val = None
            if val is None:
                if m.lastindex:
                    val = m.group(1)
                else:
                    val = m.group(0)
            if val:
                out.append(val)
    except Exception:  # timeout, erreur d'exécution -> regex inutilisable
        return []
    return out


# =============================================================================
# 4. NORMALISATION DES NUMÉROS
# =============================================================================

_SEPARATORS = re.compile(r"[\s_\-./\\|:;,#]")


def normalize_number(value: Any, cfg: PipelineConfig) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    if cfg.normalize_case:
        s = s.upper()
    if cfg.normalize_strip_separators:
        s = _SEPARATORS.sub("", s)
    if cfg.normalize_strip_leading_zeros:
        s = re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)
    return s


# =============================================================================
# 5. STRUCTURES DE DONNÉES
# =============================================================================


@dataclass
class PaymentRecord:
    payment_id: str
    label: str
    expected: set[str] = field(default_factory=set)       # numéros bruts
    expected_norm: set[str] = field(default_factory=set)  # numéros normalisés


@dataclass
class RegexCandidate:
    pattern: str
    source: str                      # "heuristic" | "llm" | "llm_refine" | "llm_global"
    contract_id: Optional[str] = None
    rationale: str = ""

    def key(self) -> str:
        return self.pattern


@dataclass
class RegexScore:
    pattern: str
    n_payments: int = 0
    tp: int = 0
    fp_known: int = 0     # extrait un vrai numéro de facture, mais pas celui attendu
    fp_unknown: int = 0   # extrait du bruit
    fn: int = 0
    payments_hit: int = 0        # au moins un numéro attendu retrouvé
    payments_exact: int = 0      # extraction == attendu (ensembles égaux)
    matched_payment_ids: set[str] = field(default_factory=set)

    @property
    def fp(self) -> int:
        return self.fp_known + self.fp_unknown

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def coverage(self) -> float:
        return self.payments_hit / self.n_payments if self.n_payments else 0.0

    def as_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "n_payments": self.n_payments,
            "tp": self.tp,
            "fp_known": self.fp_known,
            "fp_unknown": self.fp_unknown,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "coverage": round(self.coverage, 4),
            "payments_exact": self.payments_exact,
        }


# =============================================================================
# 6. PRÉPARATION DES DONNÉES
# =============================================================================


def build_dataset(
    df: pd.DataFrame, cfg: PipelineConfig
) -> tuple[dict[str, list[PaymentRecord]], set[str]]:
    """Transforme le df fusionné (1 ligne = 1 imputation) en
    {contract_id: [PaymentRecord, ...]} + l'univers des numéros de facture connus."""
    required = [COL_CONTRACT_ID, COL_PAYMENT_LABEL, COL_INVOICE_NUMBER]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"Colonnes manquantes dans le dataframe : {missing}")

    work = df.copy()
    if COL_PAYMENT_ID not in work.columns:
        LOGGER.warning("%s absent : le libellé sert d'identifiant de paiement.", COL_PAYMENT_ID)
        work[COL_PAYMENT_ID] = work[COL_PAYMENT_LABEL].astype(str)

    work = work.dropna(subset=[COL_PAYMENT_LABEL, COL_INVOICE_NUMBER])
    work[COL_CONTRACT_ID] = work[COL_CONTRACT_ID].fillna("__NO_CONTRACT__").astype(str)
    work[COL_PAYMENT_ID] = work[COL_PAYMENT_ID].astype(str)
    work[COL_PAYMENT_LABEL] = work[COL_PAYMENT_LABEL].astype(str)
    work[COL_INVOICE_NUMBER] = work[COL_INVOICE_NUMBER].astype(str).str.strip()

    known_numbers = {
        normalize_number(v, cfg) for v in work[COL_INVOICE_NUMBER].unique()
    }
    known_numbers.discard("")

    contracts: dict[str, dict[str, PaymentRecord]] = defaultdict(dict)
    for row in work.itertuples(index=False):
        cid = getattr(row, COL_CONTRACT_ID)
        pid = getattr(row, COL_PAYMENT_ID)
        label = getattr(row, COL_PAYMENT_LABEL)
        num = getattr(row, COL_INVOICE_NUMBER)

        rec = contracts[cid].get(pid)
        if rec is None:
            rec = PaymentRecord(payment_id=pid, label=label)
            contracts[cid][pid] = rec
        rec.expected.add(num)
        n = normalize_number(num, cfg)
        if n:
            rec.expected_norm.add(n)

    dataset = {cid: list(recs.values()) for cid, recs in contracts.items()}

    # Les contrats trop petits sont regroupés dans un pool résiduel :
    # ils profitent malgré tout du mining et des regex globales.
    small = [c for c, r in dataset.items() if len(r) < cfg.min_payments_per_contract]
    if small:
        pool: list[PaymentRecord] = []
        for c in small:
            pool.extend(dataset.pop(c))
        dataset["__SMALL_CONTRACTS_POOL__"] = pool

    if cfg.max_contracts is not None:
        keep = sorted(dataset, key=lambda c: -len(dataset[c]))[: cfg.max_contracts]
        dataset = {c: dataset[c] for c in keep}

    return dataset, known_numbers


# =============================================================================
# 7. MINING HEURISTIQUE (graines pour le LLM + fallback sans LLM)
# =============================================================================

_LEFT_TOKEN = re.compile(r"([A-Za-zÀ-ÿ]{2,15})[\s:._\-/#°N]*$")
# Bornes plus fiables que \b quand le pattern commence/finit par un non-word char
BOUND_LEFT = r"(?<![A-Za-z0-9])"
BOUND_RIGHT = r"(?![A-Za-z0-9])"


def _char_class(ch: str) -> str:
    if ch.isdigit():
        return r"\d"
    if ch.isalpha():
        return "[A-Z]" if ch.isupper() else "[a-z]"
    return re.escape(ch)


def structural_pattern(token: str, loose: bool = False) -> str:
    """'FAC2024-0012' -> '[A-Z]{3}\\d{4}\\-\\d{4}' (ou quantifieurs élargis si loose)."""
    if not token:
        return ""
    classes = [_char_class(c) for c in token]
    # fusionne [A-Z] et [a-z] consécutifs en [A-Za-z]
    parts: list[tuple[str, int]] = []
    for c in classes:
        if parts and parts[-1][0] == c:
            parts[-1] = (c, parts[-1][1] + 1)
        elif parts and {parts[-1][0], c} == {"[A-Z]", "[a-z]"}:
            parts[-1] = ("[A-Za-z]", parts[-1][1] + 1)
        else:
            parts.append((c, 1))

    out = []
    for cls, n in parts:
        quantified = cls.startswith("[") or cls.startswith("\\d")
        if loose and quantified:
            lo, hi = max(1, n - 1), n + 2
            out.append(f"{cls}{{{lo},{hi}}}")
        elif n > 1:
            out.append(f"{cls}{{{n}}}")
        else:
            out.append(cls)
    return "".join(out)


def _number_variants(num: str) -> list[str]:
    """Variantes plausibles d'apparition d'un numéro dans un libellé."""
    v = {num, num.upper(), num.lower(), num.replace(" ", "")}
    core = re.sub(r"^[^0-9A-Za-z]+", "", num)
    v.add(core)
    digits = re.sub(r"\D", "", num)
    if len(digits) >= 4:
        v.add(digits)
        v.add(digits.lstrip("0"))
    return [x for x in v if x and len(x) >= 3]


def locate_number(label: str, num: str) -> Optional[tuple[int, int, str]]:
    """Cherche le numéro (ou une variante) dans le libellé -> (start, end, token)."""
    for variant in sorted(_number_variants(num), key=len, reverse=True):
        idx = label.upper().find(variant.upper())
        if idx >= 0:
            return idx, idx + len(variant), label[idx: idx + len(variant)]
    return None


def diagnose_locatability(dataset: dict[str, list[PaymentRecord]]) -> pd.DataFrame:
    """Plafond de recall atteignable : un numéro qui n'apparaît pas (même partiellement)
    dans le libellé ne pourra JAMAIS être extrait par une regex. À regarder avant de
    s'acharner sur les patterns."""
    rows = []
    for cid, records in dataset.items():
        total = found = 0
        payments_ok = 0
        for rec in records:
            hit = 0
            for num in rec.expected:
                total += 1
                if locate_number(rec.label, num) is not None:
                    found += 1
                    hit += 1
            if hit:
                payments_ok += 1
        rows.append(
            {
                COL_CONTRACT_ID: cid,
                "n_payments": len(records),
                "n_invoice_links": total,
                "n_locatable": found,
                "max_recall": round(found / total, 4) if total else 0.0,
                "max_payment_coverage": round(payments_ok / len(records), 4) if records else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("max_recall")


def mine_heuristic_patterns(
    records: list[PaymentRecord], cfg: PipelineConfig
) -> tuple[list[RegexCandidate], list[dict]]:
    """Construit des regex candidates à partir des numéros effectivement localisés
    dans les libellés. Retourne aussi les 'observations' (contexte gauche, token)
    qui serviront d'exemples pour le prompt LLM."""
    counter: Counter[str] = Counter()
    observations: list[dict] = []

    for rec in records:
        for num in rec.expected:
            loc = locate_number(rec.label, num)
            if loc is None:
                continue
            start, end, token = loc
            left = rec.label[max(0, start - cfg.heuristic_prefix_window): start]
            m = _LEFT_TOKEN.search(left)
            prefix = m.group(1) if m else ""
            observations.append(
                {"label": rec.label, "number": num, "token": token,
                 "left_context": left, "prefix": prefix}
            )

            strict = structural_pattern(token)
            loose = structural_pattern(token, loose=True)
            for struct in {strict, loose}:
                if not struct:
                    continue
                counter[f"{BOUND_LEFT}(?P<{CAPTURE_GROUP}>{struct}){BOUND_RIGHT}"] += 1
                if prefix:
                    sep = r"[\s:._\-/#°]{0,3}"
                    counter[
                        f"(?i:{re.escape(prefix)}){sep}(?P<{CAPTURE_GROUP}>{struct}){BOUND_RIGHT}"
                    ] += 1

    candidates = [
        RegexCandidate(pattern=p, source="heuristic")
        for p, _ in counter.most_common(cfg.heuristic_max_candidates_per_contract)
    ]
    return candidates, observations


# =============================================================================
# 8. ÉVALUATION DES REGEX SUR LES DONNÉES
# =============================================================================


def evaluate_pattern(
    pattern: str,
    records: list[PaymentRecord],
    known_numbers: set[str],
    cfg: PipelineConfig,
) -> Optional[RegexScore]:
    compiled = compile_regex(pattern, cfg)
    if compiled is None:
        return None

    score = RegexScore(pattern=pattern, n_payments=len(records))
    for rec in records:
        preds = {
            normalize_number(x, cfg) for x in extract_matches(compiled, rec.label, cfg)
        }
        preds.discard("")
        hit = preds & rec.expected_norm
        miss = rec.expected_norm - preds
        extra = preds - rec.expected_norm

        score.tp += len(hit)
        score.fn += len(miss)
        for e in extra:
            if e in known_numbers:
                score.fp_known += 1
            else:
                score.fp_unknown += 1
        if hit:
            score.payments_hit += 1
            score.matched_payment_ids.add(rec.payment_id)
        if preds and preds == rec.expected_norm:
            score.payments_exact += 1
    return score


# =============================================================================
# 9. CLIENT LLM
# =============================================================================


class BaseLLM:
    """Interface minimale : prompt (str) -> réponse (str)."""

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError


class AnthropicLLM(BaseLLM):
    """Client Anthropic. `pip install anthropic` + variable ANTHROPIC_API_KEY."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 2000,
        temperature: float = 0.0,
        api_key: Optional[str] = None,
    ):
        import anthropic  # import tardif : le module reste utilisable sans le SDK

        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def complete(self, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


class CallableLLM(BaseLLM):
    """Wrapper autour de n'importe quelle fonction (system, user) -> str.
    Pratique pour brancher OpenAI, Bedrock, Vertex, un mock de test, etc."""

    def __init__(self, fn: Callable[[str, str], str]):
        self.fn = fn

    def complete(self, system: str, user: str) -> str:
        return self.fn(system, user)


# --------------------------- Prompts ----------------------------------------

SYSTEM_PROMPT = """Tu es un expert en expressions régulières Python et en réconciliation \
bancaire. Tu produis des regex qui extraient des numéros de facture depuis des libellés \
de paiement.

RÈGLES IMPÉRATIVES :
1. Syntaxe du module `re` de Python, compatible `re.finditer`.
2. Chaque regex DOIT contenir exactement un groupe nommé (?P<num>...) qui capture \
le numéro de facture et RIEN d'autre (pas le préfixe, pas les espaces).
3. Les regex doivent être SPÉCIFIQUES : jamais \\d+ ou .* seuls. Utilise des \
quantifieurs bornés ({4}, {2,8}) et des ancrages ((?<![A-Za-z0-9]), (?![A-Za-z0-9])).
4. Préfère plusieurs regex précises à une regex fourre-tout.
5. Échappe correctement les caractères spéciaux. Pas de flags inline en début de \
pattern (utilise (?i:...) localement si besoin).
6. Réponds UNIQUEMENT par du JSON valide, sans texte autour, sans balises markdown.
"""

CONTRACT_USER_TEMPLATE = """Contexte : contrat `{contract_id}`.
Voici des paiements et le ou les numéros de facture qui leur sont réellement imputés.

EXEMPLES (libellé -> numéros attendus) :
{examples}

{seeds_block}
{failures_block}
Objectif : proposer au maximum {max_regex} regex qui, appliquées aux libellés de ce \
contrat, extraient les numéros attendus et le moins de bruit possible.

Format de réponse (JSON strict) :
{{"regexes": [{{"pattern": "...", "rationale": "..."}}]}}"""

GLOBAL_USER_TEMPLATE = """Voici les regex qui ont été validées sur des contrats \
individuels, avec le nombre de contrats où elles fonctionnent et leurs performances.

REGEX VALIDÉES :
{patterns}

EXEMPLES DE LIBELLÉS ISSUS DE CONTRATS VARIÉS :
{examples}

Objectif : proposer au maximum {max_regex} regex GÉNÉRALISTES qui couvrent le \
maximum de ces familles de formats sans devenir trop permissives. Fusionne les \
patterns quasi identiques (ex. quantifieurs voisins, préfixes synonymes FAC/FACT/FA) \
en utilisant des alternatives et des quantifieurs bornés.

Format de réponse (JSON strict) :
{{"regexes": [{{"pattern": "...", "rationale": "..."}}]}}"""


def parse_llm_json(text: str) -> list[dict]:
    """Parse robuste de la réponse LLM (tolère les fences markdown et le bruit)."""
    if not text:
        return []
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = data.get("regexes", [])
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str):
            out.append({"pattern": item, "rationale": ""})
        elif isinstance(item, dict) and item.get("pattern"):
            out.append({"pattern": item["pattern"], "rationale": item.get("rationale", "")})
    return out


# =============================================================================
# 10. ÉCHANTILLONNAGE DES EXEMPLES ENVOYÉS AU LLM
# =============================================================================


def _label_signature(label: str) -> str:
    """Signature grossière d'un libellé pour dédupliquer les exemples similaires."""
    s = re.sub(r"\d", "0", label.upper())
    s = re.sub(r"\s+", " ", s)
    return s[:60]


def sample_examples(records: list[PaymentRecord], n: int) -> list[PaymentRecord]:
    """Sélectionne des exemples diversifiés (une par signature de libellé)."""
    seen: set[str] = set()
    picked: list[PaymentRecord] = []
    for rec in sorted(records, key=lambda r: len(r.label)):
        sig = _label_signature(rec.label)
        if sig in seen:
            continue
        seen.add(sig)
        picked.append(rec)
        if len(picked) >= n:
            break
    if len(picked) < n:  # complète avec des doublons de signature si besoin
        for rec in records:
            if rec not in picked:
                picked.append(rec)
            if len(picked) >= n:
                break
    return picked


def format_examples(records: Iterable[PaymentRecord]) -> str:
    lines = []
    for rec in records:
        nums = ", ".join(sorted(rec.expected))
        lines.append(f'- libellé: "{rec.label}"  ->  [{nums}]')
    return "\n".join(lines)


# =============================================================================
# 11. PIPELINE
# =============================================================================


@dataclass
class RegexDiscoveryResult:
    per_contract_df: pd.DataFrame
    global_df: pd.DataFrame
    portfolio_df: pd.DataFrame
    portfolio_metrics: dict
    dataset_stats: dict
    diagnostics_df: pd.DataFrame
    cfg: PipelineConfig

    @property
    def portfolio(self) -> list[str]:
        return self.portfolio_df["pattern"].tolist() if len(self.portfolio_df) else []

    def apply(self, df: pd.DataFrame, patterns: Optional[list[str]] = None) -> pd.DataFrame:
        """Applique le portefeuille de regex à un dataframe de libellés.
        Retourne une ligne par (paiement, numéro candidat extrait)."""
        patterns = patterns or self.portfolio
        compiled = [(p, compile_regex(p, self.cfg)) for p in patterns]
        compiled = [(p, c) for p, c in compiled if c is not None]

        cols = [c for c in (COL_PAYMENT_ID, COL_PAYMENT_LABEL, COL_CONTRACT_ID) if c in df.columns]
        sub = df[cols].drop_duplicates()
        rows = []
        for row in sub.itertuples(index=False):
            label = str(getattr(row, COL_PAYMENT_LABEL, "") or "")
            for rank, (pat, cre) in enumerate(compiled):
                for val in extract_matches(cre, label, self.cfg):
                    rows.append(
                        {
                            **{c: getattr(row, c) for c in cols},
                            "regex_rank": rank,
                            "pattern": pat,
                            "extracted": val,
                            "extracted_norm": normalize_number(val, self.cfg),
                        }
                    )
        return pd.DataFrame(rows)


class RegexDiscoveryPipeline:
    def __init__(self, cfg: Optional[PipelineConfig] = None, llm: Optional[BaseLLM] = None):
        self.cfg = cfg or PipelineConfig()
        self.llm = llm
        if self.cfg.verbose:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    # ------------------------------------------------------------------ run
    def run(self, df: pd.DataFrame) -> RegexDiscoveryResult:
        cfg = self.cfg
        dataset, known_numbers = build_dataset(df, cfg)
        all_records = [r for recs in dataset.values() for r in recs]
        diagnostics_df = diagnose_locatability(dataset)
        weak = diagnostics_df[diagnostics_df["max_recall"] < 0.5]
        if len(weak):
            LOGGER.warning(
                "%d contrat(s) où <50%% des numéros apparaissent dans le libellé : "
                "aucune regex ne pourra les réconcilier (voir result.diagnostics_df).",
                len(weak),
            )
        LOGGER.info(
            "Dataset : %d contrats, %d paiements, %d numéros de facture distincts",
            len(dataset), len(all_records), len(known_numbers),
        )

        # ---- Étapes 1 à 5 : par contrat --------------------------------
        per_contract_rows: list[dict] = []
        validated: dict[str, RegexCandidate] = {}

        for i, (contract_id, records) in enumerate(
            sorted(dataset.items(), key=lambda kv: -len(kv[1])), start=1
        ):
            LOGGER.info("[%d/%d] Contrat %s (%d paiements)", i, len(dataset), contract_id, len(records))
            rows = self._process_contract(contract_id, records, known_numbers)
            per_contract_rows.extend(rows)
            for r in rows:
                if r["valid"]:
                    validated.setdefault(
                        r["pattern"],
                        RegexCandidate(pattern=r["pattern"], source=r["source"], contract_id=contract_id),
                    )

        per_contract_df = pd.DataFrame(per_contract_rows)

        # ---- Étape 6 : consolidation globale ---------------------------
        global_candidates = list(validated.values())
        global_candidates += self._llm_global_generalization(per_contract_df, all_records)

        global_scores = []
        for cand in {c.pattern: c for c in global_candidates}.values():
            sc = evaluate_pattern(cand.pattern, all_records, known_numbers, self.cfg)
            if sc is None:
                continue
            n_contracts = 0
            if len(per_contract_df):
                n_contracts = int(
                    per_contract_df.loc[
                        (per_contract_df["pattern"] == cand.pattern) & per_contract_df["valid"],
                        COL_CONTRACT_ID,
                    ].nunique()
                )
            d = sc.as_dict()
            d.update({"source": cand.source, "n_contracts_validated": n_contracts})
            global_scores.append((d, sc))

        global_df = pd.DataFrame([d for d, _ in global_scores])
        if len(global_df):
            global_df = global_df.sort_values(
                ["n_contracts_validated", "f1"], ascending=False
            ).reset_index(drop=True)

        # ---- Sélection gloutonne du portefeuille -----------------------
        portfolio_df, portfolio_metrics = self._greedy_selection(
            [sc for _, sc in global_scores], all_records, global_df
        )

        stats = {
            "n_contracts": len(dataset),
            "n_payments": len(all_records),
            "n_invoice_numbers": len(known_numbers),
            "n_patterns_tested": len(global_scores),
            "max_achievable_recall": round(
                float(diagnostics_df["n_locatable"].sum())
                / max(1, int(diagnostics_df["n_invoice_links"].sum())), 4
            ),
        }
        return RegexDiscoveryResult(
            per_contract_df=per_contract_df,
            global_df=global_df,
            portfolio_df=portfolio_df,
            portfolio_metrics=portfolio_metrics,
            dataset_stats=stats,
            diagnostics_df=diagnostics_df,
            cfg=self.cfg,
        )

    # ----------------------------------------------------- par contrat
    def _process_contract(
        self, contract_id: str, records: list[PaymentRecord], known_numbers: set[str]
    ) -> list[dict]:
        cfg = self.cfg
        seeds, observations = mine_heuristic_patterns(records, cfg)
        candidates: dict[str, RegexCandidate] = {c.pattern: c for c in seeds}

        examples = sample_examples(records, cfg.llm_examples_per_contract)
        for cand in self._llm_contract_generation(contract_id, examples, seeds, failures=None):
            candidates.setdefault(cand.pattern, cand)

        rows = self._score_candidates(contract_id, candidates.values(), records, known_numbers)

        # Round(s) de raffinement sur les paiements non couverts
        for _ in range(cfg.llm_refinement_rounds):
            if not self.llm:
                break
            covered = set()
            for r in rows:
                if r["valid"]:
                    covered |= r["_matched_ids"]
            failures = [r for r in records if r.payment_id not in covered]
            if not failures:
                break
            new_cands = self._llm_contract_generation(
                contract_id, examples, seeds,
                failures=sample_examples(failures, min(15, cfg.llm_examples_per_contract)),
            )
            new_cands = [c for c in new_cands if c.pattern not in candidates]
            if not new_cands:
                break
            for c in new_cands:
                candidates[c.pattern] = c
            rows += self._score_candidates(contract_id, new_cands, records, known_numbers)

        for r in rows:
            r.pop("_matched_ids", None)
        return rows

    def _score_candidates(
        self,
        contract_id: str,
        candidates: Iterable[RegexCandidate],
        records: list[PaymentRecord],
        known_numbers: set[str],
    ) -> list[dict]:
        cfg = self.cfg
        rows = []
        for cand in candidates:
            sc = evaluate_pattern(cand.pattern, records, known_numbers, cfg)
            if sc is None:
                rows.append(
                    {COL_CONTRACT_ID: contract_id, "pattern": cand.pattern,
                     "source": cand.source, "valid": False, "reject_reason": "invalid_or_trivial",
                     "_matched_ids": set()}
                )
                continue
            valid = sc.precision >= cfg.min_precision_contract and sc.recall >= cfg.min_recall_contract
            reason = ""
            if not valid:
                reason = "low_precision" if sc.precision < cfg.min_precision_contract else "low_recall"
            rows.append(
                {
                    COL_CONTRACT_ID: contract_id,
                    **sc.as_dict(),
                    "source": cand.source,
                    "valid": valid,
                    "reject_reason": reason,
                    "rationale": cand.rationale,
                    "_matched_ids": sc.matched_payment_ids,
                }
            )
        return rows

    # ------------------------------------------------------------ LLM
    def _llm_contract_generation(
        self,
        contract_id: str,
        examples: list[PaymentRecord],
        seeds: list[RegexCandidate],
        failures: Optional[list[PaymentRecord]],
    ) -> list[RegexCandidate]:
        if not self.llm:
            return []
        seeds_block = ""
        if seeds:
            seeds_block = (
                "PATTERNS DÉTECTÉS AUTOMATIQUEMENT (pistes, à corriger/améliorer) :\n"
                + "\n".join(f"- {s.pattern}" for s in seeds[:8])
                + "\n"
            )
        failures_block = ""
        if failures:
            failures_block = (
                "LIBELLÉS NON COUVERTS par les regex déjà validées, il faut les traiter :\n"
                + format_examples(failures)
                + "\n"
            )
        user = CONTRACT_USER_TEMPLATE.format(
            contract_id=contract_id,
            examples=format_examples(examples),
            seeds_block=seeds_block,
            failures_block=failures_block,
            max_regex=self.cfg.llm_max_regex_per_contract,
        )
        try:
            raw = self.llm.complete(SYSTEM_PROMPT, user)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Appel LLM échoué (contrat %s) : %s", contract_id, exc)
            return []
        source = "llm_refine" if failures else "llm"
        return [
            RegexCandidate(pattern=d["pattern"], source=source,
                           contract_id=contract_id, rationale=d.get("rationale", ""))
            for d in parse_llm_json(raw)
        ]

    def _llm_global_generalization(
        self, per_contract_df: pd.DataFrame, all_records: list[PaymentRecord]
    ) -> list[RegexCandidate]:
        if not self.llm or not len(per_contract_df):
            return []
        valid = per_contract_df[per_contract_df["valid"]]
        if not len(valid):
            return []
        agg = (
            valid.groupby("pattern")
            .agg(n_contracts=(COL_CONTRACT_ID, "nunique"),
                 precision=("precision", "mean"),
                 recall=("recall", "mean"))
            .sort_values("n_contracts", ascending=False)
            .head(self.cfg.llm_global_candidates)
            .reset_index()
        )
        patterns = "\n".join(
            f"- {r.pattern}  (contrats: {r.n_contracts}, precision moy: {r.precision:.2f}, "
            f"recall moy: {r.recall:.2f})"
            for r in agg.itertuples(index=False)
        )
        user = GLOBAL_USER_TEMPLATE.format(
            patterns=patterns,
            examples=format_examples(sample_examples(all_records, 30)),
            max_regex=self.cfg.max_portfolio_size,
        )
        try:
            raw = self.llm.complete(SYSTEM_PROMPT, user)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Appel LLM global échoué : %s", exc)
            return []
        return [
            RegexCandidate(pattern=d["pattern"], source="llm_global", rationale=d.get("rationale", ""))
            for d in parse_llm_json(raw)
        ]

    # -------------------------------------------------- sélection finale
    def _greedy_selection(
        self, scores: list[RegexScore], all_records: list[PaymentRecord], global_df: pd.DataFrame
    ) -> tuple[pd.DataFrame, dict]:
        cfg = self.cfg
        eligible = [s for s in scores if s.precision >= cfg.min_precision_global and s.tp > 0]
        total_payments = len(all_records)
        if not eligible or not total_payments:
            return pd.DataFrame(), {"payments_covered": 0, "coverage": 0.0}

        by_pattern = {s.pattern: s for s in eligible}
        n_contracts = {}
        if len(global_df):
            n_contracts = dict(zip(global_df["pattern"], global_df["n_contracts_validated"]))

        covered: set[str] = set()
        selected: list[dict] = []
        remaining = set(by_pattern)

        while remaining and len(selected) < cfg.max_portfolio_size:
            best_pat, best_gain = None, 0
            for pat in remaining:
                gain = len(by_pattern[pat].matched_payment_ids - covered)
                # départage : plus de contrats validés, puis meilleure précision
                if gain > best_gain or (
                    gain == best_gain and gain > 0 and best_pat is not None
                    and (n_contracts.get(pat, 0), by_pattern[pat].precision)
                    > (n_contracts.get(best_pat, 0), by_pattern[best_pat].precision)
                ):
                    best_pat, best_gain = pat, gain
            if best_pat is None or best_gain / total_payments < cfg.min_marginal_gain:
                break
            sc = by_pattern[best_pat]
            covered |= sc.matched_payment_ids
            remaining.discard(best_pat)
            selected.append(
                {
                    "rank": len(selected) + 1,
                    "pattern": best_pat,
                    "marginal_gain_payments": best_gain,
                    "marginal_gain_pct": round(best_gain / total_payments, 4),
                    "cumulative_coverage": round(len(covered) / total_payments, 4),
                    "precision": round(sc.precision, 4),
                    "recall": round(sc.recall, 4),
                    "n_contracts_validated": int(n_contracts.get(best_pat, 0)),
                }
            )

        metrics = {
            "payments_total": total_payments,
            "payments_covered": len(covered),
            "coverage": round(len(covered) / total_payments, 4),
            "portfolio_size": len(selected),
        }
        return pd.DataFrame(selected), metrics


# =============================================================================
# 12. DÉMO / TEST AUTONOME
# =============================================================================


def _demo_dataframe() -> pd.DataFrame:
    import random

    random.seed(0)
    rows = []
    templates = {
        "C001": ("VIR SEPA FACTURE {num} CLIENT DUPONT", "FA{y}{n:05d}"),
        "C002": ("PRLV {num} REGLEMENT MENSUEL", "{y}-INV-{n:04d}"),
        "C003": ("PAIEMENT REF {num} /SOLDE", "F{n:06d}"),
        "C004": ("VIREMENT {num} ET {num2} REGROUPES", "FACT{y}{n:04d}"),
    }
    pid = 0
    for contract, (tpl, numfmt) in templates.items():
        for i in range(1, 30):
            pid += 1
            num = numfmt.format(y=2024, n=i)
            if "{num2}" in tpl:
                num2 = numfmt.format(y=2024, n=i + 100)
                label = tpl.format(num=num, num2=num2)
                nums = [num, num2]
            else:
                label = tpl.format(num=num)
                nums = [num]
            for n in nums:
                rows.append(
                    {COL_CONTRACT_ID: contract, COL_PAYMENT_ID: f"P{pid:05d}",
                     COL_PAYMENT_LABEL: label, COL_INVOICE_NUMBER: n}
                )
    return pd.DataFrame(rows)


if __name__ == "__main__":
    df_demo = _demo_dataframe()
    # llm=AnthropicLLM() pour activer la génération LLM
    result = RegexDiscoveryPipeline(PipelineConfig(verbose=True)).run(df_demo)

    print("\n=== STATS ===")
    print(result.dataset_stats)
    print("\n=== DIAGNOSTIC (plafond de recall par contrat) ===")
    print(result.diagnostics_df.to_string(index=False))
    print("\n=== REGEX VALIDÉES PAR CONTRAT (top) ===")
    cols = [COL_CONTRACT_ID, "pattern", "precision", "recall", "coverage", "source", "valid"]
    print(result.per_contract_df[result.per_contract_df["valid"]][cols].head(15).to_string(index=False))
    print("\n=== PORTEFEUILLE GLOBAL ===")
    print(result.portfolio_df.to_string(index=False))
    print("\n=== MÉTRIQUES PORTEFEUILLE ===")
    print(result.portfolio_metrics)
    print("\n=== EXTRACTION ===")
    print(result.apply(df_demo).head(10).to_string(index=False))
