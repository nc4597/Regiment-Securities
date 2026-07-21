import re
import pandas as pd

def term_pattern(term):
    """
    Match a keyword as a word, not as letters inside another word.

    Plain substring matching made "ach" fire on "each" and "attached", which
    put clean statements in front of the principal. The left boundary is
    always required; the right side is left open so "wire" still catches
    "wired"/"wiring" and "journal" still catches "journaled". Short acronyms
    need both boundaries, otherwise "oba" matches inside "global".
    """
    escaped = re.escape(term)
    if len(term) <= 3:
        return rf"\b{escaped}\b"
    return rf"\b{escaped}"

def find_terms(text, terms):
    """Return the terms that genuinely appear, in list order."""
    text = text or ""
    return [
        term for term in terms
        if re.search(term_pattern(term), text, flags=re.IGNORECASE)
    ]

def contains_any(text, terms):
    return bool(find_terms(text, terms))

def describe_matches(text, terms, limit=3):
    """
    Say what tripped the flag and show the statement text around it once, so
    the principal can triage from the report instead of reopening the PDF.

    Matched terms usually sit in the same sentence, so one excerpt covers
    them all -- quoting each term separately just repeated the same line.
    """
    if not terms:
        return ""

    named = ", ".join(f'"{term}"' for term in terms[:limit])
    remaining = len(terms) - limit
    if remaining > 0:
        named += f" (+{remaining} more)"

    excerpt = evidence_excerpt(text, terms[0], context_chars=60)
    return f"{named} - {excerpt}" if excerpt else named

def evidence_excerpt(text, term, context_chars=90):
    if not text or not term:
        return ""

    match = re.search(term_pattern(term), text, flags=re.IGNORECASE)
    if not match:
        return ""

    start = max(0, match.start() - context_chars)
    end = min(len(text), match.end() + context_chars)
    excerpt = re.sub(r"\s+", " ", text[start:end]).strip()

    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."

    return excerpt

def symbol_found(text, symbol):
    symbol = str(symbol).upper().strip()
    if not symbol:
        return False

    text = (text or "").upper()
    pattern = rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])"
    return re.search(pattern, text) is not None

def name_found(text, stock_name):
    stock_name = str(stock_name).strip()
    if len(stock_name) < 4:
        return False
    return stock_name.lower() in (text or "").lower()

def format_stock_reference(symbol, stock_name):
    symbol = str(symbol or "").upper().strip()
    stock_name = str(stock_name or "").strip()

    if symbol and stock_name:
        return f"{stock_name} ({symbol})"
    if stock_name:
        return stock_name
    return symbol

def flag_restricted_list(account_text, restricted_df):
    """
    Broad restricted-list check.
    Scans the full account text so restricted holdings are not missed.
    """
    flags = []

    if restricted_df is None or restricted_df.empty:
        return flags

    for _, row in restricted_df.iterrows():
        symbol = str(row.get("stock_symbol", "")).upper().strip()
        stock_name = str(row.get("stock_name", "")).strip()

        matched_symbol = symbol_found(account_text, symbol)
        matched_name = name_found(account_text, stock_name)

        if matched_symbol or matched_name:
            stock_reference = format_stock_reference(symbol, stock_name)
            flags.append({
                "flag": "Restricted Security Review",
                "severity": "High",
                "reason": stock_reference,
            })
            flags.append({
                "flag": "Insider Trading Review",
                "severity": "High",
                "reason": stock_reference,
            })

    return flags


def flag_ipo_restrictions(account_text, ipo_df):
    flags = []

    if ipo_df is None or ipo_df.empty:
        return flags

    for _, row in ipo_df.iterrows():
        symbol = str(row.get("stock_symbol", "")).upper().strip()
        company_name = str(row.get("stock_name", "")).strip()

        matched_symbol = symbol_found(account_text, symbol)
        matched_name = name_found(account_text, company_name)

        if matched_symbol or matched_name:
            stock_reference = format_stock_reference(symbol, company_name)
            match_type = []
            if matched_symbol:
                match_type.append(f"symbol {symbol}")
            if matched_name:
                match_type.append("company name")
            flags.append({
                "flag": "IPO RESTRICTION",
                "severity": "High",
                "reason": (
                    f"IPO restriction detected: {stock_reference}; "
                    f"matched on {', '.join(match_type)}."
                ),
            })

    return flags

def flag_conflicts_of_interest(account_text, restricted_df):
    return flag_restricted_list(account_text, restricted_df)

