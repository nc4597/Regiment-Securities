import json
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

NETWORK_RETRIES = 3
NETWORK_RETRY_BACKOFF_SECONDS = 1.5


def _with_network_retries(attempt_fn, description: str):
    """
    Retries transient network failures (e.g. connection reset mid-stream,
    which surfaces as a bare OSError rather than urllib.error.URLError since
    it happens during response.read(), not during connect). HTTPError
    (proper 4xx/5xx responses) is not retried -- that's an application-level
    error, not a transient blip.
    """
    last_exc = None
    for attempt in range(1, NETWORK_RETRIES + 1):
        try:
            return attempt_fn()
        except HTTPError:
            raise
        except (URLError, OSError) as exc:
            last_exc = exc
            if attempt < NETWORK_RETRIES:
                time.sleep(NETWORK_RETRY_BACKOFF_SECONDS * attempt)
    raise ShareFileError(
        f"{description} failed after {NETWORK_RETRIES} attempts: {last_exc}"
    ) from last_exc

from utils import safe_extract_zip


class ShareFileError(Exception):
    pass


def normalize_sharefile_domain(domain_or_url: str) -> str:
    cleaned = str(domain_or_url or "").strip()
    if not cleaned:
        raise ShareFileError("ShareFile domain is required.")

    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"

    parsed = urllib.parse.urlparse(cleaned)
    domain = (parsed.netloc or parsed.path).split("/")[0].strip()
    if not domain:
        raise ShareFileError("ShareFile domain is required.")

    return domain


def sharefile_subdomain(domain_or_url: str) -> str:
    domain = normalize_sharefile_domain(domain_or_url)
    first_label = domain.split(".", 1)[0].strip()
    if not first_label:
        raise ShareFileError("Could not determine ShareFile subdomain.")
    return first_label


def sharefile_api_domains(domain_or_url: str):
    domain = normalize_sharefile_domain(domain_or_url)
    subdomain = sharefile_subdomain(domain)
    candidates = [
        domain,
        f"{subdomain}.sf-api.com",
        f"{subdomain}.sharefile.com",
    ]

    unique_domains = []
    for candidate in candidates:
        if candidate not in unique_domains:
            unique_domains.append(candidate)
    return unique_domains


def _safe_error_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
    )


def _sharefile_item_url_candidates(api_domain: str, item_id: str, suffix: str = ""):
    encoded_item_id = urllib.parse.quote(item_id, safe="")
    quoted_url = f"https://{api_domain}/sf/v3/Items('{encoded_item_id}'){suffix}"
    bare_url = f"https://{api_domain}/sf/v3/Items({encoded_item_id}){suffix}"
    return [quoted_url, bare_url]


def _sharefile_error_message(exc: HTTPError) -> str:
    body = ""
    try:
        body = exc.read().decode("utf-8")
    except Exception:
        body = ""

    if body:
        try:
            data = json.loads(body)
            message = (
                data.get("error_description")
                or data.get("error", {}).get("message")
                or data.get("message")
                or data.get("error")
                or body
            )
        except Exception:
            message = body
    else:
        message = exc.reason or str(exc)

    url = _safe_error_url(getattr(exc, "url", "") or "")
    if url:
        return f"{message} (endpoint: {url})"

    return message


def _read_error_message(exc: HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
        data = json.loads(body)
        return (
            data.get("error_description")
            or data.get("error", {}).get("message")
            or data.get("message")
            or data.get("error")
            or body
        )
    except Exception:
        return exc.reason or str(exc)


def _http_get_json(url: str, headers: Optional[dict] = None) -> dict:
    request = Request(url, method="GET")
    if headers:
        for name, value in headers.items():
            request.add_header(name, value)

    def attempt():
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload)
        except HTTPError as exc:
            message = _sharefile_error_message(exc)
            raise ShareFileError(f"ShareFile API error {exc.code}: {message}") from exc

    return _with_network_retries(attempt, f"ShareFile request to {url}")


def _http_post_form_json(url: str, form_data: dict, headers: Optional[dict] = None) -> dict:
    encoded = urllib.parse.urlencode(form_data).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    if headers:
        for name, value in headers.items():
            request.add_header(name, value)

    def attempt():
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload)
        except HTTPError as exc:
            message = _sharefile_error_message(exc)
            if exc.code == 400 and "client_id" in message and "client_secret" in message:
                message = (
                    f"{message}. Check that you are using the ShareFile API Client ID/API Key "
                    "and Client Secret for this account, and that the domain matches the "
                    "ShareFile subdomain that owns those credentials."
                )
            raise ShareFileError(f"ShareFile API error {exc.code}: {message}") from exc

    return _with_network_retries(attempt, f"ShareFile request to {url}")


