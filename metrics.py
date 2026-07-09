import re
import pandas as pd
from pathlib import Path

METRIC_COLUMNS = [
    "Name",
    "PDF File",
    "Account Number",
    "Cash Expenditures",
    "Transfer Amount",
    "Cash Receipts",
    "Cash Disbursements",
    "Money Movement Amount",
    "Trade Count",
    "Trade Volume",
    "Loss Amount",
]

def parse_dollar_amounts(line: str):
    amounts = []
    matches = re.findall(r"\(?\$[\d,]+\.\d{2}\)?", line)
    for match in matches:
        cleaned = match.replace("$", "").replace(",", "")
        is_negative = cleaned.startswith("(") and cleaned.endswith(")")
        cleaned = cleaned.replace("(", "").replace(")", "")
        try:
            value = float(cleaned)
            if is_negative:
                value = -value
            amounts.append(value)
        except ValueError:
            pass
    return amounts

def extract_account_value_change(account_text: str):
    beginning = None
    ending = None
    begin_match = re.search(r"Beginning Total Value.*?\$([\d,]+\.\d{2})", account_text, flags=re.IGNORECASE | re.DOTALL)
    end_match = re.search(r"Ending Total Value.*?\$([\d,]+\.\d{2})", account_text, flags=re.IGNORECASE | re.DOTALL)
    if begin_match:
        beginning = float(begin_match.group(1).replace(",", ""))
    if end_match:
        ending = float(end_match.group(1).replace(",", ""))
    if beginning is None or ending is None:
        return 0
    decline = beginning - ending
    return max(decline, 0)

def calculate_cash_expenditures(account_text: str):
    outgoing_keywords = [
        "debit","debits","withdrawal","cash disbursement","outgoing","wire sent","ach debit","check","fee","fees","transfer out","to account"
    ]
    total = 0
    for line in account_text.splitlines():
        clean = " ".join(line.split())
        lower = clean.lower()
        if any(keyword in lower for keyword in outgoing_keywords):
            for amount in parse_dollar_amounts(clean):
                total += abs(amount)
    return total

def calculate_keyword_amount(account_text: str, keywords):
    total = 0
    for line in account_text.splitlines():
        clean = " ".join(line.split())
        lower = clean.lower()
        if any(keyword in lower for keyword in keywords):
            for amount in parse_dollar_amounts(clean):
                total += abs(amount)
    return total

def calculate_money_movement_activity(account_text: str):
    transfer_keywords = [
        "transfer", "security transfer", "securities transfer", "transfer of securities",
        "asset transfer", "journal entry", "wire", "ach", "journal", "external account",
        "from account", "to account", "third party", "third-party", "customer account",
    ]
    receipt_keywords = [
        "deposit", "credit", "cash receipt", "incoming", "incoming wire", "incoming ach",
        "funds transferred", "received from", "receipt from",
    ]
    disbursement_keywords = [
        "withdrawal", "debit", "cash disbursement", "outgoing", "outgoing wire",
        "ach debit", "wire sent", "check", "transfer out", "sent to", "disbursement to",
    ]

    transfer_amount = calculate_keyword_amount(account_text, transfer_keywords)
    receipt_amount = calculate_keyword_amount(account_text, receipt_keywords)
    disbursement_amount = calculate_keyword_amount(account_text, disbursement_keywords)
    return transfer_amount, receipt_amount, disbursement_amount

def calculate_trading_activity(account_text: str):
    trade_patterns = [
        r"\bbuy\b",
        r"\bbought\b",
        r"\bpurchase(?:d|s)?\b",
        r"\bsell\b",
        r"\bsold\b",
        r"\bsale(?:s)?\b",
        r"\bredemption(?:s)?\b",
        r"\bredeemed\b",
    ]
    non_trade_context = [
        "cost basis",
        "disclosure",
        "explanation",
        "for more information",
        "gain or loss",
        "if you sell",
        "net of assets",
        "registered/unrestricted",
        "secondary market",
        "statement of",
        "tax information",
        "typically use the value",
        "wash sale",
    ]
    count = 0
    volume = 0
    for line in account_text.splitlines():
        clean = " ".join(line.split())
        lower = clean.lower()
        amounts = parse_dollar_amounts(clean)

        if not amounts:
            continue

        if any(context in lower for context in non_trade_context):
            continue

        if not any(re.search(pattern, lower) for pattern in trade_patterns):
            continue

        count += 1
        for amount in amounts:
            volume += abs(amount)

    return count, volume

