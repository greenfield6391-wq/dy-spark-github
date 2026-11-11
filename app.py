# -*- coding: utf-8 -*-
"""统一入口。双击直接打开网页操作台；黑框菜单收进 --menu。

命令行参数：
  app.py               打开网页操作台（日常就用这个）
  app.py --menu        打开老的黑框菜单（调试用）
  app.py --auto        静默跑一轮（给 Windows 定时任务用）
  app.py --run         立即真发一轮
  app.py --dry         演练：填字不发送
  app.py --wizard      同无参数，打开网页操作台
  app.py --only 张三,李四   只发这几个人
  app.py --status      看今天的发送记录
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import common
from common import (
    LOG_DIR,
    ROOT,
    VERSION,
    is_initialized,
    load_config,
    load_record,
    mark_initialized,
)

LINE = "=" * 54


def _desktop_dir() -> str:
    home = os.environ.get("USERPROFILE") or str(Path.home())
    one = os.path.join(home, "OneDrive", "Desktop")
    return one if os.path.isdir(one) else os.path.join(home, "Desktop")


def _ensure_shortcut() -> None:
    """exe 版首次运行在桌面建一个带图标的快捷方式（幂等，只建一次）。"""
    if not getattr(sys, "frozen", False) or (common.RUNTIME_DIR / "shortcut").exists():
        return
    try:
        exe = str(Path(sys.executable).resolve())
        lnk = os.path.join(_desktop_dir(), "续火花.lnk")
        q = lambda s: s.replace("'", "''")
        ps = (
            "$ws = New-Object -ComObject WScript.Shell;"
            f"$sc = $ws.CreateShortcut('{q(lnk)}');"
            f"$sc.TargetPath = '{q(exe)}';"
            f"$sc.WorkingDirectory = '{q(str(Path(exe).parent))}';"
            f"$sc.IconLocation = '{q(exe)},0';"
            "$sc.Description = '抖音续火花';"
            "$sc.Save();"
        )
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            capture_output=True, timeout=15, creationflags=0x08000000,
        )
        (common.RUNTIME_DIR / "shortcut").write_text(lnk, encoding="utf-8")
    except Exception:
        pass


def _quiet_console() -> None:
    """网页操作台是主界面，黑框只当后台进程，最小化并把标题写清楚。"""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("续火花 · 后台服务（关掉它网页就打不开了）")
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    except Exception:
        pass


def _today_summary() -> None:
    rec = load_record(date.today())
    rows = rec.get("results") or []
    real = [r for r in rows if not r.get("dry")]
    ok_rows = [r for r in real if r.get("ok")]
    ok_names = {r["name"] for r in ok_rows}
    # 同一个人可能先失败后补发成功，所以失败名单要扣掉已经成功的人
    bad_names = sorted({r["name"] for r in real if not r.get("ok")} - ok_names)
    cfg = load_config()
    total = len(cfg.get("friends") or [])
    print()
    print(f"  今天（{date.today()}）名单共 {total} 人")
    print(f"  已发出：{len(ok_names)} 人")
    if bad_names:
        print(f"  还没成功：{len(bad_names)} 人 —— {'、'.join(bad_names)}")
        print("  失败现场截图在 screenshots/ 目录")
    if ok_rows:
        print("\n  发送明细（最近 15 条）：")
        for r in ok_rows[-15:]:
            s = r.get("streak") or ""
            tail = f"（火花 {s}）" if str(s).isdigit() else ""
            print(f"    {r.get('time','')}  {r.get('name','')} {tail}")
        if len(ok_rows) > 15:
            print(f"    … 共 {len(ok_rows)} 条，完整记录见 data/{date.today()}.json")
    if not rows:
        print("  今天还没有发送记录。")
    print()


def _send(dry: bool, only: list[str] | None) -> int:
    from core import run_once

    try:
        r = run_once(dry=dry, only=only)
    except Exception as e:
        print(f"\n[!] 运行出错：{e}")
        print("    详细日志在 logs/ 目录里。")
        return 1
    if r.get("error") == "not_logged_in":
        print("\n[!] 抖音登录失效了。重新打开设置向导扫码登录就行。")
        return 2
    if r.get("error") == "chat_page_fail":
        print("\n[!] 私信页打不开，可能是网络或抖音改版。看 logs/ 里的截图。")
        return 2
    return 0


def _menu() -> int:
    while True:
        cfg = load_config()
        n = len(cfg.get("friends") or [])
        print()
        print(LINE)
        print(f"  抖音续火花  v{VERSION}    名单 {n} 人")
        print(LINE)
        print("  [1] 立即发送一轮（真的发）")
        print("  [2] 演练一次（只填字，不发送）")
        print("  [3] 设置 / 改名单（打开浏览器设置页）")
        print("  [4] 看今天的发送情况")
        print("  [5] 定时任务：查看状态")
        print("  [6] 定时任务：安装 / 更新（按设置页里的时间）")
        print("  [7] 定时任务：删除")
        print("  [0] 退出")
        print()
        try:
            ch = input("  请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if ch == "0":
            return 0
        elif ch == "1":
            _send(False, None)
        elif ch == "2":
            _send(True, None)
        elif ch == "3":
            _wizard()
        elif ch == "4":
            _today_summary()
        elif ch == "5":
            _task("status")
        elif ch == "6":
            _task("install")
        elif ch == "7":
            _task("remove")
        else:
            print("  输入 0-7 之间的数字。")


def _task(action: str) -> None:
    try:
        import autostart

        if action == "status":
            autostart.status()
        elif action == "install":
            if autostart.install() == 0:
                print("  改过发送时间后，重跑一次这个就能生效。")
        elif action == "remove":
            autostart.remove()
    except Exception as e:
        print(f"  操作失败：{e}")


def _wizard() -> int:
    import wizard

    return wizard.main(force=True)


def main() -> int:
    args = sys.argv[1:]

    def opt(name: str) -> bool:
        return name in args

    def val(name: str) -> str | None:
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return None

    if opt("--menu"):
        return _menu()
    if opt("--status"):
        _today_summary()
        return 0
    if opt("--wizard"):
        return _wizard()
    if opt("--install-task"):
        import autostart

        return autostart.install()
    if opt("--remove-task"):
        import autostart

        return autostart.remove()
    if opt("--auto"):
        return _auto_run()

    only = None
    o = val("--only")
    if o:
        only = [x.strip() for x in o.replace("，", ",").split(",") if x.strip()]

    if opt("--dry"):
        return _send(True, only)
    if opt("--run") or only:
        return _send(False, only)

    # 双击运行：直接拉起网页操作台（菜单已挪到 --menu）
    if not opt("--no-shortcut"):
        _ensure_shortcut()
    print(LINE)
    print(f"  抖音续火花 v{VERSION} · 正在打开操作台")
    print("  这个黑框是后台服务，别关；网页关了不影响定时任务。")
    print(f"  数据目录：{common.DATA_ROOT}")
    print(LINE)
    _quiet_console()
    return _wizard()


def _auto_run() -> int:
    """定时任务专用路径：星期过滤 + 随机延迟 + 静默跑一轮。

    - weekdays 配了且今天不在里面：什么都不做直接退出（日志里留一行）；
    - jitter_minutes 抖动：用日期做种，同一天内多次触发延迟一致，避免崩溃重跑时二次抖动；
    - 火花按自然日算，凌晨补跑（错过 21:30、半夜才开机）算新的一天，照常发没问题；
    - 重复触发不用担心：任务设了 MultipleInstances IgnoreNew，
      且 run_once 自带「今天已发跳过 + 彩火跳过」双保险。
    """
    import random
    from datetime import date as _date

    cfg = load_config()
    weekdays = cfg.get("weekdays") or []
    if weekdays and _date.today().weekday() not in weekdays:
        print(f"今天星期{_date.today().weekday() + 1}，按设置不发送")
        return 0

    jitter = int(cfg.get("jitter_minutes") or 0)
    if jitter > 0:
        # 日期做种：同一天重试/补跑得到同一个延迟，跨天才有区别
        d = _date.today().isoformat()
        delay = random.Random(f"{d}-spark").randint(0, jitter * 60)
        print(f"随机等待 {delay // 60} 分 {delay % 60} 秒后开始（避免天天同一秒发送）")
        time.sleep(delay)

    return _send(False, None)


if __name__ == "__main__":
    raise SystemExit(main())