def build_sharefile_authorization_url(
    domain: str,
    client_id: str,
    redirect_uri: str,
    state: str = "",
) -> str:
    if not domain or not client_id or not redirect_uri:
        raise ShareFileError("ShareFile domain, client ID, and redirect URI are required.")

    subdomain = sharefile_subdomain(domain)
    state = state or secrets.token_urlsafe(24)
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id.strip(),
            "redirect_uri": redirect_uri,
            "state": state,
            "subdomain": subdomain,
        }
    )
    return f"https://secure.sharefile.com/oauth/authorize?{params}"


def exchange_sharefile_authorization_code(
    domain: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str:
    if not domain or not client_id or not client_secret or not code or not redirect_uri:
        raise ShareFileError(
            "ShareFile domain, client ID, client secret, authorization code, and redirect URI are required."
        )

    domain = normalize_sharefile_domain(domain)
    subdomain = sharefile_subdomain(domain)
    client_id = client_id.strip()
    client_secret = client_secret.strip()

    token_payloads = [
        (
            f"https://{domain}/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        ),
        (
            "https://secure.sharefile.com/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "subdomain": subdomain,
            },
        ),
        (
            "https://secure.sf-api.com/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "subdomain": subdomain,
            },
        ),
    ]

    errors = []
    for token_url, payload in token_payloads:
        try:
            tokens = _http_post_form_json(token_url, payload)
            access_token = tokens.get("access_token")
            if access_token:
                return access_token
            errors.append(f"ShareFile did not return an access token from {_safe_error_url(token_url)}")
        except ShareFileError as exc:
            errors.append(str(exc))

    raise ShareFileError(
        "ShareFile authorization-code exchange failed for all known OAuth endpoints. "
        + " | ".join(errors)
    )


def _http_get_bytes(url: str, headers: Optional[dict] = None) -> bytes:
    request = Request(url, method="GET")
    if headers:
        for name, value in headers.items():
            request.add_header(name, value)

    def attempt():
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            message = _sharefile_error_message(exc)
            raise ShareFileError(f"ShareFile download failed {exc.code}: {message}") from exc

    return _with_network_retries(attempt, f"ShareFile download from {url}")


def _extract_sharefile_item_id(item_id_or_url: str) -> str:
    cleaned = str(item_id_or_url or "").strip()
    if not cleaned:
        raise ShareFileError("ShareFile item ID or folder URL is required.")

    if cleaned.startswith("http"):
        parsed = urllib.parse.urlparse(cleaned)
        path = parsed.path or ""

        query = urllib.parse.parse_qs(parsed.query)
        for key in ["id", "itemid", "itemId", "folderid", "folderId", "fileid", "fileId"]:
            values = query.get(key)
            if values and values[0].strip():
                return values[0].strip()

        fragment = urllib.parse.unquote(parsed.fragment or "")
        fragment_match = re.search(
            r"(?:^|[/?&#])(fo[a-f0-9-]{32,}|fi[a-f0-9-]{32,}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(?:$|[/?&#])",
            fragment,
            flags=re.IGNORECASE,
        )
        if fragment_match:
            return fragment_match.group(1)

        match = re.search(r"Items\(['\"]?([^'\"\)]+)['\"]?\)", path)
        if match:
            return match.group(1)

        token_match = re.search(
            r"(?:^|/)(fo[a-f0-9-]{32,}|fi[a-f0-9-]{32,}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(?:/|$)",
            path,
            flags=re.IGNORECASE,
        )
        if token_match:
            return token_match.group(1)

        segments = [segment for segment in path.split("/") if segment]
        if "f" in segments:
            index = segments.index("f")
            if index + 1 < len(segments):
                return segments[index + 1]

        raise ShareFileError(
            "Could not find a ShareFile item or folder ID in that URL. "
            "Open the folder in ShareFile and copy the full browser address, "
            "or paste the folder's raw item ID."
        )

    return cleaned


def _get_sharefile_item_metadata(
    domain: str,
    access_token: str,
    item_id: str,
) -> Dict[str, Any]:
    if not domain or not access_token or not item_id:
        raise ShareFileError("ShareFile domain, access token, and item ID are required.")

    headers = {"Authorization": f"Bearer {access_token}"}
    errors = []
    for api_domain in sharefile_api_domains(domain):
        for metadata_url in _sharefile_item_url_candidates(api_domain, item_id):
            try:
                return _http_get_json(metadata_url, headers=headers)
            except ShareFileError as exc:
                errors.append(str(exc))

    raise ShareFileError(
        f"ShareFile item lookup failed for extracted item ID '{item_id}'. "
        "Make sure the root folder field contains the actual ShareFile folder URL/item ID "
        "and that the logged-in user can access it. "
        + " | ".join(errors)
    )


