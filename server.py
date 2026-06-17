from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse

import secrets
import os
import io
import hmac
import hashlib
import base64
from pathlib import Path
import pandas as pd
from typing import Optional
import tempfile
import zipfile
import traceback

from utils import (
    load_ipo_restriction_list,
    load_restricted_list,
    dataframe_to_excel_bytes,
    iter_pdf_paths,
    move_leading_account_number_from_name,
)
from pdf_utils import split_pdf_into_accounts, clean_rep_name_from_filename
from flags import review_account, combine_flags_by_violation
from metrics import extract_month_metrics, flag_month_over_month_changes


app = FastAPI(title="RSLLC Brokerage Reviewer")

ADMIN_USER = os.environ.get("ADMIN_USER", "RegimentSecurities")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "Regiment123")
SESSION_SECRET = os.environ.get("SESSION_SECRET", ADMIN_PASS)
SESSION_COOKIE = "rsllc_brokerage_session"


def create_session_token(username: str) -> str:
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    payload = f"{username}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("utf-8")


def verify_session_token(token: str) -> Optional[str]:
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        username, signature = decoded.rsplit(":", 1)
    except Exception:
        return None

    expected = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not secrets.compare_digest(signature, expected):
        return None

    if not secrets.compare_digest(username, ADMIN_USER):
        return None

    return username


def require_login(request: Request):
    if verify_session_token(request.cookies.get(SESSION_COOKIE, "")):
        return None

    return RedirectResponse("/login", status_code=303)


def login_page(error: str = "") -> str:
    error_html = f'<div class="error">{error}</div>' if error else ""
    return f"""
    <html>
        <head>
            <title>RSLLC Brokerage Reviewer</title>
            <style>
                * {{
                    box-sizing: border-box;
                }}
                body {{
                    margin: 0;
                    min-height: 100vh;
                    font-family: Arial, sans-serif;
                    background: #070708;
                    color: #f5f5f5;
                }}
                .topbar {{
                    height: 78px;
                    display: flex;
                    align-items: center;
                    padding: 0 30px;
                    background: #050505;
                    border-bottom: 2px solid #d71920;
                }}
                .brand {{
                    display: flex;
                    align-items: center;
                    gap: 14px;
                    font-size: 22px;
                    font-weight: 800;
                }}
                .logo {{
                    width: 44px;
                    height: 44px;
                    border-radius: 6px;
                    background: #ffffff;
                    color: #d71920;
                    display: grid;
                    place-items: center;
                    font-size: 26px;
                    font-weight: 900;
                }}
                .shell {{
                    min-height: calc(100vh - 78px);
                    display: grid;
                    place-items: center;
                    padding: 48px 20px;
                }}
                .login {{
                    width: min(520px, 100%);
                    text-align: center;
                }}
                .lock {{
                    width: 64px;
                    height: 64px;
                    margin: 0 auto 24px;
                    border-radius: 50%;
                    background: #d71920;
                    display: grid;
                    place-items: center;
                    font-size: 34px;
                }}
                h1 {{
                    margin: 0;
                    font-size: 34px;
                    letter-spacing: 0;
                }}
                .subtitle {{
                    margin: 12px 0 34px;
                    color: #a7a7ad;
                    font-size: 18px;
                }}
                form {{
                    text-align: left;
                }}
                label {{
                    display: block;
                    margin: 18px 0 8px;
                    color: #d8d8dc;
                    font-size: 17px;
                    font-weight: 700;
                }}
                input {{
                    width: 100%;
                    height: 58px;
                    border-radius: 8px;
                    border: 1px solid #3a3a44;
                    background: #17171b;
                    color: #ffffff;
                    padding: 0 18px;
                    font-size: 18px;
                    outline: none;
                }}
                input:focus {{
                    border-color: #d71920;
                    box-shadow: 0 0 0 3px rgba(215, 25, 32, 0.22);
                }}
                button {{
                    width: 100%;
                    height: 56px;
                    margin-top: 24px;
                    border: 0;
                    border-radius: 8px;
                    background: #d71920;
                    color: #ffffff;
                    font-size: 18px;
                    font-weight: 800;
                    cursor: pointer;
                }}
                button:hover {{
                    background: #b9151b;
                }}
                .error {{
                    margin-bottom: 18px;
                    padding: 12px 14px;
                    border-radius: 8px;
                    background: rgba(215, 25, 32, 0.16);
                    border: 1px solid rgba(215, 25, 32, 0.55);
                    color: #ffb6ba;
                    text-align: center;
                    font-weight: 700;
                }}
            </style>
        </head>
        <body>
            <div class="topbar">
                <div class="brand">
                    <div class="logo">R</div>
                    <span>RSLLC Brokerage Reviewer</span>
                </div>
            </div>
            <main class="shell">
                <section class="login">
                    <div class="lock">&#128274;</div>
                    <h1>RSLLC Brokerage Reviewer</h1>
                    <p class="subtitle">Sign in to review brokerage statements</p>
                    {error_html}
                    <form action="/login" method="post">
                        <label>Username</label>
                        <input type="text" name="username" autocomplete="username" autofocus required>

                        <label>Password</label>
                        <input type="password" name="password" autocomplete="current-password" required>

                        <button type="submit">Sign in</button>
                    </form>
                </section>
            </main>
        </body>
    </html>
    """


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if verify_session_token(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)
    return login_page()


