import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from openpyxl import Workbook, load_workbook
from PIL import Image

try:
    import requests
except Exception:
    requests = None

try:
    from fastapi import FastAPI, Request as FastAPIRequest
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    import uvicorn

    FASTAPI_AVAILABLE = True
except Exception:
    FastAPI = None
    FastAPIRequest = None
    FileResponse = None
    HTMLResponse = None
    JSONResponse = None
    uvicorn = None
    FASTAPI_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tasks.db"
OUTPUT_DIR = BASE_DIR / "output"
IMPORT_DIR = BASE_DIR / "imports"
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"

STATUSES = {
    "pending",
    "generating",
    "collaging",
    "collaging_failed",
    "quality_failed",
    "waiting_confirm",
    "awaiting_confirm",
    "approved",
    "rejected",
    "failed",
    "quota_exhausted",
}

STATUS_LABELS = {
    "pending": "待处理",
    "generating": "生成中",
    "collaging": "拼图中",
    "collaging_failed": "拼图失败",
    "quality_failed": "质检失败",
    "waiting_confirm": "待确认",
    "awaiting_confirm": "待确认",
    "approved": "已通过",
    "rejected": "已驳回",
    "failed": "真失败",
    "quota_exhausted": "额度耗尽",
}

IMAGE_DELAY_SECONDS = 3
IMAGE_TIMEOUT_SECONDS = 900
LOCAL_IMAGE_PATHS = (
    "/v1/images/generations",
    "/api/images/generations",
    "/images/generations",
)
queue_lock = threading.Lock()
worker_lock = threading.Lock()
worker_running = False
queue_control = {"state": "idle", "pause_requested": False, "stop_requested": False}
quota_state = {"status": "unknown", "message": "尚未检测", "updated_at": None}
api_config_state = {"authenticated": False, "message": "尚未配置本地服务地址", "updated_at": None}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def ensure_admin():
    if os.name != "nt":
        return
    try:
        import ctypes

        if ctypes.windll.shell32.IsUserAnAdmin():
            return
        params = subprocess.list2cmdline([str(Path(__file__).resolve())])
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            params,
            str(BASE_DIR),
            1,
        )
        if result <= 32:
            print(f"WARNING: 管理员权限申请失败，ShellExecuteW 返回：{result}；继续以当前权限运行", flush=True)
            return
        sys.exit(0)
    except Exception as exc:
        print(f"WARNING: 管理员权限自动申请失败：{exc}；继续以当前权限运行", flush=True)


def ensure_dirs():
    for path in (OUTPUT_DIR, IMPORT_DIR, EXPORT_DIR, LOG_DIR, STATIC_DIR):
        path.mkdir(parents=True, exist_ok=True)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    ensure_dirs()
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL UNIQUE,
                product_name TEXT,
                prompt TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                image_path TEXT,
                spu TEXT,
                color_code TEXT,
                color_name TEXT,
                image_folder TEXT,
                image_name TEXT,
                add_logo TEXT,
                logo_path TEXT,
                brand_check TEXT,
                optimized_prompt TEXT,
                prompt_snapshot TEXT,
                generated_at TEXT,
                generation_progress TEXT,
                error_message TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for column, ddl in {
            "spu": "ALTER TABLE tasks ADD COLUMN spu TEXT",
            "color_code": "ALTER TABLE tasks ADD COLUMN color_code TEXT",
            "color_name": "ALTER TABLE tasks ADD COLUMN color_name TEXT",
            "image_folder": "ALTER TABLE tasks ADD COLUMN image_folder TEXT",
            "image_name": "ALTER TABLE tasks ADD COLUMN image_name TEXT",
            "add_logo": "ALTER TABLE tasks ADD COLUMN add_logo TEXT",
            "logo_path": "ALTER TABLE tasks ADD COLUMN logo_path TEXT",
            "brand_check": "ALTER TABLE tasks ADD COLUMN brand_check TEXT",
            "optimized_prompt": "ALTER TABLE tasks ADD COLUMN optimized_prompt TEXT",
            "prompt_snapshot": "ALTER TABLE tasks ADD COLUMN prompt_snapshot TEXT",
            "generated_at": "ALTER TABLE tasks ADD COLUMN generated_at TEXT",
            "generation_progress": "ALTER TABLE tasks ADD COLUMN generation_progress TEXT",
        }.items():
            if column not in existing_columns:
                conn.execute(ddl)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                sku TEXT,
                event_type TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def log_event(conn, task_id, sku, event_type, message=""):
    conn.execute(
        "INSERT INTO events(task_id, sku, event_type, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, sku, event_type, message, now_iso()),
    )


def normalize_row(headers, values):
    data = {}
    for idx, header in enumerate(headers):
        if header:
            data[str(header).strip()] = "" if values[idx] is None else str(values[idx]).strip()
    return data


def normalize_header_name(value):
    return str(value or "").strip().replace("：", ":").rstrip(":").lower()


def pick_field(data, candidates):
    lower_map = {normalize_header_name(k): v for k, v in data.items()}
    for key in candidates:
        if key in data and data[key]:
            return data[key]
        value = lower_map.get(normalize_header_name(key))
        if value:
            return value
    return ""


def strip_image_name_suffix(value, image_name):
    text = (value or "").strip()
    suffix = (image_name or "").strip()
    if not text or not suffix:
        return text
    safe_text = safe_path_part(text, text)
    safe_suffix = safe_path_part(suffix, suffix)
    candidates = [
        f"_{suffix}",
        f"/{suffix}",
        f"_{safe_suffix}",
        f"/{safe_suffix}",
    ]
    for candidate in candidates:
        if text.endswith(candidate):
            return text[: -len(candidate)].rstrip("_/ ")
        if safe_text.endswith(candidate):
            return safe_text[: -len(candidate)].rstrip("_/ ")
    return text


