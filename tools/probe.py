# -*- coding: utf-8 -*-
"""页面探测工具 —— 第一次使用、或抖音网页版改版后跑一次。

作用：
  1. 打开带持久化登录态的浏览器，让你扫码登录（只需一次）
  2. 保存当前私信页的真实 HTML 结构到 probe_page.html
  3. 生成一份元素清单 probe_report.txt（带坐标、data-e2e、class）
拿到这两份文件，就能把 selectors.yaml 里的定位规则改准。
"""
from __future__ import annotations

import time

from common import HOME, ROOT, find_visible, first_page, get_logger, is_logged_in, launch, shot, shutdown

log = get_logger("probe")

COLLECT_JS = r"""
() => {
  const rect = el => { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const items = [];
  const seen = new Set();
  const push = (el, kind) => {
    const r = rect(el);
    if (r.w === 0 || r.h === 0) return;
    const key = kind + '|' + r.x + '|' + r.y + '|' + (el.innerText || '').slice(0, 20);
    if (seen.has(key)) return;
    seen.add(key);
    items.push({
      kind: kind,
      tag: el.tagName.toLowerCase(),
      e2e: el.getAttribute('data-e2e') || '',
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 140),
      ph: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      ce: el.getAttribute('contenteditable') || '',
      text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50),
      x: r.x, y: r.y, w: r.w, h: r.h
    });
  };
  document.querySelectorAll('[data-e2e]').forEach(el => push(el, 'e2e'));
  document.querySelectorAll('input, textarea, [contenteditable="true"]').forEach(el => push(el, 'input'));
  document.querySelectorAll('div, span, a, li, button, p').forEach(el => {
    if (el.children.length === 0) {
      const t = (el.textContent || '').trim();
      if (t && t.length <= 30) push(el, 'text');
    }
  });
  return items;
}
"""


def wait_login(ctx, timeout: int = 300) -> bool:
    log.info("请在弹出的浏览器里扫码登录抖音（只需登录一次，之后会记住）")
    start = time.time()
    while time.time() - start < timeout:
        if is_logged_in(ctx):
            log.info("登录状态已确认 ✓")
            return True
        time.sleep(2)
    return False


def main() -> None:
    p, ctx = launch(headless=False)
    page = first_page(ctx)
    try:
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        if not is_logged_in(ctx):
            if not wait_login(ctx):
                log.error("等太久没检测到登录，先退出。可以重跑一次 probe.py")
                return
        else:
            log.info("已有登录态，直接使用 ✓")

        page.wait_for_timeout(1500)

        # 先尝试自动进入消息页；不行就让用户手动点
        entry = find_visible(page, ["[data-e2e='message']", "a[href*='/chat']", "text=私信", "text=消息"], 3000)
        if entry is not None:
            log.info("找到消息入口，自动点击…")
            try:
                entry.click()
                page.wait_for_timeout(3000)
            except Exception as e:
                log.warning(f"自动点击失败：{e}")
        else:
            log.warning("没自动找到「消息」入口")

        print()
        print("=" * 60)
        print("现在请在浏览器里手动点开顶部的【消息 / 私信】，")
        print("确认左侧出现了好友会话列表、右侧能输入文字。")
        print("弄好后回到这个窗口，按回车继续。")
        print("=" * 60)
        try:
            input()
        except EOFError:
            page.wait_for_timeout(15000)

        page.wait_for_timeout(1000)
        shot(page, "probe")

        html = page.content()
        (ROOT / "probe_page.html").write_text(html, encoding="utf-8")

        items = page.evaluate(COLLECT_JS) or []
        lines = [f"URL: {page.url}", f"TITLE: {page.title()}", f"ITEMS: {len(items)}", ""]

        for kind, title in (("e2e", "① 带 data-e2e 的元素（最可靠的定位依据）"),
                            ("input", "② 输入类元素（input / textarea / contenteditable）"),
                            ("text", "③ 叶子文本节点（昵称、按钮文案都在这里）")):
            group = [it for it in items if it["kind"] == kind]
            lines.append(f"===== {title}  共 {len(group)} 个 =====")
            for it in group[:260]:
                lines.append(
                    f"<{it['tag']}> rect=({it['x']},{it['y']},{it['w']}x{it['h']}) "
                    f"e2e={it['e2e']!r} ph={it['ph']!r} ce={it['ce']!r} "
                    f"cls={it['cls']!r} text={it['text']!r}"
                )
            lines.append("")

        (ROOT / "probe_report.txt").write_text("\n".join(lines), encoding="utf-8")

        log.info(f"已保存 probe_page.html ({len(html) // 1024} KB) 和 probe_report.txt ({len(lines)} 行)")
        print()
        print("好了，把这两个文件留着，我直接读就行。")
        print(f"  {ROOT / 'probe_report.txt'}")
        print(f"  {ROOT / 'probe_page.html'}")
    finally:
        shutdown(p, ctx)


if __name__ == "__main__":
    main()
