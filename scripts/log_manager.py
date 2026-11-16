#!/usr/bin/env python3
"""interview-coach 跨场次练习日志管理器 v6（纯标准库，无第三方依赖）。

用法（<workspace> 为用户工作区，始终显式传入，防止换目录读到别的库）：
  python log_manager.py --workspace <dir> commit    # 读 .new-entry.json，校验后追加
  python log_manager.py --workspace <dir> latest    # 打印最近一场（JSON）
  python log_manager.py --workspace <dir> summary   # 六维曲线 + 弱项攻坚 + 校准分桶 + fail 归因
  python log_manager.py --workspace <dir> export    # 导出带时间戳备份副本
  python log_manager.py --workspace <dir> backfill --session <场号> --outcome <pass|fail|pending> [--note ...]
  python log_manager.py check-report <报告.md路径>  # 机械校验复盘报告结构

v6 变更（全部为修复）：
  - 无评分场次不再污染状态机：急救包等场次 passed 置 null，不进任何弱项序列
    （v5 及更早误存 passed:false 的急救包记录读取时一并豁免）——
    已攻克状态不被无评分场次打断；
  - fail 归因口径修正：统计所有 passed:false 的评分场次（不再只认 outcome=fail，
    模拟挂同样计入，样本量从 1~3 场扩大到整个练习史）；六维并列最低的场次
    无法唯一归因，跳过并计数提示；
  - commit 提示复用四级分级（复发预警/已复发/持续恶化），与 summary 一致；
  - check-report 修复：亮点/改进项标题正则兼容「三大」前缀（`## 三大亮点` 与
    `## 亮点（0~3 条）` 均识别）；六维检查改为解析表格行（`| 维度 |`）而非全文
    搜索（正文提一句"证据维"不再能掩盖评分表缺维）；新增改进项三段结构检查
    （每条须含 问题/示范/练习建议）。

v3~v5 遗留能力：per-weak 独立计时（交替不清零，按日期归位）；攻克可复发；
outcome/backfill 数据闭环；校准分桶（模拟/真实分开，样本<3 提示）；无评分场次；
passed 数组长度强校验；损坏熔断；原子写；session_no 取最大+1；schema 版本兼容。

安全机制：原子写（tmp+replace）；损坏熔断（exit 2 另存 corrupt 副本绝不覆盖）；
每次保存前留 .bak（仅一档，恢复会丢最后一场）。

录入文件 <workspace>/interview-log/.new-entry.json（UTF-8）：
{
  "date": "2026-11-16",             # 可省略，默认今天
  "scenario": "校招·后端技术面",
  "mode": "切片对练",                # 完整模拟 / 切片对练 / 真实复盘 / 急救包（无评分）等
  "style": "严格型",                 # 可省略
  "dimension_scores": {...六维...},  # mode=急救包 时省略（无评分场次）
  "total": 5.3,                      # 同上；有时须落在六维区间
  "grade": "B",                      # 可省略自动推导；给了须与 total 一致
  "weak_point": "证据",               # 单值或数组 ["证据","语言表达"]
  "passed": false,                   # bool 或按序数组（长度须与弱项数一致）；急救包不填
  "outcome": "fail",                 # 可选：pass/fail/pending（真实面试结果）
  "next_retest": "……", "highlights": ["…"], "improvements": ["…"]
}
commit 后脚本自动附加（勿手写）：schema、session_no、pass_map、pass_streak、
resolved；无评分场次 passed 存 null。

判定口径（与 SKILL.md / scoring-and-feedback.md 一致）：
  - passed：该弱项维度本场 ≥7 分且未触发该维度对应扣分点；
  - 无评分场次（急救包等）：不判定过关，不进弱项序列，只登记事件与风险；
  - per-weak 连续：弱项自身序列（按日期归位）中最近连续过关数，其他弱项与
    无评分场次均跳过不清零；连续 2 场过关 → 已攻克；
  - 攻克后再未过 → 复发（预警/已复发/持续恶化分级）。
"""