def import_excel(path):
    wb = load_workbook(path)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {"imported": 0, "updated": 0, "skipped": 0}

    headers = ["" if cell is None else str(cell).strip() for cell in rows[0]]
    stats = {"imported": 0, "updated": 0, "skipped": 0}
    spu_color_counters = {}
    with connect() as conn:
        for row in conn.execute("SELECT spu, image_name FROM tasks WHERE spu IS NOT NULL AND image_name IS NOT NULL").fetchall():
            image_name = row["image_name"] or ""
            prefix = image_name.split("_", 1)[0]
            if prefix.isdigit():
                spu_color_counters[row["spu"]] = max(spu_color_counters.get(row["spu"], 0), int(prefix))
        for values in rows[1:]:
            data = normalize_row(headers, list(values) + [None] * (len(headers) - len(values)))
            raw_spu = pick_field(data, ["SPU", "spu", "产品系列编号", "系列编号", "产品系列", "款号"])
            color_name = pick_field(data, ["图片颜色", "颜色名称", "颜色", "Color", "color_name"])
            image_name = pick_field(data, ["图片命名", "image_name"])
            spu = strip_image_name_suffix(raw_spu, image_name)
            image_name_source = "excel" if image_name else "empty"
            if not image_name and color_name:
                next_index = spu_color_counters.get(spu, 0) + 1
                spu_color_counters[spu] = next_index
                image_name = f"{next_index:02d}_{color_name}"
                image_name_source = "auto_color"
                spu = strip_image_name_suffix(raw_spu, image_name)
            excel_sku = pick_field(data, ["SKU", "sku", "货号", "商品编码"])
            sku = spu or image_name or excel_sku
            sku_source = "spu" if spu else ("image_name" if image_name else "excel_sku")
            if not sku:
                stats["skipped"] += 1
                continue
            print(
                f"DEBUG import row: sku={sku}, spu={spu}, image_name={image_name}, excel_sku={excel_sku}, sku_source={sku_source}",
                flush=True,
            )
            print(
                "DEBUG import row detail:",
                f"sku_repr={sku!r}",
                f"sku_source={sku_source}",
                f"raw_spu_repr={raw_spu!r}",
                f"spu_repr={spu!r}",
                f"excel_sku_repr={excel_sku!r}",
                f"color_name_repr={color_name!r}",
                f"image_name_repr={image_name!r}",
                f"image_name_source={image_name_source}",
                flush=True,
            )
            product_name = pick_field(data, ["商品名称", "产品名称", "标题", "品名", "Name"])
            prompt = pick_field(data, ["图片生成提示词", "生图提示词", "Prompt", "prompt", "提示词"])
            color_code = pick_field(data, ["颜色编号", "色号", "Color Code", "color_code"])
            if not color_code and image_name and "_" in image_name:
                color_code = image_name.split("_", 1)[0]
            add_logo = pick_field(data, ["是否添加品牌logo", "是否添加品牌Logo", "add_logo"])
            logo_path = pick_field(data, ["logo文件地址", "Logo文件地址", "logo_path"])
            brand_check = pick_field(data, ["需校验品牌文字/元素", "brand_check"])
            raw_json = json.dumps(data, ensure_ascii=False)
            existing = conn.execute("SELECT id, sku FROM tasks WHERE sku = ?", (sku,)).fetchone()
            if not existing and spu and image_name:
                existing = conn.execute(
                    "SELECT id, sku FROM tasks WHERE spu = ? AND image_name = ?",
                    (spu, image_name),
                ).fetchone()
            if not existing and excel_sku:
                existing = conn.execute("SELECT id, sku FROM tasks WHERE sku = ?", (excel_sku,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE tasks
                    SET sku = ?, product_name = ?, prompt = ?, raw_json = ?, spu = ?, color_code = ?,
                        color_name = ?, image_name = ?, add_logo = ?, logo_path = ?, brand_check = ?,
                        prompt_snapshot = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        sku,
                        product_name,
                        prompt,
                        raw_json,
                        spu,
                        color_code,
                        color_name,
                        image_name,
                        add_logo,
                        logo_path,
                        brand_check,
                        prompt,
                        now_iso(),
                        existing["id"],
                    ),
                )
                log_event(conn, existing["id"], sku, "excel_update", f"Excel updated source fields; old_sku={existing['sku']}")
                stats["updated"] += 1
            else:
                cur = conn.execute(
                    """
                    INSERT INTO tasks(
                        sku, product_name, prompt, raw_json, spu, color_code, color_name,
                        image_name, add_logo, logo_path, brand_check, prompt_snapshot,
                        status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        sku,
                        product_name,
                        prompt,
                        raw_json,
                        spu,
                        color_code,
                        color_name,
                        image_name,
                        add_logo,
                        logo_path,
                        brand_check,
                        prompt,
                        now_iso(),
                        now_iso(),
                    ),
                )
                log_event(conn, cur.lastrowid, sku, "excel_import", "Excel imported task")
                stats["imported"] += 1
    return stats


def list_tasks(status=None, q=None):
    sql = "SELECT * FROM tasks"
    params = []
    clauses = []
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if q:
        clauses.append("(sku LIKE ? OR product_name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, id DESC"
    with connect() as conn:
        tasks = [dict(row) for row in conn.execute(sql, params).fetchall()]
    for task in tasks:
        if task["status"] in ("waiting_confirm", "awaiting_confirm") and not main_image_path_for_task(task):
            task["status"] = "collaging_failed"
            task["error_message"] = "主图文件未找到，请重新拼图"
            task["generation_progress"] = None
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'collaging_failed', error_message = ?, generation_progress = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (task["error_message"], now_iso(), task["id"]),
                )
        task["status_label"] = STATUS_LABELS.get(task["status"], task["status"])
        if task["status"] == "generating" and task.get("generation_progress"):
            task["status_label"] = task["generation_progress"]
        task["raw"] = json.loads(task["raw_json"] or "{}")
    return tasks


def add_manual_task(payload):
    spu = (payload.get("spu") or payload.get("SPU") or "").strip()
    image_name = (payload.get("image_name") or payload.get("图片命名") or "").strip()
    sku_input = (payload.get("sku") or payload.get("SKU") or "").strip()
    sku = spu or sku_input or image_name
    product_name = (payload.get("product_name") or "").strip()
    prompt = (payload.get("prompt") or "").strip()
    if not sku or not product_name or not prompt:
        raise ValueError("SKU、商品名称、生图提示词均为必填")
    raw = {"SKU": sku, "SPU": spu, "图片命名": image_name, "商品名称": product_name, "生图提示词": prompt}
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks(sku, product_name, prompt, raw_json, spu, image_name, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (sku, product_name, prompt, json.dumps(raw, ensure_ascii=False), spu, image_name, now_iso(), now_iso()),
        )
        log_event(conn, cur.lastrowid, sku, "manual_add", "Manual task created")


def update_status(task_id, status, message=""):
    if status not in STATUSES:
        raise ValueError(f"Unsupported status: {status}")
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError("Task not found")
        conn.execute(
            "UPDATE tasks SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
            (status, message or None, now_iso(), task_id),
        )
        log_event(conn, task_id, task["sku"], "status_update", f"{status}: {message}")


def reset_for_generation(task_id):
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError("Task not found")
        sku = task["sku"]
        print(f"DEBUG: generate_image called for SKU: {sku}", flush=True)
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending', error_message = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), task_id),
        )
        log_event(conn, task_id, task["sku"], "queued", "Queued for generation")


def is_quota_error(text):
    lowered = (text or "").lower()
    markers = [
        "quota",
        "rate limit",
        "rate_limit",
        "too many requests",
        "usage limit",
        "message limit",
        "try again later",
        "temporarily limited",
        "429",
        "额度",
        "频率",
        "限制",
        "稍后",
    ]
    return any(marker in lowered for marker in markers)


def safe_sku(sku):
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in sku)[:120]


def safe_path_part(value, fallback):
    text = (value or fallback or "").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".", " ", "（", "）", "(", ")") else "_" for ch in text)
    return cleaned.strip(" .")[:120] or safe_sku(fallback or "unknown")


def folder_spu_for_task(task):
    spu = (task.get("spu") or "").strip()
    if spu:
        return spu
    sku = (task.get("sku") or "").strip()
    image_name = (task.get("image_name") or "").strip()
    if sku and image_name:
        suffixes = [
            f"_{image_name}",
            f"_{safe_path_part(image_name, image_name)}",
            f"/{image_name}",
        ]
        safe_sku_value = safe_path_part(sku, sku)
        safe_image_name = safe_path_part(image_name, image_name)
        if safe_image_name:
            suffixes.append(f"_{safe_image_name}")
        for suffix in suffixes:
            if sku.endswith(suffix):
                return sku[: -len(suffix)]
            if safe_sku_value.endswith(suffix):
                return safe_sku_value[: -len(suffix)]
    return sku or "unknown"


def folder_image_name_for_task(task):
    image_name = (task.get("image_name") or "").strip()
    if image_name:
        return image_name
    color_code = task.get("color_code") or "00"
    color_name = task.get("color_name") or "默认"
    return f"{safe_path_part(color_code, '00')}_{safe_path_part(color_name, '默认')}"


def image_folder_for_task(task):
    spu_folder = safe_path_part(folder_spu_for_task(task), task.get("sku") or "unknown")
    folder_path = OUTPUT_DIR / spu_folder
    print(
        "DEBUG path:",
        f"sku={task.get('sku')}",
        f"spu={task.get('spu')}",
        f"image_name={task.get('image_name')}",
        f"folder={folder_path}",
        flush=True,
    )
    return folder_path


def legacy_image_folder_for_task(task):
    spu_folder = safe_path_part(folder_spu_for_task(task), task.get("sku") or "unknown")
    image_folder = safe_path_part(folder_image_name_for_task(task), "00_默认")
    return OUTPUT_DIR / spu_folder / image_folder


def download_base_name_for_task(task, fallback_sku):
    if task:
        return safe_path_part(folder_spu_for_task(task), fallback_sku or "unknown")
    return safe_sku(str(fallback_sku))


def zip_filename_for_skus(skus):
    normalized_skus = [str(sku) for sku in skus if str(sku).strip()]
    if len(normalized_skus) == 1:
        task = get_task_by_sku(normalized_skus[0])
        return f"{download_base_name_for_task(task, normalized_skus[0])}.zip"
    return f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"


def build_codex_prompt(task, image_file):
    product_name = task["product_name"] or task["sku"]
    user_prompt = task["prompt"] or product_name
    return f"""
Use case: product-mockup
Asset type: ecommerce main product image
Primary request: {user_prompt}
Subject: {product_name}
Style/medium: clean commercial product photography
Composition/framing: square 1024x1024 main image, centered product, clear edges
Lighting/mood: polished studio lighting, realistic shadows
Constraints: no watermark, no extra text, no logos unless explicitly requested by the prompt
""".strip()


def encode_config_value(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def decode_config_value(value):
    return base64.b64decode((value or "").encode("ascii")).decode("utf-8")


def set_config(key, value):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO config(key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, encode_config_value(value)),
        )


def get_config(key):
    with connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if not row or not row["value"]:
        return None
    try:
        return decode_config_value(row["value"])
    except Exception:
        return None


def normalize_api_base_url(api_base_url):
    api_base_url = (api_base_url or "").strip().rstrip("/")
    if not api_base_url:
        raise ValueError("请填写 Cockpit 本地 API 服务地址")
    return api_base_url


def display_api_base_url(api_base_url):
    if not api_base_url:
        return None
    return api_base_url.replace("http://", "").replace("https://", "")


def set_api_config_state(authenticated, message):
    api_config_state.update(
        {"authenticated": bool(authenticated), "message": message, "updated_at": now_iso()}
    )
    return dict(api_config_state)


def load_api_base_url():
    api_base_url = get_config("api_base_url")
    if api_base_url:
        set_api_config_state(True, "Codex 本地服务已就绪，可以生成图片")
    else:
        set_api_config_state(False, "请先配置 Codex 本地服务地址才能生成图片")
    return api_base_url


def api_config_user_info():
    api_base_url = get_config("api_base_url")
    access_token = get_config("access_token")
    if not api_base_url:
        return {"authenticated": False, "has_api_key": False, "has_api_base_url": False, "has_access_token": bool(access_token)}
    return {
        "authenticated": True,
        "has_api_key": False,
        "has_api_base_url": True,
        "has_access_token": bool(access_token),
        "api_base_url": api_base_url,
        "api_base_url_display": display_api_base_url(api_base_url),
        "membership_status": "订阅有效",
    }


def set_api_base_url(api_base_url, access_token=None):
    api_base_url = normalize_api_base_url(api_base_url)
    set_config("api_base_url", api_base_url)
    access_token = (access_token or "").strip()
    if access_token:
        set_config("access_token", access_token)
    set_api_config_state(True, "本地服务地址已保存")
    return {"success": True, "message": "本地服务地址已保存", "user": api_config_user_info()}


def require_api_base_url():
    api_base_url = load_api_base_url()
    if not api_base_url:
        raise ValueError("请先配置 Codex 本地服务地址")
    return api_base_url


def get_access_token():
    return get_config("access_token")


def local_api_headers():
    access_token = get_access_token()
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def mask_token(access_token):
    if not access_token:
        return "未配置"
    if len(access_token) <= 8:
        return access_token[:2] + "****"
    return f"{access_token[:4]}****{access_token[-4:]}"


def mask_headers(headers):
    masked = dict(headers)
    authorization = masked.get("Authorization")
    if authorization and authorization.lower().startswith("bearer "):
        masked["Authorization"] = "Bearer " + mask_token(authorization[7:])
    return masked


def print_local_api_config():
    api_base_url = get_config("api_base_url") or "未配置"
    access_token = get_config("access_token")
    print(f"Config api_base_url: {api_base_url}", flush=True)
    print(f"Config access_token: {mask_token(access_token)}", flush=True)


def openai_error_message(exc):
    if isinstance(exc, HTTPError):
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            message = data.get("error", {}).get("message") or body
        except Exception:
            message = body
        return f"{exc.code} - {message}"
    if isinstance(exc, URLError):
        reason = str(exc.reason)
        if "connection refused" in reason.lower() or "actively refused" in reason.lower() or "由于目标计算机积极拒绝" in reason:
            return "无法连接到 Cockpit API 服务，请确认服务已启动且端口正确"
        return f"网络错误：{exc.reason}"
    message = str(exc)
    lowered = message.lower()
    if "connection refused" in lowered or "actively refused" in lowered or "由于目标计算机积极拒绝" in message:
        return "无法连接到 Cockpit API 服务，请确认服务已启动且端口正确"
    return message


def post_local_image_api(api_base_url, payload, headers):
    last_error = None
    for path in LOCAL_IMAGE_PATHS:
        endpoint = api_base_url.rstrip("/") + path
        print(f"DEBUG image request URL: {endpoint}", flush=True)
        print(f"DEBUG image request Headers: {json.dumps(mask_headers(headers), ensure_ascii=False)}", flush=True)
        print(f"DEBUG image request Payload: {json.dumps(payload, ensure_ascii=False)}", flush=True)
        try:
            if requests is not None:
                response = requests.post(endpoint, json=payload, headers=headers, timeout=60)
                if response.status_code == 404:
                    last_error = RuntimeError(f"404 - {response.text}")
                    print(f"DEBUG endpoint returned 404, trying next path: {path}", flush=True)
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(f"{response.status_code} - {response.text}")
                return response.json()
            request = UrlRequest(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 404:
                    last_error = exc
                    print(f"DEBUG endpoint returned 404, trying next path: {path}", flush=True)
                    continue
                raise
        except Exception as exc:
            error_text = str(exc).lower()
            if "connection refused" in error_text or "actively refused" in error_text or "由于目标计算机积极拒绝" in str(exc):
                raise RuntimeError("无法连接到 Cockpit API 服务，请确认服务已启动且端口正确") from exc
            raise
    raise RuntimeError(f"所有图片生成端点都返回 404，请确认 Cockpit API 路径是否正确：{last_error}")


def generate_image_with_api(prompt, image_file):
    api_base_url = require_api_base_url()
    debug_print_registered_routes()
    payload = {
        "prompt": prompt,
        "size": "1024x1024",
        "n": 1,
    }
    headers = local_api_headers()
    data = post_local_image_api(api_base_url, payload, headers)

    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        image_file.write_bytes(base64.b64decode(item["b64_json"]))
        return
    if item.get("url"):
        if requests is not None:
            image_response = requests.get(item["url"], headers=headers, timeout=60)
            if image_response.status_code >= 400:
                raise RuntimeError(f"{image_response.status_code} - {image_response.text}")
            image_file.write_bytes(image_response.content)
        else:
            image_request = UrlRequest(item["url"], headers=headers, method="GET")
            with urlopen(image_request, timeout=60) as response:
                image_file.write_bytes(response.read())
        return
    raise RuntimeError("本地图片 API 返回为空，未找到图片数据")


def generate_single_image(prompt, image_file):
    api_base_url = require_api_base_url()
    payload = {
        "prompt": prompt,
        "size": "1024x1024",
        "n": 1,
    }
    headers = local_api_headers()
    data = post_local_image_api(api_base_url, payload, headers)
    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        image_file.write_bytes(base64.b64decode(item["b64_json"]))
        return
    if item.get("url"):
        if requests is not None:
            image_response = requests.get(item["url"], headers=headers, timeout=60)
            if image_response.status_code >= 400:
                raise RuntimeError(f"{image_response.status_code} - {image_response.text}")
            image_file.write_bytes(image_response.content)
        else:
            image_request = UrlRequest(item["url"], headers=headers, method="GET")
            with urlopen(image_request, timeout=60) as response:
                image_file.write_bytes(response.read())
        return
    raise RuntimeError("本地图片 API 返回为空，未找到图片数据")


def compose_nine_grid(image_files, output_file):
    images = [Image.open(path).convert("RGB") for path in image_files]
    width, height = images[0].size
    canvas = Image.new("RGB", (width * 3, height * 3), "white")
    for index, image in enumerate(images):
        if image.size != (width, height):
            image = image.resize((width, height))
        x = (index % 3) * width
        y = (index // 3) * height
        canvas.paste(image, (x, y))
    canvas.save(output_file)
    for image in images:
        image.close()


def resolve_logo_path(logo_path):
    path = Path(logo_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def add_logo_to_image(image_file, logo_path, output_file, opacity=0.82):
    resolved_logo = resolve_logo_path(logo_path)
    if not resolved_logo.exists():
        raise FileNotFoundError("Logo 文件未找到")
    base = Image.open(image_file).convert("RGBA")
    logo = Image.open(resolved_logo).convert("RGBA")
    target_width = max(base.width // 5, 1)
    ratio = target_width / logo.width
    logo = logo.resize((target_width, max(int(logo.height * ratio), 1)))
    if opacity < 1:
        alpha = logo.getchannel("A")
        alpha = alpha.point(lambda value: int(value * opacity))
        logo.putalpha(alpha)
    x = (base.width - logo.width) // 2
    y = (base.height - logo.height) // 2
    base.alpha_composite(logo, (x, y))
    base.convert("RGB").save(output_file)


def run_image_for_task(task):
    sku_dir = image_folder_for_task(task)
    sku_dir.mkdir(parents=True, exist_ok=True)
    main_file = sku_dir / "主图.png"
    logo_file = sku_dir / "主图_带logo.png"
    optimized_prompt = build_codex_prompt(task, main_file)
    prompt_snapshot = task.get("prompt") or ""
    started = now_iso()
    print(
        "DEBUG generation paths:",
        f"sku={task.get('sku')}",
        f"image_folder={sku_dir}",
        f"main_file={main_file}",
        f"logo_file={logo_file}",
        flush=True,
    )

    with connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'generating', error_message = NULL, optimized_prompt = ?,
                prompt_snapshot = ?, generation_progress = '生成中(0/9)', image_folder = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (optimized_prompt, prompt_snapshot, str(sku_dir), started, task["id"]),
        )
        log_event(conn, task["id"], task["sku"], "generation_start", "Local image API started")

    try:
        image_files = []
        for index in range(1, 10):
            image_file = sku_dir / f"{index}.png"
            single_prompt = f"{optimized_prompt}\nVariant {index} of 9. Keep the same product and color, vary composition subtly."
            with connect() as conn:
                conn.execute(
                    "UPDATE tasks SET generation_progress = ?, updated_at = ? WHERE id = ?",
                    (f"生成中({index}/9)", now_iso(), task["id"]),
                )
            generate_single_image(single_prompt, image_file)
            if not image_file.exists():
                raise RuntimeError(f"第 {index} 张图片未生成")
            print(f"DEBUG image saved: sku={task.get('sku')} file={image_file}", flush=True)
            image_files.append(image_file)

        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'collaging', generation_progress = '拼图中', updated_at = ? WHERE id = ?",
                (now_iso(), task["id"]),
            )
        compose_nine_grid(image_files, main_file)
        print(f"DEBUG collage saved: sku={task.get('sku')} file={main_file}", flush=True)
        final_file = main_file

        add_logo = str(task.get("add_logo") or "").strip().lower()
        should_add_logo = add_logo in ("是", "yes", "y", "true", "1")
        if should_add_logo:
            logo_path = task.get("logo_path")
            if not logo_path:
                raise FileNotFoundError("Logo 文件未找到")
            add_logo_to_image(main_file, logo_path, logo_file)
            final_file = logo_file
            print(f"DEBUG logo image saved: sku={task.get('sku')} file={logo_file}", flush=True)

        if not final_file.exists():
            raise RuntimeError("九宫格主图未生成")

        with connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'waiting_confirm', image_path = ?, image_folder = ?, error_message = NULL,
                    generation_progress = NULL, generated_at = ?, updated_at = ?,
                    retry_count = retry_count + 1
                WHERE id = ?
                """,
                (str(final_file), str(sku_dir), now_iso(), now_iso(), task["id"]),
            )
            log_event(conn, task["id"], task["sku"], "generation_success", str(final_file))
        print(
            "DEBUG db image fields updated:",
            f"sku={task.get('sku')}",
            f"image_path={final_file}",
            f"image_folder={sku_dir}",
            flush=True,
        )
        set_quota("available", "最近一次本地 API 生图成功")
        return True
    except Exception as exc:
        message = openai_error_message(exc)
        mark_generation_error(task, message)
        return False


