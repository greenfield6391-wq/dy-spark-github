# -*- coding: utf-8 -*-
"""首次使用的设置向导：在本地开一个网页，扫码登录 → 勾选好友 → 设置时间。

设计要点：
- 用标准库 http.server，不引入任何新依赖；
- 多线程 HTTPServer：操作台每 1.5 秒轮询状态，不能被「扫描会话列表」这种
  长请求堵住；Playwright 同步接口不能并发，浏览器相关接口用一把锁串行；
- 会话列表是虚拟滚动，逐屏翻完并去重，拿到昵称 + 火花天数；
- 名单只写用户自己勾的人，包里不带任何预设好友。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import (
    CHAT_URL,
    DATA_ROOT,
    LOG_DIR,
    ROOT,
    VERSION,
    first_page,
    get_logger,
    is_initialized,
    is_logged_in,
    launch,
    load_config,
    load_record,
    mark_initialized,
    save_config,
    shutdown,
)

log = get_logger("wizard")

ITEM = "[data-e2e='conversation-item']"
LIST = ".conversationConversationListwrapper"

# 一次拿到昵称 + 火花天数 + 是群聊还是单聊。
# 群聊判据（2026-10 实测）：单聊项里有 commonIMAvatar 头像容器，
# 群聊项里是 commonConversationIcon。群聊既不算火花，发消息还会打扰一群人，必须排除。
JS_ROWS = """(sel) => [...document.querySelectorAll(sel)].map(el => {
    const t = el.querySelector('.conversationConversationItemtitle');
    const s = el.querySelector('.commonStreaknormalText');
    const name = t ? (t.textContent || '').trim() : '';
    if (!name) return null;
    const isGroup = !!el.querySelector("[class*='commonConversationIcon']");
    const isSingle = !!el.querySelector("[class*='commonIMAvatar']");
    return {
        name: name,
        streak: s ? (s.textContent || '').trim() : '',
        kind: isGroup ? 'group' : (isSingle ? 'single' : 'unknown')
    };
}).filter(Boolean)"""

PRESETS = {
    "normal": ["续", "火", "续火", "火火火", "续续续",
               "人机又来续火花了", "今日份火花已充值，不用回"],
    "short": ["续", "火", "续火", "火火火", "续续续"],
}

# 熟人组话术：只对名单里标了 group=close 的人用，其他人用 normal。
# 不配置分组时自动忽略，行为和以前完全一样。
CLOSE_PRESETS = ["续命来了", "火不能断，这是原则问题", "今天你也没理我，行",
                 "来，喂一口火", "兄弟，这火比我绩点稳"]


def do_task(action: str = "install") -> dict:
    """设置页里的定时任务开关：install / remove / status。"""
    try:
        import autostart
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if action == "remove":
                rc = autostart.remove()
            elif action == "install":
                rc = autostart.install()
            else:
                rc = 0
        out = buf.getvalue().strip()
        d = autostart.probe()
        d["ok"] = rc == 0
        if action != "status" and out:
            d["msg"] = out
        return d
    except Exception as e:
        return {"ok": False, "installed": False, "error": str(e)}


_S = {
    "p": None,
    "ctx": None,
    "friends": [],
    "scanned": False,
    "quit": False,
}


# ---------- 浏览器侧动作 ----------
def _drop_browser():
    """丢掉已经断开的连接对象。只停本进程的 Playwright，不动常驻 Edge 窗口。"""
    try:
        if _S["p"] is not None:
            _S["p"].stop()
    except Exception:
        pass
    _S["p"] = None
    _S["ctx"] = None


def _ensure_browser():
    """拿到能用的浏览器连接。

    用户扫码后常常顺手把弹出来的 Edge 窗口关掉，这时缓存的 ctx 已经死了，
    再 goto 就会抛 TargetClosedError: Playwright connection closed（设置页会
    卡在「登录成功」却退回第 1 步）。所以每次用之前先做一次 CDP 往返探活，
    断了就重连 / 重新拉起浏览器，登录态还在 browser-profile 里，不用重扫。
    """
    if _S["ctx"] is not None:
        try:
            _S["ctx"].cookies()  # 一次轻量 CDP 调用，断线必抛异常
        except Exception:
            log.info("浏览器连接已断开，正在重新连接…")
            _drop_browser()
    if _S["ctx"] is None:
        _S["p"], _S["ctx"] = launch()
        # 向导里点开会话看火花，别让页面在其他标签页里错位
        try:
            first_page(_S["ctx"])
        except Exception:
            pass
    return _S["ctx"]


def _open_chat():
    """打开私信页；连接刚好在 goto 那一瞬断掉时，重建后再试一次。"""
    for attempt in (0, 1):
        try:
            pg = first_page(_ensure_browser())
            pg.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            return pg
        except Exception as e:
            log.warning(f"打开私信页失败（第 {attempt + 1} 次）：{e}")
            _drop_browser()
    raise RuntimeError("浏览器连不上，请关掉这个黑框再双击一次程序")


def login_start() -> dict:
    try:
        pg = _open_chat()
        return {"ok": True, "logged_in": is_logged_in(pg.context)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def login_poll() -> dict:
    if _S["ctx"] is None:
        return {"logged_in": False, "started": False}
    try:
        return {"logged_in": is_logged_in(_ensure_browser()), "started": True}
    except Exception as e:
        return {"logged_in": False, "started": True, "error": str(e)}


def scan_friends(max_pages: int = 240) -> dict:
    """翻完整个会话列表，返回 [{name, streak}]。"""
    try:
        pg = _open_chat()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        pg.locator(ITEM).first.wait_for(state="visible", timeout=30000)
    except Exception:
        return {"ok": False, "error": "私信页没打开，可能登录失效或者网络卡住了"}
    pg.wait_for_timeout(2500)

    # 回到顶部再开始，避免停在中间漏掉前半截
    try:
        pg.evaluate(
            "sel => { const el = document.querySelector(sel); if (el) el.scrollTop = 0; }", LIST
        )
        pg.wait_for_timeout(600)
    except Exception:
        pass

    order: list[str] = []
    info: dict[str, dict] = {}
    for _ in range(max_pages):
        try:
            rows = pg.evaluate(JS_ROWS, ITEM)
        except Exception:
            rows = []
        for r in rows:
            n = (r.get("name") or "").strip()
            if not n or n in info:
                continue
            info[n] = {"streak": (r.get("streak") or "").strip(), "kind": r.get("kind") or "unknown"}
            order.append(n)
        try:
            moved = float(
                pg.evaluate(
                    """sel => {
                        const el = document.querySelector(sel);
                        if (!el) return -1;
                        const before = el.scrollTop;
                        el.scrollTop = before + el.clientHeight * 0.8;
                        return el.scrollTop - before;
                    }""",
                    LIST,
                )
            )
        except Exception:
            moved = -1
        if moved <= 1:
            break
        pg.wait_for_timeout(450)

    friends = [
        {"name": n, "streak": info[n]["streak"], "kind": info[n]["kind"]} for n in order
    ]
    groups = sum(1 for f in friends if f["kind"] == "group")
    log.info(f"扫完会话列表：{len(friends)} 个会话，其中群聊 {groups} 个")
    _S["friends"] = friends
    _S["scanned"] = True
    return {"ok": True, "friends": friends, "count": len(friends), "groups": groups}


def do_save(payload: dict) -> dict:
    kind_of = {f["name"]: f.get("kind") for f in (_S["friends"] or [])}
    # friends 支持两种形式：["名字", ...] 或 [{name, group}, ...]
    raw: list = []
    groups: dict[str, str] = {}
    for item in payload.get("friends") or []:
        if isinstance(item, dict):
            n = str(item.get("name") or "").strip()
            if n:
                raw.append(n)
                if item.get("group") == "close":
                    groups[n] = "close"
        else:
            n = str(item or "").strip()
            if n:
                raw.append(n)
    names = [n for n in raw if kind_of.get(n) != "group"]
    dropped = len(raw) - len(names)
    if dropped:
        log.info(f"已剔除 {dropped} 个群聊会话（群聊不计火花）")
    if not names:
        return {"ok": False, "error": "至少勾选一个好友"}

    send_time = str(payload.get("send_time") or "21:30")
    try:
        hh, mm = send_time.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
    except Exception:
        return {"ok": False, "error": "发送时间格式不对，应该是 21:30 这种"}

    gap = payload.get("gap_seconds") or [2, 15]
    try:
        gap_lo = max(1, min(int(gap[0]), 15))
        gap_hi = max(gap_lo, min(int(gap[1]), 15))
    except Exception:
        gap_lo, gap_hi = 2, 15

    # 新前端直接提交点选的句子列表；老前端/兼容路径还传 style + custom_messages
    msgs = [str(m).strip() for m in (payload.get("messages") or []) if str(m).strip()]
    if not msgs:
        style = payload.get("style") or "normal"
        if style == "custom":
            msgs = [m.strip() for m in str(payload.get("custom_messages") or "").splitlines() if m.strip()]
        msgs = msgs or list(PRESETS["normal"])
    close_msgs = [str(m).strip() for m in (payload.get("close_messages") or []) if str(m).strip()]
    if not close_msgs:
        close_msgs = list(CLOSE_PRESETS)

    cfg = load_config()
    cfg["send_time"] = send_time
    cfg["gap_seconds"] = [gap_lo, gap_hi]
    cfg["messages"] = msgs
    cfg["close_messages"] = close_msgs
    cfg.setdefault("jitter_minutes", 12)
    cfg.setdefault("weekdays", [])
    cfg.setdefault("messages_per_friend", 1)
    cfg.setdefault("screenshot_on_error", True)
    cfg["friends"] = [{"name": n, "group": groups.get(n, "normal")} for n in names]
    save_config(cfg)
    mark_initialized()

    schedule = bool(payload.get("schedule", True))
    result = {"ok": True, "count": len(names), "send_time": send_time, "task": "",
              "schedule": schedule}
    try:
        import autostart

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if schedule:
                rc = autostart.install()
            else:
                # 勾掉自动运行 = 删掉定时任务，比留着禁用状态干净
                rc = autostart.remove()
        result["task"] = buf.getvalue().strip()
        result["task_ok"] = rc == 0
        result["task_name"] = autostart.TASK
    except Exception as e:
        result["task"] = f"更新定时任务失败：{e}"
        result["task_ok"] = False
    return result


def _style_of(msgs: list[str]) -> str:
    """反查话术风格：和预设一模一样就认成预设，否则算自定义。"""
    for k, v in PRESETS.items():
        if list(msgs) == list(v):
            return k
    return "custom" if msgs else "normal"


def config_snapshot() -> dict:
    """把上次保存的配置吐给设置页，用来回填。

    没有这个接口时，设置页每次都是硬编码的 21:30 / 2-15 秒 / 日常口语，
    用户只想改个名字，时间和话术也被一起打回默认值。
    """
    cfg = load_config()
    friends = []
    close_set = set()
    for f in cfg.get("friends") or []:
        if isinstance(f, dict) and f.get("name"):
            friends.append(str(f["name"]))
            if f.get("group") == "close":
                close_set.add(str(f["name"]))
    gap = cfg.get("gap_seconds") or [2, 15]
    try:
        gap = [int(gap[0]), int(gap[1])]
    except Exception:
        gap = [2, 15]
    msgs = [str(m) for m in (cfg.get("messages") or []) if str(m).strip()]

    # 今天已经发过谁（含跳过的）：让设置页一眼看出今天的进度
    sent: list[str] = []
    try:
        from datetime import date as _date

        from common import load_record

        rec = load_record(_date.today())
        ok_names = {r["name"] for r in rec.get("results") or [] if r.get("ok") and not r.get("dry")}
        sent = sorted(ok_names)
    except Exception:
        pass

    return {
        "friends": friends,
        "close_friends": sorted(close_set),
        "send_time": str(cfg.get("send_time") or "21:30"),
        "gap_seconds": gap,
        "messages": msgs,
        "close_messages": [str(m) for m in (cfg.get("close_messages") or []) if str(m).strip()],
        "style": _style_of(msgs),
        "weekdays": cfg.get("weekdays") or [],
        "initialized": bool(friends),
        "today_sent": sent,
    }


# ---------- 操作台 ----------
_RUN = {"proc": None, "log": None, "mode": "", "started": 0.0}


def console_state() -> dict:
    """操作台首屏：今天发了谁、跳过谁、定时什么状态。"""
    rec = load_record(date.today())
    rows = [r for r in (rec.get("results") or []) if not r.get("dry")]
    sent = [r for r in rows if r.get("ok") and not r.get("skipped_lit")]
    skipped = [r for r in rows if r.get("skipped_lit")]
    failed = [r for r in rows if not r.get("ok")]
    ok_names = {r["name"] for r in sent}
    failed = [r for r in failed if r.get("name") not in ok_names]
    cfg = load_config()
    return {
        "ok": True,
        "version": VERSION,
        "date": f"{date.today():%Y-%m-%d}",
        "initialized": bool(cfg.get("friends")),
        "total": len(cfg.get("friends") or []),
        "send_time": str(cfg.get("send_time") or "21:30"),
        "sent": len(ok_names),
        "skipped": len(skipped),
        "failed": len(failed),
        "rows": [
            {
                "name": r.get("name", ""),
                "time": r.get("time", ""),
                "state": "跳过" if r.get("skipped_lit") else ("已发" if r.get("ok") else "失败"),
                "text": "、".join(r.get("text") or []),
            }
            for r in rows
        ],
        "task": do_task("status"),
        "data_dir": str(DATA_ROOT),
        "running": bool(_RUN["proc"] and _RUN["proc"].poll() is None),
    }


def start_run(dry: bool) -> dict:
    """把发送放到独立进程，网页这边只轮询日志。

    HTTPServer 是单线程的， synchronous Playwright 又不能跨线程，
    所以绝不能在请求里直接跑 run_once —— 会把整个页面卡死。
    """
    p = _RUN["proc"]
    if p and p.poll() is None:
        return {"ok": False, "error": "已经有一轮在跑，等它结束再点"}
    logf = LOG_DIR / "console_run.log"
    try:
        fh = open(logf, "w", encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"打不开日志文件：{e}"}
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, str(ROOT / "app.py")]
    cmd.append("--dry" if dry else "--run")
    kw = {"stdout": fh, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
          "cwd": str(ROOT)}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(cmd, **kw)
    except Exception as e:
        fh.close()
        return {"ok": False, "error": str(e)}
    _RUN.update(proc=proc, log=logf, mode="演练" if dry else "发送", started=time.time())
    return {"ok": True, "mode": _RUN["mode"]}


def run_status() -> dict:
    p = _RUN["proc"]
    running = bool(p and p.poll() is None)
    lines = []
    if _RUN["log"] and _RUN["log"].exists():
        try:
            lines = _RUN["log"].read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
        except Exception:
            pass
    return {"running": running, "rc": (p.poll() if p else None),
            "mode": _RUN["mode"], "lines": lines}


def set_send_time(t: str) -> dict:
    """只改时间，不动名单；任务开着就顺手把触发时刻同步过去。"""
    try:
        h, m = str(t).strip().split(":")
        h, m = int(h), int(m)
        assert 0 <= h <= 23 and 0 <= m <= 59
        t = f"{h:02d}:{m:02d}"
    except Exception:
        return {"ok": False, "error": "时间格式不对，应像 21:30"}
    cfg = load_config()
    cfg["send_time"] = t
    save_config(cfg)
    state = do_task("status")
    if state.get("enabled"):
        state = do_task("install")
    return {"ok": True, "send_time": t, "task": state}


def open_data_dir() -> dict:
    try:
        subprocess.Popen(["explorer", str(DATA_ROOT)])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- HTTP ----------
# 多线程：操作台每 1.5 秒轮询一次状态，绝不能被设置页那次 20 秒的
# 「扫描会话列表」堵住。Playwright 的同步接口不能并发，所以浏览器相关
# 的三个接口单独用一把锁串起来，其余轻量接口各走各的线程。
_BROWSER_LOCK = threading.Lock()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 服务是单线程的，绝不能让浏览器把连接挂着（keep-alive）——
        # 那样整个服务会卡死等这条连接的下一个请求。每个请求处理完立即断开。
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/favicon.ico"):
            self.send_response(204)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            return
        if self.path in ("/", "/index.html"):
            self._html(CONSOLE)
        elif self.path.startswith("/setup"):
            self._html(PAGE)
        elif self.path.startswith("/api/console"):
            self._send(console_state())
        elif self.path.startswith("/api/run_status"):
            self._send(run_status())
        elif self.path.startswith("/api/login_poll"):
            try:
                with _BROWSER_LOCK:
                    self._send(login_poll())
            except Exception as e:
                self._send({"logged_in": False, "error": str(e)})
        elif self.path.startswith("/api/state"):
            self._send({"initialized": is_initialized(), "scanned": _S["scanned"]})
        elif self.path.startswith("/api/config"):
            self._send(config_snapshot())
        elif self.path.startswith("/api/task"):
            self._send(do_task("status"))
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}

        try:
            if self.path == "/api/run":
                self._send(start_run(bool(data.get("dry"))))
            elif self.path == "/api/open_data":
                self._send(open_data_dir())
            elif self.path == "/api/time":
                self._send(set_send_time(str(data.get("time") or "")))
            elif self.path in ("/api/login_start", "/api/scan"):
                with _BROWSER_LOCK:
                    self._send(login_start() if self.path == "/api/login_start" else scan_friends())
            elif self.path == "/api/save":
                self._send(do_save(data))
            elif self.path == "/api/task":
                self._send(do_task(data.get("action") or "install"))
            elif self.path == "/api/quit":
                self._send({"ok": True})
                threading.Thread(target=_delayed_exit, daemon=True).start()
            else:
                self._send({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            log.exception("接口出错")
            self._send({"ok": False, "error": f"{e.__class__.__name__}: {e}"})


def _delayed_exit():
    time.sleep(0.6)
    _S["quit"] = True
    shutdown(_S["p"], _S["ctx"])
    threading.Thread(target=lambda: (_ for _ in ()).throw(SystemExit), daemon=True).start()
    import os

    os._exit(0)


def _pick_http_port() -> int:
    import socket

    for p in range(8765, 8865):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return 8765


def main(force: bool = False, open_browser: bool = True) -> int:
    port = _pick_http_port()
    url = f"http://127.0.0.1:{port}/"
    srv = _Server(("127.0.0.1", port), Handler)

    print("=" * 52)
    print(f"  续火花 v{VERSION} · 网页操作台")
    print("=" * 52)
    print(f"  操作台：{url}")
    print(f"  设置页：{url}setup")
    print("  浏览器没自动弹出就把上面地址粘进去。")
    print(f"  数据目录：{DATA_ROOT}")
    print("  这个黑框是后台服务，关掉它网页就打不开了。")
    print("=" * 52)

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        shutdown(_S["p"], _S["ctx"])
    return 0


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>续火花 · 设置向导</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #f7f7f5; color: #2c2c2a;
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 14px; line-height: 1.6;
  }
  .wrap { max-width: 760px; margin: 0 auto; padding: 40px 20px 64px; }
  h1 { font-size: 21px; font-weight: 600; margin: 0 0 6px; }
  .sub { color: #6b6a66; font-size: 13px; margin-bottom: 26px; }
  .steps { display: flex; gap: 8px; margin-bottom: 22px; }
  .step { flex: 1; padding: 9px 12px; border-radius: 8px; background: #fff;
          border: 1px solid #e6e5e1; font-size: 13px; color: #8a8984; }
  .step.active { border-color: #534AB7; color: #534AB7; background: #f2f1fd; font-weight: 600; }
  .step.done { border-color: #cfe6d9; color: #1d7a52; background: #f1f9f5; }
  .card { background: #fff; border: 1px solid #e6e5e1; border-radius: 12px; padding: 24px; }
  h2 { font-size: 15px; font-weight: 600; margin: 0 0 8px; }
  p.hint { color: #6b6a66; font-size: 13px; margin: 0 0 16px; }
  button {
    font: inherit; cursor: pointer; border-radius: 8px; padding: 9px 18px;
    border: 1px solid #d6d5d0; background: #fff; color: #2c2c2a;
  }
  button:hover { border-color: #bab9b3; }
  button.primary { background: #534AB7; border-color: #534AB7; color: #fff; font-weight: 600; }
  button.primary:hover { background: #4740a0; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 16px;
         padding: 13px 0; border-bottom: 1px solid #f0efeb; }
  .row:last-child { border-bottom: 0; }
  .row .k { color: #55544f; }
  input[type=time], input[type=number], textarea {
    font: inherit; padding: 7px 10px; border: 1px solid #d6d5d0; border-radius: 8px;
    background: #fff; color: #2c2c2a;
  }
  input[type=number] { width: 74px; }
  textarea { width: 100%; min-height: 110px; resize: vertical; }
  .people { max-height: 420px; overflow-y: auto; border: 1px solid #e6e5e1;
            border-radius: 10px; margin: 4px 0 16px; }
  .p { display: flex; align-items: center; gap: 11px; padding: 10px 14px;
       border-bottom: 1px solid #f4f3ef; }
  .p:last-child { border-bottom: 0; }
  .p:hover { background: #faf9f6; }
  .p input { width: 16px; height: 16px; accent-color: #534AB7; }
  .p .n { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .p .s { color: #8a8984; font-size: 12px; white-space: nowrap; }
  .p.group { background: #fafaf8; }
  .p.group .n, .p.group .s { color: #b0afa9; }
  .p.group input { cursor: not-allowed; }
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
  .toolbar input[type=search] { flex: 1; min-width: 160px; font: inherit; padding: 7px 11px;
    border: 1px solid #d6d5d0; border-radius: 8px; }
  .muted { color: #8a8984; font-size: 12px; }
  .errbar { display: flex; align-items: center; gap: 12px; margin: 0 0 14px;
            padding: 10px 14px; border: 1px solid #f0cfc8; border-radius: 10px;
            background: #fff7f5; color: #a33d2c; font-size: 13px; }
  .errbar span { flex: 1; }
  .errbar button { font: inherit; padding: 5px 12px; border: 1px solid #d9a99f;
                   border-radius: 8px; background: #fff; color: #a33d2c; cursor: pointer; }
  .p.missing { background: #fdfaf3; }
  .p.missing .s { color: #a8842c; }
  .p .tag { font-size: 11px; color: #a8842c; border: 1px solid #ecdcb8;
            background: #fdfaf3; border-radius: 6px; padding: 1px 6px; margin-left: 6px; }
  .p .cb { flex: none; font-size: 11px; padding: 2px 8px; border-radius: 6px;
           border: 1px solid #d6d5d0; background: #fff; color: #8a8984; cursor: pointer; }
  .p .cb.on { border-color: #534AB7; background: #ece9fb; color: #534AB7; font-weight: 600; }
  .row.col { flex-direction: column; align-items: stretch; gap: 10px; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip { font-size: 13px; padding: 6px 13px; border-radius: 16px; cursor: pointer;
          border: 1px solid #d6d5d0; background: #fff; color: #55544f; user-select: none; }
  .chip.on { border-color: #534AB7; background: #ece9fb; color: #534AB7; font-weight: 600; }
  .chip .x { margin-left: 5px; color: #b0afa9; font-weight: 400; }
  .chip.custom .x:hover { color: #b23b3b; }
  .chipsAdd input { width: 100%; font: inherit; padding: 7px 11px;
                    border: 1px dashed #d6d5d0; border-radius: 8px; background: #fcfcfa; }
  .acts { display: flex; gap: 10px; align-items: center; margin-top: 20px; }
  .mask { position: fixed; inset: 0; background: rgba(247,247,245,.92);
          display: flex; align-items: center; justify-content: center; flex-direction: column;
          gap: 16px; z-index: 99; }
  .spin { width: 30px; height: 30px; border: 3px solid #ded9f7; border-top-color: #534AB7;
          border-radius: 50%; animation: r .8s linear infinite; }
  @keyframes r { to { transform: rotate(360deg); } }
  .hidden { display: none !important; }
  .ok { color: #1d7a52; }
  .bad { color: #b23b3b; }
  .big { font-size: 15px; margin: 6px 0 2px; }
  code { background: #f2f1ec; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
  .note { background: #fbf9f2; border: 1px solid #efe6cf; border-radius: 8px;
          padding: 11px 14px; font-size: 13px; color: #6b5c34; margin-top: 16px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>续火花 · 设置向导</h1>
  <div class="sub">给抖音好友每天自动发一条私信，维持聊天火花。整个过程都在你自己电脑上完成。</div>

  <div class="steps">
    <div class="step active" id="s1i">1 · 登录抖音</div>
    <div class="step" id="s2i">2 · 选好友</div>
    <div class="step" id="s3i">3 · 定时间</div>
  </div>

  <div class="card" id="step1">
    <h2>先登录你的抖音账号</h2>
    <p class="hint">点下面的按钮会弹出一个 Edge 窗口，用手机抖音扫码登录就行。登录状态只保存在这台电脑的 <code>browser-profile</code> 文件夹里，不会上传到任何地方。</p>
    <p class="hint" id="reuseTip" style="display:none;color:#1d7a52">这台电脑已经登录过，正在直接读取上次的名单，不用重新扫码。</p>
    <div class="acts">
      <button class="primary" id="btnLogin" onclick="startLogin()">打开登录窗口</button>
      <span class="muted" id="loginState"></span>
    </div>
    <div class="note">设置页和抖音窗口是两个不同的窗口：这个页面用来勾选和填时间，那个 Edge 窗口是抖音本体。两个都别关，设置完会自动收尾。</div>
  </div>

  <div class="card hidden" id="step2">
    <h2>选择要续火花的好友</h2>
    <p class="hint">下面是你的抖音会话列表（读的是你自己的账号）。勾上谁，程序每天就给谁发。火花只对已经聊过的人有意义，没聊过的人不会出现在这里。</p>
    <div class="errbar hidden" id="scanErr">
      <span id="scanErrText"></span>
      <button onclick="scanPeople(true)">重试读取</button>
    </div>
    <div class="toolbar">
      <input type="search" id="q" placeholder="搜索昵称" oninput="renderPeople()">
      <button onclick="checkAll(true)">全选</button>
      <button onclick="checkAll(false)">全不选</button>
      <span class="muted" id="cnt">已选 0 人</span>
    </div>
    <div class="toolbar">
      <span class="muted" style="flex:none">排序</span>
      <select id="sortBy" onchange="renderPeople()"
        style="font:inherit;padding:6px 10px;border:1px solid #d6d5d0;border-radius:8px;background:#fff">
        <option value="list">按会话列表顺序（推荐）</option>
        <option value="fire_desc">火花天数 从多到少</option>
        <option value="fire_asc">火花天数 从少到多</option>
        <option value="sent_last">今天没发的排前面</option>
        <option value="name">按昵称</option>
      </select>
      <span class="muted">点人名右边的「熟」按钮 = 铁哥们，发话术会不一样</span>
    </div>
    <div class="people" id="people"></div>
    <div class="acts">
      <button class="primary" onclick="goStep3()">下一步</button>
    </div>
  </div>

  <div class="card hidden" id="step3">
    <h2>定发送时间和内容</h2>
    <p class="hint">时间指每天自动执行的时间。火花的规则是自然日，太晚容易跨零点，建议 20:00–22:00。</p>
    <div class="row">
      <span class="k">每天几点发</span>
      <input type="time" id="tTime" value="21:30">
    </div>
    <div class="row">
      <span class="k">每两个人之间等多久（秒）</span>
      <span><input type="number" id="tLo" value="2" min="1" max="15"> – <input type="number" id="tHi" value="15" min="1" max="15"></span>
    </div>
    <div class="row col">
      <span class="k">发什么内容（点选要用的句子，可多选）</span>
      <div class="chips" id="msgChips"></div>
      <div class="chipsAdd">
        <input id="msgNew" placeholder="想加一句自己的？输了按回车" onkeydown="if(event.key==='Enter'){addChip('msg',this)}">
      </div>
    </div>
    <div class="row col" id="closeRow">
      <span class="k">熟人话术（名单里点过「熟」的人发这些）</span>
      <div class="chips" id="closeChips"></div>
      <div class="chipsAdd">
        <input id="closeNew" placeholder="加一句，回车确认" onkeydown="if(event.key==='Enter'){addChip('close',this)}">
      </div>
    </div>
    <div class="row">
      <span class="k">每天自动运行（电脑睡眠错过会补跑）</span>
      <input type="checkbox" id="tSchedule" checked style="width:18px;height:18px;accent-color:#534AB7">
    </div>
    <div class="row">
      <span class="k">定时任务状态</span>
      <span style="text-align:right">
        <span id="taskState" class="muted">查询中…</span><br>
        <button id="btnTask" onclick="toggleTask()" style="margin-top:6px;font-size:12px;padding:5px 10px">…</button>
      </span>
    </div>
    <div class="acts">
      <button class="primary" id="btnSave" onclick="saveAll()">完成设置</button>
      <button onclick="backStep2()">上一步</button>
    </div>
  </div>

  <div class="card hidden" id="stepDone">
    <h2 class="ok">设置完成</h2>
    <p class="big" id="doneMsg"></p>
    <p class="hint" id="doneTask"></p>
    <div class="acts">
      <button class="primary" onclick="location.href='/'">返回操作台</button>
    </div>
    <div class="note">以后想改名单或时间，重新打开程序选「设置」就行，不用再走一遍全部流程。</div>
  </div>
</div>

<div class="mask hidden" id="mask">
  <div class="spin"></div>
  <div id="maskMsg">处理中…</div>
</div>

<script>
let PEOPLE = [];
let CHECKED = new Set();
let SAVED = [];      // 上次保存的名单
let SCANNED = false; // 本次是否真的扫过会话列表
let INFO = '';       // 计数栏的补充说明
let CLOSE = new Set();   // 熟人组名单
let SENT = new Set();    // 今天已经发过的（含被跳过的）
let TASK = null;         // 定时任务状态 {installed, enabled, next...}

function fireNum(s) {
  const m = String(s || '').match(/\d+/);
  return m ? parseInt(m[0], 10) : -1;
}

function $(id) { return document.getElementById(id); }
function mask(on, msg) {
  $('mask').classList.toggle('hidden', !on);
  if (msg) $('maskMsg').textContent = msg;
}
function setStep(n) {
  ['step1','step2','step3','stepDone'].forEach((id,i) => {
    $(id).classList.toggle('hidden', i !== n-1);
  });
  ['s1i','s2i','s3i'].forEach((id,i) => {
    const el = $(id);
    el.classList.toggle('active', i === n-1);
    el.classList.toggle('done', i < n-1);
  });
  window.scrollTo(0,0);
}

// 打开设置页先干活：读上次的配置回填表单，名单非空就直接进第 2 步。
// 以前不管有没有设置过都停在"请登录"，还得手动点一次按钮才能往下走。
async function boot() {
  let c = {};
  try {
    c = await (await fetch('/api/config')).json();
  } catch (e) {}
  SAVED = c.friends || [];
  CLOSE = new Set(c.close_friends || []);
  SENT = new Set(c.today_sent || []);
  refreshTask();

  if (c.send_time) $('tTime').value = c.send_time;
  if (c.gap_seconds && c.gap_seconds.length === 2) {
    $('tLo').value = c.gap_seconds[0];
    $('tHi').value = c.gap_seconds[1];
  }
  initMsgs(c.messages || [], c.close_messages || []);

  if (!SAVED.length) { await startLogin(); return; }

  // 有名单：先用上次的名单把界面铺满，不让人对着空白等十几秒
  PEOPLE = SAVED.map(n => ({name: n, streak: '', kind: '', saved: true}));
  CHECKED = new Set(SAVED);
  setStep(2);
  renderPeople();
  $('cnt').textContent = '已选 ' + CHECKED.size + ' 人 · 正在读取火花天数…';
  $('reuseTip').style.display = 'block';
  await refreshLogin();
}

// 已登录就直接扫，扫不动（失效）才退回第 1 步让人扫码
async function refreshLogin() {
  try {
    const r = await (await fetch('/api/login_start', {method:'POST'})).json();
    if (r.logged_in) { await scanPeople(true); return; }
    // 浏览器连不上 ≠ 登录失效，别把人踢回去重新扫码
    if (r.ok === false) { showScanErr(r.error || '打开浏览器失败'); setStep(2); return; }
  } catch (e) {}
  setStep(1);
  $('loginState').textContent = '登录已失效，请重新扫码';
  $('loginState').className = '';
  $('btnLogin').disabled = false;
}

async function startLogin() {
  $('btnLogin').disabled = true;
  $('loginState').textContent = '正在打开登录窗口…';
  try {
    const r = await (await fetch('/api/login_start', {method:'POST'})).json();
    if (r.ok === false) {
      $('loginState').textContent = r.error || '打开浏览器失败';
      $('btnLogin').disabled = false;
      return;
    }
    if (r.logged_in) { afterLogin(); return; }
    $('loginState').textContent = '等待扫码…请在弹出来的窗口里用抖音扫码';
    pollLogin(0);
  } catch (e) {
    $('loginState').textContent = '打开失败：' + e;
    $('btnLogin').disabled = false;
  }
}

async function pollLogin(n) {
  if (n > 90) {
    $('loginState').textContent = '等了 3 分钟还没登录成功，点按钮再试一次。';
    $('btnLogin').disabled = false;
    return;
  }
  try {
    const r = await (await fetch('/api/login_poll')).json();
    if (r.logged_in) { afterLogin(); return; }
    if (r.error) {
      $('loginState').textContent = '浏览器连接异常：' + r.error + '，点按钮重试';
      $('btnLogin').disabled = false;
      return;
    }
  } catch (e) {}
  setTimeout(() => pollLogin(n+1), 2000);
}

function afterLogin() {
  $('loginState').textContent = '登录成功';
  $('loginState').className = 'ok';
  setStep(2);
  scanPeople(false);
}

async function scanPeople(keep) {
  mask(true, '正在读取你的会话列表和火花天数，可能要十几秒…');
  try {
    const r = await (await fetch('/api/scan', {method:'POST'})).json();
    mask(false);
    // 读失败不要把人踢回登录页反复扫码：留在选人页，给一条能点重试的提示。
    // 名单已经存在的话，勾选项和保存按钮都照常可用。
    if (!r.ok) { showScanErr(r.error || '读取失败'); return; }
    $('scanErr').classList.add('hidden');
    SCANNED = true;
    const scanned = r.friends || [];
    const have = new Set(scanned.map(p => p.name));
    // 上次设过、这次列表里没扫到的人（改过备注、被抖音藏起来）不能悄悄丢掉
    const missing = (SAVED || []).filter(n => !have.has(n))
                                  .map(n => ({name: n, streak: '', kind: '', missing: true}));
    PEOPLE = [...scanned, ...missing];
    // 保留用户刚才的勾选；首次进来则按上次保存的名单来
    if (!keep) CHECKED = new Set(SAVED);
    PEOPLE.forEach(p => { if (p.kind === 'group') CHECKED.delete(p.name); });
    INFO = ' / 共 ' + scanned.length + ' 个会话'
         + (r.groups ? '（' + r.groups + ' 个群聊已排除）' : '')
         + (missing.length ? '，' + missing.length + ' 人本次没在列表里（已保留）' : '');
    renderPeople();
  } catch (e) {
    mask(false);
    showScanErr('读取失败：' + e);
  }
}

function showScanErr(msg) {
  $('scanErrText').textContent = msg;
  $('scanErr').classList.remove('hidden');
}

function sortList(list) {
  const by = $('sortBy').value;
  if (by === 'fire_desc') return [...list].sort((a,b) => fireNum(b.streak) - fireNum(a.streak));
  if (by === 'fire_asc')  return [...list].sort((a,b) => fireNum(a.streak) - fireNum(b.streak));
  if (by === 'name')      return [...list].sort((a,b) => a.name.localeCompare(b.name, 'zh'));
  if (by === 'sent_last') return [...list].sort((a,b) =>
    (SENT.has(a.name) ? 1 : 0) - (SENT.has(b.name) ? 1 : 0));
  return list; // list：保持扫描到的会话顺序
}

function renderPeople() {
  const q = ($('q').value || '').trim().toLowerCase();
  const box = $('people');
  const list = sortList(PEOPLE.filter(p => !q || p.name.toLowerCase().includes(q)));
  box.innerHTML = list.map(p => {
    const isGroup = p.kind === 'group';
    const on = (!isGroup && CHECKED.has(p.name)) ? 'checked' : '';
    const enc = encodeURIComponent(p.name);
    const today = (!isGroup && SENT.has(p.name)) ? '<span class="tag" style="color:#1d7a52;border-color:#cfe8dc;background:#f0f9f4">今天已发</span>' : '';
    const missTag = p.missing ? '<span class="tag">上次设过</span>' : '';
    const closeBtn = isGroup ? '' :
      `<button class="cb${CLOSE.has(p.name) ? ' on' : ''}" title="铁哥们：用熟人话术" onclick="toggleClose(decodeURIComponent('${enc}'))">熟</button>`;
    let tail = isGroup ? '群聊 · 不适用' : (p.missing ? '本次没找到' : (p.streak || ''));
    return `<label class="${'p' + (isGroup ? ' group' : '') + (p.missing ? ' missing' : '')}">
      <input type="checkbox" ${on} ${isGroup ? 'disabled' : ''} onchange="toggle(decodeURIComponent('${enc}'), this.checked)">
      <span class="n">${escapeHtml(p.name)}${today}${missTag}</span>
      ${closeBtn}
      <span class="s">${escapeHtml(tail)}</span></label>`;
  }).join('') || '<div class="p muted">没有匹配的人</div>';
  const doneN = [...CHECKED].filter(n => SENT.has(n)).length;
  $('cnt').textContent = '已选 ' + CHECKED.size + ' 人' + (doneN ? '（今天已发 ' + doneN + '）' : '') + INFO;
}

function toggleClose(name) {
  if (CLOSE.has(name)) CLOSE.delete(name); else CLOSE.add(name);
  renderPeople();
}

// ===== 第 3 步：话术点选 =====
// 话术池 = 建议句（常驻展示，可点选）+ 用户自加的句子（可删）。
// 上次保存时用着的句子自动点亮；建议句即使没选也留着，方便反悔。
const SUGGEST = ['续','火','续火','火火火','续续续','人机又来续火花了','今日份火花已充值，不用回'];
const SUGGEST_CLOSE = ['续命来了','火不能断，这是原则问题','今天你也没理我，行','来，喂一口火','兄弟，这火比我绩点稳'];
let MSG = { on: new Set(), custom: new Set() };
let CMSG = { on: new Set(), custom: new Set() };

function initMsgs(messages, closeMessages) {
  MSG = { on: new Set(messages), custom: new Set(messages.filter(m => !SUGGEST.includes(m))) };
  CMSG = { on: new Set(closeMessages.length ? closeMessages : SUGGEST_CLOSE),
           custom: new Set(closeMessages.filter(m => !SUGGEST_CLOSE.includes(m))) };
  renderChips('msg'); renderChips('close');
}
function renderChips(which) {
  const st = which === 'msg' ? MSG : CMSG;
  const sugg = which === 'msg' ? SUGGEST : SUGGEST_CLOSE;
  const all = [...new Set([...sugg, ...st.custom, ...st.on])];
  $(which === 'msg' ? 'msgChips' : 'closeChips').innerHTML =
    all.map(m => chipHtml(m, st, which)).join('');
}
function chipHtml(m, st, which) {
  const on = st.on.has(m) ? ' on' : '';
  const isCustom = st.custom.has(m) ? ' custom' : '';
  const q = JSON.stringify(m).replace(/"/g, '&quot;');
  const x = st.custom.has(m) ? `<span class="x" onclick="event.stopPropagation();delChip('${which}', ${q})">✕</span>` : '';
  return `<span class="chip${on}${isCustom}" onclick="toggleChip('${which}', ${q})">${escapeHtml(m)}${x}</span>`;
}
function toggleChip(which, m) {
  const st = which === 'msg' ? MSG : CMSG;
  if (st.on.has(m)) st.on.delete(m); else st.on.add(m);
  renderChips(which);
}
function delChip(which, m) {
  const st = which === 'msg' ? MSG : CMSG;
  st.on.delete(m); st.custom.delete(m);
  renderChips(which);
}
function addChip(which, input) {
  const m = input.value.trim();
  if (!m) return;
  const st = which === 'msg' ? MSG : CMSG;
  st.on.add(m); st.custom.add(m);
  input.value = '';
  renderChips(which);
}

// 定时任务状态行：随时开/关，不用重新走一遍设置
async function refreshTask() {
  try {
    const r = await (await fetch('/api/task')).json();
    TASK = r;
    renderTask();
  } catch (e) { $('taskState').textContent = '查询失败'; }
}
function renderTask() {
  if (!TASK) return;
  const s = $('taskState'), b = $('btnTask');
  if (!TASK.installed) {
    s.textContent = '未开启（只手动发送）'; s.className = 'muted';
    b.textContent = '开启定时'; b.className = '';
  } else if (TASK.enabled === false) {
    s.textContent = '已关闭（被禁用）'; s.className = 'muted';
    b.textContent = '重新开启';
  } else {
    s.textContent = `已开启 · 下次 ${TASK.next || '—'}`; s.className = 'ok';
    b.textContent = '关掉定时';
  }
}
async function toggleTask() {
  if (!TASK) return;
  const wantOn = !TASK.installed || TASK.enabled === false;
  b = $('btnTask'); b.disabled = true;
  try {
    const r = await (await fetch('/api/task', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action: wantOn ? 'install' : 'remove'})})).json();
    TASK = r; renderTask();
    $('tSchedule').checked = wantOn;
  } catch (e) { alert('操作失败：' + e); }
  b.disabled = false;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function toggle(name, on) {
  if (on) CHECKED.add(name); else CHECKED.delete(name);
  $('cnt').textContent = '已选 ' + CHECKED.size + ' 人';
}

function checkAll(on) {
  const q = ($('q').value || '').trim().toLowerCase();
  PEOPLE.filter(p => p.kind !== 'group' && (!q || p.name.toLowerCase().includes(q)))
        .forEach(p => on ? CHECKED.add(p.name) : CHECKED.delete(p.name));
  renderPeople();
}

function goStep3() {
  if (CHECKED.size === 0) { alert('至少勾选一个人'); return; }
  setStep(3);
}
function backStep2() { setStep(2); }

async function saveAll() {
  const msgs = [...MSG.on];
  const cmsgs = [...CMSG.on];
  if (!msgs.length) { alert('至少选一句要发的话'); return; }
  const body = {
    friends: [...CHECKED].map(n => ({name: n, group: CLOSE.has(n) ? 'close' : 'normal'})),
    send_time: $('tTime').value || '21:30',
    gap_seconds: [parseInt($('tLo').value)||2, parseInt($('tHi').value)||15],
    messages: msgs,
    close_messages: cmsgs,
    schedule: $('tSchedule').checked
  };
  $('btnSave').disabled = true;
  mask(true, '正在保存并更新定时任务…');
  try {
    const r = await (await fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)})).json();
    mask(false);
    if (!r.ok) { alert(r.error || '保存失败'); $('btnSave').disabled = false; return; }
    setStep(4);
    $('doneMsg').textContent = `已设置 ${r.count} 位好友` +
      (r.schedule ? `，每天 ${r.send_time} 自动发送。` : '。定时任务已关闭，只手动发送。');
    $('doneTask').textContent = r.task ? r.task : '';
    refreshTask();
  } catch (e) {
    mask(false);
    $('btnSave').disabled = false;
    alert('保存失败：' + e);
  }
}

async function quit() {
  await fetch('/api/quit', {method:'POST'});
  document.body.innerHTML = '<div style="padding:60px;text-align:center;color:#6b6a66">已关闭，这个页面可以关掉了。</div>';
}

boot();
</script>
</body>
</html>
"""


