# -*- coding: utf-8 -*-
"""公共模块：配置、日志、浏览器启动、登录检测、状态存取。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import yaml
from playwright.sync_api import BrowserContext, Locator, Page, sync_playwright


def _fix_console() -> None:
    """控制台编码。

    好友昵称里有 emoji（💩、🥕），Windows 控制台默认 GBK 编码遇到 emoji
    会直接抛 UnicodeEncodeError 把程序打断。这里统一切成 UTF-8 并容错。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


_fix_console()


def _app_root() -> Path:
    """程序所在目录。

    打包成 exe 后 __file__ 指向临时解包目录（每次运行都会变、退出即删），
    所以必须用 exe 自身的路径，否则配置、登录态、记录全都会丢。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_root() -> Path | None:
    """PyInstaller 解包目录：配置模板打在 exe 内部，不在 exe 旁边留文件。"""
    if getattr(sys, "frozen", False):
        p = getattr(sys, "_MEIPASS", "")
        return Path(p) if p else None
    return None


def _data_root() -> Path:
    """运行时数据放哪。

    exe 版统一写到 %LOCALAPPDATA%\\DySpark：把 exe 丢在桌面也不会在桌面
    拉出一堆 browser-profile / data / logs / screenshots。源码版保持原来的
    「程序目录」，避免动到本机已经配好的定时任务和登录态。
    """
    if not getattr(sys, "frozen", False):
        return ROOT
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "DySpark"


ROOT = _app_root()
BUNDLE = _bundle_root()
DATA_ROOT = _data_root()
PROFILE_DIR = DATA_ROOT / "browser-profile"
DATA_DIR = DATA_ROOT / "data"
LOG_DIR = DATA_ROOT / "logs"
SHOT_DIR = DATA_ROOT / "screenshots"
RUNTIME_DIR = DATA_ROOT / ".runtime"
CONFIG_PATH = DATA_ROOT / "config.yaml"

for _d in (DATA_ROOT, DATA_DIR, LOG_DIR, SHOT_DIR, RUNTIME_DIR):
    _d.mkdir(parents=True, exist_ok=True)

HOME = "https://www.douyin.com/"
CHAT_URL = "https://www.douyin.com/chat"
AFTER_LOGIN = "https://www.douyin.com/chat"

VERSION = "1.1.3"

# 模板万一没打进 exe 也能起得来（不依赖任何外部文件）
DEFAULT_CONFIG_YAML = """send_time: "21:30"
jitter_minutes: 12
gap_seconds: [2, 15]
weekdays: []
messages_per_friend: 1
skip_if_lit: true
friends: []
messages: ["续", "火", "续火", "火火火", "续续续"]
close_messages: ["续命来了", "火不能断，这是原则问题", "来，喂一口火"]
screenshot_on_error: true
"""


def _template(name: str) -> Path | None:
    for base in (BUNDLE, ROOT):
        if base and (base / name).exists():
            return base / name
    return None


def _migrate_legacy() -> None:
    """从「数据放在 exe 旁边」的旧版搬过来，省得重新扫码登录。"""
    if not getattr(sys, "frozen", False) or (RUNTIME_DIR / "migrated").exists():
        return
    try:
        old = ROOT / "config.yaml"
        if old.exists() and not CONFIG_PATH.exists():
            shutil.copy2(old, CONFIG_PATH)
    except Exception:
        pass
    for name in ("browser-profile", "data"):
        try:
            src, dst = ROOT / name, DATA_ROOT / name
            if src.is_dir() and not dst.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)
        except Exception:
            pass  # 浏览器正开着时文件会锁，下次启动再试
    try:
        (RUNTIME_DIR / "migrated").write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
        )
    except Exception:
        pass


_migrate_legacy()


# ---------- 配置 ----------
def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> dict:
    """读 config.yaml；没有就用 exe 内的模板生成一份。"""
    if not CONFIG_PATH.exists():
        tpl = _template("config.example.yaml")
        try:
            if tpl:
                shutil.copyfile(tpl, CONFIG_PATH)
            else:
                CONFIG_PATH.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        except Exception:
            pass
    return _load_yaml(CONFIG_PATH)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def load_selectors() -> dict:
    tpl = _template("selectors.yaml")
    return _load_yaml(tpl) if tpl else {}


def is_initialized() -> bool:
    """是否已经能正常用：只要名单里有就行。

    不能用"向导跑过的标记文件"来判断——老版本用户配好了名单却没有这个标记，
    会被当成第一次使用硬拉进向导。
    """
    return bool(load_config().get("friends"))


def mark_initialized() -> None:
    try:
        (RUNTIME_DIR / "initialized").write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
        )
    except Exception:
        pass


# ---------- 日志 ----------
def get_logger(name: str = "spark") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        fh = logging.FileHandler(LOG_DIR / f"{date.today():%Y-%m-%d}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


# ---------- 浏览器 ----------
# 常驻浏览器：用调试端口拉起一个 Edge，之后每次运行只是"连接"它，
# 不再重复启动/关闭，省掉每次十几秒的开销。浏览器会一直开着（最小化即可）。
#
# 端口不写死 9222：同一台机器上可能装了两份程序、或者端口被别的软件占了。
# 每个目录用 .runtime/cdp_port 记住自己的端口，互不干扰。
PORT_FILE = RUNTIME_DIR / "cdp_port"
_PORT_SCAN = range(9222, 9300)

_cdp_port: int | None = None
_VIA_CDP = False  # 本次连接是否复用了常驻浏览器（收尾时据此决定不杀进程）


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _cdp_alive(port: int) -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        return True
    except Exception:
        return False


def _resolve_port(refresh: bool = False) -> int:
    """确定本目录使用的调试端口。

    优先级：本目录记过的端口 → 扫一个既没被占用、也不是别人调试端口的空位。
    refresh=True 丢掉记录重选（端口被别人抢走时用）。
    """
    global _cdp_port
    if _cdp_port and not refresh:
        return _cdp_port
    if not refresh and PORT_FILE.exists():
        try:
            p = int(PORT_FILE.read_text(encoding="utf-8").strip())
            if 1024 < p < 65535:
                _cdp_port = p
                return p
        except Exception:
            pass

    # 没有记录：先看 9222 上是不是已经有能连的浏览器（旧版本留下的常驻窗口，
    # 或同机另一份程序）。能连就复用——硬抢一个新端口会和它争同一个 profile 目录。
    if _cdp_alive(9222):
        _cdp_port = 9222
        try:
            PORT_FILE.write_text("9222", encoding="utf-8")
        except Exception:
            pass
        return 9222

    for p in _PORT_SCAN:
        if _port_in_use(p):
            continue  # 别的程序（或另一份 dy-spark）占着，跳过
        _cdp_port = p
        try:
            PORT_FILE.write_text(str(p), encoding="utf-8")
        except Exception:
            pass
        return p
    raise RuntimeError("从 9222 往后找不到可用端口，先关掉一些程序再试")


def cdp_url() -> str:
    return f"http://127.0.0.1:{_resolve_port()}"


def _edge_exe() -> str:
    """找 Edge：注册表 → 常见安装位置 → PATH。装在哪个盘都能找到。"""
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
                )
                try:
                    val = winreg.QueryValue(key, None)
                finally:
                    winreg.CloseKey(key)
                if val and Path(val).exists():
                    return val
            except OSError:
                continue
    except Exception:
        pass

    for cand in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge Beta\Application\msedge.exe",
    ):
        if Path(cand).exists():
            return cand

    exe = shutil.which("msedge") or shutil.which("msedge.exe")
    if exe:
        return exe

    raise RuntimeError(
        "没找到 Microsoft Edge。\n"
        "这个程序靠系统自带的 Edge 工作，请先安装 Edge：https://www.microsoft.com/edge"
    )


def _spawn_browser() -> int:
    """拉起常驻 Edge（带调试端口），返回实际使用的端口。"""
    import subprocess

    port = _resolve_port()
    subprocess.Popen(
        [
            _edge_exe(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1440,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if _cdp_alive(port):
            return port
        time.sleep(0.5)
    # 端口被抢或 Edge 起不来：丢掉记录，下次重选
    try:
        PORT_FILE.unlink()
    except Exception:
        pass
    raise RuntimeError(
        "浏览器 20 秒内没就绪。\n"
        "多半是上次的 Edge 窗口还占着 browser-profile 目录，把弹出来的 Edge 窗口全部关掉再运行。"
    )


def launch(headless: bool = False):
    """连接常驻 Edge；没有就先拉起再连接。返回 (playwright, context)。

    注意：CDP 复用模式下 headless 无效（浏览器本来就开着）。
    结束时请用 shutdown()，不要直接 ctx.close()，否则会把常驻浏览器关掉。
    """
    global _VIA_CDP, _cdp_port
    p = sync_playwright().start()
    port = _resolve_port()
    if not _cdp_alive(port):
        port = _spawn_browser()

    try:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception:
        # 记录失效（端口被抢/浏览器异常退出）：重选一次再连
        _cdp_port = None
        try:
            PORT_FILE.unlink()
        except Exception:
            pass
        port = _spawn_browser()
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")

    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    _VIA_CDP = True
    ctx.set_default_timeout(30000)
    ctx.set_default_navigation_timeout(30000)
    try:
        # 抹掉最基础的自动化指纹（对之后新开的页面生效）
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception:
        pass
    return p, ctx


def shutdown(p=None, ctx: BrowserContext | None = None) -> None:
    """收尾：复用模式下只断开连接、浏览器留着；否则正常关闭。"""
    global _VIA_CDP
    if ctx is not None and not _VIA_CDP:
        try:
            ctx.close()
        except Exception:
            pass
    if p is not None:
        try:
            p.stop()
        except Exception:
            pass
    _VIA_CDP = False


def first_page(ctx: BrowserContext) -> Page:
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def is_logged_in(ctx: BrowserContext) -> bool:
    try:
        names = {c.get("name", "") for c in ctx.cookies(HOME)}
    except Exception:
        return False
    return bool(names & {"sessionid", "sessionid_ss", "sid_tt"})


# ---------- 元素操作 ----------
def find_visible(page: Page, candidates: list[str], timeout: int = 2500) -> Locator | None:
    """按候选列表依次尝试，返回第一个可见的定位器。"""
    for sel in candidates or []:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def find_visible_any(scope: Page | Locator, candidates: list[str], timeout: int = 2000) -> Locator | None:
    for sel in candidates or []:
        try:
            loc = scope.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


# ---------- 当日记录 ----------
def record_path(d: date | None = None) -> Path:
    d = d or date.today()
    return DATA_DIR / f"{d:%Y-%m-%d}.json"


def load_record(d: date | None = None) -> dict:
    p = record_path(d)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"date": f"{(d or date.today()):%Y-%m-%d}", "results": []}


def save_record(rec: dict, d: date | None = None) -> None:
    with open(record_path(d), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)


def shot(page: Page, name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
    from datetime import datetime

    path = SHOT_DIR / f"{datetime.now():%H%M%S}_{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass
    return path
