#!/usr/bin/env python3
"""check-demo.py — 示例文档守门：规则改了，示例必须跟上。

校验 examples/*.md（切片与完整模拟样例）是否与 SKILL.md 执行卡/报告模板一致：
  1. 开场白 ≤3 句（首个"面试官："段落，按句号/问号/感叹号计数）
  2. 复盘报告节用 ## 标题：亮点（兼容"三大亮点"）、三大改进项（兼容"改进项"）
  3. 亮点 0~3 条；改进项恰好 3 条且三段结构齐全（问题/示范/练习建议）
  4. 自评数字一致（"自评 X 分"与后文"你自评 Y"不允许出现两个不同数字）
  5. 六维表格行齐全（| 维度 | 形式）
  6. 时长含"分钟"时必须标"估算"
  7. 完整模拟样例（demo-full.md）必含：自评先行提问、压力题、反问环节

用法：python scripts/check-demo.py examples/demo-session.md [examples/demo-full.md ...]
exit 0 = 通过；exit 1 = 未通过（列出问题）。发布前跑一遍，示例过不了不许发版。
"""

import re
import sys
from pathlib import Path

DIMS = ["内容质量", "逻辑结构", "语言表达", "临场应变", "岗位匹配", "证据"]


def _stdout_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _section(text, heading_re):
    m = re.search(heading_re, text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^#{1,6}\s", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _count_items(seg):
    return [ln for ln in seg.splitlines() if re.match(r"\s*\d+[\.、]", ln)]


def check(path):
    p = Path(path)
    if not p.is_file():
        print("✗ 文件不存在：" + path)
        return 1
    text = p.read_text(encoding="utf-8-sig")
    problems = []
    ok = []
    is_full = "demo-full" in p.name

    # 1. 开场白 ≤3 句
    m = re.search(r"\*\*面试官\*\*：(.+?)(?=\n\n|\n\*\*你\*\*)", text, re.S)
    if m:
        opening = m.group(1).strip()
        # 破折号内的插入短语不算独立句
        sentences = [s for s in re.split(r"[。？！]", opening) if s.strip()]
        n = len(sentences)
        if n > 3:
            problems.append("开场白 {n} 句（执行卡第 2 条上限 3 句）。原文：{o}".format(
                n=n, o=opening[:60] + "…"))
        else:
            ok.append("开场白 {n} 句".format(n=n))
    else:
        problems.append("未找到开场白（首个「面试官：」段落）")

    # 2~4. 报告节
    seg = _section(text, r"^##\s*(三大)?\s*亮点")
    if seg is None:
        problems.append("缺「亮点」节（须 ## 标题，加粗 ** ** 不算）")
    else:
        items = _count_items(seg)
        if len(items) > 3:
            problems.append("亮点 {n} 条（上限 3）".format(n=len(items)))
        else:
            ok.append("亮点 {n} 条".format(n=len(items)))
    seg = _section(text, r"^##\s*(三大)?\s*改进项")
    if seg is None:
        problems.append("缺「三大改进项」节（须 ## 标题，加粗 ** ** 不算）")
    else:
        items = _count_items(seg)
        if len(items) != 3:
            problems.append("改进项 {n} 条（必须恰好 3）".format(n=len(items)))
        else:
            ok.append("改进项 3 条")
            for kw in ("问题", "示范", "练习建议"):
                if seg.count(kw) < 3:
                    problems.append("改进项三段结构缺 [{kw}]".format(kw=kw))
    # 自评数字一致
    nums = re.findall(r"自评\s*(?:—|is|-|:)?\s*[约]?(\d+(?:\.\d+)?)\s*分", text)
    extra = re.findall(r"你自评\s*(\d+(?:\.\d+)?)", text)
    allnums = [float(x) for x in nums + extra]
    if allnums and len(set(allnums)) > 1:
        problems.append("自评数字自相矛盾：{ns}".format(ns=allnums))
    elif allnums:
        ok.append("自评数字一致（{v}）".format(v=allnums[0]))
    # 5. 六维表格行
    for d in DIMS:
        if re.search(r"^\|\s*" + re.escape(d) + r"\s*\|", text, re.M):
            pass
        else:
            problems.append("六维评分表缺 [{d}] 行".format(d=d))
    # 6. 时长估算标注
    if "分钟" in text and "估算" not in text:
        problems.append("含时长未标「估算」")

    # 7. 完整模拟样例必备环节
    if is_full:
        for must, kw in (("自评先行提问", "自己估个分"), ("压力题", "压力题"), ("反问环节", "想问")):
            if kw not in text:
                problems.append("完整模拟样例缺必备环节：{m}（关键词「{k}」）".format(m=must, k=kw))
        else:
            pass
        found = [must for must, kw in (("自评先行提问", "自己估个分"), ("压力题", "压力题"), ("反问环节", "想问")) if kw in text]
        if found:
            ok.append("必备环节：" + "、".join(found))

    if problems:
        print("✗ {p} 未通过（{n} 项）：".format(p=path, n=len(problems)))
        for x in problems:
            print("  - " + x)
        return 1
    print("✓ {p} 通过：{o}".format(p=path, o="；".join(ok)))
    return 0


def main():
    _stdout_utf8()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("用法：check-demo.py <示例.md> [...]（至少一个示例文件）")
        return 1
    ret = 0
    for a in args:
        ret = max(ret, check(a))
    return ret


if __name__ == "__main__":
    sys.exit(main())