def _is_sharefile_folder(metadata: Dict[str, Any]) -> bool:
    if not metadata:
        return False
    type_value = str(
        metadata.get("Type", "")
        or metadata.get("ItemType", "")
        or metadata.get("FileType", "")
        or metadata.get("__type", "")
        or metadata.get("odata.type", "")
    ).lower()
    if "folder" in type_value:
        return True

    url = str(metadata.get("url", "") or metadata.get("Url", "")).lower()
    if "/folder" in url or "/folders" in url:
        return True

    if metadata.get("Children") is not None or metadata.get("children") is not None:
        return True

    return False


def _list_sharefile_folder_children(
    domain: str,
    access_token: str,
    folder_id: str,
) -> List[Dict[str, Any]]:
    if not domain or not access_token or not folder_id:
        raise ShareFileError("ShareFile domain, access token, and folder ID are required.")

    headers = {"Authorization": f"Bearer {access_token}"}
    errors = []
    for api_domain in sharefile_api_domains(domain):
        for children_url in _sharefile_item_url_candidates(api_domain, folder_id, "/Children"):
            try:
                data = _http_get_json(children_url, headers=headers)
                if isinstance(data, list):
                    return data
                return (
                    data.get("value")
                    or data.get("Children")
                    or data.get("children")
                    or data.get("Items")
                    or data.get("items")
                    or []
                )
            except ShareFileError as exc:
                errors.append(str(exc))

    raise ShareFileError(
        f"ShareFile folder child lookup failed for folder ID '{folder_id}'. "
        + " | ".join(errors)
    )