def apply_collage_for_task(task):
    sku_dir, image_files = folder_with_complete_nine_images(task)
    missing = [path.name for path in image_files if not path.exists()]
    if missing:
        raise ValueError(f"请先生成全部九张图，缺少：{', '.join(missing)}")

    main_file = sku_dir / "主图.png"
    logo_file = sku_dir / "主图_带logo.png"
    print(
        "DEBUG recollage paths:",
        f"sku={task.get('sku')}",
        f"image_folder={sku_dir}",
        f"main_file={main_file}",
        f"logo_file={logo_file}",
        flush=True,
    )
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'collaging', generation_progress = '拼图中', updated_at = ? WHERE id = ?",
            (now_iso(), task["id"]),
        )
        log_event(conn, task["id"], task["sku"], "collage_retry_start", "重新执行九宫格拼图")

    compose_nine_grid(image_files, main_file)
    print(f"DEBUG recollage saved: sku={task.get('sku')} file={main_file}", flush=True)
    final_file = main_file
    add_logo = str(task.get("add_logo") or "").strip().lower()
    should_add_logo = add_logo in ("是", "yes", "y", "true", "1")
    if should_add_logo:
        logo_path = task.get("logo_path")
        if not logo_path:
            raise FileNotFoundError("Logo 文件未找到")
        add_logo_to_image(main_file, logo_path, logo_file)
        final_file = logo_file
        print(f"DEBUG recollage logo saved: sku={task.get('sku')} file={logo_file}", flush=True)

    if not final_file.exists():
        raise RuntimeError("九宫格主图未生成")

    with connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'waiting_confirm', image_path = ?, image_folder = ?, error_message = NULL,
                generation_progress = NULL, generated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(final_file), str(sku_dir), now_iso(), now_iso(), task["id"]),
        )
        log_event(conn, task["id"], task["sku"], "collage_retry_success", str(final_file))
    print(
        "DEBUG db image fields updated by recollage:",
        f"sku={task.get('sku')}",
        f"image_path={final_file}",
        f"image_folder={sku_dir}",
        flush=True,
    )
    return final_file


