from __future__ import annotations

import base64
import binascii
import json
import queue
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from curl_cffi import requests

BASE_URL = "https://kpopping.com"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
PHOTOS_URL = f"{BASE_URL}/api/photos"
COOKIE_FILE = Path(__file__).with_name("session_cookies.json")
CONFIG_FILE = Path(__file__).with_name("config.json")
DOWNLOAD_HISTORY_FILE = Path(__file__).with_name("download_history.json")
PAGE_SIZE = 50
REQUEST_TIMEOUT = 60
MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_DOWNLOAD_WORKERS = 4
MAX_DOWNLOAD_WORKERS = 16

DEFAULT_IDOL_ID = "077c4f02-7ca6-49a6-9daf-df1dabc55d0f"
DEFAULT_IDOL_NAME = "Karina"

BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "origin": BASE_URL,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


class DownloadCancelled(Exception):
    """用户主动取消下载。"""


class LoginRequired(Exception):
    """本地会话不可用且没有提供登录凭证。"""


@dataclass(frozen=True)
class DownloadResult:
    albums: int
    history_skipped_albums: int
    downloaded: int
    skipped: int
    failed: int
    output_dir: Path


def write_json_file(path: Path, data: object) -> None:
    """原子写入 JSON，避免程序退出时留下半个文件。"""
    temp_file = path.with_name(path.name + ".tmp")
    try:
        temp_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_file.replace(path)
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise


