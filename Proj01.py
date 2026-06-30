from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


DEFAULT_START_URL = "https://kjxyjy.91wllm.cn/admin/"
DEFAULT_GRID_SELECTOR = "#jinfo-grid table tbody tr"
DEFAULT_CONFIG_PATH = "crawler_config.py"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".pdf"}
CLASS_HEADERS = ("班级", "所在班级", "行政班")
STUDENT_ID_HEADERS = ("学号", "学生学号")
NAME_HEADERS = ("学生姓名", "姓名")
PROOF_HEADERS = ("上传就业相关证明", "就业相关证明", "材料", "附件")
STATUS_DOWNLOADED = "已下载"
STATUS_SKIPPED_EXISTING = "已存在，跳过"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "cookie_json": "cookies.json",
    "start_url": DEFAULT_START_URL,
    "out": "downloads",
    "insecure": True,
    "headful": False,
    "grid_selector": DEFAULT_GRID_SELECTOR,
    "next_selector": None,
    "no_pagination": False,
    "max_pages": 200,
    "limit": 5,
    "timeout_ms": 30000,
    "class_col": None,
    "student_id_col": None,
    "name_col": None,
    "attachment_col": None,
}
CONFIG_VARIABLES = {
    "COOKIE_JSON": "cookie_json",
    "START_URL": "start_url",
    "OUT_DIR": "out",
    "INSECURE": "insecure",
    "HEADFUL": "headful",
    "GRID_SELECTOR": "grid_selector",
    "NEXT_SELECTOR": "next_selector",
    "NO_PAGINATION": "no_pagination",
    "MAX_PAGES": "max_pages",
    "LIMIT": "limit",
    "TIMEOUT_MS": "timeout_ms",
    "CLASS_COL": "class_col",
    "STUDENT_ID_COL": "student_id_col",
    "NAME_COL": "name_col",
    "ATTACHMENT_COL": "attachment_col",
}

ProgressCallback = Callable[[str, "Counters | None"], None]


@dataclass
class ParsedRow:
    page_index: int
    row_index: int
    class_name: str
    student_id: str
    name: str
    row_text: str
    attachments: list[str]


@dataclass
class Counters:
    pages: int = 0
    table_rows: int = 0
    rows_with_attachments: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    metadata_failures: int = 0
    download_failures: int = 0


def emit_progress(callback: ProgressCallback | None, message: str, counters: Counters | None = None) -> None:
    if callback is not None:
        callback(message, counters)


def stop_requested(stop_event: Any | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def sanitize_filename_part(value: str, fallback: str) -> str:
    value = clean_text(value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return value or fallback


def decode_wrapped_url(raw_url: str) -> str:
    if not raw_url:
        return ""

    parsed = urlparse(raw_url)
    query = parse_qs(parsed.query)
    wrapped = query.get("imageUrl") or query.get("url")
    if wrapped:
        value = wrapped[0]
        for _ in range(3):
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
        return value

    return raw_url


def url_extension(url: str) -> str:
    path = unquote(urlparse(url).path).lower()
    suffix = Path(path).suffix
    return suffix if suffix in ALLOWED_EXTENSIONS else ""


def extension_from_content_type(content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "text/html": ".html",
    }.get(content_type, "")


def is_allowed_attachment_url(url: str) -> bool:
    parsed = urlparse(url)
    path = unquote(parsed.path).lower()
    if is_direct_file_url(url):
        return True
    return is_agreement_page_url(url)


def is_direct_file_url(url: str) -> bool:
    parsed = urlparse(url)
    path = unquote(parsed.path).lower()
    return "/attachment/" in path and Path(path).suffix in ALLOWED_EXTENSIONS


def is_agreement_page_url(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    return "/electronic/default/agreement/" in path


def extract_download_urls_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()

    for tag in soup.find_all(True):
        for attr in ("href", "src", "data-url", "data-href", "data-original"):
            raw_value = tag.get(attr)
            if not raw_value:
                continue
            decoded = decode_wrapped_url(raw_value)
            absolute = urljoin(base_url, decoded)
            if is_direct_file_url(absolute) and absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)

    for match in re.finditer(r"((?:https?:)?//[^\"'\s<>]+/attachment/[^\"'\s<>]+|/attachment/[^\"'\s<>]+)", html):
        raw_value = match.group(1).replace("&amp;", "&")
        decoded = decode_wrapped_url(raw_value)
        absolute = urljoin(base_url, decoded)
        if is_direct_file_url(absolute) and absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)

    return urls


def resolve_attachment_urls(
    session: requests.Session,
    row: ParsedRow,
    url: str,
    verify_tls: bool,
) -> tuple[list[str], dict[str, Any] | None]:
    if not is_agreement_page_url(url):
        return [url], None

    try:
        response = session.get(url, timeout=(15, 120), verify=verify_tls)
    except requests.RequestException as exc:
        return [], download_failure_record(row, url, "", str(exc))

    if response.status_code != 200:
        return [], download_failure_record(row, url, response.status_code, f"HTTP {response.status_code}")

    content_type = response.headers.get("Content-Type", "")
    extension = extension_from_content_type(content_type)
    if extension and extension != ".html":
        return [url], None

    urls = extract_download_urls_from_html(response.text, response.url)
    return urls or [url], None


def extract_headers(grid: Any) -> list[str]:
    selectors = (
        "thead tr",
        ".gridHeader table tr",
        ".gridHeader tr",
        "table tr",
    )
    for selector in selectors:
        for row in grid.select(selector):
            cells = row.find_all(["th", "td"], recursive=False)
            texts = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]
            if not texts:
                continue
            if selector == "table tr" and not row.find("th"):
                continue
            header_choices = CLASS_HEADERS + STUDENT_ID_HEADERS + NAME_HEADERS + PROOF_HEADERS
            if any(match_header(text, header_choices) for text in texts):
                return texts
    return []


