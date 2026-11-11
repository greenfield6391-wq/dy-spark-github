# -*- coding: utf-8 -*-
"""核心逻辑：在抖音独立私信页(www.douyin.com/chat)按名单逐个发一句话。

流程（2026-10 实测页面结构）：
  1. 直接打开 /chat，等会话列表渲染
  2. 在列表里按昵称找到会话（列表是虚拟滚动的，找不到就往下滚）
  3. 点开会话 -> 等输入框出现 -> 读火花天数
  4. 输入消息 -> 回车 -> 验证输入框被清空
  5. 记录结果（含火花天数），下一个人
"""
from __future__ import annotations

import random
import re
import time
import unicodedata
from datetime import date

from playwright.sync_api import Locator, Page

from common import CHAT_URL, first_page, get_logger, is_logged_in, launch, load_config, load_record, save_record, shot, shutdown

log = get_logger("send")

ITEM = "[data-e2e='conversation-item']"
TITLE = ".conversationConversationItemtitle"
STREAK = ".commonStreaknormalText"
EDITOR = "div[contenteditable='true']"
# 会话列表真正的滚动容器（虚拟滚动，scrollHeight 9000+、可视 840）
LIST = ".conversationConversationListwrapper"
# 点开会话后右侧聊天窗的头部：昵称和火花天数都在这里（2026-10 实测）
HEADER_TITLE = ".RightPanelHeadertitle"
HEADER_BOX = ".RightPanelHeaderinfoContainer"

# 本轮里"读不到聊天窗标题"的次数，用来判断是不是页面改版了
_header_misses = 0


def pick_message(pool: list[str], used: list[str]) -> str:
    if not pool:
        return "在吗"
    cands = [m for m in pool if m not in used]
    return random.choice(cands or pool)


def editor_text(page: Page) -> str:
    try:
        ed = page.locator(EDITOR).first
        if not ed.count():
            return ""
        return (ed.inner_text() or "").replace("\u200b", "").strip()
    except Exception:
        return ""


def norm(s: str) -> str:
    """昵称归一化：页面里的空格可能是不间断空格(\xa0)、全角字符，配置里是普通空格。"""
    s = unicodedata.normalize("NFKC", s or "")
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        s = s.replace(ch, "")
    return re.sub(r"\s+", "", s).lower()


def reset_list_scroll(page: Page) -> bool:
    """把会话列表滚回顶部，并确认真的回到了顶部。

    点开会话后列表会按“最近活跃”重排，浏览器有时会忽略一次 scrollTop=0，
    所以这里滚动后校验，最多重试 3 次。
    """
    for _ in range(3):
        try:
            top = page.evaluate(
                "sel => { const el = document.querySelector(sel); return el ? Math.round(el.scrollTop) : -1; }",
                LIST,
            )
        except Exception:
            top = -1
        if top == 0:
            return True
        list_scroll(page, top=0)
        try:
            page.wait_for_timeout(450)
        except Exception:
            pass
    return False


def list_scroll(page: Page, top: int | None = None, delta_ratio: float = 0.0) -> float:
    """直接操作会话列表容器的 scrollTop。

    列表是虚拟滚动（scrollHeight 9000+，可视 840，一次只渲染 17 项），
    用鼠标滚轮不可靠——滚轮滚的是鼠标位置下的元素，经常滚到聊天窗上。
    返回本次实际滚动的像素数，0 表示到底了。
    """
    try:
        return float(
            page.evaluate(
                """({sel, top, delta}) => {
                    const el = document.querySelector(sel);
                    if (!el) return -1;
                    const before = el.scrollTop;
                    if (top !== null && top !== undefined) el.scrollTop = top;
                    else el.scrollTop = before + el.clientHeight * delta;
                    return el.scrollTop - before;
                }""",
                {"sel": LIST, "top": top, "delta": delta_ratio},
            )
        )
    except Exception:
        return -1.0


TEXTS_JS = """(sel) => [...document.querySelectorAll(sel)].map(el => {
    const t = el.querySelector('.conversationConversationItemtitle');
    return t ? (t.textContent || '').trim() : '';
})"""