def rerun_collage_for_sku(sku):
    task = get_task_by_sku(str(sku))
    if not task:
        raise ValueError("Task not found")
    try:
        final_file = apply_collage_for_task(task)
        return {"ok": True, "image_path": str(final_file), "image_url": output_url_for_path(final_file)}
    except Exception as exc:
        message = str(exc)
        with connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'collaging_failed', error_message = ?, generation_progress = NULL, updated_at = ?
                WHERE id = ?
                """,
                (message[:4000], now_iso(), task["id"]),
            )
            log_event(conn, task["id"], task["sku"], "collage_retry_error", message[:800])
        raise


def mark_generation_error(task, message):
    current_status = get_task(task["id"])
    is_collage_stage = current_status and current_status.get("status") == "collaging"
    status = "quota_exhausted" if is_quota_error(message) else ("collaging_failed" if is_collage_stage else "failed")
    if status == "quota_exhausted":
        set_quota("exhausted", "OpenAI API 返回额度不足或频率限制")
    print(
        "DEBUG generation failed:",
        f"sku={task.get('sku')}",
        f"status={status}",
        f"reason={message[:800]}",
        flush=True,
    )
    with connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, error_message = ?, updated_at = ?, retry_count = retry_count + 1
            WHERE id = ?
            """,
            (status, message[:4000], now_iso(), task["id"]),
        )
        log_event(conn, task["id"], task["sku"], "generation_error", f"{status}: {message[:800]}")