def match_header(header: str, choices: tuple[str, ...]) -> bool:
    normalized = clean_text(header)
    return any(normalized == choice or choice in normalized for choice in choices)


def find_header_column(headers: list[str], choices: tuple[str, ...]) -> int | None:
    for index, header in enumerate(headers):
        normalized = clean_text(header)
        if any(normalized == choice for choice in choices):
            return index
    for index, header in enumerate(headers):
        if match_header(header, choices):
            return index
    return None


def manual_column(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise ValueError("列号必须是从 1 开始的正整数。")
    return value - 1


def value_at(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return clean_text(cells[index])


def infer_class_name(cells: list[str]) -> str:
    candidates = [cell for cell in cells if re.fullmatch(r"20\d{6}", cell)]
    return candidates[0] if len(candidates) == 1 else ""


def infer_student_id(cells: list[str]) -> str:
    candidates = [cell for cell in cells if re.fullmatch(r"20\d{8,10}", cell)]
    return candidates[0] if len(candidates) == 1 else ""


def infer_name(cells: list[str]) -> str:
    stop_words = {
        "已上传",
        "未上传",
        "查看",
        "下载",
        "编辑",
        "删除",
        "详情",
        "男",
        "女",
        "是",
        "否",
        "无",
        "暂无",
    }
    bad_keywords = ("学院", "专业", "班级", "就业", "上传", "查看", "下载", "电话", "手机")
    candidates: list[str] = []
    for cell in cells:
        if cell in stop_words or any(keyword in cell for keyword in bad_keywords):
            continue
        if re.fullmatch(r"[\u4e00-\u9fff·]{2,8}", cell):
            candidates.append(cell)
    return candidates[0] if len(candidates) == 1 else ""


def find_body_rows(grid: Any) -> list[Any]:
    rows = grid.select(".gridScroller table tbody tr")
    if rows:
        return rows

    rows = grid.select("table tbody tr")
    if rows:
        return rows

    return [
        row
        for row in grid.select("table tr")
        if row.find_all("td", recursive=False)
    ]


def ordered_attachment_cells(cells: list[Any], attachment_index: int | None) -> list[Any]:
    ordered: list[Any] = []
    if attachment_index is not None and 0 <= attachment_index < len(cells):
        ordered.append(cells[attachment_index])
    elif len(cells) >= 20:
        ordered.append(cells[19])

    ordered.extend(cell for cell in cells if cell not in ordered)
    return ordered


def extract_attachment_urls(cells: list[Any], base_url: str, attachment_index: int | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    for cell in ordered_attachment_cells(cells, attachment_index):
        for tag in cell.find_all(True):
            for attr in ("href", "src", "data-url", "data-href"):
                raw_value = tag.get(attr)
                if not raw_value:
                    continue
                decoded = decode_wrapped_url(raw_value)
                absolute = urljoin(base_url, decoded)
                if is_allowed_attachment_url(absolute) and absolute not in seen:
                    seen.add(absolute)
                    urls.append(absolute)

    return urls


def parse_grid_html(
    html: str,
    base_url: str,
    page_index: int,
    class_col: int | None,
    student_id_col: int | None,
    name_col: int | None,
    attachment_col: int | None,
) -> list[ParsedRow]:
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.select_one("#jinfo-grid") or soup
    headers = extract_headers(grid)

    class_index = manual_column(class_col) if class_col else find_header_column(headers, CLASS_HEADERS)
    student_id_index = (
        manual_column(student_id_col) if student_id_col else find_header_column(headers, STUDENT_ID_HEADERS)
    )
    name_index = manual_column(name_col) if name_col else find_header_column(headers, NAME_HEADERS)
    attachment_index = manual_column(attachment_col) if attachment_col else find_header_column(headers, PROOF_HEADERS)

    parsed_rows: list[ParsedRow] = []
    for row_index, row in enumerate(find_body_rows(grid), start=1):
        cell_nodes = row.find_all("td", recursive=False)
        if not cell_nodes:
            continue

        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in cell_nodes]
        row_text = " | ".join(cell for cell in cells if cell)
        class_name = value_at(cells, class_index) or infer_class_name(cells)
        student_id = value_at(cells, student_id_index) or infer_student_id(cells)
        name = value_at(cells, name_index) or infer_name(cells)
        attachments = extract_attachment_urls(cell_nodes, base_url, attachment_index)

        parsed_rows.append(
            ParsedRow(
                page_index=page_index,
                row_index=row_index,
                class_name=class_name,
                student_id=student_id,
                name=name,
                row_text=row_text,
                attachments=attachments,
            )
        )

    return parsed_rows


def default_cookie_domain(start_url: str) -> str:
    host = urlparse(start_url).hostname or "kjxyjy.91wllm.cn"
    if host == "91wllm.cn" or host.endswith(".91wllm.cn"):
        return ".91wllm.cn"
    return host


def cookie_file_help(path: Path) -> str:
    return cookie_input_help(str(path))


def cookie_input_help(source: str = "Cookie 输入") -> str:
    return (
        f"{source} 必须保存浏览器 Cookie。支持的格式："
        'JSON Cookie 列表、{"cookies": [...]} 对象、名称/值 JSON 对象，'
        '或一整行原始请求头 "Cookie: name=value; ..."。'
        "建议从已登录后台页面的 Network 请求里复制 Request Headers 的完整 Cookie。"
    )


def parse_cookie_header(value: str, default_host: str) -> list[dict[str, str]]:
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1]

    cookies: list[dict[str, str]] = []
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not name:
            continue
        cookies.append({"name": name.strip(), "value": cookie_value.strip(), "domain": default_host, "path": "/"})
    return cookies


def load_cookie_text(raw_text: str, start_url: str, source: str = "Cookie 输入") -> list[dict[str, Any]]:
    default_host = default_cookie_domain(start_url)
    stripped = raw_text.strip()
    if not stripped:
        raise ValueError(f"{source} 为空。{cookie_input_help(source)}")

    for line in stripped.splitlines():
        line = line.strip()
        if line.lower().startswith("cookie:"):
            stripped = line
            break

    if stripped.lower().startswith("cookie:"):
        raw_cookies = parse_cookie_header(stripped, default_host)
    elif "=" in stripped and ";" in stripped and "\n" not in stripped and "{" not in stripped:
        raw_cookies = parse_cookie_header(stripped, default_host)
    else:
        if stripped.startswith("fetch("):
            raise ValueError(
                f"{source} 是复制出来的 fetch(...) 记录，不是 Cookie。"
                "浏览器导出的 fetch 记录通常只显示 credentials='include'，不包含真正的 Cookie 请求头。"
                f"{cookie_input_help(source)}"
            )
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{source} 不是有效的 Cookie JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}。"
                f"{cookie_input_help(source)}"
            ) from exc

        if isinstance(data, list):
            raw_cookies = data
        elif isinstance(data, dict):
            raw_cookies = data.get("cookies") or data.get("Cookies")
            if raw_cookies is None and all(isinstance(value, str) for value in data.values()):
                raw_cookies = [{"name": name, "value": value} for name, value in data.items()]
        else:
            raw_cookies = None

    if not isinstance(raw_cookies, list):
        raise ValueError(cookie_input_help(source))

    normalized: list[dict[str, Any]] = []
    for cookie in raw_cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        if not name:
            continue

        domain = str(cookie.get("domain") or cookie.get("host") or default_host).strip()
        if domain.startswith("http://") or domain.startswith("https://"):
            domain = urlparse(domain).hostname or default_host
        path_value = str(cookie.get("path") or "/")

        normalized_cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path_value,
        }

        expires = cookie.get("expires", cookie.get("expirationDate", cookie.get("expiry")))
        if expires not in (None, "", -1):
            try:
                expires_float = float(expires)
            except (TypeError, ValueError):
                expires_float = 0
            if expires_float > 0:
                normalized_cookie["expires"] = expires_float

        if "secure" in cookie:
            normalized_cookie["secure"] = bool(cookie["secure"])
        if "httpOnly" in cookie:
            normalized_cookie["httpOnly"] = bool(cookie["httpOnly"])

        same_site = str(cookie.get("sameSite", "")).strip().lower()
        same_site_map = {
            "strict": "Strict",
            "lax": "Lax",
            "none": "None",
            "no_restriction": "None",
            "unspecified": "Lax",
        }
        if same_site in same_site_map:
            normalized_cookie["sameSite"] = same_site_map[same_site]

        normalized.append(normalized_cookie)

    if not normalized:
        raise ValueError(f"没有找到可用 Cookie。{cookie_input_help(source)}")
    return normalized