import argparse
import json
import re
import shutil
import sys
from datetime import date as _date, datetime as _dt
from pathlib import Path

LOG_DIR_NAME = "interview-log"
LOG_FILE_NAME = "log.json"
ENTRY_FILE_NAME = ".new-entry.json"

DIMENSIONS = ["内容质量", "逻辑结构", "语言表达", "临场应变", "岗位匹配", "证据"]
REQUIRED_FULL = ["scenario", "mode", "weak_point", "dimension_scores", "total"]
REQUIRED_EVENT = ["scenario", "mode", "weak_point"]
NO_SCORE_MODES = ("急救包",)          # 无评分场次：只记事件
REAL_MODE_MARK = "真实"               # mode 含此词 → 真实面试桶
OUTCOMES = ("pass", "fail", "pending")
SCHEMA = 5


def _stdout_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _norm(weak):
    return str(weak or "").strip().lower()


def _truthy(v):
    return v is True or v == "true" or v == 1


def _grade(total):
    if total >= 8.5:
        return "S"
    if total >= 7.0:
        return "A"
    if total >= 5.5:
        return "B"
    return "C"


def _die(code, msg):
    print(msg)
    sys.exit(code)


def _is_real(e):
    return REAL_MODE_MARK in str(e.get("mode", ""))


def _is_scored(e):
    return isinstance(e.get("dimension_scores"), dict) and "total" in e


def _sort_key(e, idx_hint=0):
    """按日期归位排序：date 升序；同日按录入序（session_no 或列表位置）。"""
    d = str(e.get("date", ""))
    sn = e.get("session_no")
    seq = sn if isinstance(sn, (int, float)) else idx_hint
    return (d, seq)


