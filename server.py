from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse

import secrets
import os
import io
import hmac
import hashlib
import base64
import calendar
import html
import re
import shutil
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
import pandas as pd
from typing import Callable, List, Optional
import tempfile
import zipfile
import traceback

import fitz  # PyMuPDF

from pydantic import BaseModel

from llm_client import generate_text
from prompts import prompt_for_review, summarize_scan_text
from sharefile_client import (
    ShareFileError,
    build_sharefile_authorization_url,
    download_sharefile_item_to_directory,
    exchange_sharefile_authorization_code,
    get_sharefile_access_token,
    get_sharefile_item_name,
    normalize_sharefile_domain,
)

from utils import (
    load_ipo_restriction_list,
    load_restricted_list,
    dataframe_to_excel_bytes,
    iter_pdf_paths,
    resolve_rep_name_for_pdf,
    move_leading_account_number_from_name,
)
from pdf_utils import split_pdf_into_accounts
from flags import review_account, combine_flags_by_violation
from metrics import extract_month_metrics, flag_month_over_month_changes


app = FastAPI(title="RSLLC Brokerage Reviewer")

PENDING_SHAREFILE_OAUTH = {}
SHAREFILE_REVIEW_JOBS = {}
DEFAULT_FORM_MODEL = os.getenv("DEFAULT_LLM_MODEL", "mistral")

ADMIN_USER = os.environ.get("ADMIN_USER", "").strip()
AUTHORIZED_USERS = {
    user.strip()
    for user in os.environ.get("AUTHORIZED_USERS", ADMIN_USER).split(",")
    if user.strip()
}
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", ADMIN_PASS)
SESSION_COOKIE = "rsllc_brokerage_session"


def public_base_url(request: Request) -> str:
    configured_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if configured_url:
        return configured_url.rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def public_callback_url(request: Request) -> str:
    return f"{public_base_url(request)}/sharefile/oauth/callback"


def auth_is_configured() -> bool:
    return bool(AUTHORIZED_USERS and ADMIN_PASS and SESSION_SECRET)


def username_is_authorized(username: str) -> bool:
    return any(
        secrets.compare_digest(username, authorized_user)
        for authorized_user in AUTHORIZED_USERS
    )


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

    if not username_is_authorized(username):
        return None

    return username


def require_login(request: Request):
    if verify_session_token(request.cookies.get(SESSION_COOKIE, "")):
        return None

    return RedirectResponse("/login", status_code=303)


