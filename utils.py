import re
import zipfile
from pathlib import Path
import pandas as pd
from io import BytesIO

RESTRICTED_LIST_PATH = Path("restricted_list_prior_month.csv")
IPO_LIST_PATHS = [
    Path("IPO_Calendar_Tracking_Sheet.csv"),
    Path("IPO Calendar Tracking Sheet 6.8.csv"),
]


def iter_pdf_paths(month_dir: Path):
    """
    Yield real PDF files from a directory tree.
    ZIP uploads often preserve uppercase .PDF names and macOS metadata entries.
    """
    for path in sorted(Path(month_dir).rglob("*")):
        name = path.name
        parts = {str(part) for part in path.parts}

        if name.startswith("._") or "__MACOSX" in parts:
            continue

        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue

        try:
            if path.stat().st_size == 0:
                continue
        except Exception:
            pass

        yield path


def safe_extract_zip(data: bytes, target_dir: Path) -> None:
    """
    Safely extract a ZIP file while preventing path traversal.
    """
    if not data:
        raise ValueError("Uploaded ZIP was empty.")

    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            for member in z.infolist():
                destination = (target_dir / member.filename).resolve()
                if target_dir.resolve() not in destination.parents and destination != target_dir.resolve():
                    raise ValueError(f"Unsafe ZIP entry rejected: {member.filename}")
            z.extractall(target_dir)
    except zipfile.BadZipFile as exc:
        raise ValueError("Uploaded file is not a valid ZIP archive.") from exc


def clean_rep_name_from_filename(file_name: str) -> str:
    name = Path(file_name).stem.lower()

    removable_terms = [
        "_statement", "-statement", " statement",
        "_brokerage", "-brokerage", " brokerage",
        "_monthly", "-monthly", " monthly",
    ]

    for term in removable_terms:
        name = name.replace(term, "")

    name = name.replace("_", " ").replace("-", " ").strip()
    return name.title() if name else Path(file_name).stem

_MONTH_NAME_TOKENS = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
}

GENERIC_FILE_STEMS = {
    "statement", "statements", "brokerage", "monthly",
    "doc", "document", "file", "copy", "scan", "review",
}


def _looks_like_bare_month_label(folder_name: str) -> bool:
    """
    True for folder names that are just a month label (e.g. "May",
    "June 2026") and carry no rep/account identity -- e.g. a ZIP made by
    compressing a folder named after the month, with every statement
    dumped directly inside it.
    """
    normalized = re.sub(r"[_-]", " ", str(folder_name or "")).strip().lower()
    tokens = normalized.split()
    if not tokens or len(tokens) > 2:
        return False
    if tokens[0] not in _MONTH_NAME_TOKENS:
        return False
    if len(tokens) == 2 and not re.fullmatch(r"20\d{2}", tokens[1]):
        return False
    return True


def resolve_rep_name_for_pdf(pdf_path: Path, month_dir: Path) -> str:
    """
    Builds a human-readable rep/account identity for a statement PDF.

    Real-world exports vary a lot: some nest one PDF per rep/account
    folder, some (e.g. ShareFile's "Rep/Broker/AccountNumber/statement.pdf"
    structure) nest several levels deep with the filename itself already
    self-sufficient (e.g. "20260630 David Rosen Individual TOD 6352.PDF"),
    and some dump many files into one shared, non-identifying wrapper
    folder (e.g. a ZIP compressed from a folder literally named "May").

    Strategy: prefer the filename (stripped of any leading date) when it's
    already descriptive; otherwise combine it with the top-level folder
    directly under `month_dir` -- which is reliably the rep's own folder
    in ShareFile's structure -- unless that top folder is just a bare
    month label, which carries no identity.

    Deliberately depends only on this file's own relative path, never on
    how many other files happen to share a folder, so the same file
    resolves to the same name whether it lands in the "current" or "prior"
    bucket -- otherwise month-over-month comparisons silently stop
    matching up.
    """
    try:
        relative_parts = list(pdf_path.relative_to(month_dir).parts)
    except ValueError:
        relative_parts = [pdf_path.name]

    filename_stem = re.sub(r"^\d{4}[-_]?\d{2}[-_]?\d{2}[\s_-]*", "", pdf_path.stem)
    filename_identity = clean_rep_name_from_filename(filename_stem)

    if len(relative_parts) <= 1:
        return filename_identity

    top_folder = relative_parts[0]
    if _looks_like_bare_month_label(top_folder):
        return filename_identity

    top_identity = clean_rep_name_from_filename(top_folder)

    if top_identity.strip().lower() in filename_identity.strip().lower():
        return filename_identity

    if not filename_identity or filename_identity.strip().lower() in GENERIC_FILE_STEMS:
        return top_identity

    return f"{top_identity} {filename_identity}".strip()


def move_leading_account_number_from_name(name: str, account_number: str):
    """
    Some statement filenames start with the account id, while the PDF text
    itself may not expose a parsable account number. Move that leading id into
    the Account Number column so the name column stays human-readable.
    """
    clean_name = str(name or "").strip()
    clean_account = str(account_number or "").strip()

    match = re.match(r"^(\d{6,12})\s+(.+)$", clean_name)
    if not match:
        return clean_name, clean_account

    leading_account, remaining_name = match.groups()

    if clean_account and clean_account.lower() != "unknown":
        return clean_name, clean_account

    return remaining_name.strip(), leading_account


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [
        str(col).strip().lower().replace(" ", "_")
        for col in df.columns
    ]
    return df


