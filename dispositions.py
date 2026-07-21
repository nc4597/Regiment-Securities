"""
Statement-level dispositions for the WSP "Supervision of Outside Accounts".

The rest of the tool works at account granularity: one PDF can split into
several rows when it contains several account numbers. The WSP counts
*statements*, and requires every statement in the monthly population to carry
exactly one of three dispositions. So this module runs its own pass over the
month directory, one row per PDF, and rolls the account-level flags up.

The pass is deliberately independent of review_month_directory() and
extract_month_metrics(). Those two skip anything they cannot parse, which is
what previously let an unreadable statement disappear from the workbook or --
worse -- come out looking clean. Here an unreadable statement is an affirmative
"unable to process" that the Designated Principal has to review individually.
"""

import math
import random
from datetime import datetime
from pathlib import Path

import pandas as pd

from pdf_utils import extract_account_number_from_text, extract_pages_from_pdf
from utils import (
    iter_pdf_paths,
    move_leading_account_number_from_name,
    resolve_rep_name_for_pdf,
)

FLAGGED = "Flagged for principal review"
NO_EXCEPTION = "Screened - no exception"
UNABLE = "Unable to process"

DISPOSITION_ORDER = [FLAGGED, UNABLE, NO_EXCEPTION]

# A text-bearing brokerage statement page runs to thousands of characters.
# A scanned/image-only page extracts near zero, so anything this sparse is
# treated as unreadable rather than as a statement with nothing on it.
MIN_CHARS_PER_PAGE = 100

DISPOSITION_COLUMNS = [
    "Name",
    "Statement File",
    "Account Numbers",
    "Pages",
    "Disposition",
    "Exceptions",
    "Reason",
]

QA_SAMPLE_MINIMUM = 15
QA_SAMPLE_PERCENT = 10


def assess_statement(pdf_path: Path) -> dict:
    """
    Decide whether a single statement PDF could actually be screened.

    Returns the account numbers and page count too, so the caller does not
    have to re-open the PDF to describe it.
    """
    try:
        pages = extract_pages_from_pdf(pdf_path)
    except Exception as exc:
        return {
            "processable": False,
            "reason": f"PDF could not be read: {exc}",
            "account_numbers": [],
            "page_count": 0,
        }

    if not pages:
        return {
            "processable": False,
            "reason": "PDF could not be opened or contains no pages.",
            "account_numbers": [],
            "page_count": 0,
        }

    account_numbers = []
    total_chars = 0
    for page in pages:
        text = (page.get("text") or "").strip()
        total_chars += len(text)
        account_number = extract_account_number_from_text(text)
        if account_number and account_number not in account_numbers:
            account_numbers.append(account_number)

    page_count = len(pages)
    chars_per_page = total_chars / page_count

    if chars_per_page < MIN_CHARS_PER_PAGE:
        return {
            "processable": False,
            "reason": (
                f"No extractable text ({total_chars} characters across {page_count} "
                f"page(s)); likely a scanned or image-only statement requiring OCR."
            ),
            "account_numbers": account_numbers,
            "page_count": page_count,
        }

    # A missing account number does NOT make a statement unprocessable. The
    # text was read and every keyword criterion ran against it. What it does
    # break is the month-over-month comparison, which matches on account
    # number -- so it is reported as a note against a screened statement
    # rather than pretending the statement was never screened.
    note = ""
    if not account_numbers:
        note = (
            "Account number not recognised, so month-over-month comparison "
            "(excessive trading, excessive losses, cash-movement increases) "
            "could not run for this statement."
        )

    return {
        "processable": True,
        "reason": "",
        "note": note,
        "account_numbers": account_numbers,
        "page_count": page_count,
    }


