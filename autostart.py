# -*- coding: utf-8 -*-
"""把程序注册成 Windows 定时任务（每天自动执行，不用一直开着窗口）。

用法（源码版）：
  python autostart.py install    注册/更新任务（时间取 config.yaml 的 send_time）
  python autostart.py remove     删除任务
  python autostart.py status     查看状态
  python autostart.py run        立刻触发一次（等于手动跑一轮，会真发）

打包成 exe 后，任务直接调用 exe --auto，不再依赖 .bat。

任务名带目录指纹：同一台机器上放两份程序也不会互相覆盖。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys

from common import ROOT, load_config

TASK_OLD = "DySpark_AutoSend"  # 旧版本固定用的名字，升级时清掉
_FP = hashlib.md5(str(ROOT).lower().encode("utf-8")).hexdigest()[:6]
TASK = f"DySpark_{_FP}"

_FROZEN = bool(getattr(sys, "frozen", False))


def _ps(cmd: str) -> tuple[int, str]:
    """跑一段 PowerShell，返回 (退出码, 输出)。"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _action_ps() -> str:
    """任务实际执行什么：exe 直接带 --auto，源码版走 auto_run.bat。"""
    if _FROZEN:
        return "New-ScheduledTaskAction -Execute $env:exe -Argument '--auto'"
    bat = ROOT / "auto_run.bat"
    if not bat.exists():
        raise FileNotFoundError(f"找不到 {bat}，无法注册定时任务")
    return f"New-ScheduledTaskAction -Execute '{bat}'"


def _drop_old_task() -> None:
    """删掉旧版固定名任务——只删"指向本目录"的，别误删别人的。"""
    root = str(ROOT).lower().replace("'", "''")
    _ps(
        f"""
$old = Get-ScheduledTask -TaskName '{TASK_OLD}' -ErrorAction SilentlyContinue
if ($old) {{
  $ex = ''
  if ($old.Actions -and $old.Actions[0].Execute) {{ $ex = $old.Actions[0].Execute.ToLower() }}
  if ($ex.Contains('{root}')) {{
    Unregister-ScheduledTask -TaskName '{TASK_OLD}' -Confirm:$false
    'REMOVED_OLD'
  }}
}}
"""
    )


def install() -> int:
    cfg = load_config()
    hhmm = str(cfg.get("send_time") or "21:30")
    if not cfg.get("friends"):
        print("[!] 名单还是空的，定时任务照样能装，但没东西可发。建议先跑一次设置向导。")

    try:
        action_ps = _action_ps()
    except FileNotFoundError as e:
        print(f"[X] {e}")
        return 1

    exe_line = f"$env:exe = '{sys.executable}'" if _FROZEN else ""

    script = f"""
$ErrorActionPreference = 'Stop'
{exe_line}
$action  = {action_ps}
$trigger = New-ScheduledTaskTrigger -Daily -At '{hhmm}'
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
        -MultipleInstances IgnoreNew
$prin = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK}' -Action $action -Trigger $trigger -Settings $set -Principal $prin -Force | Out-Null
Write-Output 'OK'
"""
    code, out = _ps(script)
    if "OK" not in out:
        print("[X] 注册失败：")
        print(out.strip()[:800])
        print("\n提示：如果提示权限不足，右键以管理员身份再运行一次。")
        return 1

    _drop_old_task()
    print(f"[OK] 定时任务已注册：{TASK}")
    print(f"     每天 {hhmm} 自动执行一轮（错过时间会在开机后补跑）")
    print("     不需要任何窗口开着，也不需要提前打开浏览器")
    return 0


def remove() -> int:
    _drop_old_task()
    _ps(
        f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false -ErrorAction SilentlyContinue; 'done'"
    )
    code2, out2 = _ps(
        f"if (Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue) {{ 'still' }} else {{ 'gone' }}"
    )
    if "gone" in out2:
        print(f"[OK] 已删除定时任务：{TASK}")
        return 0
    print("[X] 删除失败，可能需要管理员权限：")
    print((out2 or "").strip()[:600])
    return 1


def probe() -> dict:
    """轻量查询任务状态，给设置页用。不抛异常。"""
    code, out = _ps(
        f"""
$t = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue
if (-not $t) {{ 'NOT_INSTALLED'; exit }}
$i = Get-ScheduledTaskInfo -TaskName '{TASK}'
"STATE=" + $t.State
"NEXT=" + $i.NextRunTime
"LAST=" + $i.LastRunTime
"RESULT=" + $i.LastTaskResult
"""
    )
    if "NOT_INSTALLED" in out or "STATE=" not in out:
        return {"installed": False, "name": TASK}
    d = {"installed": True, "name": TASK, "state": ""}
    for line in out.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[{"STATE": "state", "NEXT": "next", "LAST": "last", "RESULT": "result"}.get(k.strip(), k.strip())] = v.strip()
    if d.get("state") == "Disabled":
        d["enabled"] = False
    else:
        d["enabled"] = True
    return d


def status() -> int:
    code, out = _ps(
        f"""
$t = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue
if (-not $t) {{ 'NOT_INSTALLED'; exit }}
$i = Get-ScheduledTaskInfo -TaskName '{TASK}'
"STATE=" + $t.State
"NEXT=" + $i.NextRunTime
"LAST=" + $i.LastRunTime
"RESULT=" + $i.LastTaskResult
"""
    )
    if "NOT_INSTALLED" in out or "STATE=" not in out:
        print("定时任务：未安装（设置向导里可以开启，或 python autostart.py install）")
        return 1
    print("定时任务状态：")
    for line in out.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            label = {
                "STATE": "  状态",
                "NEXT": "  下次执行",
                "LAST": "  上次执行",
                "RESULT": "  上次结果",
            }.get(k.strip(), k)
            print(f"{label}: {v.strip()}")
    print("  日志: logs/auto_run.log 和 logs/日期.log")
    return 0


def run_now() -> int:
    code, out = _ps(f"Start-ScheduledTask -TaskName '{TASK}'; Write-Output 'started'")
    if "started" not in out:
        print("[X] 触发失败：")
        print(out.strip()[:600])
        return 1
    print("[OK] 已触发一次，进度看 logs/auto_run.log")
    return 0


def main() -> None:
    action = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if action == "install":
        sys.exit(install())
    elif action == "remove":
        sys.exit(remove())
    elif action == "status":
        sys.exit(status())
    elif action == "run":
        sys.exit(run_now())
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