def _texts(page: Page) -> list[str]:
    """读当前这一屏的昵称；读空了就等一下再试，避免恰好撞上重渲染。"""
    for _ in range(3):
        try:
            t = page.evaluate(TEXTS_JS, ITEM)
            if t:
                return t
        except Exception:
            pass
        page.wait_for_timeout(400)
    return []


def _scan_down(page: Page, target: str, max_pages: int) -> Locator | None:
    """从当前位置一屏一屏往下翻着找。"""
    items = page.locator(ITEM)
    for _ in range(max_pages):
        texts = _texts(page)
        for i, t in enumerate(texts):
            if t and norm(t) == target:
                it = items.nth(i)
                try:
                    it.scroll_into_view_if_needed(timeout=2000)
                    page.wait_for_timeout(200)
                except Exception:
                    pass
                return it
        if list_scroll(page, delta_ratio=0.8) <= 1:
            break
        page.wait_for_timeout(550)
    return None


JS_ROWS = """(sel) => [...document.querySelectorAll(sel)].map(el => {
    const t = el.querySelector('.conversationConversationItemtitle');
    const s = el.querySelector('.commonStreaknormalText');
    const ic = el.querySelector('img.commonStreakicon');
    const name = t ? (t.textContent || '').trim() : '';
    if (!name) return null;
    let fire = 'none';
    if (ic) fire = (ic.getAttribute('src') || '').includes('gray') ? 'gray' : 'lit';
    return {name: name, streak: s ? (s.textContent || '').trim() : '', fire: fire};
}).filter(Boolean)"""