CONSOLE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>续火花 · 操作台</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f7f6f3;color:#2c2c2a;font:14px/1.6 "Microsoft YaHei","Segoe UI",sans-serif;padding:28px 20px 60px}
.wrap{max-width:880px;margin:0 auto}
header{display:flex;align-items:baseline;gap:12px}
h1{font-size:20px;font-weight:500;margin:0}
.ver{color:#888780;font-size:12px}
.sub{color:#888780;font-size:12px;margin:2px 0 22px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
.card{background:#fff;border:1px solid #e6e4de;border-radius:12px;padding:18px 20px;margin-bottom:16px}
h2{font-size:14px;font-weight:500;margin:0 0 14px;color:#3c3489}
.nums{display:flex;gap:24px;margin-bottom:12px}
.num b{display:block;font-size:26px;font-weight:500;line-height:1.2}
.num span{font-size:12px;color:#888780}
.row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-top:1px solid #f1efe8;font-size:13px}
.row span:last-child{color:#888780;white-space:nowrap}
.tag{font-size:11px;padding:1px 7px;border-radius:6px;border:1px solid #ddd}
.ok{color:#0f6e56;background:#e1f5ee;border-color:#c9eadb}
.skip{color:#854f0b;background:#faeeda;border-color:#f0d9a8}
.bad{color:#a32d2d;background:#fcebeb;border-color:#f7c1c1}
button{font:inherit;padding:8px 15px;border:1px solid #d6d5d0;background:#fff;border-radius:8px;cursor:pointer}
button:hover{border-color:#534ab7;color:#534ab7}
button.primary{background:#534ab7;color:#fff;border-color:#534ab7}
button.primary:hover{background:#3c3489;color:#fff}
button:disabled{opacity:.45;cursor:not-allowed}
.acts{display:flex;gap:10px;flex-wrap:wrap}
.hint{font-size:12px;color:#888780;margin-top:10px}
pre{background:#f7f6f3;border:1px solid #e6e4de;border-radius:8px;padding:12px;max-height:190px;overflow:auto;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;margin:12px 0 0}
.line{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid #f1efe8;flex-wrap:wrap}
.line:last-child{border:0}
input[type=time]{font:inherit;padding:6px 9px;border:1px solid #d6d5d0;border-radius:8px;background:#fff}
a{color:#534ab7;text-decoration:none;font-size:13px}
.foot{margin-top:18px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.on{color:#0f6e56}.off{color:#a32d2d}
.hidden{display:none}
</style>
</head>
<body>
<div class="wrap">
  <header><h1>续火花</h1><span class="ver" id="ver"></span></header>
  <div class="sub" id="sub"></div>

  <div id="firstUse" class="card hidden">
    <h2>第一次用，先设置一下</h2>
    <p>扫码登录 → 勾选要续火花的好友 → 定时间，两分钟就好。</p>
    <a href="/setup"><button class="primary">开始设置</button></a>
  </div>

  <div id="dash" class="hidden">
    <div class="grid">
      <div class="card">
        <h2>今日情况</h2>
        <div class="nums">
          <div class="num"><b id="nSent">–</b><span>已发</span></div>
          <div class="num"><b id="nSkip">–</b><span>已续跳过</span></div>
          <div class="num"><b id="nFail">–</b><span>失败</span></div>
          <div class="num"><b id="nTotal">–</b><span>名单</span></div>
        </div>
        <div id="rows"></div>
        <div class="hint" id="todayHint"></div>
      </div>
      <div class="card">
        <h2>日常操作</h2>
        <div class="acts">
          <button class="primary" id="btnSend" onclick="runJob(false)">立即发送</button>
          <button id="btnDry" onclick="runJob(true)">演练（不发送）</button>
          <button onclick="loadState()">刷新</button>
        </div>
        <div class="hint">演练只填字不回车；发送会真的发出去。跑起来后按钮会锁住。</div>
        <pre id="log" class="hidden"></pre>
      </div>
    </div>
    <div class="card">
      <h2>定时</h2>
      <div class="line"><span id="taskState">读取中…</span><span><button id="btnTask" onclick="toggleTask()">…</button></span></div>
      <div class="line"><span>每天发送时间</span><span><input type="time" id="tTime"><button onclick="saveTime()">保存并生效</button></span></div>
      <div class="hint">到点后随机往后延 0–12 分钟；睡眠错过会补跑一次。不用开着这个网页。</div>
    </div>
  </div>

  <div class="foot">
    <a href="/setup">改名单 / 话术 / 分组</a>
    <a href="#" onclick="openData();return false">打开数据文件夹</a>
    <a href="#" onclick="quit();return false">退出程序</a>
    <span class="hint" id="dataDir"></span>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let BUSY = false, TASK = null, TIME_DIRTY = false;

async function api(path, body) {
  const o = body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {};
  return await (await fetch(path, o)).json();
}
function esc(s) { return String(s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

async function loadState() {
  try {
    const c = await api('/api/console');
    $('ver').textContent = 'v' + c.version;
    $('sub').textContent = c.date + ' · 数据都在本机，不上传';
    $('dataDir').textContent = c.data_dir;
    $('firstUse').classList.toggle('hidden', c.initialized);
    $('dash').classList.toggle('hidden', !c.initialized);
    if (!c.initialized) return;
    $('nSent').textContent = c.sent;
    $('nSkip').textContent = c.skipped;
    $('nFail').textContent = c.failed;
    $('nTotal').textContent = c.total;
    $('rows').innerHTML = c.rows.length
      ? c.rows.slice().reverse().slice(0, 30).map(r => {
          const cls = r.state === '已发' ? 'ok' : (r.state === '跳过' ? 'skip' : 'bad');
          return '<div class="row"><span><span class="tag ' + cls + '">' + esc(r.state) + '</span> ' + esc(r.name) + '</span><span>' + esc(r.time) + '</span></div>';
        }).join('')
      : '<div class="hint">今天还没有记录。</div>';
    $('todayHint').textContent = c.skipped ? '彩火（今天已续过）的人自动跳过，不管是谁续的。' : '';
    if (!TIME_DIRTY) $('tTime').value = c.send_time;
    TASK = c.task || {};
    renderTask();
    if (c.running) setBusy(true);
  } catch (e) { $('sub').textContent = '后台没响应：' + e; }
}

function renderTask() {
  const on = TASK && TASK.installed && TASK.enabled !== false;
  $('taskState').innerHTML = on
    ? '<span class="on">已开启</span>　下次 ' + esc(TASK.next || '未知')
    : '<span class="off">已关闭</span>　不会自动发送';
  $('btnTask').textContent = on ? '关掉定时' : '开启定时';
}
async function toggleTask() {
  const on = TASK && TASK.installed && TASK.enabled !== false;
  $('btnTask').disabled = true;
  TASK = await api('/api/task', {action: on ? 'remove' : 'install'});
  $('btnTask').disabled = false;
  renderTask();
}
async function saveTime() {
  TIME_DIRTY = true;
  const r = await api('/api/time', {time: $('tTime').value});
  if (!r.ok) { alert(r.error); return; }
  if (r.task) { TASK = r.task; renderTask(); }
  $('todayHint').textContent = '发送时间已保存';
}

async function runJob(dry) {
  if (BUSY) return;
  if (!dry && !confirm('真的给名单里还没续的人各发一条？')) return;
  $('log').classList.remove('hidden');
  $('log').textContent = '正在启动…';
  const r = await api('/api/run', {dry: dry});
  if (!r.ok) { alert(r.error); return; }
  setBusy(true);
  poll();
}
function setBusy(b) {
  BUSY = b;
  $('btnSend').disabled = b;
  $('btnDry').disabled = b;
}
async function poll() {
  try {
    const r = await api('/api/run_status');
    if (r.lines && r.lines.length) {
      $('log').classList.remove('hidden');
      $('log').textContent = r.lines.join('\n');
      $('log').scrollTop = $('log').scrollHeight;
    }
    if (BUSY && !r.running) { setBusy(false); loadState(); }
    else setBusy(r.running);
  } catch (e) {}
}
async function openData() { await api('/api/open_data', {}); }
async function quit() {
  if (!confirm('退出后台？网页会关掉，正在跑的一轮也会被中断。')) return;
  await api('/api/quit', {});
  document.body.innerHTML = '<div style="padding:60px;text-align:center;color:#6b6a66">已退出，这个页面可以关掉了。</div>';
}
$('tTime').addEventListener('change', () => { TIME_DIRTY = true; });
loadState();
setInterval(poll, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import sys

    raise SystemExit(
        main(force="--force" in sys.argv, open_browser="--no-browser" not in sys.argv)
    )