@app.post("/login")
def login_post(
    username: str = Form(""),
    password: str = Form(""),
):
    correct_username = secrets.compare_digest(username, ADMIN_USER)
    correct_password = secrets.compare_digest(password, ADMIN_PASS)

    if not (correct_username and correct_password):
        return HTMLResponse(login_page("Invalid username or password."), status_code=401)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(username),
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    return """
    <html>
        <head>
            <title>RSLLC Brokerage Reviewer</title>
            <style>
                * {
                    box-sizing: border-box;
                }
                body {
                    font-family: Arial, sans-serif;
                    margin: 0;
                    min-height: 100vh;
                    background: #080809;
                    color: #f5f5f5;
                }
                .topbar {
                    height: 78px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    padding: 0 30px;
                    background: #050505;
                    border-bottom: 2px solid #d71920;
                }
                .brand {
                    display: flex;
                    align-items: center;
                    gap: 14px;
                    font-size: 22px;
                    font-weight: 800;
                }
                .logo {
                    width: 44px;
                    height: 44px;
                    border-radius: 6px;
                    background: #ffffff;
                    color: #d71920;
                    display: grid;
                    place-items: center;
                    font-size: 26px;
                    font-weight: 900;
                }
                .nav a {
                    color: #c8c8ce;
                    text-decoration: none;
                    font-weight: 700;
                    margin-left: 22px;
                }
                .nav a:hover {
                    color: #ffffff;
                }
                main {
                    max-width: 980px;
                    margin: 0 auto;
                    padding: 44px 24px 64px;
                }
                h1 {
                    margin: 0 0 10px;
                    color: #ffffff;
                    font-size: 36px;
                }
                .intro {
                    margin: 0 0 28px;
                    color: #a7a7ad;
                    font-size: 17px;
                }
                .box {
                    border: 1px solid #2a2a32;
                    background: #111115;
                    padding: 28px;
                    border-radius: 12px;
                    margin-bottom: 20px;
                    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
                }
                label {
                    display: block;
                    margin-top: 18px;
                    font-weight: bold;
                    color: #ececf0;
                }
                input {
                    margin-top: 6px;
                    padding: 12px;
                    width: 100%;
                    min-height: 46px;
                    box-sizing: border-box;
                    border-radius: 8px;
                    border: 1px solid #3a3a44;
                    background: #18181d;
                    color: #ffffff;
                    font-size: 15px;
                }
                .slider-row {
                    display: flex;
                    gap: 14px;
                    align-items: center;
                    margin-top: 6px;
                }
                .slider-row input {
                    flex: 1;
                    margin-top: 0;
                    padding: 0;
                }
                .slider-value {
                    min-width: 56px;
                    text-align: right;
                    font-weight: bold;
                    color: #ffffff;
                }
                button {
                    margin-top: 26px;
                    background: #d71920;
                    color: white;
                    border: none;
                    padding: 14px 20px;
                    border-radius: 8px;
                    font-weight: bold;
                    cursor: pointer;
                    font-size: 16px;
                }
                button:hover {
                    background: #b9151b;
                }
                .note {
                    color: #a7a7ad;
                    font-size: 14px;
                }
                a {
                    color: #ff7a80;
                }
            </style>
        </head>
        <body>
            <div class="topbar">
                <div class="brand">
                    <div class="logo">R</div>
                    <span>RSLLC Brokerage Reviewer</span>
                </div>
                <div class="nav">
                    <a href="/">Review</a>
                    <a href="/logout">Sign out</a>
                </div>
            </div>

            <main>
            <h1>RSLLC Brokerage Reviewer</h1>
            <p class="intro">Upload statement files, run the review, and download the formatted Excel output.</p>

            <div class="box">
                <p>
                    Upload prior-month and current-month brokerage statement ZIP files,
                    plus any additional Restricted List CSV to check.
                </p>

                <form action="/run" method="post" enctype="multipart/form-data">
                    <label>Additional Restricted List CSV</label>
                    <input type="file" name="restricted_csv" accept=".csv">

                    <label>Additional IPO Calendar CSV</label>
                    <input type="file" name="ipo_csv" accept=".csv">

                    <label>Prior-Month Statements ZIP</label>
                    <input type="file" name="prior_zip" accept=".zip">

                    <label>Current-Month Statements ZIP</label>
                    <input type="file" name="current_zip" accept=".zip">

                    <p class="note">
                        Optional fallback: if you are not uploading ZIP files, you can enter local server folder paths below.
                    </p>

                    <label>Prior-Month Folder Path</label>
                    <input type="text" name="prior_dir" placeholder="Example: statements/prior_month">

                    <label>Current-Month Folder Path</label>
                    <input type="text" name="current_dir" placeholder="Example: statements/current_month">

                    <label>Excessive Trading Increase Threshold</label>
                    <div class="slider-row">
                        <input
                            type="range"
                            name="excessive_trading_pct"
                            min="0"
                            max="500"
                            step="5"
                            value="25"
                            oninput="document.getElementById('excessive-trading-value').textContent = this.value + '%'"
                        >
                        <span class="slider-value" id="excessive-trading-value">25%</span>
                    </div>

                    <button type="submit">Run Review and Download Excel</button>
                </form>
            </div>

            <p class="note">
                You can also test the API at <a href="/docs">/docs</a>.
            </p>
            </main>
        </body>
    </html>
    """


