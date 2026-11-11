# -*- coding: utf-8 -*-
"""把会话列表从头翻到底，导出全部昵称，用来核对某个昵称到底存不存在。"""
import sys

from common import CHAT_URL, first_page, launch, is_logged_in
from core import ITEM, LIST, TEXTS_JS, list_scroll

KEY = sys.argv[1] if len(sys.argv) > 1 else ""


def main():
    p, ctx = launch(headless=False)
    try:
        if not is_logged_in(ctx):
            print("登录态失效")
            return
        pg = first_page(ctx)
        pg.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
        pg.locator(ITEM).first.wait_for(state="visible", timeout=30000)
        pg.wait_for_timeout(2500)
        list_scroll(pg, top=0)
        pg.wait_for_timeout(600)

        seen, order = set(), []
        for page_i in range(200):
            texts = pg.evaluate(TEXTS_JS, ITEM)
            for t in texts:
                if t and t not in seen:
                    seen.add(t)
                    order.append(t)
            moved = list_scroll(pg, delta_ratio=0.8)
            if moved <= 1:
                break
            pg.wait_for_timeout(550)

        print(f"共收集 {len(order)} 个会话，翻页 {page_i + 1} 次")
        if KEY:
            hits = [t for t in order if KEY in t]
            print(f"含「{KEY}」的：{hits if hits else '（没有）'}")
        for i, t in enumerate(order):
            print(f"{i:>3}  {t}")
    finally:
        ctx.close()
        p.stop()


if __name__ == "__main__":
    main()