def login_page(error: str = "") -> str:
    if not auth_is_configured() and not error:
        error = "Login is not configured. Set company credentials in the server environment."

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
                    background: #ffffff;
                    color: #050505;
                }}
                .topbar {{
                    height: 78px;
                    display: flex;
                    align-items: center;
                    padding: 0 30px;
                    background: #ffffff;
                    border-bottom: 2px solid #050505;
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
                    background: #050505;
                    color: #ffffff;
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
                    background: #050505;
                    color: #ffffff;
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
                    color: #555555;
                    font-size: 18px;
                }}
                form {{
                    text-align: left;
                }}
                label {{
                    display: block;
                    margin: 18px 0 8px;
                    color: #050505;
                    font-size: 17px;
                    font-weight: 700;
                }}
                input {{
                    width: 100%;
                    height: 58px;
                    border-radius: 8px;
                    border: 1px solid #9a9a9a;
                    background: #ffffff;
                    color: #050505;
                    padding: 0 18px;
                    font-size: 18px;
                    outline: none;
                }}
                input:focus {{
                    border-color: #050505;
                    box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.12);
                }}
                button {{
                    width: 100%;
                    height: 56px;
                    margin-top: 24px;
                    border: 0;
                    border-radius: 8px;
                    background: #050505;
                    color: #ffffff;
                    font-size: 18px;
                    font-weight: 800;
                    cursor: pointer;
                }}
                button:hover {{
                    background: #303030;
                }}
                .error {{
                    margin-bottom: 18px;
                    padding: 12px 14px;
                    border-radius: 8px;
                    background: #f2f2f2;
                    border: 1px solid #050505;
                    color: #050505;
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
                    <p class="subtitle">Sign in with company-authorized credentials</p>
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
    username = username.strip()

    if not auth_is_configured():
        return HTMLResponse(
            login_page("Login is not configured. Set company credentials in the server environment."),
            status_code=503,
        )

    correct_username = username_is_authorized(username)
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
                    background: #ffffff;
                    color: #050505;
                }
                .topbar {
                    height: 78px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    padding: 0 30px;
                    background: #ffffff;
                    border-bottom: 2px solid #050505;
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
                    background: #050505;
                    color: #ffffff;
                    display: grid;
                    place-items: center;
                    font-size: 26px;
                    font-weight: 900;
                }
                .nav a {
                    color: #050505;
                    text-decoration: none;
                    font-weight: 700;
                    margin-left: 22px;
                }
                .nav a:hover {
                    color: #4a4a4a;
                }
                main {
                    max-width: 980px;
                    margin: 0 auto;
                    padding: 44px 24px 64px;
                }
                h1 {
                    margin: 0 0 10px;
                    color: #050505;
                    font-size: 36px;
                }
                .intro {
                    margin: 0 0 28px;
                    color: #555555;
                    font-size: 17px;
                }
                .box {
                    border: 1px solid #cfcfcf;
                    background: #ffffff;
                    padding: 28px;
                    border-radius: 12px;
                    margin-bottom: 20px;
                    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
                }
                label {
                    display: block;
                    margin-top: 18px;
                    font-weight: bold;
                    color: #050505;
                }
                input {
                    margin-top: 6px;
                    padding: 12px;
                    width: 100%;
                    min-height: 46px;
                    box-sizing: border-box;
                    border-radius: 8px;
                    border: 1px solid #9a9a9a;
                    background: #ffffff;
                    color: #050505;
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
                    color: #050505;
                }
                button {
                    margin-top: 26px;
                    background: #050505;
                    color: #ffffff;
                    border: none;
                    padding: 14px 20px;
                    border-radius: 8px;
                    font-weight: bold;
                    cursor: pointer;
                    font-size: 16px;
                }
                button:hover {
                    background: #303030;
                }
                .note {
                    color: #555555;
                    font-size: 14px;
                }
                a {
                    color: #050505;
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


@app.get("/ping")
def ping() -> dict:
    return {"status": "ok", "message": "server is running"}


class LLMSummaryRequest(BaseModel):
    text: str
    model: str = "gpt-4o-mini"
    max_tokens: int = 512
    temperature: float = 0.2


@app.post("/llm/summarize")
async def llm_summarize(request: Request, body: LLMSummaryRequest):
    # Authentication disabled for easier local testing.
    try:
        prompt = summarize_scan_text(body.text)
        summary = generate_text(
            prompt,
            model=body.model,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
        )
        return {"summary": summary}
    except RuntimeError as exc:
        return HTMLResponse(str(exc), status_code=500)
    except Exception:
        return HTMLResponse(
            "LLM request failed. Check OPENAI_API_KEY and model availability.",
            status_code=502,
        )


def render_review_text_for_llm(summary_df: pd.DataFrame) -> str:
    lines = []
    for _, row in summary_df.iterrows():
        lines.append(f"Name: {row.get('Name', '')}")
        lines.append(f"Account Number: {row.get('Account Number', '')}")
        lines.append("Current-Month Scan:")
        lines.append(str(row.get('Current-Month Scan', '')).strip())
        lines.append("Prior Month vs Current Month Comparison:")
        lines.append(str(row.get('Prior Month vs Current Month Comparison', '')).strip())
        lines.append("---")
    return "\n".join(lines)


def pdf_sort_key_for_llm(path: Path):
    parts = path.parts
    parent = path.parent.name if path.parent else ""
    return (parent.lower(), path.name.lower(), str(path).lower())


def select_pdf_paths_for_llm(month_dir: Path, max_pdfs: int = 80):
    by_rep = {}
    for pdf_path in iter_pdf_paths(month_dir):
        rep_key = str(pdf_path.parent).lower()
        by_rep.setdefault(rep_key, []).append(pdf_path)

    for paths in by_rep.values():
        paths.sort(key=pdf_sort_key_for_llm)

    selected = []
    rep_keys = sorted(by_rep)
    while rep_keys and len(selected) < max_pdfs:
        next_rep_keys = []
        for rep_key in rep_keys:
            paths = by_rep[rep_key]
            if paths and len(selected) < max_pdfs:
                selected.append(paths.pop(0))
            if paths:
                next_rep_keys.append(rep_key)
        rep_keys = next_rep_keys

    return selected


def extract_statement_text_for_llm(
    month_dir: Path,
    label: str,
    max_chars: int = 30000,
    max_pdfs: int = 80,
    chars_per_pdf: int = 1200,
) -> str:
    sections = []
    remaining = max_chars

    for pdf_path in select_pdf_paths_for_llm(month_dir, max_pdfs=max_pdfs):
        if remaining <= 0:
            break

        try:
            doc = fitz.open(pdf_path)
            text = ""
            for page_index, page in enumerate(list(doc)[:2], start=1):
                text += f"\n--- PAGE {page_index} ---\n"
                text += page.get_text("text")
        except Exception:
            continue

        try:
            display_path = pdf_path.relative_to(month_dir)
        except ValueError:
            display_path = Path(pdf_path.name)

        section_text = text.strip()[:chars_per_pdf]
        section = f"=== {label}: {display_path} ===\n{section_text}\n"
        sections.append(section[:remaining])
        remaining -= len(sections[-1])

    return "\n".join(sections)


def prompt_for_independent_statement_review(
    raw_statement_text: str,
    existing_flag_text: str,
) -> str:
    return (
        "You are a financial compliance analyst reviewing brokerage statements. "
        "First, independently scan the raw statement text for compliance issues. "
        "Then compare your findings against the existing rule-based flag summary. "
        "Identify matches, possible misses, and items that need human follow-up. "
        "Do not invent facts that are not present in the statements or flag summary. "
        "Use short, actionable language.\n\n"
        "Existing rule-based flag summary:\n"
        f"{existing_flag_text.strip() or 'None'}\n\n"
        "Raw brokerage statement text:\n"
        f"{raw_statement_text.strip() or 'No statement text found.'}"
    )


def add_llm_analysis_sheet(excel_bytes: bytes, llm_analysis: str) -> bytes:
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font

    workbook = load_workbook(io.BytesIO(excel_bytes))
    if "LLM Analysis" in workbook.sheetnames:
        del workbook["LLM Analysis"]

    worksheet = workbook.create_sheet("LLM Analysis")
    worksheet["A1"] = "LLM Analysis"
    worksheet["A1"].font = Font(bold=True, size=16)
    worksheet["A2"] = llm_analysis
    worksheet["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    worksheet.column_dimensions["A"].width = 120
    worksheet.row_dimensions[2].height = 260

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def add_diagnostics_sheet(excel_bytes: bytes, diagnostics: dict) -> bytes:
    from openpyxl import load_workbook
    from openpyxl.styles import Font

    workbook = load_workbook(io.BytesIO(excel_bytes))
    if "Diagnostics" in workbook.sheetnames:
        del workbook["Diagnostics"]

    worksheet = workbook.create_sheet("Diagnostics")
    worksheet["A1"] = "Metric"
    worksheet["B1"] = "Value"
    worksheet["A1"].font = Font(bold=True)
    worksheet["B1"].font = Font(bold=True)

    for row_index, (key, value) in enumerate(diagnostics.items(), start=2):
        worksheet.cell(row=row_index, column=1, value=str(key))
        worksheet.cell(row=row_index, column=2, value=str(value))

    worksheet.column_dimensions["A"].width = 36
    worksheet.column_dimensions["B"].width = 72

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def add_skipped_files_sheet(excel_bytes: bytes, skipped_files: List[str]) -> bytes:
    from openpyxl import load_workbook
    from openpyxl.styles import Font

    workbook = load_workbook(io.BytesIO(excel_bytes))
    if "Skipped Files" in workbook.sheetnames:
        del workbook["Skipped Files"]

    worksheet = workbook.create_sheet("Skipped Files")
    worksheet["A1"] = "File (skipped by filename pre-filter, not downloaded)"
    worksheet["A1"].font = Font(bold=True)

    for row_index, path in enumerate(sorted(skipped_files), start=2):
        worksheet.cell(row=row_index, column=1, value=path)

    worksheet.column_dimensions["A"].width = 100

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def parse_sharefile_root_items(raw: str) -> List[str]:
    """
    The root-folder field accepts either one ShareFile folder URL/item ID
    (the normal full-tree crawl, e.g. the whole "Outside Brokerage
    Statements" root) or several, one per line/comma, to target a handful
    of specific reps without crawling everyone.
    """
    items = []
    for chunk in re.split(r"[\r\n,]+", raw or ""):
        cleaned = chunk.strip()
        if cleaned:
            items.append(cleaned)
    return items


def build_sharefile_review_excel_bytes(
    sharefile_domain: str,
    access_token: str,
    root_item: str,
    restricted_df: pd.DataFrame,
    ipo_df: pd.DataFrame,
    excessive_trading_pct: int = 25,
    model: str = DEFAULT_FORM_MODEL,
    max_tokens: int = 512,
    temperature: float = 0.2,
    progress_callback=None,
    current_month_override: str = "",
    prior_month_override: str = "",
) -> bytes:
    target_current_month, target_prior_month = resolve_target_months(
        current_month_override, prior_month_override
    )
    name_filter = build_month_name_filter(target_current_month, target_prior_month)

    root_items = parse_sharefile_root_items(root_item)
    if not root_items:
        raise ValueError("At least one ShareFile root folder URL or item ID is required.")

    with tempfile.TemporaryDirectory(prefix="brokerage_sharefile_review_") as tmp_root:
        tmp_root_path = Path(tmp_root)
        root_path = tmp_root_path / "sharefile_root"
        root_path.mkdir(parents=True, exist_ok=True)

        if progress_callback:
            progress_callback(
                f"Starting ShareFile download of {len(root_items)} target folder(s) "
                f"(targeting {month_label(target_current_month)} and {month_label(target_prior_month)})."
            )
        file_count = [0]
        skipped_count = [0]
        skipped_files = []
        per_target_results = None

        if len(root_items) == 1:
            # Single root: matches the normal full-tree crawl, where the
            # root's children are already the individual rep folders.
            download_sharefile_item_to_directory(
                sharefile_domain,
                access_token,
                root_items[0],
                root_path,
                progress_callback=progress_callback,
                file_name_filter=name_filter,
                file_count=file_count,
                skipped_count=skipped_count,
                skipped_files=skipped_files,
            )
        else:
            # Multiple explicit targets: downloading a folder's *children*
            # straight into root_path would otherwise drop that folder's
            # own name (e.g. "David Rosen") -- preserve it as a directory
            # level so rep-name resolution downstream still works.
            #
            # Each target is isolated with its own try/except and its own
            # before/after file count, so one bad folder URL can't silently
            # zero out or crash the whole batch -- and so it's visible
            # afterward (via Diagnostics) exactly how many files came from
            # each target, rather than only a single combined total.
            per_target_results = []
            for item in root_items:
                files_before = file_count[0]
                try:
                    item_name = get_sharefile_item_name(sharefile_domain, access_token, item)
                    safe_name = re.sub(r'[\\/:*?"<>|]', "_", item_name).strip() or item
                    item_dir = root_path / safe_name
                    item_dir.mkdir(parents=True, exist_ok=True)
                    if progress_callback:
                        progress_callback(f"Downloading {item_name}.")
                    download_sharefile_item_to_directory(
                        sharefile_domain,
                        access_token,
                        item,
                        item_dir,
                        progress_callback=progress_callback,
                        file_name_filter=name_filter,
                        file_count=file_count,
                        skipped_count=skipped_count,
                        skipped_files=skipped_files,
                    )
                    files_from_item = file_count[0] - files_before
                    per_target_results.append(f"{item_name}: {files_from_item} files")
                    if progress_callback:
                        progress_callback(f"{item_name}: downloaded {files_from_item} files.")
                except Exception as exc:
                    per_target_results.append(f"{item} FAILED: {exc}")
                    if progress_callback:
                        progress_callback(f"Failed to download {item}: {exc}")

        downloaded_paths = [path for path in root_path.rglob("*") if path.is_file()]
        downloaded_pdfs = list(iter_pdf_paths(root_path))
        if progress_callback:
            progress_callback(
                f"Downloaded {len(downloaded_paths)} files, including {len(downloaded_pdfs)} PDFs "
                f"({skipped_count[0]} skipped by filename pre-filter)."
            )

        if progress_callback:
            progress_callback("Organizing statements by detected month.")
        month_work_path = tmp_root_path / "detected_months"
        (
            current_path,
            prior_path,
            current_month_label,
            prior_month_label,
            unknown_month_pdfs,
            current_fallback_used,
            prior_fallback_used,
        ) = organize_sharefile_root_by_latest_months(
            root_path,
            month_work_path,
            target_current_month=target_current_month,
            target_prior_month=target_prior_month,
        )
        detected_month_distribution, detected_unknown_count = summarize_pdf_month_distribution(root_path)
        current_pdfs = list(iter_pdf_paths(current_path))
        prior_pdfs = list(iter_pdf_paths(prior_path)) if prior_path else []
        current_llm_sample_count = len(select_pdf_paths_for_llm(current_path))
        prior_llm_sample_count = len(select_pdf_paths_for_llm(prior_path)) if prior_path else 0

        if progress_callback:
            progress_callback("Running rule-based brokerage review.")
        current_results_df = review_month_directory(
            current_path,
            restricted_df,
            ipo_df,
        )

        if progress_callback:
            progress_callback("Extracting statement metrics for month-over-month comparison.")
        current_metrics_df = extract_month_metrics(current_path)
        if prior_path:
            prior_metrics_df = extract_month_metrics(prior_path)
            comparison_flags_df = flag_month_over_month_changes(
                current_metrics_df,
                prior_metrics_df,
                excessive_trading_pct=excessive_trading_pct,
            )
        else:
            comparison_flags_df = pd.DataFrame(
                columns=["Name", "Account Number", "Violation", "Severity", "Flag Count", "Reason", "Pages Reviewed"]
            )

        summary_df = build_summary_dataframe(
            current_results_df,
            comparison_flags_df,
            current_metrics_df,
        )

        if summary_df.empty:
            summary_df = pd.DataFrame(
                [{
                    "Name": "",
                    "Account Number": "",
                    "Current-Month Scan": "No parsable account rows found",
                    "Prior Month vs Current Month Comparison": "No prior comparison available",
                }],
                columns=[
                    "Name",
                    "Account Number",
                    "Current-Month Scan",
                    "Prior Month vs Current Month Comparison",
                ],
            )

        excel_bytes = dataframe_to_excel_bytes(summary_df)

        existing_flag_text = render_review_text_for_llm(summary_df)
        month_context = (
            f"Detected current month: {current_month_label}. "
            f"Detected prior month: {prior_month_label or 'none available'}. "
            f"PDFs without a detectable month: {len(unknown_month_pdfs)}."
        )
        if existing_flag_text.strip():
            existing_flag_text = f"{month_context}\n\n{existing_flag_text}"
        else:
            existing_flag_text = month_context

        statement_sections = []
        if prior_path:
            statement_sections.append(
                extract_statement_text_for_llm(
                    prior_path,
                    f"Prior month ({prior_month_label})",
                )
            )
        statement_sections.append(
            extract_statement_text_for_llm(
                current_path,
                f"Current month ({current_month_label})",
            )
        )
        raw_statements = "\n".join(section for section in statement_sections if section.strip())

        if raw_statements.strip() or existing_flag_text.strip():
            llm_prompt = prompt_for_independent_statement_review(
                raw_statements,
                existing_flag_text,
            )
            if progress_callback:
                progress_callback("Asking the local LLM for the independent review.")
            try:
                llm_analysis = generate_text(
                    llm_prompt,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as exc:
                llm_analysis = f"LLM independent review unavailable: {exc}"
        else:
            llm_analysis = "No statements found to analyze."

        if progress_callback:
            progress_callback("Writing Excel workbook.")
        excel_bytes = add_llm_analysis_sheet(excel_bytes, llm_analysis)
        excel_bytes = add_diagnostics_sheet(
            excel_bytes,
            {
                "Target current month (calendar)": month_label(target_current_month),
                "Target prior month (calendar)": month_label(target_prior_month),
                "Actual current month used": (
                    f"{current_month_label} (target month had no statements yet)"
                    if current_fallback_used else current_month_label
                ),
                "Actual prior month used": (
                    f"{prior_month_label} (target month had no statements yet)"
                    if prior_fallback_used and prior_month_label else (prior_month_label or "none available")
                ),
                "Files downloaded": file_count[0],
                "Files skipped by filename pre-filter": skipped_count[0],
                **(
                    {"Per-target file counts": "; ".join(per_target_results)}
                    if per_target_results is not None else {}
                ),
                "Downloaded PDFs": len(downloaded_pdfs),
                "Downloaded files": len(downloaded_paths),
                "Detected month distribution": detected_month_distribution or "none",
                "Detected current month": current_month_label,
                "Current-month PDFs": len(current_pdfs),
                "Detected prior month": prior_month_label or "none available",
                "Prior-month PDFs": len(prior_pdfs),
                "PDFs without detectable month": len(unknown_month_pdfs),
                "PDFs without detectable month recheck": detected_unknown_count,
                "Current-month PDFs sampled for LLM": current_llm_sample_count,
                "Prior-month PDFs sampled for LLM": prior_llm_sample_count,
                "Current metrics rows": len(current_metrics_df),
                "Rule flag rows": len(current_results_df),
                "Comparison flag rows": len(comparison_flags_df),
                "Summary rows": len(summary_df),
            },
        )

        readable_skipped_files = []
        for raw_path in skipped_files:
            try:
                readable_skipped_files.append(str(Path(raw_path).relative_to(root_path)))
            except ValueError:
                readable_skipped_files.append(raw_path)
        excel_bytes = add_skipped_files_sheet(excel_bytes, readable_skipped_files)

    return excel_bytes


def run_sharefile_review_from_access_token(
    sharefile_domain: str,
    access_token: str,
    root_item: str,
    restricted_df: pd.DataFrame,
    ipo_df: pd.DataFrame,
    excessive_trading_pct: int = 25,
    model: str = DEFAULT_FORM_MODEL,
    max_tokens: int = 512,
    temperature: float = 0.2,
    current_month_override: str = "",
    prior_month_override: str = "",
):
    excel_bytes = build_sharefile_review_excel_bytes(
        sharefile_domain,
        access_token,
        root_item,
        restricted_df,
        ipo_df,
        excessive_trading_pct=excessive_trading_pct,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        current_month_override=current_month_override,
        prior_month_override=prior_month_override,
    )

    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="sharefile_review_with_llm.xlsx"'
        },
    )


def start_sharefile_review_job(
    sharefile_domain: str,
    access_token: str,
    root_item: str,
    excessive_trading_pct: int,
    model: str,
    max_tokens: int,
    temperature: float,
    current_month_override: str = "",
    prior_month_override: str = "",
) -> str:
    job_id = uuid.uuid4().hex
    SHAREFILE_REVIEW_JOBS[job_id] = {
        "status": "running",
        "message": "ShareFile review is queued.",
        "file_path": "",
    }

    def update_progress(message: str) -> None:
        SHAREFILE_REVIEW_JOBS[job_id].update({
            "status": "running",
            "message": message,
        })

    def worker():
        try:
            update_progress("Loading restriction lists.")
            restricted_df = load_restricted_list(None)
            ipo_df = load_ipo_restriction_list(None)
            excel_bytes = build_sharefile_review_excel_bytes(
                sharefile_domain,
                access_token,
                root_item,
                restricted_df,
                ipo_df,
                excessive_trading_pct=excessive_trading_pct,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                progress_callback=update_progress,
                current_month_override=current_month_override,
                prior_month_override=prior_month_override,
            )

            update_progress("Saving Excel workbook.")
            outputs_dir = Path("outputs")
            outputs_dir.mkdir(exist_ok=True)
            file_path = outputs_dir / f"sharefile_review_{job_id}.xlsx"
            file_path.write_bytes(excel_bytes)
            SHAREFILE_REVIEW_JOBS[job_id].update({
                "status": "complete",
                "message": "ShareFile review complete.",
                "file_path": str(file_path),
            })
        except Exception as exc:
            SHAREFILE_REVIEW_JOBS[job_id].update({
                "status": "failed",
                "message": str(exc),
                "file_path": "",
            })

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return job_id


@app.get("/sharefile/jobs/{job_id}")
def sharefile_job_status(job_id: str):
    job = SHAREFILE_REVIEW_JOBS.get(job_id)
    if not job:
        return HTMLResponse("Review job not found.", status_code=404)

    escaped_message = html.escape(job.get("message", ""))
    status = job.get("status", "unknown")
    if status == "complete":
        body = (
            f"<h1>ShareFile review complete</h1>"
            f"<p>{escaped_message}</p>"
            f'<p><a href="/sharefile/jobs/{job_id}/download">Download Excel workbook</a></p>'
        )
    elif status == "failed":
        body = f"<h1>ShareFile review failed</h1><pre>{escaped_message}</pre>"
    else:
        body = (
            "<h1>ShareFile review running</h1>"
            f"<p>{escaped_message}</p>"
            "<p>Refresh this page in a minute.</p>"
            "<script>setTimeout(() => location.reload(), 60000);</script>"
        )

    return HTMLResponse(
        f"<html><body>{body}</body></html>",
        status_code=200,
    )


@app.get("/sharefile/jobs/{job_id}/download")
def sharefile_job_download(job_id: str):
    job = SHAREFILE_REVIEW_JOBS.get(job_id)
    if not job:
        return HTMLResponse("Review job not found.", status_code=404)
    if job.get("status") != "complete":
        return HTMLResponse("Review job is not complete yet.", status_code=409)

    file_path = Path(job.get("file_path", ""))
    if not file_path.exists():
        return HTMLResponse("Review workbook was not found.", status_code=404)

    return StreamingResponse(
        io.BytesIO(file_path.read_bytes()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="sharefile_review_with_llm.xlsx"'
        },
    )


def month_label(month_key) -> str:
    year, month = month_key
    return f"{calendar.month_name[month]} {year}"


def month_sort_key(month_key):
    year, month = month_key
    return year * 12 + month


def _current_month_key():
    today = date.today()
    return today.year, today.month


def _prior_month_key(month_key):
    year, month = month_key
    return (year - 1, 12) if month == 1 else (year, month - 1)


def parse_month_override(value: str):
    """Accepts 'YYYY-MM', 'Month YYYY', or 'Mon YYYY'. Returns (year, month) or None if blank."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m", "%B %Y", "%b %Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            return (parsed.year, parsed.month)
        except ValueError:
            continue
    raise ValueError(f"Could not parse month override '{value}'. Use YYYY-MM, e.g. 2026-07.")


def resolve_target_months(current_override: str = "", prior_override: str = ""):
    current_month = parse_month_override(current_override) or _current_month_key()
    prior_month = parse_month_override(prior_override) or _prior_month_key(current_month)
    return current_month, prior_month


def _is_reasonable_statement_month(month_key):
    year, month = month_key
    current_year, current_month = _current_month_key()
    current_index = current_year * 12 + current_month
    statement_index = year * 12 + month
    return 2000 <= year <= current_year + 1 and statement_index <= current_index + 1


def _latest_reasonable_month(candidates):
    if not candidates:
        return None

    reasonable = [candidate for candidate in candidates if _is_reasonable_statement_month(candidate)]
    if reasonable:
        return max(reasonable, key=month_sort_key)
    return None


def detect_statement_month_from_text(text: str):
    text = text or ""
    month_names = "|".join(calendar.month_name[1:])
    short_months = "|".join(calendar.month_abbr[1:])
    month_word = rf"(?:{month_names}|{short_months})\.?"

    priority_patterns = [
        rf"(?:statement|report|account|billing|portfolio|quarter|period|month)[^\n]{{0,80}}(?:ending|ended|end|through|thru|as of|date|for)[^\n]{{0,80}}\b({month_word})\s+\d{{1,2}},?\s+(20\d{{2}})\b",
        rf"(?:statement|report|account|billing|portfolio|quarter|period|month)[^\n]{{0,80}}(?:ending|ended|end|through|thru|as of|date|for)[^\n]{{0,80}}\b(0?[1-9]|1[0-2])[-_/\.]\d{{1,2}}[-_/\.](20\d{{2}})\b",
        rf"(?:statement|report|account|billing|portfolio|quarter|period|month)[^\n]{{0,80}}(?:ending|ended|end|through|thru|as of|date|for)[^\n]{{0,80}}\b(20\d{{2}})[-_/\.](0?[1-9]|1[0-2])[-_/\.]\d{{1,2}}\b",
        rf"\b({month_word})\s+\d{{1,2}},?\s+(20\d{{2}})\s*(?:-|to|through|thru)\s*({month_word})\s+\d{{1,2}},?\s+(20\d{{2}})\b",
        r"\b(0?[1-9]|1[0-2])[-_/\.]\d{1,2}[-_/\.](20\d{2})\s*(?:-|to|through|thru)\s*(0?[1-9]|1[0-2])[-_/\.]\d{1,2}[-_/\.](20\d{2})\b",
        # 2-digit-year range, e.g. "For the Period 3/1/26 to 3/31/26" -- some
        # brokers (e.g. JPMorgan) print only a 2-digit year.
        r"\b(0?[1-9]|1[0-2])[-_/\.]\d{1,2}[-_/\.](\d{2})\s*(?:-|to|through|thru)\s*(0?[1-9]|1[0-2])[-_/\.]\d{1,2}[-_/\.](\d{2})\b",
    ]

    priority_candidates = []
    for match in re.finditer(priority_patterns[0], text, flags=re.IGNORECASE):
        month_name, year = match.groups()
        month = month_name_to_number(month_name)
        if month:
            priority_candidates.append((int(year), month))

    for match in re.finditer(priority_patterns[1], text, flags=re.IGNORECASE):
        month, year = match.groups()
        priority_candidates.append((int(year), int(month)))

    for match in re.finditer(priority_patterns[2], text, flags=re.IGNORECASE):
        year, month = match.groups()
        priority_candidates.append((int(year), int(month)))

    for match in re.finditer(priority_patterns[3], text, flags=re.IGNORECASE):
        _start_month, _start_year, end_month, end_year = match.groups()
        month = month_name_to_number(end_month)
        if month:
            priority_candidates.append((int(end_year), month))

    for match in re.finditer(priority_patterns[4], text, flags=re.IGNORECASE):
        _start_month, _start_year, end_month, end_year = match.groups()
        priority_candidates.append((int(end_year), int(end_month)))

    for match in re.finditer(priority_patterns[5], text, flags=re.IGNORECASE):
        _start_month, _start_year, end_month, end_year = match.groups()
        priority_candidates.append((2000 + int(end_year), int(end_month)))

    priority_month = _latest_reasonable_month(priority_candidates)
    if priority_month:
        return priority_month

    patterns = [
        rf"\b({month_word})\s+\d{{1,2}},?\s+(20\d{{2}})\b",
        rf"\b({month_word})\s+(20\d{{2}})\b",
        r"\b(20\d{2})[-_/\.](0?[1-9]|1[0-2])[-_/\.]\d{1,2}\b",
        r"\b(0?[1-9]|1[0-2])[-_/\.]\d{1,2}[-_/\.](20\d{2})\b",
        r"\b(0?[1-9]|1[0-2])[-_/\.](20\d{2})\b",
        r"\b(20\d{2})[-_/\.](0?[1-9]|1[0-2])\b",
        # 2-digit-year single date, e.g. "3/31/26".
        r"\b(0?[1-9]|1[0-2])[-_/\.]\d{1,2}[-_/\.](\d{2})\b",
        # Compact "YYYYMMDD" with no separators, e.g. a filename like
        # "20260331 Matt Cooperman Joint 6006.pdf".
        r"\b(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b",
    ]

    candidates = []
    for match in re.finditer(patterns[0], text, flags=re.IGNORECASE):
        month_name, year = match.groups()
        month = month_name_to_number(month_name)
        if month:
            candidates.append((int(year), month))

    for match in re.finditer(patterns[1], text, flags=re.IGNORECASE):
        month_name, year = match.groups()
        month = month_name_to_number(month_name)
        if month:
            candidates.append((int(year), month))

    for match in re.finditer(patterns[2], text):
        year, month = match.groups()
        candidates.append((int(year), int(month)))

    for match in re.finditer(patterns[3], text):
        month, year = match.groups()
        candidates.append((int(year), int(month)))

    for match in re.finditer(patterns[4], text):
        month, year = match.groups()
        candidates.append((int(year), int(month)))

    for match in re.finditer(patterns[5], text):
        year, month = match.groups()
        candidates.append((int(year), int(month)))

    for match in re.finditer(patterns[6], text):
        month, year = match.groups()
        candidates.append((2000 + int(year), int(month)))

    for match in re.finditer(patterns[7], text):
        year, month, _day = match.groups()
        candidates.append((int(year), int(month)))

    return _latest_reasonable_month(candidates)


def month_name_to_number(month_name: str):
    normalized = str(month_name or "").strip().lower().rstrip(".")
    for index in range(1, 13):
        if normalized in {
            calendar.month_name[index].lower(),
            calendar.month_abbr[index].lower(),
        }:
            return index
    return None


def detect_statement_month(pdf_path: Path):
    try:
        doc = fitz.open(pdf_path)
        first_page_text = list(doc)[0].get_text("text") if len(doc) else ""
        top_text = first_page_text[:5000]
    except Exception:
        top_text = ""

    month_key = detect_statement_month_from_text(top_text)
    if month_key:
        return month_key

    path_text = " ".join(pdf_path.parts)
    month_key = detect_statement_month_from_text(path_text)
    if month_key:
        return month_key

    return None


def build_month_name_filter(
    target_current_month, target_prior_month, buffer_months: int = 2
) -> Callable[[str], bool]:
    """
    Pre-download filter applied to a ShareFile item's filename alone (no
    download needed). Keeps target-current, target-prior, and up to
    `buffer_months` further back, so a rep who's a bit behind on uploads
    still gets a fallback candidate (see organize_sharefile_root_by_latest_months).
    Anything whose name doesn't yield a confident month is always kept
    as a safety net -- we can't know its true date without opening it.
    """
    newest = month_sort_key(target_current_month)
    oldest_allowed = month_sort_key(target_prior_month) - buffer_months

    def keep(file_name: str) -> bool:
        if file_name.lower().endswith(".zip"):
            return True
        # Filenames commonly use "_"/"-" where the detector expects whitespace
        # between a month name and year (e.g. "Smith_March_2026_Statement.pdf").
        normalized_name = re.sub(r"[_-]", " ", file_name)
        month_key = detect_statement_month_from_text(normalized_name)
        if month_key is None:
            return True
        return oldest_allowed <= month_sort_key(month_key) <= newest

    return keep


def organize_sharefile_root_by_latest_months(
    root_path: Path,
    work_path: Path,
    target_current_month=None,
    target_prior_month=None,
):
    month_to_pdfs = {}
    unknown_month_pdfs = []

    for pdf_path in iter_pdf_paths(root_path):
        month_key = detect_statement_month(pdf_path)
        if month_key:
            month_to_pdfs.setdefault(month_key, []).append(pdf_path)
        else:
            unknown_month_pdfs.append(pdf_path)

    current_fallback_used = False
    prior_fallback_used = False

    if target_current_month is None:
        if not month_to_pdfs:
            return root_path, None, "all detected statements", None, unknown_month_pdfs, False, False
        ordered_months = sorted(month_to_pdfs, key=month_sort_key)
        current_month = ordered_months[-1]
        prior_month = ordered_months[-2] if len(ordered_months) >= 2 else None
    else:
        available = sorted(month_to_pdfs, key=month_sort_key, reverse=True)

        if target_current_month in month_to_pdfs:
            current_month = target_current_month
        else:
            current_month = available[0] if available else target_current_month
            current_fallback_used = current_month != target_current_month

        if target_prior_month in month_to_pdfs and target_prior_month != current_month:
            prior_month = target_prior_month
        else:
            prior_month = next((m for m in available if m != current_month), None)
            prior_fallback_used = prior_month != target_prior_month

    current_path = work_path / "current"
    prior_path = work_path / "prior"
    current_path.mkdir(parents=True, exist_ok=True)
    prior_path.mkdir(parents=True, exist_ok=True)

    copy_pdf_group(month_to_pdfs.get(current_month, []), root_path, current_path)
    if prior_month:
        copy_pdf_group(month_to_pdfs.get(prior_month, []), root_path, prior_path)

    return (
        current_path,
        prior_path if prior_month else None,
        month_label(current_month),
        month_label(prior_month) if prior_month else None,
        unknown_month_pdfs,
        current_fallback_used,
        prior_fallback_used,
    )


def summarize_pdf_month_distribution(root_path: Path):
    month_counts = {}
    unknown_count = 0
    for pdf_path in iter_pdf_paths(root_path):
        month_key = detect_statement_month(pdf_path)
        if month_key:
            month_counts[month_label(month_key)] = month_counts.get(month_label(month_key), 0) + 1
        else:
            unknown_count += 1

    ordered = sorted(
        month_counts.items(),
        key=lambda item: month_sort_key(
            (
                int(item[0].split()[-1]),
                month_name_to_number(item[0].rsplit(" ", 1)[0]),
            )
        ),
    )
    return ", ".join(f"{label}: {count}" for label, count in ordered), unknown_count


def copy_pdf_group(pdf_paths, source_root: Path, target_root: Path) -> None:
    for pdf_path in pdf_paths:
        try:
            relative_path = pdf_path.relative_to(source_root)
        except ValueError:
            relative_path = Path(pdf_path.name)

        target_path = target_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, target_path)


@app.get("/run-llm-review")
def run_review_with_llm_form():
    return HTMLResponse(
        """
        <html>
            <head>
                <title>Run LLM Review</title>
                <style>
                    body { font-family: Arial, sans-serif; padding: 24px; background: #f7f7f7; }
                    form { max-width: 720px; margin: 0 auto; background: #fff; padding: 24px; border-radius: 12px; box-shadow: 0 0 24px rgba(0,0,0,0.08); }
                    label { display: block; margin-top: 16px; font-weight: 700; }
                    input, button { width: 100%; padding: 10px 14px; margin-top: 8px; border-radius: 8px; border: 1px solid #ccc; }
                    button { background: #2d7ff9; color: white; border: none; cursor: pointer; }
                    button:hover { background: #1d5cd5; }
                    .small { max-width: 160px; display: inline-block; }
                </style>
            </head>
            <body>
                <h1>Run LLM Review</h1>
                <form action="/run-llm-review" method="post" enctype="multipart/form-data">
                    <label>Prior month ZIP</label>
                    <input type="file" name="prior_zip" accept=".zip" required>

                    <label>Current month ZIP</label>
                    <input type="file" name="current_zip" accept=".zip" required>

                    <label>Prompt model</label>
                    <input type="text" name="model" value="gpt-4o-mini">

                    <label>Max tokens</label>
                    <input type="number" name="max_tokens" value="512" min="1">

                    <label>Temperature</label>
                    <input type="number" step="0.1" min="0" max="1" name="temperature" value="0.2">

                    <button type="submit">Submit Review</button>
                </form>
            </body>
        </html>
        """,
        status_code=200,
    )


@app.post("/run-llm-review")
async def run_review_with_llm(
    request: Request,
    prior_dir: str = Form(""),
    current_dir: str = Form(""),
    excessive_trading_pct: int = Form(25),
    restricted_csv: Optional[UploadFile] = File(None),
    ipo_csv: Optional[UploadFile] = File(None),
    prior_zip: Optional[UploadFile] = File(None),
    current_zip: Optional[UploadFile] = File(None),
    model: str = Form(DEFAULT_FORM_MODEL),
    max_tokens: int = Form(512),
    temperature: float = Form(0.2),
):
    # Authentication disabled for easier local testing.
    try:
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

        with tempfile.TemporaryDirectory(prefix="brokerage_review_") as tmp_root:
            tmp_root_path = Path(tmp_root)

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

            current_results_df = review_month_directory(
                current_path,
                restricted_df,
                ipo_df,
            )

            prior_metrics_df = extract_month_metrics(prior_path)
            current_metrics_df = extract_month_metrics(current_path)
            comparison_flags_df = flag_month_over_month_changes(
                current_metrics_df,
                prior_metrics_df,
                excessive_trading_pct=excessive_trading_pct,
            )

            summary_df = build_summary_dataframe(
                current_results_df,
                comparison_flags_df,
                current_metrics_df,
            )

        review_text = render_review_text_for_llm(summary_df)
        prompt = prompt_for_review(review_text)
        llm_response = generate_text(
            prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return {
            "llm_summary": llm_response,
            "review_rows": len(summary_df),
            "raw_review_text": review_text,
        }
    except RuntimeError as exc:
        return HTMLResponse(str(exc), status_code=500)
    except Exception as exc:
        return HTMLResponse(
            f"LLM review failed: {exc}",
            status_code=502,
        )


@app.get("/run-sharefile-review")
def run_sharefile_review_form(request: Request):
    callback_url = public_callback_url(request)
    escaped_callback_url = html.escape(callback_url, quote=True)
    escaped_model = html.escape(DEFAULT_FORM_MODEL, quote=True)
    html_text = """
        <html>
            <head>
                <title>Run ShareFile LLM Review</title>
                <style>
                    body { font-family: Arial, sans-serif; padding: 24px; background: #f7f7f7; }
                    form { max-width: 720px; margin: 0 auto; background: #fff; padding: 24px; border-radius: 12px; box-shadow: 0 0 24px rgba(0,0,0,0.08); }
                    label { display: block; margin-top: 16px; font-weight: 700; }
                    input, button, textarea { width: 100%; padding: 10px 14px; margin-top: 8px; border-radius: 8px; border: 1px solid #ccc; font-family: inherit; }
                    button { background: #2d7ff9; color: white; border: none; cursor: pointer; }
                    button:hover { background: #1d5cd5; }
                    .slider-row { display: flex; gap: 14px; align-items: center; margin-top: 8px; }
                    .slider-row input { flex: 1; margin-top: 0; padding: 0; }
                    .slider-value { min-width: 56px; text-align: right; font-weight: 700; }
                </style>
            </head>
            <body>
                <h1>Run ShareFile LLM Review</h1>
                <form action="/sharefile/oauth/start" method="post">
                    <h2>Browser OAuth Login</h2>
                    <label>ShareFile domain</label>
                    <input type="text" name="sharefile_domain" placeholder="regiment339.sharefile.com" required>

                    <label>ShareFile client ID</label>
                    <input type="text" name="sharefile_client_id" required>

                    <label>ShareFile client secret</label>
                    <input type="password" name="sharefile_client_secret" required>

                    <label>Public HTTPS callback URL</label>
                    <input type="url" name="sharefile_redirect_uri" value="__CALLBACK_URL__" required>

                    <label>ShareFile brokerage statements root folder URL or item ID</label>
                    <textarea name="sharefile_root_item_id" rows="3" placeholder="Outside brokerage statements folder URL (whole tree), or one specific rep folder URL per line to target only a few reps" required></textarea>

                    <label>Current month override (optional)</label>
                    <input type="text" name="current_month_override" placeholder="auto: current calendar month, e.g. 2026-07">

                    <label>Prior month override (optional)</label>
                    <input type="text" name="prior_month_override" placeholder="auto: prior calendar month, e.g. 2026-06">

                    <label>Excessive Trading Increase Threshold</label>
                    <div class="slider-row">
                        <input
                            type="range"
                            name="excessive_trading_pct"
                            min="0"
                            max="500"
                            step="5"
                            value="25"
                            oninput="document.getElementById('excessive-trading-value-oauth').textContent = this.value + '%'"
                        >
                        <span class="slider-value" id="excessive-trading-value-oauth">25%</span>
                    </div>

                    <label>Prompt model</label>
                    <input type="text" name="model" value="__MODEL__">

                    <label>Max tokens</label>
                    <input type="number" name="max_tokens" value="512" min="1">

                    <label>Temperature</label>
                    <input type="number" step="0.1" min="0" max="1" name="temperature" value="0.2">

                    <button type="submit">Connect with ShareFile OAuth</button>
                </form>

                <form action="/run-sharefile-review" method="post" enctype="multipart/form-data">
                    <h2>Direct Token Login</h2>
                    <label>ShareFile domain</label>
                    <input type="text" name="sharefile_domain" placeholder="example.sharefile.com" required>

                    <label>ShareFile client ID</label>
                    <input type="text" name="sharefile_client_id" required>

                    <label>ShareFile client secret</label>
                    <input type="password" name="sharefile_client_secret" required>

                    <label>ShareFile username</label>
                    <input type="text" name="sharefile_username" autocomplete="username">

                    <label>ShareFile password</label>
                    <input type="password" name="sharefile_password" autocomplete="current-password">

                    <label>ShareFile brokerage statements root folder URL or item ID</label>
                    <textarea name="sharefile_root_item_id" rows="3" placeholder="Outside brokerage statements folder URL (whole tree), or one specific rep folder URL per line to target only a few reps" required></textarea>

                    <label>Additional Restricted List CSV</label>
                    <input type="file" name="restricted_csv" accept=".csv">

                    <label>Additional IPO Calendar CSV</label>
                    <input type="file" name="ipo_csv" accept=".csv">

                    <label>Current month override (optional)</label>
                    <input type="text" name="current_month_override" placeholder="auto: current calendar month, e.g. 2026-07">

                    <label>Prior month override (optional)</label>
                    <input type="text" name="prior_month_override" placeholder="auto: prior calendar month, e.g. 2026-06">

                    <label>Excessive Trading Increase Threshold</label>
                    <div class="slider-row">
                        <input
                            type="range"
                            name="excessive_trading_pct"
                            min="0"
                            max="500"
                            step="5"
                            value="25"
                            oninput="document.getElementById('excessive-trading-value-token').textContent = this.value + '%'"
                        >
                        <span class="slider-value" id="excessive-trading-value-token">25%</span>
                    </div>

                    <label>Prompt model</label>
                    <input type="text" name="model" value="__MODEL__">

                    <label>Max tokens</label>
                    <input type="number" name="max_tokens" value="512" min="1">

                    <label>Temperature</label>
                    <input type="number" step="0.1" min="0" max="1" name="temperature" value="0.2">

                    <button type="submit">Run Review</button>
                </form>
            </body>
        </html>
        """
    html_text = html_text.replace("__CALLBACK_URL__", escaped_callback_url)
    html_text = html_text.replace("__MODEL__", escaped_model)
    return HTMLResponse(
        html_text,
        status_code=200,
    )


@app.post("/sharefile/oauth/start")
async def sharefile_oauth_start(
    request: Request,
    sharefile_domain: str = Form(""),
    sharefile_client_id: str = Form(""),
    sharefile_client_secret: str = Form(""),
    sharefile_redirect_uri: str = Form(""),
    sharefile_root_item_id: str = Form(""),
    excessive_trading_pct: int = Form(25),
    model: str = Form(DEFAULT_FORM_MODEL),
    max_tokens: int = Form(512),
    temperature: float = Form(0.2),
    current_month_override: str = Form(""),
    prior_month_override: str = Form(""),
):
    try:
        if not sharefile_domain.strip():
            raise ValueError("ShareFile domain is required.")
        if not sharefile_client_id.strip() or not sharefile_client_secret.strip():
            raise ValueError("ShareFile client ID and secret are required.")
        if not sharefile_redirect_uri.strip():
            raise ValueError("Public HTTPS callback URL is required.")
        if not sharefile_root_item_id.strip():
            raise ValueError("ShareFile brokerage statements root folder URL or item ID is required.")

        sharefile_domain = normalize_sharefile_domain(sharefile_domain)
        state = secrets.token_urlsafe(32)
        redirect_uri = sharefile_redirect_uri.strip()

        PENDING_SHAREFILE_OAUTH[state] = {
            "sharefile_domain": sharefile_domain,
            "sharefile_client_id": sharefile_client_id.strip(),
            "sharefile_client_secret": sharefile_client_secret.strip(),
            "sharefile_root_item_id": sharefile_root_item_id.strip(),
            "excessive_trading_pct": excessive_trading_pct,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "redirect_uri": redirect_uri,
            "current_month_override": current_month_override,
            "prior_month_override": prior_month_override,
        }

        authorization_url = build_sharefile_authorization_url(
            sharefile_domain,
            sharefile_client_id,
            redirect_uri,
            state,
        )
        return RedirectResponse(authorization_url, status_code=303)
    except (ShareFileError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)


@app.get("/sharefile/oauth/callback")
def sharefile_oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    try:
        if error:
            raise ShareFileError(error_description or error)
        if not code or not state:
            raise ShareFileError("ShareFile did not return an authorization code.")

        pending = PENDING_SHAREFILE_OAUTH.pop(state, None)
        if pending is None:
            raise ShareFileError("ShareFile OAuth state expired or was not recognized. Start the OAuth flow again.")

        access_token = exchange_sharefile_authorization_code(
            pending["sharefile_domain"],
            pending["sharefile_client_id"],
            pending["sharefile_client_secret"],
            code,
            pending["redirect_uri"],
        )

        job_id = start_sharefile_review_job(
            pending["sharefile_domain"],
            access_token,
            pending["sharefile_root_item_id"],
            pending["excessive_trading_pct"],
            pending["model"],
            pending["max_tokens"],
            pending["temperature"],
            current_month_override=pending.get("current_month_override", ""),
            prior_month_override=pending.get("prior_month_override", ""),
        )
        return RedirectResponse(f"/sharefile/jobs/{job_id}", status_code=303)
    except (ShareFileError, ValueError, RuntimeError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except Exception as exc:
        return HTMLResponse(f"ShareFile OAuth review failed: {exc}", status_code=502)


@app.post("/run-sharefile-review")
async def run_sharefile_review(
    request: Request,
    sharefile_domain: str = Form(""),
    sharefile_client_id: str = Form(""),
    sharefile_client_secret: str = Form(""),
    sharefile_username: str = Form(""),
    sharefile_password: str = Form(""),
    sharefile_file_id: str = Form(""),
    sharefile_root_item_id: str = Form(""),
    prior_sharefile_item_id: str = Form(""),
    current_sharefile_item_id: str = Form(""),
    excessive_trading_pct: int = Form(25),
    restricted_csv: Optional[UploadFile] = File(None),
    ipo_csv: Optional[UploadFile] = File(None),
    model: str = Form(DEFAULT_FORM_MODEL),
    max_tokens: int = Form(512),
    temperature: float = Form(0.2),
    current_month_override: str = Form(""),
    prior_month_override: str = Form(""),
):
    try:
        if not sharefile_domain.strip():
            raise ValueError("ShareFile domain is required.")
        if not sharefile_client_id.strip() or not sharefile_client_secret.strip():
            raise ValueError("ShareFile client ID and secret are required.")

        root_item = (
            sharefile_root_item_id.strip()
            or current_sharefile_item_id.strip()
            or sharefile_file_id.strip()
        )
        if not root_item:
            raise ValueError("ShareFile brokerage statements root folder URL or item ID is required.")

        sharefile_domain = normalize_sharefile_domain(sharefile_domain)

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

        token = get_sharefile_access_token(
            sharefile_domain,
            sharefile_client_id,
            sharefile_client_secret,
            sharefile_username,
            sharefile_password,
        )

        return run_sharefile_review_from_access_token(
            sharefile_domain,
            token,
            root_item,
            restricted_df,
            ipo_df,
            excessive_trading_pct=excessive_trading_pct,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            current_month_override=current_month_override,
            prior_month_override=prior_month_override,
        )
    except (ShareFileError, ValueError, RuntimeError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except Exception as exc:
        return HTMLResponse(f"ShareFile review failed: {exc}", status_code=502)


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

        rep_name = resolve_rep_name_for_pdf(pdf_path, month_dir)

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
    excessive_trading_pct: int = Form(25),
    restricted_csv: Optional[UploadFile] = File(None),
    ipo_csv: Optional[UploadFile] = File(None),
    prior_zip: Optional[UploadFile] = File(None),
    current_zip: Optional[UploadFile] = File(None),
):
    # Authentication disabled for easier local testing.
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
            if not upload_has_file(prior_zip):
                return HTMLResponse(
                    "Please upload a prior-month ZIP.",
                    status_code=400,
                )
            prior_data = await prior_zip.read()
            prior_path = tmp_root_path / "prior"
            prior_path.mkdir(parents=True, exist_ok=True)
            safe_extract_zip(prior_data, prior_path)

            # Current month source
            if not upload_has_file(current_zip):
                return HTMLResponse(
                    "Please upload a current-month ZIP.",
                    status_code=400,
                )
            current_data = await current_zip.read()
            current_path = tmp_root_path / "current"
            current_path.mkdir(parents=True, exist_ok=True)
            safe_extract_zip(current_data, current_path)

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