def calculate_loss_activity(account_text: str):
    loss_keywords = ["short-term (loss)","long-term (loss)","realized loss","unrealized loss","net loss","disallowed loss"]
    loss_amount = 0
    for line in account_text.splitlines():
        clean = " ".join(line.split())
        lower = clean.lower()
        if any(keyword in lower for keyword in loss_keywords):
            for amount in parse_dollar_amounts(clean):
                loss_amount += abs(amount)
    account_decline = extract_account_value_change(account_text)
    return max(loss_amount, account_decline)

def extract_month_metrics(month_dir: Path):
    rows = []
    from utils import iter_pdf_paths
    from utils import move_leading_account_number_from_name
    from utils import resolve_rep_name_for_pdf
    from pdf_utils import split_pdf_into_accounts
    for pdf_path in iter_pdf_paths(month_dir):
        rep_name = resolve_rep_name_for_pdf(pdf_path, month_dir)
        try:
            accounts = split_pdf_into_accounts(pdf_path)
        except Exception:
            accounts = []

        if not accounts:
            row_name, row_account_number = move_leading_account_number_from_name(
                rep_name,
                "Unknown",
            )
            rows.append({
                "Name": row_name,
                "PDF File": pdf_path.name,
                "Account Number": row_account_number,
                "Cash Expenditures": 0,
                "Transfer Amount": 0,
                "Cash Receipts": 0,
                "Cash Disbursements": 0,
                "Money Movement Amount": 0,
                "Trade Count": 0,
                "Trade Volume": 0,
                "Loss Amount": 0,
            })
            continue

        for account in accounts:
            account_number = account["account_number"]
            row_name, row_account_number = move_leading_account_number_from_name(
                rep_name,
                account_number,
            )
            account_text = account["text"]
            trade_count, trade_volume = calculate_trading_activity(account_text)
            cash_expenditures = calculate_cash_expenditures(account_text)
            transfer_amount, receipt_amount, disbursement_amount = calculate_money_movement_activity(account_text)
            loss_amount = calculate_loss_activity(account_text)
            rows.append({
                "Name": row_name,
                "PDF File": pdf_path.name,
                "Account Number": row_account_number,
                "Cash Expenditures": cash_expenditures,
                "Transfer Amount": transfer_amount,
                "Cash Receipts": receipt_amount,
                "Cash Disbursements": disbursement_amount,
                "Money Movement Amount": transfer_amount + receipt_amount + disbursement_amount,
                "Trade Count": trade_count,
                "Trade Volume": trade_volume,
                "Loss Amount": loss_amount,
            })
    return pd.DataFrame(rows, columns=METRIC_COLUMNS)

def pct_change(current, prior):
    current = number_or_zero(current)
    prior = number_or_zero(prior)

    if prior == 0:
        if current == 0:
            return 0
        return None

    return (current - prior) / abs(prior)

def is_25pct_higher(current, prior, minimum=0):
    return is_pct_higher(current, prior, 25, minimum)

def is_pct_higher(current, prior, threshold_pct, minimum=0):
    current = number_or_zero(current)
    prior = number_or_zero(prior)
    threshold_pct = max(number_or_zero(threshold_pct), 0)

    if current < minimum:
        return False

    if prior == 0:
        return current >= minimum

    return current >= prior * (1 + threshold_pct / 100)

def number_or_zero(value):
    if pd.isna(value):
        return 0
    return value or 0

def fmt_change(change):
    if change is None:
        return "new activity vs. no prior-month activity"
    return f"{change:.1%} increase"