def load_restricted_list(uploaded_csv=None) -> pd.DataFrame:
    import io
    from pandas.errors import EmptyDataError

    def empty_df():
        return pd.DataFrame(
            columns=["stock_symbol", "stock_name", "date_added", "date_removed"]
        )

    if uploaded_csv is not None:
        try:
            if hasattr(uploaded_csv, "read"):
                uploaded_csv.seek(0)
                raw = uploaded_csv.read()

                if isinstance(raw, bytes):
                    if not raw:
                        return empty_df()

                    try:
                        text_io = io.StringIO(raw.decode("utf-8"))
                    except UnicodeDecodeError:
                        text_io = io.StringIO(raw.decode("latin-1"))

                    df = pd.read_csv(text_io)
                else:
                    if not str(raw).strip():
                        return empty_df()
                    df = pd.read_csv(io.StringIO(str(raw)))

            else:
                if isinstance(uploaded_csv, (bytes, bytearray)):
                    if not uploaded_csv:
                        return empty_df()
                    try:
                        df = pd.read_csv(io.StringIO(uploaded_csv.decode("utf-8")))
                    except UnicodeDecodeError:
                        df = pd.read_csv(io.BytesIO(uploaded_csv))
                else:
                    df = pd.read_csv(uploaded_csv)

        except EmptyDataError:
            return empty_df()
        except Exception:
            return empty_df()

    elif RESTRICTED_LIST_PATH.exists():
        try:
            df = pd.read_csv(RESTRICTED_LIST_PATH)
        except Exception:
            return empty_df()
    else:
        return empty_df()

    df = normalize_columns(df)

    rename_map = {
        "stock": "stock_symbol",
        "symbol": "stock_symbol",
        "ticker": "stock_symbol",
        "name": "stock_name",
        "issuer_name": "stock_name",
        "company_name": "stock_name",
    }

    for old, new in rename_map.items():
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})

    for col in ["stock_symbol", "stock_name", "date_added", "date_removed"]:
        if col not in df.columns:
            df[col] = ""

    df["stock_symbol"] = (
        df["stock_symbol"]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    df["stock_name"] = (
        df["stock_name"]
        .astype(str)
        .str.replace("_", " ", regex=False)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    has_symbol = (df["stock_symbol"] != "") & (df["stock_symbol"].str.lower() != "nan")
    has_name = (df["stock_name"] != "") & (df["stock_name"].str.lower() != "nan")

    df = df[has_symbol | has_name]

    return df[["stock_symbol", "stock_name", "date_added", "date_removed"]]


def load_ipo_restriction_list(uploaded_csv=None) -> pd.DataFrame:
    if uploaded_csv is not None:
        df = load_restricted_list(uploaded_csv)
        return df[["stock_symbol", "stock_name"]]

    for ipo_list_path in IPO_LIST_PATHS:
        if ipo_list_path.exists():
            df = load_restricted_list(ipo_list_path)
            return df[["stock_symbol", "stock_name"]]

    return pd.DataFrame(columns=["stock_symbol", "stock_name"])


def dataframe_to_excel_bytes(df: pd.DataFrame):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Flags")
        worksheet = writer.sheets["Flags"]

        worksheet.freeze_panes = "A2"
        worksheet.sheet_view.showGridLines = False

        header_fill = PatternFill("solid", fgColor="1F2937")
        header_font = Font(
            name="Times New Roman",
            size=16,
            bold=True,
            color="FFFFFF",
        )
        body_font = Font(name="Times New Roman", size=16)
        thin_gray = Side(style="thin", color="D1D5DB")
        border = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)

        for row in worksheet.iter_rows():
            for cell in row:
                cell.font = body_font
                cell.border = border
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=True,
                )

        for header_cell in worksheet[1]:
            header_cell.fill = header_fill
            header_cell.font = header_font
            header_cell.alignment = Alignment(
                horizontal="center",
                vertical="center",
                wrap_text=True,
            )

        preferred_widths = {
            "Name": 34,
            "Account Number": 20,
            "Current-Month Scan": 56,
            "Prior Month vs Current Month Comparison": 44,
        }

        for column_index, column_cells in enumerate(worksheet.columns, start=1):
            header = str(column_cells[0].value or "")
            max_length = 0
            column_letter = get_column_letter(column_index)

            for cell in column_cells:
                value = str(cell.value) if cell.value is not None else ""
                max_length = max(max_length, len(value))

            worksheet.column_dimensions[column_letter].width = preferred_widths.get(
                header,
                min(max_length + 4, 70),
            )

        worksheet.row_dimensions[1].height = 38

        for row in worksheet.iter_rows(min_row=2):
            line_count = max(str(cell.value or "").count("\n") + 1 for cell in row)
            worksheet.row_dimensions[row[0].row].height = max(42, min(34 * line_count, 150))

        if worksheet.max_row >= 2 and worksheet.max_column >= 1:
            table_ref = (
                f"A1:{get_column_letter(worksheet.max_column)}{worksheet.max_row}"
            )
            table = Table(displayName="FlagResults", ref=table_ref)
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=False,
                showColumnStripes=False,
            )
            worksheet.add_table(table)

    return output.getvalue()