def scan_list(page: Page, max_pages: int = 240) -> list[dict]:
    """一遍翻完整个会话列表，按当前显示顺序拿到 昵称 / 火花天数 / 灰彩状态。

    找人的真实瓶颈：名单顺序和列表顺序对不上，每发一个人就要回顶重扫。
    先把整张表拿下来，之后按表里的顺序从上往下走，翻屏从「N 次×若干屏」降到一次。
    扫完回顶，保证第一次找人是往下走。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for _ in range(max_pages):
        try:
            cur = page.evaluate(JS_ROWS, ITEM)
        except Exception:
            cur = []
        for r in cur:
            n = r.get("name") or ""
            if n and n not in seen:
                seen.add(n)
                rows.append(r)
        if list_scroll(page, delta_ratio=0.6) <= 1:
            break
        page.wait_for_timeout(480)
    reset_list_scroll(page)
    return rows


JS_FIND_POINT = """({sel, target}) => {
    const norm = s => (s || '').normalize('NFKC').replace(/[\\s\\u00a0\\u200b\\ufeff]+/g, '').toLowerCase();
    for (const el of document.querySelectorAll(sel)) {
        const t = el.querySelector('.conversationConversationItemtitle');
        if (t && norm(t.textContent) === target) {
            el.scrollIntoView({block: 'center'});
            const r = el.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }
    }
    return null;
}"""


def click_item(page: Page, name: str) -> bool:
    """找到目标那一行，滚到视野中间，再用真鼠标点它。

    为什么不用 nth(i).click()：这一屏的昵称是读出来的，点之前只要发生一次虚拟滚动
    重渲染，序号就错位，消息会点进别人窗口（实测点错过一次会话）。
    为什么不用 el.click()：合成事件不触发页面的真实处理逻辑，点了等于没点。
    所以取坐标 + 真鼠标点击。
    """
    try:
        pt = page.evaluate(JS_FIND_POINT, {"sel": ITEM, "target": norm(name)})
    except Exception:
        return False
    if not pt:
        return False
    try:
        page.wait_for_timeout(180)
        page.mouse.click(pt["x"], pt["y"])
        return True
    except Exception:
        return False


def _row_step(page: Page) -> int:
    """相邻两个会话项的像素间距，用来按位置直接跳，而不是一屏屏翻。"""
    try:
        return int(
            page.evaluate(
                """(sel) => {
                    const els = [...document.querySelectorAll(sel)];
                    if (els.length < 2) return 0;
                    return Math.round(els[1].getBoundingClientRect().top - els[0].getBoundingClientRect().top);
                }""",
                ITEM,
            )
        )
    except Exception:
        return 0


def find_item(page: Page, name: str, max_pages: int = 25, hint: int | None = None) -> Locator | None:
    """按昵称找人：给了序号就按位置直接跳过去，再就着附近找；找不到再全表重扫。"""
    _exit_search(page)  # 上一次若走了搜索兜底，先把列表还原
    target = norm(name)
    if hint is not None and hint > 0:
        step = _row_step(page)
        if step > 10:
            list_scroll(page, top=max(0, (hint - 1) * step - 120))
            page.wait_for_timeout(380)
            hit = _scan_down(page, target, 4)  # 预估值附近 4 屏内找
            if hit is not None:
                return hit
    hit = _scan_down(page, target, max_pages)
    if hit is not None:
        return hit
    if not reset_list_scroll(page):
        # 回顶失败（极端情况）：至少再试一次强制回顶
        list_scroll(page, top=0)
        page.wait_for_timeout(700)
    return _scan_down(page, target, max_pages)


def _exit_search(page: Page) -> None:
    """若处于搜索态（左上角有「取消」），点它还原会话列表；不在就立刻返回。"""
    try:
        cancel = page.get_by_text("取消", exact=True).first
        if cancel.count() and cancel.is_visible():
            cancel.click(timeout=3000)
            page.locator(ITEM).first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass


def open_by_search(page: Page, name: str) -> bool:
    """兜底：列表翻不到时用左上角搜索框搜昵称，点「发消息」打开会话。

    成功后保持聊天窗打开（退出搜索交给下一轮 find_item 开头的 _exit_search）。
    """
    log.info(f"    改用搜索框找「{name}」…")
    try:
        inp = page.locator("input[placeholder*='搜索']").first
        inp.click()
        inp.fill("")
        inp.fill(name)
        page.wait_for_timeout(2000)
        # 搜索结果里每条都带一个「发消息」，直接点第一个会点到别人头上
        # （实测出现过：搜「再不剪齐刘海」结果点开成「10X」）。
        # 所以先找到同一行容器里含目标昵称的那个按钮再点。
        target = norm(name)
        btns = page.get_by_text("发消息", exact=True)
        picked = False
        for i in range(min(btns.count(), 12)):
            b = btns.nth(i)
            try:
                row = b.locator("xpath=ancestor::*[self::li or self::div][1]")
                if target in norm(row.inner_text(timeout=800)):
                    b.click(timeout=5000)
                    picked = True
                    break
            except Exception:
                continue
        if not picked:
            btns.first.click(timeout=8000)
    except Exception as e:
        log.info(f"    搜索也没找到（{e.__class__.__name__}），放弃这个人")
        _exit_search(page)
        return False
    try:
        page.locator(EDITOR).first.wait_for(state="visible", timeout=10000)
    except Exception:
        log.info("    搜索打开了但输入框没出现")
        return False
    return True


def read_streak(item: Locator, page: Page) -> str:
    """火花天数：先从列表项读，读不到再从聊天窗头部读。"""
    try:
        s = item.locator(STREAK).first
        if s.count():
            t = (s.inner_text(timeout=800) or "").strip()
            if t:
                return t
    except Exception:
        pass
    # 这里不能兜底去读聊天窗头部 —— 此刻会话还没点开，头部显示的是上一个人，
    # 会把上一个人的天数记到这个人头上。列表里没有就留空，点开后再从头部补。
    return ""


def read_header_streak(page: Page) -> str:
    """聊天窗头部的火花标识最准（列表项里有时代的是“N天后消失”这种提醒）。"""
    for sel in (
        f"{HEADER_BOX} .commonStreaknormalText",
        ".RightPanelHeaderconvHeader .commonStreaknormalText",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                t = (loc.inner_text(timeout=800) or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


def read_header_name(page: Page) -> str:
    """读聊天窗里当前打开的是谁。读不到返回空串。"""
    for sel in (HEADER_TITLE, f"{HEADER_BOX} [class*='title']"):
        try:
            loc = page.locator(sel).first
            if loc.count():
                t = (loc.inner_text(timeout=800) or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


def verify_target(page: Page, name: str) -> tuple[str, str]:
    """发送前确认"聊天窗里现在打开的人"就是目标好友。

    存在的意义：2026-10-19 发生过一次误发——运行中手动滚动会话列表，
    程序点的会话和它以为的不一致，于是把消息发给了名单外的人。
    这里读聊天窗口头部的昵称做二次确认，不一致就绝不发。

    返回 (状态, 头部实际昵称)，状态为 ok / mismatch / unknown。
    """
    hdr = read_header_name(page)
    if not hdr:
        return "unknown", ""
    a, b = norm(hdr), norm(name)
    if a == b:
        return "ok", hdr
    # 头部可能带"昵称 · 状态"这类后缀，按分隔符拆开再看一次
    for part in re.split(r"[·|/\\\-—_（）()\[\]【】\s]+", hdr):
        if part and norm(part) == b:
            return "ok", hdr
    return "mismatch", hdr


def clear_editor(page: Page) -> None:
    """清空输入框，别把半截内容留在别人的聊天窗里。"""
    try:
        ed = page.locator(EDITOR).first
        ed.click(timeout=3000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
    except Exception:
        pass


def read_item_fire(item: Locator) -> str:
    """从会话列表项读火花状态（2026-11-11 零点实测）：
    图标是 img.commonStreakicon，src 带 gray_ = 今天没续，彩色 = 今天已续。
    没有图标 = 这个会话当前没有火花（可能是从没续过或已断）。
    """
    try:
        ic = item.locator("img.commonStreakicon").first
        if ic.count():
            src = ic.get_attribute("src") or ""
            return "gray" if "gray" in src else "lit"
    except Exception:
        pass
    return "none"


def send_one(
    page: Page,
    name: str,
    text: str,
    dry: bool = False,
    skip_lit: bool = True,
    hint: int | None = None,
) -> tuple[str, str, str]:
    """给一个人发消息，返回 (状态, 说明, 火花天数)。状态：ok / fail / skip。"""
    global _header_misses
    item = find_item(page, name, hint=hint)
    via_search = False
    if item is None:
        # 列表翻不到（可能被抖音的排序/渲染问题藏起来了），走搜索兜底
        if not open_by_search(page, name):
            shot(page, f"notfound_{name}")
            try:
                titles = [
                    t.strip()
                    for t in page.locator(TITLE).all_inner_texts()
                    if t.strip()
                ]
                log.info(f"    列表里实际看到的昵称：{'、'.join(titles[:25])}")
            except Exception:
                pass
            return "fail", "列表和搜索都找不到这个人（日志里有实际昵称，核对下差异）", ""
        via_search = True
        streak = ""
    else:
        streak = read_streak(item, page)
        # 火花已亮 = 今天已经续过（不管是谁续的：程序、本人手动、对方互动完成）。
        # 这是"重复发显得莫名其妙"的根治：列表上一眼就能看出来，不用点开会话。
        if skip_lit and read_item_fire(item) == "lit":
            return "skip", "火花已亮，今天已续过，不重复发", streak

    if not via_search:
        if not click_item(page, name):
            # 列表里没点中（位置估算偏了/改过备注）：退到搜索，别直接算失败
            log.info("    列表里没点中，改用搜索兜底")
            if not open_by_search(page, name):
                shot(page, f"clickfail_{name}")
                return "fail", "列表里没点中，搜索也没找到", streak
            via_search = True

    try:
        page.locator(EDITOR).first.wait_for(state="visible", timeout=10000)
    except Exception:
        shot(page, f"noeditor_{name}")
        return "fail", "点开了但输入框没出现", streak
    page.wait_for_timeout(random.randint(600, 1200))

    # —— 防误发：确认窗口里打开的这个人，就是名单里的目标 ——
    status, actual = verify_target(page, name)
    if status == "mismatch":
        shot(page, f"mismatch_{name}")
        clear_editor(page)
        log.warning(f"    身份校验不通过：窗口里是「{actual}」，不是「{name}」，已放弃（没有发送）")
        return "fail", f"点开的是「{actual}」而不是「{name}」，为防发错已放弃", streak
    if status == "unknown":
        _header_misses += 1
        log.warning(f"    读不到聊天窗标题（本轮第 {_header_misses} 次），按原逻辑继续")
        if _header_misses == 3:
            log.warning("    连续读不到标题，多半是抖音改版了，需要更新 core.py 里的 HEADER_TITLE")

    # 聊天窗头部的数字才是真火花天数，列表项里可能只是“N天后消失”的提醒
    hs = read_header_streak(page)
    if hs and (hs.isdigit() or not streak.isdigit()):
        streak = hs

    ed = page.locator(EDITOR).first
    try:
        ed.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(random.randint(200, 500))
        page.keyboard.insert_text(text)
        page.wait_for_timeout(random.randint(400, 900))
    except Exception as e:
        shot(page, f"typefail_{name}")
        return "fail", f"输入文字失败：{e}", streak

    if text[:4] not in editor_text(page):
        shot(page, f"typelost_{name}")
        return "fail", "文字没进输入框", streak

    if dry:
        ed.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        return "ok", "演练：已填入并清空，未发送", streak

    page.keyboard.press("Enter")
    page.wait_for_timeout(1800)

    if editor_text(page):
        shot(page, f"sendfail_{name}")
        return "fail", "回车后输入框没清空，可能没发出去", streak

    return "ok", "已发送", streak


def open_chat_page(page: Page) -> bool:
    """一步到位：直接打开独立私信页，不再先过首页。"""
    page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        page.locator(ITEM).first.wait_for(state="visible", timeout=30000)
        return True
    except Exception:
        log.error("私信页 30 秒内没渲染出会话列表（可能出了验证码，或登录已失效）")
        shot(page, "chat_page_fail")
        return False


def run_once(dry: bool = False, headless: bool = False, only: list[str] | None = None) -> dict:
    global _header_misses
    _header_misses = 0
    cfg = load_config()
    # friends 支持两种形态：["名字"]（老配置）或 [{name, group}]
    group_of: dict[str, str] = {}
    friends: list[str] = []
    for f in cfg.get("friends") or []:
        if isinstance(f, dict):
            if f.get("name"):
                friends.append(str(f["name"]))
                if f.get("group") == "close":
                    group_of[str(f["name"])] = "close"
        elif f:
            friends.append(str(f))
    if only:
        friends = [n for n in friends if n in only]
    if not friends:
        log.error("config.yaml 里的 friends 是空的（或没匹配到 --only）")
        return {"ok": 0, "fail": 0}

    pool = [str(m) for m in (cfg.get("messages") or []) if str(m).strip()]
    # 熟人组用另一套话术；没单独配就用全局池子（行为同旧版）
    close_pool = [str(m) for m in (cfg.get("close_messages") or []) if str(m).strip()]
    if not close_pool:
        close_pool = pool
    per = max(1, int(cfg.get("messages_per_friend") or 1))
    gap_lo, gap_hi = (cfg.get("gap_seconds") or [2, 15])[:2]
    gap_hi = min(int(gap_hi), 15)  # 上限硬性 15 秒
    gap_lo = max(1, min(int(gap_lo), gap_hi))
    skip_lit = bool(cfg.get("skip_if_lit", True))
    today = date.today()

    rec = load_record(today)
    done = {r["name"] for r in rec.get("results", []) if r.get("ok") and not r.get("dry")}

    p, ctx = launch(headless=headless)
    page = first_page(ctx)
    ok = fail = skip = 0
    used: list[str] = []
    round_results: list[tuple[str, bool]] = []

    try:
        # 登录态直接从持久化 profile 的 cookie 判断，不用为了检查先开一次首页
        if not is_logged_in(ctx):
            log.error("登录态失效了。请重跑 probe.py 重新扫码登录")
            return {"ok": 0, "fail": 0, "error": "not_logged_in"}

        if not open_chat_page(page):
            return {"ok": 0, "fail": 0, "error": "chat_page_fail"}

        # 先把整张会话列表读下来：
        #   1) 拿到每个人的灰彩状态 —— 彩火的不用点开就知道今天已续；
        #   2) 按列表顺序发 —— 名单顺序和列表顺序对不上，硬按名单找就是每次都回顶重扫。
        rows = scan_list(page)
        state = {r["name"]: r for r in rows}
        idx = {r["name"]: i for i, r in enumerate(rows)}
        wanted = set(friends)
        in_list = [r["name"] for r in rows if r["name"] in wanted]
        tail = [n for n in friends if n not in state]
        targets = in_list + tail
        clicked = 0  # 每点开一个人，他就被顶到列表最前，后面的位置整体上移一位
        log.info(
            f"会话列表 {len(rows)} 个，名单内 {len(in_list)} 个按列表顺序处理"
            + (f"；{len(tail)} 个不在列表里，走搜索" if tail else "")
        )
        log.info(f"开始处理 {len(targets)} 位好友{'（演练模式）' if dry else ''}")

        for i, name in enumerate(targets, 1):
            if name in done and not dry:
                log.info(f"[{i}/{len(targets)}] {name} —— 今天已经发过，跳过")
                skip += 1
                continue

            okflag = False
            skipped_lit = False
            streak = (state.get(name) or {}).get("streak") or ""
            msgs = []
            # 列表上已经能看出今天续过了，连点开都省了
            if skip_lit and (state.get(name) or {}).get("fire") == "lit":
                skipped_lit = True
            # 预期他现在的位置：原始序号 - 已经点开过的人数
            hint = None
            if name in idx:
                hint = max(1, idx[name] + 1 - clicked)
            my_pool = close_pool if group_of.get(name) == "close" else pool
            for k in range(0 if skipped_lit else per):
                text = pick_message(my_pool, used)
                used.append(text)
                status, msg, streak = send_one(
                    page, name, text, dry=dry, skip_lit=skip_lit, hint=hint
                )
                if status == "skip":
                    skipped_lit = True
                    msgs = []
                    break
                okflag = status == "ok"
                msgs.append(text)
                label = f"{streak}天" if streak.isdigit() else (streak or "?")
                log.info(f"[{i}/{len(targets)}] {name}（火花{label}）—— {msg}")
                if not okflag:
                    break
                if k < per - 1:
                    page.wait_for_timeout(random.randint(3000, 8000))

            if skipped_lit:
                log.info(f"[{i}/{len(targets)}] {name}（火花{streak}）—— 火花已亮，今天已续，跳过")
            rec.setdefault("results", []).append(
                {
                    "name": name,
                    "time": time.strftime("%H:%M:%S"),
                    "ok": True,
                    "dry": dry,
                    "streak": streak,
                    "text": msgs,
                    "skipped_lit": skipped_lit,
                }
            )
            save_record(rec, today)
            round_results.append((name, okflag or skipped_lit))
            if skipped_lit:
                skip += 1
            elif okflag:
                ok += 1
            else:
                fail += 1

            if not skipped_lit:
                clicked += 1

            if not skipped_lit and i < len(targets) and not dry:
                wait = random.randint(int(gap_lo), int(gap_hi))
                log.info(f"    歇 {wait} 秒…")
                time.sleep(wait)

        round_fail = [n for n, o in round_results if not o]
        today_ok = sorted({r["name"] for r in rec["results"] if r.get("ok") and not r.get("dry")})
        log.info("=" * 46)
        log.info(f"本轮完成：成功 {ok}，失败 {fail}，跳过 {skip}")
        if round_fail:
            log.info(f"本轮失败：{'、'.join(round_fail)}")
            log.info("失败现场截图在 screenshots/ 目录里")
        log.info(f"今日累计已发：{len(today_ok)} 人{'：' + '、'.join(today_ok) if today_ok else ''}")
        return {"ok": ok, "fail": fail, "skip": skip}

    finally:
        # 浏览器现在是常驻的，运行完只断开连接、窗口留着（下次秒连）
        shutdown(p, ctx)