@app.get("/health")
def health_check(request: Request):
    redirect = require_login(request)
    if redirect:
        return redirect
    return {"status": "ok"}


def upload_has_file(upload: Optional[UploadFile]) -> bool:
    return bool(upload and upload.filename)


def safe_extract_zip(data: bytes, target_dir: Path) -> None:
    """
    Safely extract a ZIP file while preventing path traversal.
    """
    if not data:
        raise ValueError("Uploaded ZIP was empty.")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.infolist():
                destination = (target_dir / member.filename).resolve()

                if target_dir.resolve() not in destination.parents and destination != target_dir.resolve():
                    raise ValueError(f"Unsafe ZIP entry rejected: {member.filename}")

            z.extractall(target_dir)

    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded file is not a valid ZIP archive.") from exc


def review_month_directory(
    month_dir: Path,
    restricted_df: pd.DataFrame,
    ipo_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Current-month scan:
    - restricted securities
    - insider trading tied to restricted list/transactions
    - conflicts
    - money movement indicators
    - transfers/cash movement indicators
    """
    rows = []

    for pdf_path in iter_pdf_paths(month_dir):
        try:
            accounts = split_pdf_into_accounts(pdf_path)
        except Exception:
            continue

        if not accounts:
            continue

        rep_name = clean_rep_name_from_filename(pdf_path.name)

        for account in accounts:
            account_number = account.get("account_number", "Unknown")
            account_text = account.get("text", "")
            pages = account.get("pages", [])
            row_name, row_account_number = move_leading_account_number_from_name(
                rep_name,
                account_number,
            )

            flags = review_account(account_text, restricted_df, ipo_df)
            grouped_flags = combine_flags_by_violation(flags)

            for flag in grouped_flags:
                rows.append({
                    "Name": row_name,
                    "PDF File": pdf_path.name,
                    "Account Number": row_account_number,
                    "Violation": flag.get("flag", "Review Flag"),
                    "Severity": flag.get("severity", "Medium"),
                    "Flag Count": flag.get("count", 1),
                    "Reason": flag.get("reason", ""),
                    "Pages Reviewed": ", ".join(map(str, pages)),
                })

    return pd.DataFrame(rows)


def build_summary_dataframe(
    current_results_df: pd.DataFrame,
    comparison_flags_df: pd.DataFrame,
    current_accounts_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Builds the concise output your boss wants:
    one row per rep/account, with two compact columns:
    - Current-Month Scan
    - Prior Month vs Current Month Comparison
    """
    grouped = {}

    def ensure_account(row):
        name = str(row.get("Name", "") or "").strip()
        account = str(row.get("Account Number", "") or "").strip()

        if not name and not account:
            return

        key = (name, account)
        grouped.setdefault(key, {
            "current": set(),
            "comparison": set(),
            "current_reasons": {},
            "comparison_reasons": {},
        })

    def add_violation(row, bucket: str):
        name = str(row.get("Name", "") or "").strip()
        account = str(row.get("Account Number", "") or "").strip()
        violation = str(row.get("Violation", "") or "").strip().lower()
        reason = str(row.get("Reason", "") or "").strip()

        if not name and not account:
            return

        key = (name, account)
        ensure_account(row)
        grouped[key][bucket].add(violation)

        if reason:
            grouped[key][f"{bucket}_reasons"].setdefault(violation, set()).add(reason)

    if current_accounts_df is not None and not current_accounts_df.empty:
        for _, row in current_accounts_df.iterrows():
            ensure_account(row)

    if current_results_df is not None and not current_results_df.empty:
        for _, row in current_results_df.iterrows():
            add_violation(row, "current")

    if comparison_flags_df is not None and not comparison_flags_df.empty:
        for _, row in comparison_flags_df.iterrows():
            add_violation(row, "comparison")

    rows = []

    for (name, account), violations in sorted(grouped.items()):
        current_violations = violations["current"]
        comparison_violations = violations["comparison"]
        current_reasons = violations["current_reasons"]

        current_lines = []

        def reasons_for_matching(predicate):
            return [
                reason
                for violation, reasons in current_reasons.items()
                if predicate(violation)
                for reason in sorted(reasons)
            ]

        restricted_security_reasons = reasons_for_matching(
            lambda v: "restricted security" in v
        )

        if restricted_security_reasons:
            current_lines.append(
                "Restricted security: " + "; ".join(restricted_security_reasons)
            )

        insider_reasons = reasons_for_matching(lambda v: "insider" in v)
        insider_security_reasons = [
            reason for reason in insider_reasons
            if reason in restricted_security_reasons
        ]
        if insider_security_reasons:
            current_lines.append(
                "Insider trading: " + "; ".join(insider_security_reasons)
            )
        elif any("insider" in v for v in current_violations):
            current_lines.append("Insider trading")

        if any(
            "conflict" in v and "restricted security" not in v
            for v in current_violations
        ):
            current_lines.append("Conflicts of interest")

        if any("money movement" in v for v in current_violations):
            current_lines.append("Money movement activity")

        ipo_reasons = reasons_for_matching(lambda v: "ipo restriction" in v)
        if ipo_reasons:
            current_lines.append("IPO RESTRICTION: " + " ".join(ipo_reasons))
        elif any("ipo restriction" in v for v in current_violations):
            current_lines.append("IPO RESTRICTION")

        if any("transfer" in v for v in current_violations):
            current_lines.append("Transfers of money or securities from other accounts or customers")

        if any("receipt" in v for v in current_violations):
            current_lines.append("Cash receipts from other accounts or customers")

        if any("disbursement" in v for v in current_violations):
            current_lines.append("Cash disbursements to other accounts or customers")

        comparison_lines = []

        if any("money movement" in v for v in comparison_violations):
            comparison_lines.append("Money movement activity")

        if any("transfer" in v for v in comparison_violations):
            comparison_lines.append("Transfers of money or securities from other accounts or customers")

        if any("receipt" in v for v in comparison_violations):
            comparison_lines.append("Cash receipts from other accounts or customers")

        if any("disbursement" in v for v in comparison_violations):
            comparison_lines.append("Cash disbursements to other accounts or customers")

        if any("cash expenditures" in v or "expend" in v for v in comparison_violations):
            comparison_lines.append("Cash expenditures")

        if any("loss" in v for v in comparison_violations):
            comparison_lines.append("Excessive losses")

        if any("trade" in v or "trading" in v for v in comparison_violations):
            comparison_lines.append("Excessive trading")

        rows.append({
            "Name": name,
            "Account Number": account,
            "Current-Month Scan": "\n".join(current_lines or ["None"]),
            "Prior Month vs Current Month Comparison": "\n".join(comparison_lines or ["None"]),
        })

    return pd.DataFrame(
        rows,
        columns=[
            "Name",
            "Account Number",
            "Current-Month Scan",
            "Prior Month vs Current Month Comparison",
        ],
    )


@app.post("/run")
async def run_review(
    request: Request,
    prior_dir: str = Form(""),
    current_dir: str = Form(""),
    excessive_trading_pct: int = Form(25),
    restricted_csv: Optional[UploadFile] = File(None),
    ipo_csv: Optional[UploadFile] = File(None),
    prior_zip: Optional[UploadFile] = File(None),
    current_zip: Optional[UploadFile] = File(None),
):
    redirect = require_login(request)
    if redirect:
        return redirect

    try:
        # Load the built-in restricted list, then append any uploaded list.
        # This lets users add a second list without editing the local CSV.
        restricted_df = load_restricted_list(None)

        if upload_has_file(restricted_csv):
            contents = await restricted_csv.read()
            uploaded_restricted_df = load_restricted_list(io.BytesIO(contents))
            restricted_df = pd.concat(
                [restricted_df, uploaded_restricted_df],
                ignore_index=True,
            ).drop_duplicates(
                subset=["stock_symbol", "stock_name", "date_added", "date_removed"],
                keep="first",
            )

        print("Restricted list rows loaded:", len(restricted_df))
        print("Restricted list columns:", restricted_df.columns.tolist())

        # Load the local IPO calendar, then append any uploaded IPO calendar.
        ipo_df = load_ipo_restriction_list(None)

        if upload_has_file(ipo_csv):
            contents = await ipo_csv.read()
            uploaded_ipo_df = load_ipo_restriction_list(io.BytesIO(contents))
            ipo_df = pd.concat(
                [ipo_df, uploaded_ipo_df],
                ignore_index=True,
            ).drop_duplicates(
                subset=["stock_symbol", "stock_name"],
                keep="first",
            )

        print("IPO restriction rows loaded:", len(ipo_df))
        print("IPO restriction columns:", ipo_df.columns.tolist())

        with tempfile.TemporaryDirectory(prefix="brokerage_review_") as tmp_root:
            tmp_root_path = Path(tmp_root)

            # Prior month source
            if upload_has_file(prior_zip):
                prior_data = await prior_zip.read()
                prior_path = tmp_root_path / "prior"
                prior_path.mkdir(parents=True, exist_ok=True)
                safe_extract_zip(prior_data, prior_path)
            else:
                if not prior_dir.strip():
                    return HTMLResponse(
                        "Please upload a prior-month ZIP or provide a prior-month folder path.",
                        status_code=400,
                    )
                prior_path = Path(prior_dir).expanduser()

            # Current month source
            if upload_has_file(current_zip):
                current_data = await current_zip.read()
                current_path = tmp_root_path / "current"
                current_path.mkdir(parents=True, exist_ok=True)
                safe_extract_zip(current_data, current_path)
            else:
                if not current_dir.strip():
                    return HTMLResponse(
                        "Please upload a current-month ZIP or provide a current-month folder path.",
                        status_code=400,
                    )
                current_path = Path(current_dir).expanduser()

            if not prior_path.exists():
                return HTMLResponse(
                    f"Prior-month directory not found: {prior_path}",
                    status_code=400,
                )

            if not current_path.exists():
                return HTMLResponse(
                    f"Current-month directory not found: {current_path}",
                    status_code=400,
                )

            # 1. Current-month rule scan
            current_results_df = review_month_directory(
                current_path,
                restricted_df,
                ipo_df,
            )

            # 2. Prior/current metric comparison
            prior_metrics_df = extract_month_metrics(prior_path)
            current_metrics_df = extract_month_metrics(current_path)
            comparison_flags_df = flag_month_over_month_changes(
                current_metrics_df,
                prior_metrics_df,
                excessive_trading_pct=excessive_trading_pct,
            )

            # 3. Concise boss-facing summary
            summary_df = build_summary_dataframe(
                current_results_df,
                comparison_flags_df,
                current_metrics_df,
            )

            # If no current-month accounts could be read, return a formatted empty workbook.
            if summary_df.empty:
                summary_df = pd.DataFrame(
                    [{
                        "Name": "",
                        "Account Number": "",
                        "Current-Month Scan": "None",
                        "Prior Month vs Current Month Comparison": "None",
                    }],
                    columns=[
                        "Name",
                        "Account Number",
                        "Current-Month Scan",
                        "Prior Month vs Current Month Comparison",
                    ],
                )

            excel_bytes = dataframe_to_excel_bytes(summary_df)

        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": 'attachment; filename="monthly_flag_results.xlsx"'
            },
        )

    except ValueError as exc:
        return HTMLResponse(
            f"<h2>Upload Error</h2><pre>{exc}</pre>",
            status_code=400,
        )

    except Exception:
        tb = traceback.format_exc()
        return HTMLResponse(
            f"<h2>Internal Server Error</h2><pre>{tb}</pre>",
            status_code=500,
        )