def load_cookie_json(path: Path, start_url: str) -> list[dict[str, Any]]:
    return load_cookie_text(path.read_text(encoding="utf-8-sig"), start_url, str(path))


def build_requests_session(cookies: list[dict[str, Any]]) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    return session


def make_target_path(out_dir: Path, row: ParsedRow, index: int, extension: str) -> Path:
    class_name = sanitize_filename_part(row.class_name, "unknown_class")
    student_id = sanitize_filename_part(row.student_id, "unknown_id")
    name = sanitize_filename_part(row.name, "unknown_name")
    suffix = ""
    if not (row.class_name and row.student_id and row.name):
        suffix = f"_p{row.page_index:03d}_r{row.row_index:03d}"
    return out_dir / class_name / f"{student_id}_{name}{suffix}_{index:02d}{extension}"


def find_existing_target(out_dir: Path, row: ParsedRow, index: int) -> Path | None:
    for extension in sorted(ALLOWED_EXTENSIONS | {".html"}):
        target = make_target_path(out_dir, row, index, extension)
        if target.exists():
            return target
    return None


def metadata_failure_record(row: ParsedRow) -> dict[str, str | int]:
    missing = []
    if not row.class_name:
        missing.append("班级")
    if not row.student_id:
        missing.append("学号")
    if not row.name:
        missing.append("姓名")

    return {
        "页码": row.page_index,
        "行号": row.row_index,
        "已识别姓名": row.name,
        "已识别班级": row.class_name,
        "已识别学号": row.student_id,
        "行文本": row.row_text,
        "原因": "缺少：" + "、".join(missing),
    }