class LogStore:
    def __init__(self, workspace):
        self.dir = Path(workspace) / LOG_DIR_NAME
        self.log_file = self.dir / LOG_FILE_NAME
        self.entry_file = self.dir / ENTRY_FILE_NAME

    def load(self):
        """读主库；损坏时熔断（保存现场副本后退出），绝不返回空库。"""
        if not self.log_file.exists():
            return []
        try:
            data = json.loads(self.log_file.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            ts = _dt.now().strftime("%Y%m%d-%H%M%S")
            corrupt = self.dir / ("{}.corrupt-{}".format(LOG_FILE_NAME, ts))
            try:
                shutil.copy2(self.log_file, corrupt)
                kept = "原文件已另存为 {}".format(corrupt.name)
            except OSError:
                kept = "原文件保留在原处未动"
            _die(2, "练习日志 {} 损坏（{}），为防止覆盖历史已停止操作。{}\n"
                    "注意：.bak 只保留最近一版，从 .bak 恢复会丢掉最后一场已提交记录；"
                    "若那一场重要，请先查看 {} 尾部能否手工拼接。\n"
                    "请人工检查（常见原因：同步盘冲突、写入被中断），修复后重试。".format(
                        self.log_file, exc, kept, corrupt.name))
        if not isinstance(data, list):
            _die(2, "练习日志 {} 内容不是记录列表，已停止操作以防覆盖。".format(self.log_file))
        return data

    def save(self, entries):
        """原子写：tmp + replace；写前留 .bak。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.log_file.exists():
            shutil.copy2(self.log_file, self.dir / (LOG_FILE_NAME + ".bak"))
        tmp = self.dir / (LOG_FILE_NAME + ".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.log_file)


def _read_entry(store):
    if not store.entry_file.exists():
        _die(1, "未找到 interview-log/.new-entry.json；先按 SKILL.md 格式写入该文件再 commit。")
    try:
        return json.loads(store.entry_file.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        _die(1, ".new-entry.json 解析失败：" + str(exc))


def _weak_list(entry):
    wp = entry.get("weak_point")
    if isinstance(wp, str) and wp.strip():
        return [wp.strip()]
    if isinstance(wp, list) and wp and all(
            isinstance(w, str) and w.strip() for w in wp):
        return [str(w).strip() for w in wp]
    return None


def _validate(entry):
    mode = str(entry.get("mode", "")).strip()
    if not entry.get("scenario"):
        _die(1, "scenario 不能为空")
    if not mode:
        _die(1, "mode 不能为空")
    event_mode = any(mode.startswith(m) or mode == m for m in NO_SCORE_MODES)
    required = REQUIRED_EVENT if event_mode else REQUIRED_FULL
    missing = [k for k in required if k not in entry]
    if missing:
        hint = "" if not event_mode else "（{} 模式为无评分场次，可不填六维分与总分）".format("、".join(NO_SCORE_MODES))
        _die(1, "缺少必填字段：" + "、".join(missing) + hint)
    weaks = _weak_list(entry)
    if weaks is None:
        _die(1, "weak_point 需为非空字符串或非空字符串数组")
    if event_mode:
        entry.pop("dimension_scores", None)
        entry.pop("total", None)
        entry.pop("grade", None)
        entry["passed"] = None      # 无评分场次不判定过关（填了也覆盖为 null）
    else:
        scores = entry.get("dimension_scores") or {}
        bad = []
        for d in DIMENSIONS:
            v = scores.get(d)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (1 <= v <= 10):
                bad.append("{0}={1!r}".format(d, v))
        if bad:
            _die(1, "dimension_scores 需为六维 1~10 数值分，以下不合法：" + "；".join(bad))
        total = entry.get("total")
        if not isinstance(total, (int, float)) or isinstance(total, bool) or not (1 <= total <= 10):
            _die(1, "total 需为 1~10 的数值，当前：{0!r}".format(total))
        vals = [scores[d] for d in DIMENSIONS]
        if not (min(vals) - 0.05 <= total <= max(vals) + 0.05):
            _die(1, "total={0} 超出六维分范围 [{1}, {2}]（加权平均不可能越界），请复核"
                .format(total, min(vals), max(vals)))
        grade = entry.get("grade")
        expect = _grade(total)
        if grade is not None and str(grade).strip().upper() != expect:
            _die(1, "grade={0!r} 与 total={1} 对应的评级 {2!r} 不一致，请复核"
                .format(grade, total, expect))
        entry["grade"] = expect
        p = entry.get("passed")
        if p is None:
            entry["passed"] = False
        elif isinstance(p, bool):
            pass
        elif isinstance(p, list):
            if not all(isinstance(v, bool) for v in p):
                _die(1, "passed 数组元素需为布尔值")
            if len(p) != len(weaks):
                _die(1, "passed 数组长度（{0}）必须与 weak_point 数量（{1}）一致——"
                    "长度不一致会静默篡改未给值的弱项为'未过关'，直接拒绝".format(len(p), len(weaks)))
        else:
            if _truthy(p):
                entry["passed"] = True
            elif p is False or p == "false":
                entry["passed"] = False
            else:
                _die(1, "passed 仅接受 bool 或与弱项等长的 bool 数组，当前：{0!r}".format(p))
    oc = entry.get("outcome")
    if oc is not None and oc not in OUTCOMES:
        _die(1, "outcome 仅接受 pass / fail / pending，当前：{0!r}".format(oc))
    return entry


def _weak_items(entry):
    """归一化弱项与过关：返回 [(weak_name, passed_bool), ...]。

    无评分场次（急救包等，含 v5 及更早误存 passed:false 的历史记录）一律返回空——
    不进任何弱项序列，不判定过关，只登记事件。
    """
    if not _is_scored(entry):
        return []
    weaks = _weak_list(entry) or []
    p = entry.get("passed")
    if p is None:
        return []
    if isinstance(p, list):
        passed = [_truthy(v) for v in p[:len(weaks)]]
        while len(passed) < len(weaks):
            passed.append(False)
    else:
        v = _truthy(p)
        passed = [v] * len(weaks)
    return list(zip(weaks, passed))


def _ordered_entries(entries):
    """按日期归位排序（同日按 session_no / 录入序），用于连胜与归因计算。"""
    return sorted(enumerate(entries), key=lambda kv: _sort_key(kv[1], kv[0]))


def _streak_of(entries, weak):
    """per-weak 独立计时（按日期归位序列）：其他弱项与无评分场次跳过不清零。"""
    streak = 0
    for _, e in reversed(_ordered_entries(entries)):
        mapping = dict(_weak_items(e))
        if weak not in mapping:
            continue
        if mapping[weak]:
            streak += 1
        else:
            break
    return streak


def _fail_streak_of(entries, weak):
    """该弱项自身序列中最近连续未过关数（不含当前正在提交的场次）。"""
    fails = 0
    for _, e in reversed(_ordered_entries(entries)):
        mapping = dict(_weak_items(e))
        if weak not in mapping:
            continue
        if mapping[weak]:
            break
        fails += 1
    return fails


def _was_resolved_before(entries, weak):
    target = _norm(weak)
    for e in entries:
        r = e.get("resolved")
        if isinstance(r, list) and target in [_norm(x) for x in r if isinstance(x, str)]:
            return True
        ps = e.get("pass_streak")
        if isinstance(ps, dict):
            for k, v in ps.items():
                if _norm(k) == target and isinstance(v, (int, float)) and v >= 2:
                    return True
        if (r is True or (isinstance(ps, (int, float)) and ps >= 2)) and \
                _norm(e.get("weak_point")) == target:
            return True
    return False


def cmd_commit(store):
    entry = _validate(_read_entry(store))
    entry.setdefault("date", _date.today().isoformat())
    entry["schema"] = SCHEMA
    entries = store.load()
    insert_hint = ""
    if entries and str(entries[-1].get("date", "")) > str(entry["date"]):
        insert_hint = "（补录归位）"
    existing_max = 0
    for e in entries:
        sn = e.get("session_no")
        if isinstance(sn, (int, float)) and sn > existing_max:
            existing_max = int(sn)
    items = _weak_items(entry)
    entry["pass_map"] = dict(items)
    streaks = {}
    fail_counts = {}
    for w, passed in items:
        if passed:
            streaks[w] = 1 + _streak_of(entries, w)
            fail_counts[w] = 0
        else:
            streaks[w] = 0
            fail_counts[w] = 1 + _fail_streak_of(entries, w)
    entry["session_no"] = existing_max + 1
    entry["pass_streak"] = streaks
    entry["resolved"] = [w for w, s in streaks.items() if s >= 2]
    entries.append(entry)
    store.save(entries)
    store.entry_file.unlink(missing_ok=True)
    print("已记录第 {n} 场：{d} {s}（{m}）{h}".format(
        n=entry["session_no"], d=entry["date"], s=entry["scenario"],
        m=entry["mode"], h=insert_hint))
    if not _is_scored(entry):
        for w in (_weak_list(entry) or []):
            print("  弱项[{w}] 登记风险（无评分场次：不判定过关、不进弱项序列）。".format(w=w))
    for w, s in streaks.items():
        if s >= 2:
            print("  弱项[{w}] 连续过关 {k} 场——已攻克，建议转攻下一弱项。".format(w=w, k=s))
        elif s == 1:
            print("  弱项[{w}] 连续过关 1 场；该弱项下次再练时过关即攻克。".format(w=w))
        else:
            n = fail_counts.get(w, 1)
            if _was_resolved_before(entries[:-1], w):
                if n == 1:
                    print("  弱项[{w}] 本场未过关——曾攻克，复发预警（最近 1 场未过）。".format(w=w))
                elif n == 2:
                    print("  弱项[{w}] 本场未过关——曾攻克，已复发（连续 2 场未过），建议优先重攻。".format(w=w))
                else:
                    print("  弱项[{w}] 本场未过关——曾攻克，已连续 {k} 场未过，持续恶化，立即重攻。".format(w=w, k=n))
            else:
                print("  弱项[{w}] 本场未过关，连续 2 场过关才视为攻克。".format(w=w))
    if entry.get("outcome"):
        print("  已登记真实结果：{o}".format(o=entry["outcome"]))
    return 0


def cmd_latest(store):
    entries = store.load()
    if not entries:
        print("暂无练习记录。")
        return 0
    print(json.dumps(entries[-1], ensure_ascii=False, indent=2))
    return 0


def cmd_summary(store):
    entries = store.load()
    if not entries:
        print("暂无练习记录；commit 第一场后开始积累进步曲线。")
        return 0
    print("累计 {n} 场（{a} → {b}）".format(
        n=len(entries), a=entries[0].get("date"), b=entries[-1].get("date")))
    scored = [e for e in entries if _is_scored(e)]
    if scored:
        print("六维曲线（首场 → 最近，仅评分场次）：")
        for d in DIMENSIONS:
            vals = [e.get("dimension_scores", {}).get(d) for e in scored]
            vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if not vals:
                continue
            trend = "升" if vals[-1] > vals[0] else ("降" if vals[-1] < vals[0] else "平")
            print("  {d}：{f} → {l}（{t}）历史区间 {lo}~{hi}".format(
                d=d, f=vals[0], l=vals[-1], t=trend, lo=min(vals), hi=max(vals)))
    print("弱项攻坚状态（各弱项独立计时；无评分场次不参与；攻克可复发分级显示）：")
    ordered = []
    for e in entries:
        for w, _ in _weak_items(e):
            if w not in ordered:
                ordered.append(w)
    seq = [e for _, e in _ordered_entries(entries)]
    for w in ordered:
        passed_seq = []
        for e in seq:
            m = dict(_weak_items(e))
            if w in m:
                passed_seq.append(bool(m[w]))
        best = run = 0
        for v in passed_seq:
            if v:
                run += 1
                best = max(best, run)
            else:
                run = 0
        cur_pass = 0
        for v in reversed(passed_seq):
            if v:
                cur_pass += 1
            else:
                break
        cur_fail = 0
        for v in reversed(passed_seq):
            if not v:
                cur_fail += 1
            else:
                break
        if cur_pass >= 2:
            status = "已攻克（当前连续 {} 场过关）".format(cur_pass)
        elif best >= 2 and cur_fail >= 3:
            status = "曾攻克，已连续 {} 场未过——持续恶化，立即重攻".format(cur_fail)
        elif best >= 2 and cur_fail == 2:
            status = "曾攻克，近 2 场复发，建议优先重攻"
        elif best >= 2 and cur_fail == 1:
            status = "曾攻克，最近 1 场未过（复发预警）"
        elif best >= 2:
            status = "曾攻克，复发后恢复中（当前连过 {} 场）".format(cur_pass)
        elif cur_pass >= 1:
            status = "当前连续 {}/2".format(cur_pass)
        else:
            status = "尚未过关"
        print("  [{}] 练 {} 场，{}".format(w, len(passed_seq), status))
    fails = [e for e in scored if any(not v for _, v in _weak_items(e))]
    if fails:
        counts = {d: 0 for d in DIMENSIONS}
        skipped_ties = 0
        for e in fails:
            sc = e.get("dimension_scores", {})
            vals = {d: sc.get(d) for d in DIMENSIONS
                    if isinstance(sc.get(d), (int, float)) and not isinstance(sc.get(d), bool)}
            if not vals:
                continue
            lo = min(vals.values())
            lows = [d for d, v in vals.items() if v == lo]
            if len(lows) == 1:
                counts[lows[0]] += 1
            else:
                skipped_ties += 1
        ranked = ["{d} ×{c}".format(d=d, c=c)
                  for d, c in sorted(counts.items(), key=lambda kv: -kv[1]) if c > 0]
        print("fail 归因（{n} 场未过关的评分场次中，各维为全场唯一最低维的次数）：".format(n=len(fails)))
        if ranked:
            print("  " + "；".join(ranked))
            top = sorted(counts.items(), key=lambda kv: -kv[1])[0]
            if len(fails) >= 2:
                print("  → 主因指向 [{d}]：未过关场次里它最常拖底，优先攻它而非换新题。".format(d=top[0]))
        else:
            print("  尚无唯一最低维（并列场次未计入）")
        if skipped_ties:
            print("  另有 {k} 场六维并列最低，无法唯一归因，未计入。".format(k=skipped_ties))
    graded_sim = [e for e in scored if e.get("outcome") in ("pass", "fail") and not _is_real(e)]
    graded_real = [e for e in scored if e.get("outcome") in ("pass", "fail") and _is_real(e)]
    if graded_sim or graded_real:
        print("真实结果校准（模拟与真实分开统计）：")
        for label, group in (("模拟", graded_sim), ("真实面试", graded_real)):
            if not group:
                continue
            buckets = {}
            for e in group:
                g = str(e.get("grade", "-"))
                p, f = buckets.get(g, (0, 0))
                if e.get("outcome") == "pass":
                    p += 1
                else:
                    f += 1
                buckets[g] = (p, f)
            for g in ["S", "A", "B", "C"]:
                if g in buckets:
                    p, f = buckets[g]
                    note = "（样本 <3，仅供参考）" if p + f < 3 else ""
                    print("  " + label + " " + g + " 级 " + str(p + f) + " 场："
                          + str(p) + " 过 " + str(f) + " 挂" + note)
    else:
        print("真实结果校准：尚无回填——真实面试后回来复盘时写入 outcome（或用 backfill "
              "回填到为它练过的模拟场），即可校准模拟分与真实结果的偏差。")
    pending = sum(1 for e in entries if e.get("outcome") == "pending")
    if pending:
        print("  另有 {k} 场结果待定（pending）".format(k=pending))
    last = entries[-1]
    if _is_scored(last):
        print("最近一场：{d} {s}，总分 {t}（{g}）".format(
            d=last.get("date"), s=last.get("scenario"),
            t=last.get("total"), g=last.get("grade", "-")))
    else:
        print("最近一场：{d} {s}（{m}，无评分场次）".format(
            d=last.get("date"), s=last.get("scenario"), m=last.get("mode")))
    if last.get("next_retest"):
        print("上次留下的重考方案：{}".format(last.get("next_retest")))
    return 0


def cmd_export(store):
    entries = store.load()
    if not entries:
        print("暂无记录可导出。")
        return 0
    store.dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%d-%H%M%S")
    out = store.dir / ("log-backup-{}.json".format(ts))
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已导出 {n} 场记录到 {p}".format(n=len(entries), p=out))
    return 0


def cmd_backfill(store, session, outcome, note):
    entries = store.load()
    hits = [e for e in entries if e.get("session_no") == session]
    if not hits:
        _die(1, "未找到 session_no={0} 的记录（可用 latest 查看最近场号）".format(session))
    if len(hits) > 1:
        _die(1, "session_no={0} 存在 {1} 条重复记录，库数据异常，请人工检查后重试".format(
            session, len(hits)))
    hit = hits[0]
    hit["outcome"] = outcome
    if note:
        hit["outcome_note"] = note
    store.save(entries)
    print("已回填第 {n} 场（{s}）的真实结果：{o}".format(
        n=session, s=hit.get("scenario"), o=outcome))
    return 0


def _section(text, heading_re):
    """取标题节内容（到下一个标题为止）；未找到返回 None。"""
    m = re.search(heading_re, text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^#{1,6}\s", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _count_items(seg):
    return [ln for ln in seg.splitlines() if re.match(r"\s*\d+[\.、]", ln)]


def cmd_check_report(path):
    p = Path(path)
    if not p.is_file():
        _die(1, "报告文件不存在：" + path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        _die(1, "报告读取失败：" + str(exc))
    problems = []
    ok = []
    # 六维：解析评分表格行（| 维度 |），正文提一句维度名不再算数
    for d in DIMENSIONS:
        if re.search(r"^\|\s*" + re.escape(d) + r"\s*\|", text, re.M):
            ok.append(d)
        else:
            problems.append("六维评分表缺 [{d}] 行（须为表格行「| {d} | 得分 |…」）".format(d=d))
    # 亮点：兼容「## 亮点 / ## 三大亮点 / ## 亮点（0~3 条）」
    seg = _section(text, r"^##\s*(三大)?\s*亮点")
    if seg is None:
        problems.append("缺「亮点」节（标题须为 ## 亮点（0~3 条）或 ## 三大亮点）")
    else:
        items = _count_items(seg)
        if len(items) > 3:
            problems.append("亮点 {n} 条（上限 3，禁止硬凑）".format(n=len(items)))
        else:
            ok.append("亮点 {n} 条".format(n=len(items)))
    # 改进项：兼容「## 三大改进项 / ## 改进项」；恰好 3 条；每条含三段
    seg = _section(text, r"^##\s*(三大)?\s*改进项")
    if seg is None:
        problems.append("缺「三大改进项」节")
    else:
        items = _count_items(seg)
        if len(items) != 3:
            problems.append("改进项 {n} 条（必须恰好 3 条，不缩水不硬凑）".format(n=len(items)))
        else:
            ok.append("改进项 3 条")
            for kw in ("问题", "示范", "练习建议"):
                if seg.count(kw) < 3:
                    problems.append("改进项三段结构缺 [{kw}]（每条须含 问题/示范改法/练习建议）".format(kw=kw))
            if not any("三段" in x for x in problems):
                ok.append("三段结构齐全")
    # 时长估算标注
    if "估算" not in text and "分钟" in text:
        problems.append("报告含时长但未标注「估算」口径（文字模拟时长均为估算）")
    if problems:
        print("报告校验未通过（{n} 项）：".format(n=len(problems)))
        for x in problems:
            print("  ✗ " + x)
        return 1
    print("报告校验通过：{o}".format(o="；".join(ok)))
    return 0


def main():
    _stdout_utf8()
    parser = argparse.ArgumentParser(description="interview-coach 练习日志管理")
    parser.add_argument("--workspace", default=".", help="用户工作区目录（缺省当前目录）")
    parser.add_argument("command", choices=["commit", "latest", "summary", "export", "backfill", "check-report"])
    parser.add_argument("report_path", nargs="?", default=None, help="check-report 用：报告 md 文件路径")
    parser.add_argument("--session", type=int, default=None, help="backfill 用：目标场次编号")
    parser.add_argument("--outcome", choices=list(OUTCOMES), default=None, help="backfill 用：真实结果")
    parser.add_argument("--note", default="", help="backfill 用：备注（可省略）")
    args = parser.parse_args()
    if args.command == "check-report":
        if not args.report_path:
            _die(1, "check-report 用法：log_manager.py check-report <报告.md路径>")
        return cmd_check_report(args.report_path)
    store = LogStore(args.workspace)
    if args.command == "commit":
        return cmd_commit(store)
    if args.command == "latest":
        return cmd_latest(store)
    if args.command == "summary":
        return cmd_summary(store)
    if args.command == "export":
        return cmd_export(store)
    if args.session is None or args.outcome is None:
        _die(1, "backfill 用法：log_manager.py --workspace <dir> backfill "
                "--session <场号> --outcome <pass|fail|pending> [--note ...]")
    return cmd_backfill(store, args.session, args.outcome, args.note)


if __name__ == "__main__":
    sys.exit(main())
