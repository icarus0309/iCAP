"""判分与结果汇总：纯函数，不进行任何网络与 IO，保证单测可控。"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import defaultdict
from typing import Awaitable, Callable


def _norm(text: str) -> str:
    """统一小写、去首尾空白、NFKC（全角转半角）、压缩中间空白。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", "", text)


def score_case(case: dict, answer: str) -> dict:
    """对单条用例判分，返回 {"points": int, "verdict": "pass|partial|fail", "detail": str}。

    规则（确定性，不执行任何代码）：
    - type=math：answer 中首个数值与 reference 首个数值容差 1e-6 比对；
    - type=code：静态检查 answer_keys（语法词）包含度；参考代码本身也含这些词；
    - type=format：非空且 len(_norm(answer)) <= case.get("max_len", 64)；
    - type=choice/open：answer_keys 关键词包含度（全含→full；命中>=ceil(N/2)→partial）。
    """
    norm = _norm(answer)
    ctype = case["type"]
    maxp = case.get("max_points", 2)
    keys = case.get("answer_keys") or []

    if ctype == "math":
        nums = re.findall(r"\d+(?:\.\d+)?", norm)
        refs = re.findall(r"\d+(?:\.\d+)?", _norm(case["reference"]))
        if nums and refs and abs(float(nums[0]) - float(refs[0])) < 1e-6:
            return {"points": maxp, "verdict": "pass", "detail": "数值一致"}
        return {"points": 0, "verdict": "fail",
                "detail": f"期望 {case['reference']}，回答：{answer[:60]}"}

    if ctype == "code":
        hits = [k for k in keys if _norm(k) in norm]
        if keys and len(hits) == len(keys):
            return {"points": maxp, "verdict": "pass", "detail": "静态语法关键词完整"}
        if hits:
            return {"points": max(1, maxp // 2), "verdict": "partial", "detail": f"命中关键词：{hits}"}
        return {"points": 0, "verdict": "fail", "detail": "未命中任何代码特征关键词"}

    if ctype == "format":
        limit = case.get("max_len", 64)
        if norm and len(norm) <= limit:
            return {"points": maxp, "verdict": "pass", "detail": f"长度合规（{len(norm)} ≤ {limit}）"}
        return {"points": 0, "verdict": "fail", "detail": "长度不合规或空回答"}

    # choice / open
    hits = [k for k in keys if _norm(k) in norm]
    if keys and len(hits) == len(keys):
        return {"points": maxp, "verdict": "pass", "detail": "全部关键词命中"}
    if keys and len(hits) >= (len(keys) + 1) // 2:
        return {"points": max(1, maxp // 2), "verdict": "partial", "detail": f"部分关键词命中：{hits}"}
    return {"points": 0, "verdict": "fail", "detail": "关键词未命中"}


def flatten_cases(dim_cases: dict[str, list[dict]],
                  limit: int | None | dict[str, int] = None) -> list[tuple[str, dict]]:
    """按规格维度顺序取样，展开为 [(dim_key, case), ...]。

    `limit` 支持三种形式：
    - `None`：每个维度取全部样例；
    - `int`：每个维度取前 N 条（旧的统一口径，保留兼容）；
    - `dict[dim_key, N]`：逐维度指定取样条数，未列出的维度取全部。

    若某维度实际条数少于指定条数，以实际条数执行（不报错）；输出前做一次 id 唯一性校验。
    """
    if isinstance(limit, dict):
        limits = {key: value for key, value in limit.items()}
        for dim_key, value in limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"维度 {dim_key} 的取样条数至少为 1")
    else:
        if limit is not None and limit < 1:
            raise ValueError("sample_limit 至少为 1")
        limits = None
    flat: list[tuple[str, dict]] = []
    for dim_key, cases in dim_cases.items():
        if limits is None:
            picked = cases if limit is None else cases[:limit]
        else:
            picked = cases[:limits[dim_key]] if dim_key in limits else cases
        for case in picked:
            if not case.get("id"):
                raise ValueError(f"用例缺少 id：{case}")
            flat.append((dim_key, case))
    ids = [c["id"] for _, c in flat]
    if len(ids) != len(set(ids)):
        raise ValueError("样例 id 必须唯一")
    return flat


#: 答题函数注入点：async (case) -> answer str
EvaluateFn = Callable[[dict], Awaitable[str]]


async def run_cases(
    cases: list[tuple[str, dict]],
    answer_fn: EvaluateFn,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    on_case_done: Callable[[int, dict], None] | None = None,
) -> list[dict]:
    """顺序执行用例。

    - cases: flatten_cases 的输出（保证顺序 = 维度内顺序）；
    - answer_fn: 注入的答题函数（demo/live）；
    - should_stop / should_pause: 外部状态检查回调（由 worker 传入，读取任务状态；
      should_stop 除任务取消外还应感知全局 stop 事件）；
    - on_case_done: 每个用例完成后回调 (已完成条数, case_result)，供写进度/日志。

    返回 [{"dimension","index","id","type","max_points","prompt","reference","answer",
    "points","verdict","detail"}...]。
    结束语义：
    - 遍历完毕 → 正常返回全部 results；
    - should_stop 为真（任务取消或应用关闭）→ break 提前返回（已是结果的部分保留，
      由 worker 判定为 cancelled 而丢弃，不生成报告）。
    """
    results: list[dict] = []
    done = 0
    for dim, case in cases:
        if should_stop and should_stop():
            break                                     # cancelled/关闭：提前结束（不算失败）
        while should_pause and should_pause():         # paused：原地等待，进程内轮询
            await asyncio.sleep(0.2)
            if should_stop and should_stop():
                break
        if should_stop and should_stop():              # 暂停等待退出后复查：应用关闭/取消时不再发起作答
            break
        answer = await answer_fn(case)
        scored = score_case(case, answer)
        record = {"dimension": dim, "index": done, "id": case["id"], "type": case.get("type"),
                  "max_points": case.get("max_points", 2),
                  "prompt": case.get("prompt"), "reference": case.get("reference"),
                  "answer": answer, **scored}
        results.append(record)
        done += 1
        if on_case_done:
            on_case_done(done, record)
    return results


def build_result(ruler_spec, case_results: list[dict]) -> dict:
    """汇总维度分与综合分。

    - 维度分 = 该维度 case 得分和 / 满分和 * 100（保留 1 位）；
    - 综合分 total = Σ(维度分/100 * weight) * 100（保留 1 位）；
    - summary：按 total 档位生成一句中文结论（<60 待提升 / <80 良好 / else 优秀）。
    返回 {"total", "dimensions", "cases", "summary"}。
    """
    by_dim: dict[str, list[dict]] = defaultdict(list)
    for r in case_results:
        by_dim[r["dimension"]].append(r)

    dimensions = []
    for d in ruler_spec.dimensions:
        rows = by_dim.get(d["key"], [])
        total_points = sum(r["points"] for r in rows)
        max_points = sum(r.get("max_points", 2) for r in rows) or 1
        dimensions.append({
            "key": d["key"], "label": d["label"], "weight": round(d["weight"], 4),
            "score": round(total_points / max_points * 100, 1),
            "case_total": len(rows), "case_passed": sum(1 for r in rows if r["verdict"] == "pass"),
        })

    total = round(sum(d["score"] / 100 * d["weight"] for d in dimensions) * 100, 1)
    if total >= 80:
        conclusion = "综合能力强，处于优秀档位。"
    elif total >= 60:
        conclusion = "综合能力良好，仍有提升空间。"
    else:
        conclusion = "综合能力有待提升，建议针对性优化与重测。"
    summary = f"{ruler_spec.label}（{ruler_spec.scope}）完成 {len(case_results)} 条用例：{conclusion}"
    return {"total": total, "dimensions": dimensions, "cases": case_results, "summary": summary}