def set_quota(status, message):
    quota_state.update({"status": status, "message": message, "updated_at": now_iso()})


def next_pending_task():
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


def generation_worker():
    global worker_running
    with worker_lock:
        if worker_running:
            return
        worker_running = True
        queue_control["state"] = "running"
        queue_control["pause_requested"] = False
        queue_control["stop_requested"] = False
    try:
        while True:
            if queue_control["stop_requested"] or queue_control["pause_requested"]:
                return
            task = next_pending_task()
            if not task:
                queue_control["state"] = "idle"
                return
            ok = run_image_for_task(task)
            if not ok:
                latest = get_task(task["id"])
                if latest and latest["status"] == "quota_exhausted":
                    queue_control["state"] = "paused"
                    return
            if queue_control["stop_requested"] or queue_control["pause_requested"]:
                return
            time.sleep(IMAGE_DELAY_SECONDS)
    finally:
        with worker_lock:
            worker_running = False
            if queue_control["stop_requested"]:
                queue_control["state"] = "stopped"
            elif queue_control["pause_requested"]:
                queue_control["state"] = "paused"
            elif queue_control["state"] == "running":
                queue_control["state"] = "idle"


def start_worker():
    queue_control["state"] = "running"
    queue_control["pause_requested"] = False
    queue_control["stop_requested"] = False
    thread = threading.Thread(target=generation_worker, daemon=True)
    thread.start()


def pause_generation():
    if worker_running:
        queue_control["pause_requested"] = True
        queue_control["stop_requested"] = False
        queue_control["state"] = "paused"
    return generate_status()


def stop_generation():
    queue_control["stop_requested"] = True
    queue_control["pause_requested"] = False
    queue_control["state"] = "stopped"
    with connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending', updated_at = ?
            WHERE status = 'generating'
            """,
            (now_iso(),),
        )
    return generate_status()


def generate_status():
    with connect() as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
        }
    pending = counts.get("pending", 0)
    generating = counts.get("generating", 0)
    total = sum(counts.values())
    completed = max(total - pending - generating, 0)
    state = queue_control["state"]
    if worker_running and not queue_control["pause_requested"] and not queue_control["stop_requested"]:
        state = "running"
    elif not worker_running and state == "running":
        state = "idle"
    return {
        "state": state,
        "running": worker_running,
        "paused": state == "paused",
        "pending": pending,
        "generating": generating,
        "completed": completed,
        "total": total,
    }


def queue_all_paused():
    with connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending', error_message = NULL, updated_at = ?
            WHERE status = 'quota_exhausted'
            """,
            (now_iso(),),
        )
        conn.execute(
            """
            INSERT INTO events(task_id, sku, event_type, message, created_at)
            SELECT id, sku, 'resume_quota', 'Quota paused task queued again', ?
            FROM tasks
            WHERE status = 'pending'
            """,
            (now_iso(),),
        )
    set_quota("unknown", "已加入待续跑队列，等待下一次 API 结果")
    start_worker()