def download_attachment(
    session: requests.Session,
    row: ParsedRow,
    url: str,
    attachment_index: int,
    out_dir: Path,
    verify_tls: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    predicted_extension = url_extension(url)
    if predicted_extension:
        predicted_target = make_target_path(out_dir, row, attachment_index, predicted_extension)
        if predicted_target.exists():
            return (
                {
                    "页码": row.page_index,
                    "行号": row.row_index,
                    "班级": row.class_name,
                    "学号": row.student_id,
                    "姓名": row.name,
                    "附件序号": attachment_index,
                    "链接": url,
                    "文件路径": str(predicted_target),
                    "大小(字节)": predicted_target.stat().st_size,
                    "状态": STATUS_SKIPPED_EXISTING,
                },
                None,
            )
    else:
        existing_target = find_existing_target(out_dir, row, attachment_index)
        if existing_target is not None:
            return (
                {
                    "页码": row.page_index,
                    "行号": row.row_index,
                    "班级": row.class_name,
                    "学号": row.student_id,
                    "姓名": row.name,
                    "附件序号": attachment_index,
                    "链接": url,
                    "文件路径": str(existing_target),
                    "大小(字节)": existing_target.stat().st_size,
                    "状态": STATUS_SKIPPED_EXISTING,
                },
                None,
            )

    try:
        with session.get(url, stream=True, timeout=(15, 120), verify=verify_tls) as response:
            if response.status_code != 200:
                return None, download_failure_record(row, url, response.status_code, f"HTTP {response.status_code}")

            content_type = response.headers.get("Content-Type", "")
            extension = predicted_extension or extension_from_content_type(content_type)
            if not extension:
                return None, download_failure_record(row, url, response.status_code, f"不支持的 Content-Type：{content_type}")

            target = make_target_path(out_dir, row, attachment_index, extension)
            if target.exists():
                return (
                    {
                        "页码": row.page_index,
                        "行号": row.row_index,
                        "班级": row.class_name,
                        "学号": row.student_id,
                        "姓名": row.name,
                        "附件序号": attachment_index,
                        "链接": url,
                        "文件路径": str(target),
                        "大小(字节)": target.stat().st_size,
                        "状态": STATUS_SKIPPED_EXISTING,
                    },
                    None,
                )

            part_target = target.with_suffix(target.suffix + ".part")
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with part_target.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    size += len(chunk)
                    file.write(chunk)

            part_target.replace(target)
            return (
                {
                    "页码": row.page_index,
                    "行号": row.row_index,
                    "班级": row.class_name,
                    "学号": row.student_id,
                    "姓名": row.name,
                    "附件序号": attachment_index,
                    "链接": url,
                    "文件路径": str(target),
                    "大小(字节)": size,
                    "状态": STATUS_DOWNLOADED,
                },
                None,
            )
    except requests.RequestException as exc:
        return None, download_failure_record(row, url, "", str(exc))


def download_failure_record(row: ParsedRow, url: str, status_code: Any, error: str) -> dict[str, Any]:
    return {
        "页码": row.page_index,
        "行号": row.row_index,
        "班级": row.class_name,
        "学号": row.student_id,
        "姓名": row.name,
        "链接": url,
        "状态码": status_code,
        "错误": error,
    }


def page_grid_html(page: Any) -> str:
    locator = page.locator("#jinfo-grid")
    if locator.count() > 0:
        return locator.first.evaluate("element => element.outerHTML")
    return page.content()


def looks_like_login_page(page: Any) -> bool:
    html = page.content()
    markers = (
        "UserULoginForm_password",
        "universityLogin",
        "StudentLoginForm_password",
        "请输入验证码",
    )
    return any(marker in html for marker in markers) and "jinfo-grid" not in html


def start_url_with_all_rows(url: str) -> str:
    parts = urlsplit(url)
    if "/admin/tempdb/jinfo/list/" not in parts.path:
        return url

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = "1"
    query["limit"] = "0"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def row_signature(page: Any) -> str:
    return page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll('#jinfo-grid table tbody tr'));
            return String(rows.length) + '\\n' + rows
                .map(row => row.innerText.trim())
                .join('\\n')
                .slice(0, 5000);
        }"""
    )


def select_all_page_rows(page: Any, grid_selector: str, timeout_ms: int) -> bool:
    select = page.locator("select#limit")
    if select.count() == 0 or select.locator("option[value='0']").count() == 0:
        return False

    if select.input_value() == "0":
        return True

    try:
        before = row_signature(page)
    except Exception:
        before = ""

    select.select_option("0")
    deadline = time.time() + max(timeout_ms / 1000, 1)
    while time.time() < deadline:
        try:
            page.wait_for_load_state("networkidle", timeout=1000)
        except Exception:
            pass
        try:
            page.wait_for_selector(grid_selector, timeout=1000)
        except Exception:
            pass
        time.sleep(0.3)
        try:
            if page.locator("select#limit").input_value() == "0" and (
                not before or row_signature(page) != before
            ):
                return True
        except Exception:
            pass

    return True


def click_next_page(page: Any, next_selector: str | None, timeout_ms: int) -> bool:
    before = row_signature(page)

    clicked = False
    if next_selector:
        locator = page.locator(next_selector)
        count = locator.count()
        for index in range(count):
            candidate = locator.nth(index)
            if candidate.is_visible() and candidate.is_enabled():
                candidate.click()
                clicked = True
                break
    else:
        clicked = page.evaluate(
            """() => {
                const scopes = [document.querySelector('#jinfo-grid'), document].filter(Boolean);
                const nextTexts = ['下一页', '下页', 'Next', 'next', '›', '»'];

                function visible(element) {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                }

                function disabled(element) {
                    const classes = String(element.className || '').toLowerCase();
                    return element.disabled
                        || element.getAttribute('aria-disabled') === 'true'
                        || classes.includes('disabled')
                        || classes.includes('disable');
                }

                for (const scope of scopes) {
                    const nodes = Array.from(scope.querySelectorAll('a, button, span'));
                    for (const node of nodes) {
                        const label = [
                            node.innerText || node.textContent || '',
                            node.getAttribute('title') || '',
                            node.getAttribute('aria-label') || '',
                            String(node.className || '')
                        ].join(' ').trim();
                        if (!label || !visible(node) || disabled(node)) {
                            continue;
                        }
                        if (nextTexts.some(text => label.includes(text))) {
                            const clickable = node.closest('a, button') || node;
                            if (!disabled(clickable)) {
                                clickable.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            }"""
        )

    if not clicked:
        return False

    deadline = time.time() + max(timeout_ms / 1000, 1)
    while time.time() < deadline:
        try:
            page.wait_for_load_state("networkidle", timeout=1000)
        except Exception:
            pass
        time.sleep(0.3)
        if row_signature(page) != before:
            return True

    return False


def find_local_chromium_executable() -> Path | None:
    roots: list[Path] = []
    app_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    roots.append(app_root / "ms-playwright")
    roots.append(app_root / "_internal" / "ms-playwright")
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.append(Path(bundle_root) / "ms-playwright")
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(Path(local_appdata) / "ms-playwright")
    roots.append(Path.home() / "AppData" / "Local" / "ms-playwright")

    seen_roots: set[Path] = set()
    for root in roots:
        if root in seen_roots or not root.exists():
            continue
        seen_roots.add(root)
        for candidate in sorted(root.glob("chromium-*/chrome-win64/chrome.exe"), reverse=True):
            if candidate.exists():
                return candidate
    return None


def process_rows(
    rows: list[ParsedRow],
    session: requests.Session,
    out_dir: Path,
    verify_tls: bool,
    counters: Counters,
    success_writer: csv.DictWriter,
    metadata_writer: csv.DictWriter,
    failed_writer: csv.DictWriter,
    limit: int | None,
    progress_callback: ProgressCallback | None = None,
    stop_event: Any | None = None,
) -> bool:
    for row in rows:
        if stop_requested(stop_event):
            emit_progress(progress_callback, "已收到停止请求，后续行不再处理。", counters)
            return True

        counters.table_rows += 1
        if not row.attachments:
            continue

        if limit is not None and counters.rows_with_attachments >= limit:
            return True

        counters.rows_with_attachments += 1
        emit_progress(
            progress_callback,
            f"处理第 {row.page_index} 页第 {row.row_index} 行：{row.class_name or '未知班级'} "
            f"{row.student_id or '未知学号'} {row.name or '未知姓名'}",
            counters,
        )
        if not (row.class_name and row.student_id and row.name):
            counters.metadata_failures += 1
            metadata_writer.writerow(metadata_failure_record(row))

        attachment_index = 0
        for url in row.attachments:
            if stop_requested(stop_event):
                emit_progress(progress_callback, "已收到停止请求，后续附件不再处理。", counters)
                return True

            resolved_urls, resolve_failure = resolve_attachment_urls(
                session=session,
                row=row,
                url=url,
                verify_tls=verify_tls,
            )
            if resolve_failure:
                counters.download_failures += 1
                failed_writer.writerow(resolve_failure)
                emit_progress(
                    progress_callback,
                    f"解析证明页面失败：{resolve_failure['错误']}；链接：{resolve_failure['链接']}",
                    counters,
                )
                continue

            for resolved_url in resolved_urls:
                attachment_index += 1
                success, failure = download_attachment(
                    session=session,
                    row=row,
                    url=resolved_url,
                    attachment_index=attachment_index,
                    out_dir=out_dir,
                    verify_tls=verify_tls,
                )
                if success:
                    success_writer.writerow(success)
                    if success["状态"] == STATUS_SKIPPED_EXISTING:
                        counters.skipped_existing += 1
                        emit_progress(progress_callback, f"已存在，跳过：{success['文件路径']}", counters)
                    else:
                        counters.downloaded += 1
                        emit_progress(progress_callback, f"已下载：{success['文件路径']}", counters)
                if failure:
                    counters.download_failures += 1
                    failed_writer.writerow(failure)
                    emit_progress(progress_callback, f"下载失败：{failure['错误']}；链接：{failure['链接']}", counters)

    return False


def run_live(
    args: argparse.Namespace,
    progress_callback: ProgressCallback | None = None,
    stop_event: Any | None = None,
) -> int:
    def report(message: str, counters: Counters | None = None, error: bool = False) -> None:
        emit_progress(progress_callback, message, counters)
        print(message, file=sys.stderr if error else sys.stdout)

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        report("未安装 Playwright。请运行：python -m pip install playwright", error=True)
        return 2

    if args.insecure:
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    try:
        cookie_text = str(getattr(args, "cookie_text", "")).strip()
        if cookie_text:
            cookies = load_cookie_text(cookie_text, args.start_url, "Cookie 粘贴内容")
        else:
            cookie_path = Path(args.cookie_json)
            if not cookie_path.exists():
                report(f"找不到 Cookie 文件：{cookie_path}", error=True)
                return 5
            cookies = load_cookie_json(cookie_path, args.start_url)
    except ValueError as exc:
        report(str(exc), error=True)
        return 5

    session = build_requests_session(cookies)
    counters = Counters()

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report(f"无法创建输出目录：{out_dir}。{exc}", counters, error=True)
        return 6
    try:
        probe = out_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        report(
            f"输出目录没有写入权限：{out_dir}。请更换目录，或右键选择“以管理员身份运行”。{exc}",
            counters,
            error=True,
        )
        return 6

    success_fields = [
        "页码",
        "行号",
        "班级",
        "学号",
        "姓名",
        "附件序号",
        "链接",
        "文件路径",
        "大小(字节)",
        "状态",
    ]
    metadata_fields = [
        "页码",
        "行号",
        "已识别姓名",
        "已识别班级",
        "已识别学号",
        "行文本",
        "原因",
    ]
    failed_fields = ["页码", "行号", "班级", "学号", "姓名", "链接", "状态码", "错误"]

    with (
        (out_dir / "download_log.csv").open("w", newline="", encoding="utf-8-sig") as success_file,
        (out_dir / "metadata_failures.csv").open("w", newline="", encoding="utf-8-sig") as metadata_file,
        (out_dir / "failed_downloads.csv").open("w", newline="", encoding="utf-8-sig") as failed_file,
    ):
        success_writer = csv.DictWriter(success_file, fieldnames=success_fields)
        metadata_writer = csv.DictWriter(metadata_file, fieldnames=metadata_fields)
        failed_writer = csv.DictWriter(failed_file, fieldnames=failed_fields)
        success_writer.writeheader()
        metadata_writer.writeheader()
        failed_writer.writeheader()

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=not args.headful)
            except PlaywrightError as launch_error:
                chromium_executable = find_local_chromium_executable()
                if chromium_executable is None:
                    report(
                        "Playwright 浏览器未安装或启动失败。请运行：python -m playwright install chromium",
                        counters,
                        error=True,
                    )
                    return 2
                try:
                    browser = playwright.chromium.launch(
                        executable_path=str(chromium_executable),
                        headless=not args.headful,
                    )
                except PlaywrightError:
                    report(
                        "Playwright 浏览器启动失败。请运行：python -m playwright install chromium",
                        counters,
                        error=True,
                    )
                    report(f"原始错误：{launch_error}", counters, error=True)
                    return 2
            context = browser.new_context(ignore_https_errors=args.insecure, user_agent=USER_AGENT)
            context.add_cookies(cookies)
            page = context.new_page()

            start_url = start_url_with_all_rows(args.start_url)
            if start_url != args.start_url:
                report("已将列表请求参数设置为：limit=0（全部）", counters)

            page.goto(start_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            if looks_like_login_page(page):
                browser.close()
                report(
                    "Cookie 可能已过期或不完整，页面仍停留在登录页。"
                    "请从已登录后台页面的 Network 请求里复制 Request Headers 的完整 Cookie 后重新粘贴。",
                    counters,
                    error=True,
                )
                return 3

            stop_after_limit = False
            for page_index in range(1, args.max_pages + 1):
                if stop_requested(stop_event):
                    report("已收到停止请求，后续页面不再处理。", counters)
                    break

                counters.pages = page_index
                report(f"开始处理第 {page_index} 页。", counters)
                try:
                    page.wait_for_selector(args.grid_selector, timeout=args.timeout_ms)
                except PlaywrightTimeoutError:
                    browser.close()
                    report(
                        f"找不到表格行选择器 {args.grid_selector!r}。"
                        "请打开包含 #jinfo-grid 的后台页面，或传入 --grid-selector。",
                        counters,
                        error=True,
                    )
                    return 4

                if page_index == 1 and select_all_page_rows(page, args.grid_selector, args.timeout_ms):
                    report("已将页面显示条数切换为：全部", counters)

                rows = parse_grid_html(
                    html=page_grid_html(page),
                    base_url=page.url,
                    page_index=page_index,
                    class_col=args.class_col,
                    student_id_col=args.student_id_col,
                    name_col=args.name_col,
                    attachment_col=args.attachment_col,
                )
                stop_after_limit = process_rows(
                    rows=rows,
                    session=session,
                    out_dir=out_dir,
                    verify_tls=not args.insecure,
                    counters=counters,
                    success_writer=success_writer,
                    metadata_writer=metadata_writer,
                    failed_writer=failed_writer,
                    limit=args.limit,
                    progress_callback=progress_callback,
                    stop_event=stop_event,
                )
                if stop_after_limit or args.no_pagination:
                    break
                if not click_next_page(page, args.next_selector, args.timeout_ms):
                    report("没有找到下一页，分页处理结束。", counters)
                    break

            browser.close()

    report(
        "完成："
        f"页面数={counters.pages}，"
        f"表格行数={counters.table_rows}，"
        f"有证明链接的行数={counters.rows_with_attachments}，"
        f"已下载={counters.downloaded}，"
        f"已存在跳过={counters.skipped_existing}，"
        f"信息缺失={counters.metadata_failures}，"
        f"下载失败={counters.download_failures}",
        counters,
    )
    report(f"输出目录：{out_dir.resolve()}", counters)
    return 0


def run_self_test() -> int:
    sample_html = """
    <div id="jinfo-grid">
      <div class="gridHeader"><table><tr>
        <th>班级</th><th>学号</th><th>姓名</th><th>材料</th>
      </tr></table></div>
      <div class="gridScroller"><table><tbody>
        <tr>
          <td>20224125</td><td>2022493219</td><td>张三</td>
          <td><a target="_blank" href="/attachment/91wllm/electronic/202604/07/69d4f07c8cdb3.jpg">已上传</a></td>
        </tr>
      </tbody></table></div>
    </div>
    """
    rows = parse_grid_html(sample_html, DEFAULT_START_URL, 1, None, None, None, None)
    assert len(rows) == 1
    assert rows[0].class_name == "20224125"
    assert rows[0].student_id == "2022493219"
    assert rows[0].name == "张三"
    assert rows[0].attachments == [
        "https://kjxyjy.91wllm.cn/attachment/91wllm/electronic/202604/07/69d4f07c8cdb3.jpg"
    ]

    wrapped = (
        "chrome-extension://mdihpmidlgahpnbhinkcpiaahpkcfhpa/popup/index.html"
        "?imageUrl=https%253A%252F%252Fkjxyjy.91wllm.cn%252Fattachment%252F91wllm"
        "%252Felectronic%252F202604%252F07%252F69d4f07c8cdb3.jpg&imageExt=.jpg"
    )
    assert decode_wrapped_url(wrapped) == (
        "https://kjxyjy.91wllm.cn/attachment/91wllm/electronic/202604/07/69d4f07c8cdb3.jpg"
    )

    missing_html = """
    <div id="jinfo-grid">
      <table><thead><tr><th>学号</th><th>姓名</th><th>材料</th></tr></thead>
      <tbody><tr>
        <td>2022493220</td><td>李四</td>
        <td><a href="/attachment/91wllm/electronic/202604/07/test.pdf">已上传</a></td>
      </tr></tbody></table>
    </div>
    """
    missing_rows = parse_grid_html(missing_html, DEFAULT_START_URL, 1, None, None, None, None)
    failure = metadata_failure_record(missing_rows[0])
    assert failure["已识别姓名"] == "李四"
    assert failure["已识别学号"] == "2022493220"
    assert failure["已识别班级"] == ""
    assert failure["原因"] == "缺少：班级"

    agreement_html = """
    <div id="jinfo-grid">
      <table><thead><tr>
        <th>单位行业</th><th>单位性质</th><th>实际所在地</th><th>上传就业相关证明</th><th>最后操作时间</th>
      </tr></thead>
      <tbody><tr>
        <td>制造业</td><td>国有企业</td><td>湖北省武汉市武昌区</td>
        <td><a target="_blank" href="https://www.91wllm.cn/electronic/default/agreement/k/testkey">已上传</a></td>
        <td>2026-06-29 17:18:22</td>
      </tr></tbody></table>
    </div>
    """
    agreement_rows = parse_grid_html(agreement_html, DEFAULT_START_URL, 1, None, None, None, None)
    assert agreement_rows[0].attachments == [
        "https://www.91wllm.cn/electronic/default/agreement/k/testkey"
    ]
    assert extension_from_content_type("text/html; charset=utf-8") == ".html"
    assert str(make_target_path(Path("out"), agreement_rows[0], 1, ".html")).endswith(
        str(Path("unknown_class") / "unknown_id_unknown_name_p001_r001_01.html")
    )

    target = make_target_path(Path("out"), rows[0], 2, ".jpg")
    assert target == Path("out") / "20224125" / "2022493219_张三_02.jpg"

    cookies = load_cookie_text("Cookie: PHPSESSID=abc123; token=xyz", DEFAULT_START_URL, "测试 Cookie")
    assert cookies == [
        {"name": "PHPSESSID", "value": "abc123", "domain": ".91wllm.cn", "path": "/"},
        {"name": "token", "value": "xyz", "domain": ".91wllm.cn", "path": "/"},
    ]
    cookies_without_prefix = load_cookie_text("PHPSESSID=abc123; token=xyz", DEFAULT_START_URL, "测试 Cookie")
    assert cookies_without_prefix == cookies
    pasted_headers = load_cookie_text(
        "Accept: */*\nCookie: PHPSESSID=abc123; token=xyz\nUser-Agent: test",
        DEFAULT_START_URL,
        "测试 Cookie",
    )
    assert pasted_headers == cookies

    cookie_object = load_cookie_text('{"PHPSESSID":"abc123"}', DEFAULT_START_URL, "测试 Cookie")
    assert cookie_object == [
        {"name": "PHPSESSID", "value": "abc123", "domain": ".91wllm.cn", "path": "/"}
    ]

    try:
        load_cookie_text("", DEFAULT_START_URL, "测试 Cookie")
    except ValueError as exc:
        assert "为空" in str(exc)
    else:
        raise AssertionError("空 Cookie 应该报错")

    try:
        load_cookie_text("fetch('https://example.com')", DEFAULT_START_URL, "测试 Cookie")
    except ValueError as exc:
        assert "fetch" in str(exc)
    else:
        raise AssertionError("fetch 记录不应被当作 Cookie")

    print("自检通过。")
    return 0


def load_runtime_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    spec = importlib.util.spec_from_file_location("crawler_config", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载配置文件：{path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config: dict[str, Any] = {}
    for variable_name, arg_name in CONFIG_VARIABLES.items():
        if hasattr(module, variable_name):
            config[arg_name] = getattr(module, variable_name)
    return config


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Python 配置文件，默认 crawler_config.py。",
    )
    pre_args, _ = pre_parser.parse_known_args()

    runtime_config = dict(DEFAULT_RUNTIME_CONFIG)
    try:
        runtime_config.update(load_runtime_config(Path(pre_args.config)))
    except Exception as exc:
        pre_parser.error(str(exc))

    parser = argparse.ArgumentParser(
        parents=[pre_parser],
        description="下载 91wllm 表格行里的就业证明附件或证明页面。",
    )
    parser.add_argument("--cookie-json", default=argparse.SUPPRESS, help="Cookie 导出文件。")
    parser.add_argument("--start-url", default=argparse.SUPPRESS, help="包含 #jinfo-grid 的后台页面地址。")
    parser.add_argument("--out", default=argparse.SUPPRESS, help="输出目录。")
    parser.add_argument(
        "--insecure",
        dest="insecure",
        action="store_true",
        default=argparse.SUPPRESS,
        help="禁用 TLS 证书校验。",
    )
    parser.add_argument(
        "--secure",
        dest="insecure",
        action="store_false",
        default=argparse.SUPPRESS,
        help="启用 TLS 证书校验，覆盖配置。",
    )
    parser.add_argument(
        "--headful",
        dest="headful",
        action="store_true",
        default=argparse.SUPPRESS,
        help="抓取时显示 Chromium 窗口。",
    )
    parser.add_argument(
        "--headless",
        dest="headful",
        action="store_false",
        default=argparse.SUPPRESS,
        help="隐藏 Chromium 窗口，覆盖配置。",
    )
    parser.add_argument("--grid-selector", default=argparse.SUPPRESS, help="等待表格行出现的选择器。")
    parser.add_argument("--next-selector", default=argparse.SUPPRESS, help="下一页按钮/链接的可选选择器。")
    parser.add_argument(
        "--no-pagination",
        dest="no_pagination",
        action="store_true",
        default=argparse.SUPPRESS,
        help="只处理当前页。",
    )
    parser.add_argument(
        "--paginate",
        dest="no_pagination",
        action="store_false",
        default=argparse.SUPPRESS,
        help="启用分页，覆盖配置。",
    )
    parser.add_argument("--max-pages", type=int, default=argparse.SUPPRESS, help="分页安全上限。")
    parser.add_argument("--limit", type=int, default=argparse.SUPPRESS, help="最多处理多少个有证明链接的行。")
    parser.add_argument(
        "--all",
        dest="limit",
        action="store_const",
        const=None,
        default=argparse.SUPPRESS,
        help="处理所有有证明链接的行，覆盖配置 LIMIT。",
    )
    parser.add_argument("--timeout-ms", type=int, default=argparse.SUPPRESS, help="Playwright 超时时间，单位毫秒。")
    parser.add_argument("--class-col", type=int, default=argparse.SUPPRESS, help="班级列号，从 1 开始。")
    parser.add_argument("--student-id-col", type=int, default=argparse.SUPPRESS, help="学号列号，从 1 开始。")
    parser.add_argument("--name-col", type=int, default=argparse.SUPPRESS, help="姓名列号，从 1 开始。")
    parser.add_argument("--attachment-col", type=int, default=argparse.SUPPRESS, help="证明/附件列号，从 1 开始。")
    parser.add_argument("--self-test", action="store_true", help="运行解析自检后退出。")
    cli_values = vars(parser.parse_args())

    config_path = cli_values.pop("config")
    self_test = cli_values.pop("self_test")
    merged = {**runtime_config, **cli_values}
    merged["config"] = config_path
    merged["self_test"] = self_test
    args = argparse.Namespace(**merged)

    if not args.self_test and not args.cookie_json:
        parser.error("请在 crawler_config.py 中设置 COOKIE_JSON，或传入 --cookie-json。")
    return args


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