def get_sharefile_access_token(
    domain: str,
    client_id: str,
    client_secret: str,
    username: str = "",
    password: str = "",
) -> str:
    if not domain or not client_id or not client_secret:
        raise ShareFileError("ShareFile domain, client ID, and client secret are required.")

    domain = normalize_sharefile_domain(domain)
    subdomain = sharefile_subdomain(domain)
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    username = username.strip()
    password = password.strip()

    token_payloads = []

    if username and password:
        token_payloads.extend([
            (
                f"https://{domain}/oauth/token",
                {
                    "grant_type": "password",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "username": username,
                    "password": password,
                },
            ),
            (
                "https://secure.sharefile.com/oauth/token",
                {
                    "grant_type": "password",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "username": username,
                    "password": password,
                    "subdomain": subdomain,
                },
            ),
            (
                "https://secure.sf-api.com/oauth/token",
                {
                    "grant_type": "password",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "username": username,
                    "password": password,
                    "subdomain": subdomain,
                },
            ),
        ])

    token_payloads.extend([
            (
                f"https://{domain}/oauth/token",
                {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            ),
            (
                "https://secure.sharefile.com/oauth/token",
                {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "subdomain": subdomain,
                },
            ),
            (
                "https://secure.sf-api.com/oauth/token",
                {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "subdomain": subdomain,
                },
            ),
    ])

    errors = []
    tokens = None
    for token_url, payload in token_payloads:
        try:
            tokens = _http_post_form_json(token_url, payload)
            break
        except ShareFileError as exc:
            errors.append(str(exc))

    if tokens is None:
        raise ShareFileError(
            "ShareFile token request failed for all known OAuth endpoints. "
            + " | ".join(errors)
        )

    access_token = tokens.get("access_token")
    if not access_token:
        raise ShareFileError("ShareFile did not return an access token.")
    return access_token


def download_sharefile_file(
    domain: str,
    access_token: str,
    file_id: str,
) -> bytes:
    if not domain or not access_token or not file_id:
        raise ShareFileError("ShareFile domain, access token, and file ID are required.")

    headers = {"Authorization": f"Bearer {access_token}"}
    errors = []
    for api_domain in sharefile_api_domains(domain):
        for file_url in _sharefile_item_url_candidates(api_domain, file_id, "/Download"):
            try:
                return _http_get_bytes(file_url, headers=headers)
            except ShareFileError as exc:
                errors.append(str(exc))

    raise ShareFileError(
        f"ShareFile file download failed for file ID '{file_id}'. "
        + " | ".join(errors)
    )


def get_sharefile_item_name(domain: str, access_token: str, item_id_or_url: str) -> str:
    """
    Looks up an item's own display name without downloading its contents.
    Used when targeting a specific rep's folder directly as a root item --
    the item's own name (e.g. "David Rosen") needs to be preserved as a
    directory level, since downloading a folder's *children* straight into
    a target directory does not otherwise carry the folder's own name.
    """
    domain = normalize_sharefile_domain(domain)
    item_id = _extract_sharefile_item_id(item_id_or_url)
    metadata = _get_sharefile_item_metadata(domain, access_token, item_id)
    return str(metadata.get("Name") or metadata.get("name") or item_id)


def download_sharefile_item_to_directory(
    domain: str,
    access_token: str,
    item_id_or_url: str,
    target_dir: Path,
    progress_callback: Optional[Callable[[str], None]] = None,
    file_name_filter: Optional[Callable[[str], bool]] = None,
    file_count: Optional[List[int]] = None,
    skipped_count: Optional[List[int]] = None,
    skipped_files: Optional[List[str]] = None,
) -> str:
    domain = normalize_sharefile_domain(domain)
    item_id = _extract_sharefile_item_id(item_id_or_url)
    if progress_callback:
        progress_callback("Looking up the ShareFile root item.")
    metadata = _get_sharefile_item_metadata(domain, access_token, item_id)

    if _is_sharefile_folder(metadata):
        if progress_callback:
            progress_callback("Downloading files from ShareFile folders.")
        _download_sharefile_folder_items(
            domain,
            access_token,
            item_id,
            target_dir,
            visited_folder_ids=set(),
            depth=0,
            progress_callback=progress_callback,
            file_name_filter=file_name_filter,
            file_count=file_count,
            skipped_count=skipped_count,
            skipped_files=skipped_files,
        )
        return item_id

    file_name = str(metadata.get("Name", item_id))
    file_bytes = download_sharefile_file(domain, access_token, item_id)

    if file_name.lower().endswith(".zip"):
        safe_extract_zip(file_bytes, target_dir)
    else:
        target_path = target_dir / file_name
        target_path.write_bytes(file_bytes)

    return item_id


def _download_sharefile_folder_items(
    domain: str,
    access_token: str,
    folder_id: str,
    target_dir: Path,
    visited_folder_ids: Optional[set] = None,
    depth: int = 0,
    max_depth: int = 25,
    max_files: int = 5000,
    progress_callback: Optional[Callable[[str], None]] = None,
    file_count: Optional[List[int]] = None,
    file_name_filter: Optional[Callable[[str], bool]] = None,
    skipped_count: Optional[List[int]] = None,
    skipped_files: Optional[List[str]] = None,
) -> None:
    visited_folder_ids = visited_folder_ids if visited_folder_ids is not None else set()
    file_count = file_count if file_count is not None else [0]
    skipped_count = skipped_count if skipped_count is not None else [0]
    if folder_id in visited_folder_ids:
        return
    visited_folder_ids.add(folder_id)

    if depth > max_depth:
        raise ShareFileError(
            f"ShareFile folder traversal exceeded {max_depth} nested levels. "
            "Check for an unexpected folder loop or choose a more specific root folder."
        )

    children = _list_sharefile_folder_children(domain, access_token, folder_id)
    if progress_callback:
        progress_callback(
            f"Scanning ShareFile folder level {depth + 1}; found {len(children)} items."
        )

    for child_metadata in children:
        if file_count[0] >= max_files:
            raise ShareFileError(
                f"Stopped after downloading {max_files} files. "
                "Choose a narrower ShareFile root folder or raise the download limit in code."
            )

        child_id = str(child_metadata.get("Id") or child_metadata.get("id") or "").strip()
        if not child_id:
            continue

        full_child_metadata = child_metadata

        child_name = str(
            full_child_metadata.get("Name")
            or full_child_metadata.get("name")
            or child_metadata.get("Name")
            or child_metadata.get("name")
            or child_id
        )

        if _is_sharefile_folder(full_child_metadata):
            subdir = target_dir / child_name
            subdir.mkdir(parents=True, exist_ok=True)
            _download_sharefile_folder_items(
                domain,
                access_token,
                child_id,
                subdir,
                visited_folder_ids=visited_folder_ids,
                depth=depth + 1,
                max_depth=max_depth,
                max_files=max_files,
                progress_callback=progress_callback,
                file_count=file_count,
                file_name_filter=file_name_filter,
                skipped_count=skipped_count,
                skipped_files=skipped_files,
            )
            continue

        if file_name_filter is not None and not file_name_filter(child_name):
            skipped_count[0] += 1
            if skipped_files is not None:
                skipped_files.append(str(target_dir / child_name))
            continue

        file_bytes = download_sharefile_file(domain, access_token, child_id)
        if child_name.lower().endswith(".zip"):
            safe_extract_zip(file_bytes, target_dir)
        else:
            target_path = target_dir / child_name
            target_path.write_bytes(file_bytes)
        file_count[0] += 1