def get_task(task_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def get_task_by_sku(sku):
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE sku = ?", (sku,)).fetchone()
        return dict(row) if row else None


def image_path_for_task(task):
    if task and task.get("image_path"):
        return Path(task["image_path"])
    return None


def download_image_path_for_sku(sku):
    task = get_task_by_sku(sku)
    if not task:
        return None
    return main_image_path_for_task(task)


def main_image_path_for_task(task):
    candidates = []
    if task.get("image_path"):
        candidates.append(Path(task["image_path"]))
    if task.get("image_folder"):
        candidates.append(Path(task["image_folder"]) / "主图_带logo.png")
        candidates.append(Path(task["image_folder"]) / "主图.png")
        candidates.append(Path(task["image_folder"]) / "main.png")
    candidates.append(image_folder_for_task(task) / "主图_带logo.png")
    candidates.append(image_folder_for_task(task) / "主图.png")
    candidates.append(image_folder_for_task(task) / "main.png")
    candidates.append(legacy_image_folder_for_task(task) / "主图_带logo.png")
    candidates.append(legacy_image_folder_for_task(task) / "主图.png")
    candidates.append(legacy_image_folder_for_task(task) / "main.png")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def image_files_for_task(task):
    if not task:
        return []
    folders = []
    folders.append(image_folder_for_task(task))
    if task.get("image_folder"):
        folders.append(Path(task["image_folder"]))
    folders.append(legacy_image_folder_for_task(task))
    files = []
    seen = set()

    for index in range(1, 10):
        for folder in folders:
            candidate = folder / f"{index}.png"
            if candidate.exists():
                resolved = candidate.resolve()
                if resolved not in seen:
                    files.append((f"{index}.png", candidate))
                    seen.add(resolved)
                break

    main_path = main_image_path_for_task(task)
    if main_path and main_path.exists():
        resolved = main_path.resolve()
        if resolved not in seen:
            files.append(("主图.png", main_path))

    return files


def folder_with_complete_nine_images(task):
    folders = [image_folder_for_task(task)]
    if task.get("image_folder"):
        folders.append(Path(task["image_folder"]))
    folders.append(legacy_image_folder_for_task(task))
    seen = set()
    for folder in folders:
        resolved = folder.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        image_files = [folder / f"{index}.png" for index in range(1, 10)]
        if all(path.exists() for path in image_files):
            return folder, image_files
    return folders[0], [folders[0] / f"{index}.png" for index in range(1, 10)]


def output_url_for_path(path):
    resolved = Path(path).resolve()
    output_root = OUTPUT_DIR.resolve()
    if resolved == output_root:
        return "/output"
    if output_root not in resolved.parents:
        return ""
    relative = resolved.relative_to(output_root)
    return "/output/" + "/".join(quote(part) for part in relative.parts)


def image_list_for_sku(sku):
    task = get_task_by_sku(str(sku))
    files = image_files_for_task(task)
    return [
        {
            "name": name,
            "url": output_url_for_path(path),
            "path": str(path),
        }
        for name, path in files
    ]


def zip_all_images_for_sku(sku):
    task = get_task_by_sku(str(sku))
    files = image_files_for_task(task)
    if not files:
        raise ValueError("该 SKU 暂无可下载图片")

    EXPORT_DIR.mkdir(exist_ok=True)
    zip_path = EXPORT_DIR / zip_filename_for_skus([sku])
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, path in files:
            archive.write(path, arcname=arcname)
    return zip_path


def zip_images_for_skus(skus):
    EXPORT_DIR.mkdir(exist_ok=True)
    filename = zip_filename_for_skus(skus)
    zip_path = EXPORT_DIR / filename
    added = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for sku in skus:
            task = get_task_by_sku(str(sku))
            folder_name = download_base_name_for_task(task, str(sku))
            for arcname, image_path in image_files_for_task(task):
                archive.write(image_path, arcname=f"{folder_name}/{arcname}")
                added += 1
    if added == 0:
        raise ValueError("选中的 SKU 暂无可下载图片")
    return zip_path


def delete_tasks_by_skus(skus):
    deleted = 0
    with connect() as conn:
        for sku in skus:
            task = conn.execute("SELECT * FROM tasks WHERE sku = ?", (str(sku),)).fetchone()
            if not task:
                continue
            image_folder = task["image_folder"]
            image_path = task["image_path"]
            conn.execute("DELETE FROM events WHERE sku = ?", (task["sku"],))
            conn.execute("DELETE FROM tasks WHERE sku = ?", (task["sku"],))
            deleted += 1
            folder = Path(image_folder) if image_folder else (Path(image_path).parent if image_path else image_folder_for_task(dict(task)))
            if folder and folder.exists() and OUTPUT_DIR.resolve() in folder.resolve().parents:
                shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True, "deleted": deleted}


def export_generation_status(status):
    if status in ("waiting_confirm", "awaiting_confirm", "approved"):
        return "成功"
    if status in ("failed", "collaging_failed", "quality_failed", "rejected", "quota_exhausted"):
        return "失败"
    return "待生成"


def export_excel():
    EXPORT_DIR.mkdir(exist_ok=True)
    filename = f"erp_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    path = EXPORT_DIR / filename
    wb = Workbook()
    ws = wb.active
    ws.title = "ERP导出"
    columns = [
        "SPU",
        "图片生成提示词",
        "图片颜色",
        "图片命名",
        "是否添加品牌logo",
        "logo文件地址",
        "需校验品牌文字/元素",
        "优化后提示词",
        "提示词原文快照",
        "生成状态",
        "失败原因",
        "生成时间",
        "输出文件地址",
        "SKU",
    ]
    ws.append(columns)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
    for row in rows:
        ws.append(
            [
                row["spu"] or "",
                row["prompt"] or "",
                row["color_name"] or "",
                row["image_name"] or "",
                row["add_logo"] or "",
                row["logo_path"] or "",
                row["brand_check"] or "",
                row["optimized_prompt"] or "",
                row["prompt_snapshot"] or "",
                export_generation_status(row["status"]),
                row["error_message"] or "",
                row["generated_at"] or "",
                row["image_path"] or "",
                row["sku"],
            ]
        )
    wb.save(path)
    return path


def app_state():
    with connect() as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
        }
    return {
        "counts": counts,
        "quota": quota_state,
        "codex": api_config_state,
        "worker_running": worker_running,
        "generation": generate_status(),
        "statuses": STATUS_LABELS,
    }


HTTP_ROUTES = [
    ("GET", "/"),
    ("GET", "/docs"),
    ("GET", "/app.css"),
    ("GET", "/app.js"),
    ("GET", "/api/tasks"),
    ("GET", "/api/state"),
    ("GET", "/api/codex/status"),
    ("GET", "/api/codex/user-info"),
    ("GET", "/api/test-route"),
    ("GET", "/api/export"),
    ("GET", "/api/images/{sku}"),
    ("GET", "/output/{path}"),
    ("POST", "/api/download/batch"),
    ("GET", "/api/download/main/{sku}"),
    ("GET", "/api/download/all/{sku}"),
    ("GET", "/api/download/{sku}"),
    ("DELETE", "/api/tasks"),
    ("POST", "/api/manual"),
    ("POST", "/api/import"),
    ("POST", "/api/collage/test"),
    ("POST", "/api/collage/{sku:path}"),
    ("POST", "/api/generate/start"),
    ("POST", "/api/generate/nine/{sku}"),
    ("POST", "/api/generate/pause"),
    ("POST", "/api/generate/stop"),
    ("GET", "/api/generate/status"),
    ("POST", "/api/quota/resume"),
    ("POST", "/api/codex/refresh"),
    ("POST", "/api/codex/set-config"),
    ("POST", "/api/codex/set-config/"),
    ("POST", "/api/codex/set-key"),
    ("POST", "/api/codex/set-key/"),
    ("POST", "/api/tasks/{task_id}/{action}"),
]


def docs_html():
    rows = "\n".join(
        f"<tr><td>{method}</td><td><code>{path}</code></td></tr>" for method, path in HTTP_ROUTES
    )
    return f"""
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>SKU Workflow API Docs</title>
    <style>
      body {{ font-family: Segoe UI, Microsoft YaHei, sans-serif; margin: 32px; color: #1f2937; }}
      table {{ border-collapse: collapse; width: 100%; max-width: 920px; }}
      th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 12px; text-align: left; }}
      th {{ background: #f8fafc; }}
      code {{ background: #f1f5f9; border-radius: 4px; padding: 2px 6px; }}
    </style>
  </head>
  <body>
    <h1>SKU Workflow API</h1>
    <p>当前环境未启用 FastAPI 自动文档，以下为本地服务已注册路由。</p>
    <table>
      <thead><tr><th>Method</th><th>Path</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </body>
</html>
""".strip()