def flag_month_over_month_changes(current_df, prior_df, excessive_trading_pct=25):
    rows = []

    if current_df.empty:
        return pd.DataFrame(rows)

    current_df = current_df.reindex(columns=METRIC_COLUMNS)
    prior_df = prior_df.reindex(columns=METRIC_COLUMNS)

    merged = current_df.merge(
        prior_df,
        on="Account Number",
        how="left",
        suffixes=("_Current", "_Prior"),
    )

    for _, row in merged.iterrows():
        name = row.get("Name_Current", "")
        pdf_file = row.get("PDF File_Current", "")
        account = row.get("Account Number", "")

        current_cash = row.get("Cash Expenditures_Current", 0) or 0
        prior_cash = row.get("Cash Expenditures_Prior", 0) or 0

        current_transfer = row.get("Transfer Amount_Current", 0) or 0
        prior_transfer = row.get("Transfer Amount_Prior", 0) or 0

        current_receipts = row.get("Cash Receipts_Current", 0) or 0
        prior_receipts = row.get("Cash Receipts_Prior", 0) or 0

        current_disbursements = row.get("Cash Disbursements_Current", 0) or 0
        prior_disbursements = row.get("Cash Disbursements_Prior", 0) or 0

        current_money_movement = row.get("Money Movement Amount_Current", 0) or 0
        prior_money_movement = row.get("Money Movement Amount_Prior", 0) or 0

        current_loss = row.get("Loss Amount_Current", 0) or 0
        prior_loss = row.get("Loss Amount_Prior", 0) or 0

        current_trades = row.get("Trade Count_Current", 0) or 0
        prior_trades = row.get("Trade Count_Prior", 0) or 0

        current_volume = row.get("Trade Volume_Current", 0) or 0
        prior_volume = row.get("Trade Volume_Prior", 0) or 0

        if is_25pct_higher(current_cash, prior_cash, minimum=1000):
            change = pct_change(current_cash, prior_cash)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Cash Expenditures Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": f"Cash expenditures increased by {fmt_change(change)}.",
                "Pages Reviewed": "",
            })

        if is_25pct_higher(current_transfer, prior_transfer, minimum=1000):
            change = pct_change(current_transfer, prior_transfer)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Transfers of Money or Securities Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": f"Transfers of money or securities increased by {fmt_change(change)}.",
                "Pages Reviewed": "",
            })

        if is_25pct_higher(current_receipts, prior_receipts, minimum=1000):
            change = pct_change(current_receipts, prior_receipts)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Cash Receipts Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": f"Cash receipts increased by {fmt_change(change)}.",
                "Pages Reviewed": "",
            })

        if is_25pct_higher(current_disbursements, prior_disbursements, minimum=1000):
            change = pct_change(current_disbursements, prior_disbursements)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Cash Disbursements Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": f"Cash disbursements increased by {fmt_change(change)}.",
                "Pages Reviewed": "",
            })

        if is_25pct_higher(current_money_movement, prior_money_movement, minimum=1000):
            change = pct_change(current_money_movement, prior_money_movement)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Money Movement Activity Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": f"Total money-movement activity increased by {fmt_change(change)}.",
                "Pages Reviewed": "",
            })

        if is_25pct_higher(current_loss, prior_loss, minimum=1000):
            change = pct_change(current_loss, prior_loss)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Excessive Losses Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": f"Losses/account decline increased by {fmt_change(change)}.",
                "Pages Reviewed": "",
            })

        if is_pct_higher(current_trades, prior_trades, excessive_trading_pct, minimum=10):
            change = pct_change(current_trades, prior_trades)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Excessive Trading Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": (
                    f"Trade count increased by {fmt_change(change)} "
                    f"(threshold: {excessive_trading_pct}%)."
                ),
                "Pages Reviewed": "",
            })

        if is_pct_higher(current_volume, prior_volume, excessive_trading_pct, minimum=10000):
            change = pct_change(current_volume, prior_volume)
            rows.append({
                "Name": name,
                "PDF File": pdf_file,
                "Account Number": account,
                "Violation": "Excessive Trading Review",
                "Severity": "Medium",
                "Flag Count": 1,
                "Reason": (
                    f"Trade volume increased by {fmt_change(change)} "
                    f"(threshold: {excessive_trading_pct}%)."
                ),
                "Pages Reviewed": "",
            })

    return pd.DataFrame(rows)


def combine_duplicate_output_rows(df):
    if df.empty:
        return df

    grouped_rows = []

    for keys, group in df.groupby(["Name", "PDF File", "Account Number", "Violation"], dropna=False):
        name, pdf_file, account, violation = keys

        severity = "High" if "High" in set(group["Severity"]) else "Medium"
        count = int(group["Flag Count"].sum())
        reason = " ".join(sorted(set(group["Reason"].dropna().astype(str))))

        grouped_rows.append({
            "Name": name,
            "PDF File": pdf_file,
            "Account Number": account,
            "Violation": violation,
            "Severity": severity,
            "Flag Count": count,
            "Review Note": reason,
        })

    return pd.DataFrame(grouped_rows)