def flag_money_movement(account_text):
    flags = []
    # Matching is case-insensitive now, so keep the original casing -- the
    # evidence excerpts go in front of a human.
    text = account_text or ""

    transfer_terms = [
        "security transfer",
        "securities transfer",
        "transfer of securities",
        "asset transfer",
        "journal entry",
        "journal",
        "wire",
        "ach",
        "external account",
        "third party",
        "third-party",
        "customer account",
        "from account",
        "to account",
        "transfer in",
        "transfer out",
    ]

    receipt_terms = [
        "incoming wire",
        "incoming ach",
        "funds received",
        "received from",
        "receipt from",
        "deposit from",
        "transfer in",
        "third party deposit",
        "third-party deposit",
    ]

    disbursement_terms = [
        "outgoing wire",
        "ach debit",
        "wire sent",
        "sent to",
        "disbursement to",
        "transfer out",
        "third party payment",
        "third-party payment",
        "check issued",
    ]

    for flag_name, terms in (
        ("Transfers of Money or Securities Review", transfer_terms),
        ("Cash Receipts Review", receipt_terms),
        ("Cash Disbursements Review", disbursement_terms),
    ):
        matched = find_terms(text, terms)
        if matched:
            flags.append({
                "flag": flag_name,
                "severity": "Medium",
                "reason": describe_matches(text, matched),
            })

    return flags

def matched_stock_references(account_text, securities_df):
    references = []

    if securities_df is None or securities_df.empty:
        return references

    seen = set()
    for _, row in securities_df.iterrows():
        symbol = str(row.get("stock_symbol", "")).upper().strip()
        stock_name = str(row.get("stock_name", "")).strip()

        if not (symbol_found(account_text, symbol) or name_found(account_text, stock_name)):
            continue

        stock_reference = format_stock_reference(symbol, stock_name)
        if stock_reference and stock_reference not in seen:
            references.append(stock_reference)
            seen.add(stock_reference)

    return references

def flag_restricted_transactions(account_text, restricted_df=None):
    text = account_text or ""
    terms = [
        "restricted transaction",
        "restricted security",
        "restricted stock",
        "private placement",
        "unregistered security",
        "144 stock",
        "rule 144",
        "legend removal",
        "control stock",
        "penny stock",
    ]
    matched_terms = find_terms(text, terms)

    if not matched_terms:
        return []

    stock_references = matched_stock_references(account_text, restricted_df)
    if not stock_references:
        return []

    reason = f"{'; '.join(stock_references)} ({describe_matches(text, matched_terms, limit=2)})"

    return [{
        "flag": "Restricted Security Review",
        "severity": "High",
        "reason": reason,
    }, {
        "flag": "Insider Trading Review",
        "severity": "High",
        "reason": reason,
    }]

def flag_conflicts_keywords(account_text):
    text = account_text or ""
    terms = [
        "conflict of interest",
        "outside business activity",
        "oba",
        "selling away",
        "referral fee",
        "compensation arrangement",
        "finder's fee",
        "finder fee",
        "personal interest",
        "related party",
    ]
    matched = find_terms(text, terms)
    if not matched:
        return []
    return [{
        "flag": "Conflicts of Interest Review",
        "severity": "High",
        "reason": describe_matches(text, matched),
    }]

def flag_current_month_categories(account_text, restricted_df=None):
    flags = []
    flags.extend(flag_money_movement(account_text))
    flags.extend(flag_restricted_transactions(account_text, restricted_df))
    flags.extend(flag_conflicts_keywords(account_text))
    return flags

def combine_flags_by_violation(flags):
    """
    One row per account + violation type.
    """
    grouped = {}

    for flag in flags:
        name = flag.get("flag", "Review Flag")
        if name not in grouped:
            grouped[name] = {
                "flag": name,
                "severity": flag.get("severity", "Medium"),
                "count": 0,
                "reasons": set(),
            }
        grouped[name]["count"] += 1
        grouped[name]["reasons"].add(flag.get("reason", ""))
        if flag.get("severity") == "High":
            grouped[name]["severity"] = "High"

    output = []
    for item in grouped.values():
        output.append({
            "flag": item["flag"],
            "severity": item["severity"],
            "count": item["count"],
            "reason": " ".join(sorted(r for r in item["reasons"] if r)),
        })

    return output

def review_account(account_text, restricted_df, ipo_df=None):
    flags = []
    flags.extend(flag_restricted_list(account_text, restricted_df))
    flags.extend(flag_ipo_restrictions(account_text, ipo_df))
    flags.extend(flag_current_month_categories(account_text, restricted_df))
    return flags