def _violations_by_statement(current_results_df, comparison_flags_df, current_metrics_df) -> dict:
    """
    Map statement filename -> set of violation names.

    current_results_df already carries the originating "PDF File".
    comparison_flags_df does not -- it is keyed on (Name, Account Number) --
    so current_metrics_df supplies that lookup.

    Filenames are not guaranteed unique across rep folders. Where two reps
    happen to use the same filename, both statements inherit the union of the
    flags. That over-flags rather than under-flags, which is the safe
    direction here: a statement wrongly in bucket (i) still gets individual
    principal review, one wrongly in bucket (ii) does not.
    """
    violations = {}

    def add(file_name, violation, reason=""):
        file_name = str(file_name or "").strip()
        violation = str(violation or "").strip()
        reason = str(reason or "").strip()
        if not (file_name and violation):
            return
        entry = f"{violation}: {reason}" if reason else violation
        violations.setdefault(file_name, set()).add(entry)

    if current_results_df is not None and not current_results_df.empty:
        for _, row in current_results_df.iterrows():
            add(row.get("PDF File"), row.get("Violation"), row.get("Reason"))

    account_to_file = {}
    if current_metrics_df is not None and not current_metrics_df.empty:
        for _, row in current_metrics_df.iterrows():
            key = (
                str(row.get("Name", "") or "").strip(),
                str(row.get("Account Number", "") or "").strip(),
            )
            account_to_file[key] = str(row.get("PDF File", "") or "").strip()

    if comparison_flags_df is not None and not comparison_flags_df.empty:
        for _, row in comparison_flags_df.iterrows():
            key = (
                str(row.get("Name", "") or "").strip(),
                str(row.get("Account Number", "") or "").strip(),
            )
            add(account_to_file.get(key), row.get("Violation"), row.get("Reason"))

    return violations


def build_statement_dispositions(
    month_dir: Path,
    current_results_df=None,
    comparison_flags_df=None,
    current_metrics_df=None,
    undated_pdfs=None,
    undated_root: Path = None,
) -> pd.DataFrame:
    """
    One row per statement in the monthly population, each with exactly one
    disposition.

    undated_pdfs are statements that were downloaded but whose statement month
    could not be detected, so they never reached the current/prior split. They
    are part of the population and cannot be screened, so they land in
    "unable to process" rather than vanishing.
    """
    violations = _violations_by_statement(
        current_results_df,
        comparison_flags_df,
        current_metrics_df,
    )

    rows = []

    for pdf_path in iter_pdf_paths(month_dir):
        rep_name = resolve_rep_name_for_pdf(pdf_path, month_dir)
        assessment = assess_statement(pdf_path)
        account_numbers = assessment["account_numbers"]

        display_name, _ = move_leading_account_number_from_name(
            rep_name,
            account_numbers[0] if account_numbers else "Unknown",
        )

        statement_violations = sorted(violations.get(pdf_path.name, set()))

        if not assessment["processable"]:
            disposition = UNABLE
            reason = assessment["reason"]
        elif statement_violations:
            disposition = FLAGGED
            reason = assessment.get("note", "")
        else:
            disposition = NO_EXCEPTION
            reason = assessment.get("note", "")

        rows.append({
            "Name": display_name,
            "Statement File": pdf_path.name,
            "Account Numbers": ", ".join(account_numbers),
            "Pages": assessment["page_count"],
            "Disposition": disposition,
            "Exceptions": "\n".join(statement_violations),
            "Reason": reason,
        })

    screened_files = {row["Statement File"] for row in rows}

    for pdf_path in undated_pdfs or []:
        pdf_path = Path(pdf_path)
        if pdf_path.name in screened_files:
            # Already screened under a detected month; listing it again would
            # double-count the population.
            continue
        try:
            rep_name = resolve_rep_name_for_pdf(pdf_path, undated_root or pdf_path.parent)
        except Exception:
            rep_name = ""

        rows.append({
            "Name": rep_name,
            "Statement File": pdf_path.name,
            "Account Numbers": "",
            "Pages": "",
            "Disposition": UNABLE,
            "Exceptions": "",
            "Reason": "Statement month could not be detected, so the statement was not screened against a month.",
        })

    disposition_df = pd.DataFrame(rows, columns=DISPOSITION_COLUMNS)

    if not disposition_df.empty:
        disposition_df = disposition_df.sort_values(
            by=["Disposition", "Name", "Statement File"],
            key=lambda column: (
                column.map(lambda v: DISPOSITION_ORDER.index(v) if v in DISPOSITION_ORDER else 99)
                if column.name == "Disposition" else column
            ),
        ).reset_index(drop=True)

    return disposition_df