class DownloadHistory:
    def __init__(self, path: Path, log: Callable[[str], None]) -> None:
        self.path = path
        self.log = log
        self.album_folders: set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not all(
                isinstance(folder_name, str) for folder_name in data
            ):
                raise ValueError("内容不是相册文件夹名数组")
            self.album_folders = set(data)
            self.log(
                f"已加载下载记录：{self.path.name}（{len(self.album_folders)} 个相册）"
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.log(f"下载记录无法读取，本次不会使用历史记录：{exc}")

    def contains(self, folder_name: str) -> bool:
        return folder_name in self.album_folders

    def record(self, folder_name: str) -> None:
        updated_folders = self.album_folders | {folder_name}
        write_json_file(self.path, sorted(updated_folders))
        self.album_folders = updated_folders


def safe_name(value: str, fallback: str = "untitled", max_length: int = 100) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    while len(value.encode("utf-8")) > max_length:
        value = value[:-1]
    return value.rstrip(" .") or fallback


def is_kpopping_host(host: str) -> bool:
    host = host.lower()
    return host == "kpopping.com" or host.endswith(".kpopping.com")


def to_download_url(src: str) -> str:
    parsed = urlparse(src)
    if not parsed.path:
        raise ValueError("图片地址缺少路径")
    # 旧图集 src 已是 kpopping.com/documents/...，会 302 到 legacy.kpopping.com。
    # 把这些路径改到 cdn.kpopping.com 会 404。
    if is_kpopping_host(parsed.netloc):
        return src
    return f"https://cdn.kpopping.com{parsed.path}"


def retry_after_seconds(value: str | None) -> int:
    if not value:
        return 60
    try:
        return max(1, int(float(value)))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(1, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 60


def auth_token_is_expired(token: str) -> bool:
    """只读取 JWT 的 exp，签名仍由服务端验证。无法解析时交给服务端处理。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        expires_at = float(data["exp"])
        return expires_at <= time.time() + 30
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return False


class KpoppingDownloader:
    def __init__(
        self,
        *,
        email: str,
        password: str,
        device_fingerprint: str,
        idol_id: str,
        idol_name: str,
        sort: str,
        download_workers: int,
        output_dir: Path,
        stop_event: threading.Event,
        log: Callable[[str], None],
        album_progress: Callable[[int, int], None],
    ) -> None:
        self.email = email
        self.password = password
        self.device_fingerprint = device_fingerprint
        self.idol_id = idol_id
        self.idol_name = idol_name
        self.sort = sort
        self.download_workers = max(
            1, min(download_workers, MAX_DOWNLOAD_WORKERS)
        )
        self.output_dir = output_dir
        self.stop_event = stop_event
        self.log = log
        self.album_progress = album_progress
        self.history = DownloadHistory(DOWNLOAD_HISTORY_FILE, log)
        self.session = self.create_session()
        self.image_thread_local = threading.local()
        self.image_sessions: list[requests.Session] = []
        self.image_sessions_lock = threading.Lock()
        self.rate_limit_lock = threading.Lock()
        self.rate_limit_until = 0.0

    def create_session(self) -> requests.Session:
        session = requests.Session(impersonate="chrome")
        session.headers.update(BASE_HEADERS)
        if COOKIE_FILE.exists():
            try:
                saved = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    session.cookies.update(saved)
                    self.log(f"已加载本地会话：{COOKIE_FILE.name}")
            except (OSError, json.JSONDecodeError) as exc:
                self.log(f"本地 Cookie 无法读取，将重新登录：{exc}")
        return session

    def save_session(self) -> None:
        cookies = dict(self.session.cookies)
        try:
            COOKIE_FILE.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.log(f"登录成功，会话已保存到 {COOKIE_FILE.name}")
        except OSError as exc:
            self.log(f"登录成功，但本地会话保存失败：{exc}")

    def check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise DownloadCancelled

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_cancelled()
            time.sleep(min(0.25, deadline - time.monotonic()))

    def wait_for_rate_limit(self) -> None:
        while True:
            with self.rate_limit_lock:
                remaining = self.rate_limit_until - time.monotonic()
            if remaining <= 0:
                return
            self.wait(min(remaining, 1.0))

    def request(
        self,
        method: str,
        url: str,
        *,
        client: requests.Session | None = None,
        **kwargs,
    ):
        request_session = client or self.session
        for retry in range(MAX_RATE_LIMIT_RETRIES + 1):
            self.check_cancelled()
            self.wait_for_rate_limit()
            response = request_session.request(
                method, url, timeout=REQUEST_TIMEOUT, **kwargs
            )
            if response.status_code != 429:
                return response
            seconds = retry_after_seconds(response.headers.get("retry-after"))
            response.close()
            if retry == MAX_RATE_LIMIT_RETRIES:
                raise RuntimeError("多次触发 429 限流，请稍后再试")
            with self.rate_limit_lock:
                self.rate_limit_until = max(
                    self.rate_limit_until, time.monotonic() + seconds
                )
            self.log(f"触发站点限流，按 retry-after 等待 {seconds} 秒……")
        raise RuntimeError("请求重试失败")

    def get_image_session(self) -> requests.Session:
        image_session = getattr(self.image_thread_local, "session", None)
        if image_session is None:
            image_session = requests.Session(impersonate="chrome")
            image_session.headers.update(BASE_HEADERS)
            self.image_thread_local.session = image_session
            with self.image_sessions_lock:
                self.image_sessions.append(image_session)
        return image_session

    def close_sessions(self) -> None:
        with self.image_sessions_lock:
            image_sessions = list(self.image_sessions)
            self.image_sessions.clear()
        for image_session in image_sessions:
            image_session.close()
        self.session.close()

    def ensure_login(self) -> None:
        auth_token = self.session.cookies.get("auth_token")
        if auth_token:
            if not auth_token_is_expired(auth_token):
                self.log("已有 auth_token，跳过登录")
                return
            self.log("本地 auth_token 已过期，将重新登录")
            self.session.cookies.delete("auth_token")
        if not self.email or not self.password:
            raise LoginRequired("没有可用的 auth_token，请填写账号和密码")

        response = self.request(
            "POST",
            LOGIN_URL,
            json={
                "email": self.email,
                "password": self.password,
                "deviceFingerprint": self.device_fingerprint,
            },
        )
        try:
            if response.status_code != 200:
                raise RuntimeError(f"登录失败，HTTP {response.status_code}")
            body = response.json()
            if not isinstance(body, dict) or not body.get("success"):
                raise RuntimeError("登录失败，站点未返回 success=true")
            if not self.session.cookies.get("auth_token"):
                raise RuntimeError("登录响应中没有 auth_token")
        finally:
            response.close()
        self.save_session()

    def fetch_all_albums(self) -> list[dict]:
        albums: list[dict] = []
        seen_slugs: set[str] = set()
        offset = 0
        referer = f"{BASE_URL}/kpics?idol={self.idol_id}&idolName={self.idol_name}"

        while True:
            self.check_cancelled()
            self.log(f"正在读取图集列表：offset={offset}")
            response = self.request(
                "GET",
                PHOTOS_URL,
                params={
                    "idolId": self.idol_id,
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "sort": self.sort,
                },
                headers={"referer": referer},
            )
            try:
                response.raise_for_status()
                page = response.json()
            finally:
                response.close()
            if not isinstance(page, list):
                raise ValueError("图集列表接口返回了非数组数据")

            new_count = 0
            for album in page:
                if not isinstance(album, dict):
                    continue
                slug = album.get("slug")
                if slug and slug not in seen_slugs:
                    seen_slugs.add(slug)
                    albums.append(album)
                    new_count += 1
            self.log(f"当前已找到 {len(albums)} 个图集")

            if len(page) < PAGE_SIZE:
                break
            if new_count == 0:
                self.log("接口返回了重复分页，停止继续翻页")
                break
            offset += PAGE_SIZE
        return albums

    def fetch_album(self, slug: str) -> dict:
        response = self.request(
            "GET",
            f"{BASE_URL}/api/kpics/{slug}",
            headers={"referer": f"{BASE_URL}/kpics/{slug}"},
        )
        try:
            response.raise_for_status()
            detail = response.json()
        finally:
            response.close()
        if not isinstance(detail, dict):
            raise ValueError(f"图集 {slug} 的详情不是对象")
        return detail

    def download_image(self, url: str, destination: Path) -> bool:
        if destination.exists() and destination.stat().st_size > 0:
            return False

        temp_file = destination.with_name(destination.name + ".part")
        image_session = self.get_image_session()
        response = self.request(
            "GET",
            url,
            client=image_session,
            stream=True,
            headers={"referer": BASE_URL + "/"},
        )
        try:
            response.raise_for_status()
            with temp_file.open("wb") as file:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    self.check_cancelled()
                    if chunk:
                        file.write(chunk)
            temp_file.replace(destination)
            return True
        except Exception:
            temp_file.unlink(missing_ok=True)
            raise
        finally:
            response.close()

    def run(self) -> DownloadResult:
        try:
            return self._run()
        finally:
            self.close_sessions()

    def _run(self) -> DownloadResult:
        self.ensure_login()
        albums = self.fetch_all_albums()
        if not albums:
            raise RuntimeError("没有找到图集，请检查 Idol ID")

        idol_dir = self.output_dir / safe_name(
            self.idol_name or self.idol_id, fallback="idol"
        )
        idol_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"共找到 {len(albums)} 个图集，开始读取图片并下载")

        downloaded = 0
        skipped = 0
        failed = 0
        history_skipped_albums = 0
        self.album_progress(0, len(albums))

        self.log(f"单图集图片下载并发数：{self.download_workers}")
        with ThreadPoolExecutor(
            max_workers=self.download_workers,
            thread_name_prefix="kpopping-image",
        ) as executor:
            for album_index, album in enumerate(albums, start=1):
                self.check_cancelled()
                slug = str(album["slug"])
                title = str(album.get("title") or slug)
                safe_title = safe_name(title, max_length=100)
                safe_slug = safe_name(slug, max_length=70)
                album_folder_name = f"{safe_title}_{safe_slug}"
                self.log(f"[{album_index}/{len(albums)}] 正在处理：{title}")

                try:
                    if self.history.contains(album_folder_name):
                        history_skipped_albums += 1
                        self.log("  下载记录中已有该相册，跳过")
                        continue

                    detail = self.fetch_album(slug)
                    raw_images = detail.get("albumImages") or []
                    if not isinstance(raw_images, list):
                        raise ValueError("albumImages 不是数组")
                    images = sorted(
                        (
                            item
                            for item in raw_images
                            if isinstance(item, dict) and item.get("src")
                        ),
                        key=lambda item: item.get("sortOrder") or 0,
                    )
                    album_dir = idol_dir / album_folder_name
                    album_dir.mkdir(parents=True, exist_ok=True)

                    jobs = {}
                    for image_index, image in enumerate(images, start=1):
                        self.check_cancelled()
                        url = to_download_url(str(image["src"]))
                        original_name = safe_name(
                            unquote(Path(urlparse(url).path).name),
                            fallback=f"image_{image_index:04d}.jpg",
                            max_length=140,
                        )
                        destination = album_dir / f"{image_index:04d}_{original_name}"
                        future = executor.submit(
                            self.download_image, url, destination
                        )
                        jobs[future] = url

                    try:
                        for future in as_completed(jobs):
                            try:
                                if future.result():
                                    downloaded += 1
                                else:
                                    skipped += 1
                            except DownloadCancelled:
                                for pending in jobs:
                                    pending.cancel()
                                raise
                            except Exception as exc:
                                failed += 1
                                self.log(
                                    f"  图片下载失败：{jobs[future]}（{exc}）"
                                )
                    finally:
                        if self.stop_event.is_set():
                            for pending in jobs:
                                pending.cancel()
                    self.log(f"  图集包含 {len(images)} 张图片")
                    self.history.record(album_folder_name)
                    self.log(f"  已写入下载记录：{DOWNLOAD_HISTORY_FILE.name}")
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    failed += 1
                    self.log(f"  图集处理失败：{exc}")
                finally:
                    self.album_progress(album_index, len(albums))

        return DownloadResult(
            albums=len(albums),
            history_skipped_albums=history_skipped_albums,
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            output_dir=idol_dir,
        )


class DownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Kpopping Idol 图片下载器")
        self.geometry("820x680")
        self.minsize(700, 580)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        config, config_warning = self.load_config()
        self.email_var = tk.StringVar(value=config.get("email", ""))
        self.password_var = tk.StringVar()
        self.fingerprint_var = tk.StringVar(
            value=config.get("device_fingerprint", secrets.token_hex(8))
        )
        self.idol_id_var = tk.StringVar(value=config.get("idol_id", DEFAULT_IDOL_ID))
        self.idol_name_var = tk.StringVar(
            value=config.get("idol_name", DEFAULT_IDOL_NAME)
        )
        self.sort_var = tk.StringVar(value=config.get("sort", "hot"))
        self.workers_var = tk.StringVar(
            value=str(config.get("download_workers", DEFAULT_DOWNLOAD_WORKERS))
        )
        self.output_var = tk.StringVar(
            value=config.get("output_dir", str(Path.cwd() / "downloads"))
        )
        self.status_var = tk.StringVar(value="就绪")

        self.build_ui()
        if config_warning:
            self.append_log(config_warning)
        elif CONFIG_FILE.exists():
            self.append_log(f"已加载配置：{CONFIG_FILE.name}")
        self.after(100, self.poll_events)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def load_config() -> tuple[dict, str | None]:
        if not CONFIG_FILE.exists():
            return {}, None
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("配置内容不是对象")
            allowed = {
                "email",
                "device_fingerprint",
                "idol_id",
                "idol_name",
                "sort",
                "download_workers",
                "output_dir",
            }
            config = {key: value for key, value in data.items() if key in allowed}
            if config.get("sort") not in (None, "hot", "date"):
                config.pop("sort", None)
            workers = config.get("download_workers")
            if not isinstance(workers, int) or not 1 <= workers <= MAX_DOWNLOAD_WORKERS:
                config.pop("download_workers", None)
            for key in allowed - {"download_workers"}:
                if key in config and not isinstance(config[key], str):
                    config.pop(key)
            return config, None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {}, f"配置文件无法读取，已使用默认值：{exc}"

    def build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(8, weight=1)

        ttk.Label(outer, text="账号 / 邮箱").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.email_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(outer, text="密码").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.password_var, show="•").grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(outer, text="设备指纹").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.fingerprint_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(outer, text="Idol ID").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.idol_id_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(outer, text="Idol 名称").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.idol_name_var).grid(
            row=4, column=1, sticky="ew", pady=4
        )
        sort_frame = ttk.Frame(outer)
        sort_frame.grid(row=4, column=2, sticky="e", padx=(12, 0))
        ttk.Label(sort_frame, text="排序").pack(side="left", padx=(0, 6))
        ttk.Combobox(
            sort_frame,
            textvariable=self.sort_var,
            values=("hot", "date"),
            state="readonly",
            width=8,
        ).pack(side="left")
        ttk.Label(sort_frame, text="并发").pack(side="left", padx=(12, 6))
        ttk.Spinbox(
            sort_frame,
            textvariable=self.workers_var,
            from_=1,
            to=MAX_DOWNLOAD_WORKERS,
            width=4,
        ).pack(side="left")

        ttk.Label(outer, text="保存目录").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.output_var).grid(
            row=5, column=1, sticky="ew", pady=4
        )
        ttk.Button(outer, text="选择…", command=self.choose_output).grid(
            row=5, column=2, sticky="e", padx=(12, 0), pady=4
        )

        hint = "已有 session_cookies.json 且含 auth_token 时可不填账号和密码。"
        ttk.Label(outer, text=hint, foreground="#666666").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(2, 10)
        )

        actions = ttk.Frame(outer)
        actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.start_button = ttk.Button(actions, text="开始下载", command=self.start_download)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(
            actions, text="取消", command=self.cancel_download, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=8)
        self.save_config_button = ttk.Button(
            actions, text="保存配置", command=self.save_config
        )
        self.save_config_button.pack(side="left")
        ttk.Label(actions, textvariable=self.status_var).pack(side="left", padx=8)

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=16, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(10, 0))

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(
            title="选择图片保存目录", initialdir=self.output_var.get()
        )
        if selected:
            self.output_var.set(selected)

    def append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def save_config(self) -> None:
        try:
            download_workers = int(self.workers_var.get())
        except ValueError:
            messagebox.showwarning("参数错误", "图片并发数必须是整数")
            return
        if not 1 <= download_workers <= MAX_DOWNLOAD_WORKERS:
            messagebox.showwarning(
                "参数错误", f"图片并发数必须在 1–{MAX_DOWNLOAD_WORKERS} 之间"
            )
            return

        config = {
            "email": self.email_var.get().strip(),
            "device_fingerprint": self.fingerprint_var.get().strip(),
            "idol_id": self.idol_id_var.get().strip(),
            "idol_name": self.idol_name_var.get().strip(),
            "sort": self.sort_var.get(),
            "download_workers": download_workers,
            "output_dir": self.output_var.get().strip(),
        }
        try:
            write_json_file(CONFIG_FILE, config)
        except OSError as exc:
            messagebox.showerror("保存失败", f"配置保存失败：{exc}")
            return
        self.append_log(f"配置已保存到 {CONFIG_FILE.name}（不包含密码）")
        messagebox.showinfo("保存配置", "配置已保存。为安全起见，密码不会保存。")

    def start_download(self) -> None:
        idol_id = self.idol_id_var.get().strip()
        output_text = self.output_var.get().strip()
        if not idol_id:
            messagebox.showwarning("缺少参数", "请填写 Idol ID")
            return
        if not output_text:
            messagebox.showwarning("缺少参数", "请选择保存目录")
            return
        try:
            download_workers = int(self.workers_var.get())
        except ValueError:
            messagebox.showwarning("参数错误", "图片并发数必须是整数")
            return
        if not 1 <= download_workers <= MAX_DOWNLOAD_WORKERS:
            messagebox.showwarning(
                "参数错误", f"图片并发数必须在 1–{MAX_DOWNLOAD_WORKERS} 之间"
            )
            return

        self.stop_event.clear()
        self.progress.configure(value=0, maximum=1)
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("正在准备……")

        settings = {
            "email": self.email_var.get().strip(),
            "password": self.password_var.get(),
            "device_fingerprint": self.fingerprint_var.get().strip(),
            "idol_id": idol_id,
            "idol_name": self.idol_name_var.get().strip(),
            "sort": self.sort_var.get(),
            "download_workers": download_workers,
            "output_dir": Path(output_text).expanduser(),
        }
        self.worker = threading.Thread(
            target=self.download_worker, args=(settings,), daemon=True
        )
        self.worker.start()

    def download_worker(self, settings: dict) -> None:
        try:
            downloader = KpoppingDownloader(
                **settings,
                stop_event=self.stop_event,
                log=lambda message: self.events.put(("log", message)),
                album_progress=lambda current, total: self.events.put(
                    ("progress", (current, total))
                ),
            )
            result = downloader.run()
            self.events.put(("done", result))
        except DownloadCancelled:
            self.events.put(("cancelled", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def cancel_download(self) -> None:
        self.stop_event.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("正在取消……")
        self.append_log("已请求取消，将在当前网络操作结束后停止")

    def poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.append_log(str(payload))
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    current, total = payload  # type: ignore[misc]
                    self.progress.configure(maximum=max(1, total), value=current)
                elif kind == "done":
                    result = payload
                    assert isinstance(result, DownloadResult)
                    self.finish_run()
                    summary = (
                        f"处理图集：{result.albums}\n"
                        f"历史记录跳过图集：{result.history_skipped_albums}\n"
                        f"新下载：{result.downloaded}\n"
                        f"已跳过：{result.skipped}\n"
                        f"失败：{result.failed}\n\n"
                        f"保存位置：{result.output_dir}"
                    )
                    self.status_var.set("下载完成")
                    self.append_log(summary.replace("\n", "；"))
                    messagebox.showinfo("下载完成", summary)
                elif kind == "cancelled":
                    self.finish_run()
                    self.status_var.set("已取消")
                    self.append_log("下载已取消")
                elif kind == "error":
                    self.finish_run()
                    self.status_var.set("运行失败")
                    self.append_log(f"运行失败：{payload}")
                    messagebox.showerror("运行失败", str(payload))
        except queue.Empty:
            pass
        finally:
            self.after(100, self.poll_events)

    def finish_run(self) -> None:
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.worker = None

    def on_close(self) -> None:
        self.stop_event.set()
        self.destroy()


def main() -> None:
    app = DownloaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