def print_registered_routes():
    if FASTAPI_AVAILABLE and app is not None:
        for route in app.routes:
            methods = ", ".join(sorted(route.methods or []))
            print(f"Route: {route.path} [{methods}]", flush=True)
    else:
        for method, path in HTTP_ROUTES:
            print(f"Route: {path} [{method}]", flush=True)


def debug_print_registered_routes():
    print("已注册的路由:", flush=True)
    print_registered_routes()


app = FastAPI(title="SKU Workflow API", docs_url="/docs") if FASTAPI_AVAILABLE else None

if FASTAPI_AVAILABLE:
    @app.post("/api/collage/test")
    async def test_collage():
        return {"status": "ok"}

    @app.get("/api/test-route")
    async def test_route():
        return {"status": "ok"}

    @app.get("/")
    def fastapi_index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/app.css")
    def fastapi_css():
        return FileResponse(STATIC_DIR / "app.css", media_type="text/css")

    @app.get("/app.js")
    def fastapi_js():
        return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")

    @app.get("/api/tasks")
    def fastapi_tasks(status: str = "all", q: str = ""):
        return list_tasks(status, q)

    @app.get("/api/state")
    def fastapi_state():
        return app_state()

    @app.get("/api/codex/status")
    def fastapi_codex_status():
        load_api_base_url()
        return dict(api_config_state)

    @app.get("/api/codex/user-info")
    def fastapi_codex_user_info():
        return api_config_user_info()

    @app.post("/api/codex/set-key")
    @app.post("/api/codex/set-key/")
    @app.post("/api/codex/set-config")
    @app.post("/api/codex/set-config/")
    async def set_codex_config(request: FastAPIRequest):
        try:
            payload = await request.json()
            result = set_api_base_url(
                payload.get("api_base_url") or payload.get("api_key"),
                payload.get("access_token"),
            )
            return {"success": True, "message": "本地服务地址已保存", **result}
        except Exception as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=400)

    @app.post("/api/codex/refresh")
    def fastapi_codex_refresh():
        load_api_base_url()
        return dict(api_config_state)

    @app.get("/api/export")
    def fastapi_export():
        path = export_excel()
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=path.name,
        )

    @app.get("/api/images/{sku:path}")
    def fastapi_images_sku(sku: str):
        task = get_task_by_sku(sku)
        if not task:
            return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)
        images = image_list_for_sku(sku)
        print(f"DEBUG images route hit: sku={sku}, count={len(images)}", flush=True)
        return {"ok": True, "sku": sku, "images": images}

    @app.post("/api/download/batch")
    async def fastapi_download_batch(request: FastAPIRequest):
        try:
            payload = await request.json()
            path = zip_images_for_skus(payload.get("skus") or [])
            return FileResponse(path, media_type="application/zip", filename=path.name)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.get("/api/download/main/{sku:path}")
    def fastapi_download_main_sku(sku: str):
        task = get_task_by_sku(sku)
        image_path = download_image_path_for_sku(sku)
        print(f"DEBUG download main route hit: sku={sku}, image_path={image_path}", flush=True)
        if not image_path:
            return JSONResponse({"ok": False, "error": "图片未生成"}, status_code=404)
        return FileResponse(image_path, media_type="image/png", filename=f"{download_base_name_for_task(task, sku)}_主图.png")

    @app.get("/api/download/all/{sku:path}")
    def fastapi_download_all_sku(sku: str):
        try:
            zip_path = zip_all_images_for_sku(sku)
            print(f"DEBUG download all route hit: sku={sku}, zip_path={zip_path}", flush=True)
            return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @app.get("/api/download/{sku:path}")
    def fastapi_download_sku(sku: str):
        try:
            zip_path = zip_all_images_for_sku(sku)
            print(f"DEBUG download route hit: sku={sku}, zip_path={zip_path}", flush=True)
            return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @app.delete("/api/tasks")
    async def fastapi_delete_tasks(request: FastAPIRequest):
        try:
            payload = await request.json()
            return delete_tasks_by_skus(payload.get("skus") or [])
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.get("/output/{file_path:path}")
    def fastapi_output(file_path: str):
        requested = (OUTPUT_DIR / file_path).resolve()
        if OUTPUT_DIR.resolve() not in requested.parents and requested != OUTPUT_DIR.resolve():
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        if not requested.exists():
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(requested, media_type="image/png")

    @app.post("/api/manual")
    async def fastapi_manual(request: FastAPIRequest):
        try:
            add_manual_task(await request.json())
            if load_api_base_url():
                start_worker()
            return {"ok": True}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/import")
    async def fastapi_import(request: FastAPIRequest):
        try:
            body = await request.body()
            name = request.headers.get("X-Filename") or f"upload_{uuid.uuid4().hex}.xlsx"
            safe_name = Path(name).name
            path = IMPORT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
            path.write_bytes(body)
            stats = import_excel(path)
            if stats.get("imported") and load_api_base_url():
                start_worker()
            return {"ok": True, "stats": stats}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/generate/start")
    def fastapi_generate_start():
        try:
            require_api_base_url()
            start_worker()
            return {"ok": True}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/collage/{sku:path}")
    def fastapi_collage_sku(sku: str):
        try:
            return rerun_collage_for_sku(sku)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/generate/nine/{sku:path}")
    def fastapi_generate_nine(sku: str):
        try:
            task = get_task_by_sku(sku)
            if not task:
                return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)
            reset_for_generation(task["id"])
            require_api_base_url()
            start_worker()
            return {"ok": True}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/generate/pause")
    def fastapi_generate_pause():
        return pause_generation()

    @app.post("/api/generate/stop")
    def fastapi_generate_stop():
        return stop_generation()

    @app.get("/api/generate/status")
    def fastapi_generate_status():
        return generate_status()

    @app.post("/api/quota/resume")
    def fastapi_quota_resume():
        try:
            require_api_base_url()
            queue_all_paused()
            return {"ok": True}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.post("/api/tasks/{task_id}/{action}")
    async def fastapi_task_action(task_id: int, action: str, request: FastAPIRequest):
        try:
            if action == "approve":
                update_status(task_id, "approved")
            elif action == "reject":
                update_status(task_id, "rejected")
            elif action == "quality-fail":
                payload = await request.json()
                update_status(task_id, "quality_failed", payload.get("message", "质检失败"))
            elif action == "retry":
                require_api_base_url()
                reset_for_generation(task_id)
                start_worker()
            elif action == "queue":
                reset_for_generation(task_id)
            else:
                return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
            return {"ok": True}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        LOG_DIR.mkdir(exist_ok=True)
        with (LOG_DIR / "server.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {self.address_string()} {fmt % args}\n")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type="application/octet-stream", download_name=None):
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if download_name:
            ascii_name = "".join(ch if ord(ch) < 128 else "_" for ch in download_name)
            encoded_name = quote(download_name)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
            )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif parsed.path == "/docs":
            body = docs_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/app.css":
            self.send_file(STATIC_DIR / "app.css", "text/css; charset=utf-8")
        elif parsed.path == "/app.js":
            self.send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
        elif parsed.path == "/api/tasks":
            qs = parse_qs(parsed.query)
            self.send_json(list_tasks(qs.get("status", ["all"])[0], qs.get("q", [""])[0]))
        elif parsed.path == "/api/state":
            self.send_json(app_state())
        elif parsed.path == "/api/generate/status":
            self.send_json(generate_status())
        elif parsed.path == "/api/codex/status":
            load_api_base_url()
            self.send_json(dict(api_config_state))
        elif parsed.path == "/api/codex/user-info":
            self.send_json(api_config_user_info())
        elif parsed.path == "/api/test-route":
            self.send_json({"status": "ok"})
        elif parsed.path == "/api/export":
            path = export_excel()
            self.send_file(path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", path.name)
        elif parsed.path.startswith("/api/images/"):
            sku = unquote(parsed.path.split("/api/images/", 1)[1])
            task = get_task_by_sku(sku)
            if not task:
                self.send_json({"ok": False, "error": "Task not found"}, 404)
                return
            images = image_list_for_sku(sku)
            print(f"DEBUG images route hit: sku={sku}, count={len(images)}", flush=True)
            self.send_json({"ok": True, "sku": sku, "images": images})
        elif parsed.path.startswith("/api/download/main/"):
            sku = unquote(parsed.path.split("/api/download/main/", 1)[1])
            task = get_task_by_sku(sku)
            image_path = download_image_path_for_sku(sku)
            print(f"DEBUG download main route hit: sku={sku}, image_path={image_path}", flush=True)
            if not image_path:
                self.send_json({"ok": False, "error": "图片未生成"}, 404)
                return
            self.send_file(image_path, "image/png", f"{download_base_name_for_task(task, sku)}_主图.png")
        elif parsed.path.startswith("/api/download/all/"):
            sku = unquote(parsed.path.split("/api/download/all/", 1)[1])
            zip_path = zip_all_images_for_sku(sku)
            print(f"DEBUG download all route hit: sku={sku}, zip_path={zip_path}", flush=True)
            self.send_file(zip_path, "application/zip", zip_path.name)
        elif parsed.path.startswith("/api/download/"):
            sku = unquote(parsed.path.split("/api/download/", 1)[1])
            zip_path = zip_all_images_for_sku(sku)
            print(f"DEBUG download route hit: sku={sku}, zip_path={zip_path}", flush=True)
            self.send_file(zip_path, "application/zip", zip_path.name)
        elif parsed.path.startswith("/output/"):
            relative_output_path = unquote(parsed.path.split("/output/", 1)[1])
            requested = (OUTPUT_DIR / relative_output_path).resolve()
            if OUTPUT_DIR.resolve() not in requested.parents and requested != OUTPUT_DIR.resolve():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.send_file(requested, "image/png")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            normalized_path = parsed.path.rstrip("/") or "/"
            if normalized_path in ("/api/codex/set-config", "/api/codex/set-key"):
                print(f"DEBUG: set local API config called: {parsed.path}", flush=True)
                payload = self.read_json()
                self.send_json(
                    set_api_base_url(
                        payload.get("api_base_url") or payload.get("api_key"),
                        payload.get("access_token"),
                    )
                )
            elif parsed.path == "/api/manual":
                add_manual_task(self.read_json())
                if load_api_base_url():
                    start_worker()
                self.send_json({"ok": True})
            elif parsed.path == "/api/import":
                self.handle_import()
            elif parsed.path == "/api/generate/start":
                require_api_base_url()
                start_worker()
                self.send_json({"ok": True})
            elif parsed.path == "/api/collage/test":
                self.send_json({"status": "ok"})
            elif parsed.path.startswith("/api/collage/"):
                sku = unquote(parsed.path.split("/api/collage/", 1)[1])
                self.send_json(rerun_collage_for_sku(sku))
            elif parsed.path.startswith("/api/generate/nine/"):
                sku = unquote(parsed.path.split("/api/generate/nine/", 1)[1])
                task = get_task_by_sku(sku)
                if not task:
                    self.send_json({"ok": False, "error": "Task not found"}, 404)
                    return
                reset_for_generation(task["id"])
                require_api_base_url()
                start_worker()
                self.send_json({"ok": True})
            elif parsed.path == "/api/generate/pause":
                self.send_json(pause_generation())
            elif parsed.path == "/api/generate/stop":
                self.send_json(stop_generation())
            elif parsed.path == "/api/quota/resume":
                require_api_base_url()
                queue_all_paused()
                self.send_json({"ok": True})
            elif parsed.path == "/api/codex/refresh":
                load_api_base_url()
                self.send_json(dict(api_config_state))
            elif parsed.path == "/api/download/batch":
                payload = self.read_json()
                path = zip_images_for_skus(payload.get("skus") or [])
                self.send_file(path, "application/zip", path.name)
            elif parsed.path.startswith("/api/tasks/"):
                self.handle_task_action(parsed.path)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"ok": False, "error": str(exc)}, 400)

    def do_DELETE(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/tasks":
                payload = self.read_json()
                self.send_json(delete_tasks_by_skus(payload.get("skus") or []))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"ok": False, "error": str(exc)}, 400)

    def handle_import(self):
        length = int(self.headers.get("Content-Length", "0"))
        name = self.headers.get("X-Filename") or f"upload_{uuid.uuid4().hex}.xlsx"
        safe_name = Path(name).name
        path = IMPORT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
        path.write_bytes(self.rfile.read(length))
        stats = import_excel(path)
        if stats.get("imported") and load_api_base_url():
            start_worker()
        self.send_json({"ok": True, "stats": stats})

    def handle_task_action(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        task_id = int(parts[2])
        action = parts[3]
        if action == "approve":
            update_status(task_id, "approved")
        elif action == "reject":
            update_status(task_id, "rejected")
        elif action == "quality-fail":
            payload = self.read_json()
            update_status(task_id, "quality_failed", payload.get("message", "质检失败"))
        elif action == "retry":
            require_api_base_url()
            reset_for_generation(task_id)
            start_worker()
        elif action == "queue":
            reset_for_generation(task_id)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True})


def main():
    print("Server started, waiting for requests...", flush=True)
    print(f"DEBUG app.py path: {Path(__file__).resolve()}", flush=True)
    print(f"DEBUG cwd: {Path.cwd()}", flush=True)
    print(f"DEBUG server mode: {'FastAPI' if FASTAPI_AVAILABLE else 'fallback-http'}", flush=True)
    ensure_admin()
    init_db()
    api_base_url = load_api_base_url()
    print_local_api_config()
    if not api_base_url:
        print("WARNING: 未配置 Codex 本地服务地址：请先在网页右上角配置本地服务", flush=True)
    host = "127.0.0.1"
    port = int(os.environ.get("SKU_APP_PORT", "8765"))
    print_registered_routes()
    if FASTAPI_AVAILABLE:
        print(f"SKU workflow dashboard running at http://{host}:{port}", flush=True)
        uvicorn.run(app, host=host, port=port, log_level="info")
        return
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"SKU workflow dashboard running at http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