def disposition_counts(disposition_df) -> dict:
    counts = {name: 0 for name in DISPOSITION_ORDER}
    if disposition_df is not None and not disposition_df.empty:
        for name, count in disposition_df["Disposition"].value_counts().items():
            counts[str(name)] = int(count)
    counts["Total statements"] = sum(counts[name] for name in DISPOSITION_ORDER)

    # Screened, but not comparable month over month. Worth its own line: a
    # large number here means the trading and loss criteria were effectively
    # inactive for that share of the population.
    if disposition_df is not None and not disposition_df.empty:
        no_account = int(
            (
                (disposition_df["Disposition"] != UNABLE)
                & (disposition_df["Account Numbers"].astype(str).str.strip() == "")
            ).sum()
        )
        if no_account:
            counts["Screened but not comparable (no account number)"] = no_account

    return counts


def screening_inputs_summary(restricted_df=None, ipo_df=None) -> dict:
    """
    Record what the statements were actually screened *against*.

    An empty restricted or IPO list produces a clean run that looks identical
    to a genuinely clean month, so the row counts belong on the sign-off sheet
    rather than buried in diagnostics.
    """
    restricted_count = 0 if restricted_df is None else len(restricted_df)
    ipo_count = 0 if ipo_df is None else len(ipo_df)

    summary = {
        "Restricted list entries screened against": restricted_count,
        "IPO list entries screened against": ipo_count,
    }

    empty_lists = [
        label
        for label, count in (("restricted", restricted_count), ("IPO", ipo_count))
        if not count
    ]
    if empty_lists:
        summary["WARNING"] = (
            f"The {' and '.join(empty_lists)} list loaded with zero entries. "
            "Those screening criteria did not run; statements below are not "
            "evidence of screening against them."
        )

    return summary


def qa_sample_size(population_size: int, minimum: int = QA_SAMPLE_MINIMUM, percent: int = QA_SAMPLE_PERCENT) -> int:
    """
    WSP: "the greater of 10% or 15 statements". Where the no-exception
    population is smaller than 15, the whole population is reviewed.
    """
    if population_size <= 0:
        return 0
    target = max(minimum, math.ceil(population_size * percent / 100))
    return min(target, population_size)


def select_qa_sample(disposition_df, seed=None, minimum: int = QA_SAMPLE_MINIMUM, percent: int = QA_SAMPLE_PERCENT):
    """
    Draw the monthly QA sample from the "screened with no exception" bucket.

    The seed is recorded alongside the sample so the draw is reproducible and
    can be shown to have been made without selection -- a random sample nobody
    can re-derive is not evidence of anything.
    """
    if disposition_df is None or disposition_df.empty:
        population = pd.DataFrame(columns=DISPOSITION_COLUMNS)
    else:
        population = disposition_df[disposition_df["Disposition"] == NO_EXCEPTION]

    population_size = len(population)
    sample_size = qa_sample_size(population_size, minimum=minimum, percent=percent)

    if seed is None:
        seed = random.SystemRandom().getrandbits(32)

    if sample_size:
        chosen = random.Random(seed).sample(list(population.index), sample_size)
        sample_df = population.loc[sorted(chosen)].reset_index(drop=True)
    else:
        sample_df = population.head(0).reset_index(drop=True)

    metadata = {
        "Sampled from": NO_EXCEPTION,
        "Population size": population_size,
        "Required sample": f"greater of {minimum} or {percent}% ({sample_size} selected)",
        "Sample size": sample_size,
        "Random seed": seed,
        "Selected at (UTC)": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "Method": (
            f"random.Random(seed).sample over the {population_size} statement(s) "
            f"dispositioned '{NO_EXCEPTION}', sorted by Name then Statement File."
        ),
    }

    return sample_df, metadata
