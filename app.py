import re
from pathlib import Path
from io import BytesIO

import fitz  # PyMuPDF
import pandas as pd

OUTPUTS_DIR = Path("outputs")
OUTPUTS_DIR.mkdir(exist_ok=True)

RESTRICTED_LIST_PATH = Path("restricted_list_prior_month.csv")

def format_scan_summary(current_evidence, comparison_evidence):
    """
    Build a compact text summary.
    - current_evidence: iterable of dicts or strings (current-month flags)
    - comparison_evidence: iterable of dicts or strings (prior vs current flags)
    """
    def keys_from(evidence):
        if not evidence:
            return set()
        keys = set()
        for e in evidence:
            if isinstance(e, dict):
                k = e.get("violation") or e.get("flag") or ""
            else:
                k = str(e)
            keys.add(k.lower())
        return keys

    def reasons_from(evidence, predicate):
        if not evidence:
            return []

        reasons = []
        for e in evidence:
            if not isinstance(e, dict):
                continue

            key = (e.get("violation") or e.get("flag") or "").lower()
            reason = str(e.get("reason") or "").strip()

            if reason and predicate(key):
                reasons.append(reason)

        return sorted(set(reasons))

    cur_keys = keys_from(current_evidence)
    cmp_keys = keys_from(comparison_evidence)

    # Current-month categories
    cur_lines = []
    restricted_reasons = reasons_from(
        current_evidence,
        lambda k: "restricted security" in k,
    )
    if restricted_reasons:
        cur_lines.append("Restricted security: " + "; ".join(restricted_reasons))
    elif any(k for k in cur_keys if "restricted security" in k):
        cur_lines.append("Restricted security")

    if any(k for k in cur_keys if "conflict" in k and "restricted security" not in k):
        cur_lines.append("Conflicts of interest")

    # Prior vs current categories (ordered)
    cmp_lines = []
    if any(k for k in cmp_keys if "money movement" in k):
        cmp_lines.append("Money movement activity")
    if any(k for k in cmp_keys if "cash" in k or "expend" in k):
        cmp_lines.append("Cash expenditures")
    if any(k for k in cmp_keys if "loss" in k or "losses" in k):
        cmp_lines.append("Excessive losses")
    if any(k for k in cmp_keys if "trade" in k or "trading" in k or "excessive_trading" in k):
        cmp_lines.append("Excessive trading")

    # Build final text
    parts = []
    parts.append("Current-month scan:")
    if cur_lines:
        parts.extend(cur_lines)
    else:
        parts.append("None")

    parts.append("")  # blank line
    parts.append("Prior vs current comparison:")
    if cmp_lines:
        parts.extend(cmp_lines)
    else:
        parts.append("None")

    return "\n".join(parts)
