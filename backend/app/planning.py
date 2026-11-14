"""课程阅读规划：目标先修成环检测、先修闭包、确定性贪心近似选章与未覆盖诊断。

核心约束（对应需求）：
- 先修关系成环时先指出涉及目标，不产出规划；
- 先补齐所需先修（先修闭包整体进入候选目标集）；
- 逐次按「新增覆盖权重 ÷ 新增篇幅」选章，已选章节不重复计费；
- 章节篇幅未知者不可自动选入，不能当作零成本；
- 教研员可锁定必读章与已有基础；锁定内容超出阅读上限时保留冲突，
  不删除必读内容凑可行；
- 未覆盖目标逐项区分缺教材 / 缺先修 / 预算不足，并给出补齐所需增加的阅读量；
- 结果是明确算法得到的近似方案，不标注为最短阅读路线；
- 后台作业带令牌与取消标志，取消后返回的旧计算不得覆盖当前选择。

算法主体 ``run_planning`` 是不接触数据库的纯函数，便于离线复现与单元测试。
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Callable

from . import text as textmod
from .db import tx


class PlanningError(Exception):
    """规划输入不合法（目标不存在、版本不可用等）。"""


class CycleError(PlanningError):
    def __init__(self, cycles: list[list[str]]):
        self.cycles = cycles
        super().__init__("先修依赖成环：" + "；".join(
            " → ".join(c) for c in cycles))


class JobCancelled(Exception):
    """后台作业被取消，结果必须丢弃。"""


ALGORITHM_NOTE = (
    "本方案由确定性贪心近似算法生成：每轮在未选且篇幅已知的章节中，选取"
    "「新增覆盖权重 ÷ 新增篇幅」最高、且加入后不超过阅读上限的章节；"
    "已选章节不重复计费，篇幅未知章节不自动选入，锁定必读章优先保留。"
    "结果为近似方案，不保证、也不标注为最短阅读路线。"
)


# ============================================================
# 先修图：Tarjan 强连通分量找成环
# ============================================================

def find_cycles(prereq_map: dict[int, set[int]],
                code_map: dict[int, str]) -> list[list[str]]:
    """Tarjan SCC：节点数 > 1 的强连通分量即依赖回路（自环已由库表 CHECK 禁止）。"""
    index: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    counter = 0
    sccs: list[list[int]] = []

    def strongconnect(v: int) -> None:
        nonlocal counter
        index[v] = lowlink[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in prereq_map.get(v, ()):  # 边 v -> 先修 w
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp: list[int] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    for v in list(prereq_map.keys()):
        if v not in index:
            strongconnect(v)

    cycles: list[list[str]] = []
    for comp in sccs:
        if len(comp) > 1:
            codes = sorted(code_map.get(i, str(i)) for i in comp)
            cycles.append(codes + [codes[0]])  # 首尾相连展示回路
    return cycles


def prerequisite_closure(requested: list[int],
                         prereq_map: dict[int, set[int]]) -> set[int]:
    """先修闭包：目标本身 + 全部传递先修，成环也能终止（visited 去重）。"""
    seen: set[int] = set()
    stack = list(requested)
    while stack:
        v = stack.pop()
        if v in seen:
            continue
        seen.add(v)
        stack.extend(prereq_map.get(v, ()))
    return seen


# ============================================================
# 纯函数规划算法
# ============================================================

def _chapter_key(link_or_metric: dict) -> tuple[int, str]:
    return link_or_metric["edition_id"], link_or_metric["chapter_code"]


def run_planning(snapshot: dict, requested_ids: list[int],
                 required_locks: list[tuple[int, str]],
                 known_locks: list[tuple[int, str]],
                 budget: int,
                 is_cancelled: Callable[[], bool] | None = None) -> dict:
    """对固定快照执行规划。

    snapshot 键：
      objectives: [{objective_id, code, title, prereq_ids}]
      links:      [{objective_id, edition_id, chapter_code, weight}]  # 仅已审
      metrics:    [{edition_id, chapter_code, chapter_path, reading_length|null}]
    """
    is_cancelled = is_cancelled or (lambda: False)
    objectives = snapshot["objectives"]
    links = snapshot["links"]
    metrics = snapshot["metrics"]

    obj_by_id = {o["objective_id"]: o for o in objectives}
    code_map = {o["objective_id"]: o["code"] for o in objectives}
    prereq_map: dict[int, set[int]] = {
        o["objective_id"]: set(o.get("prereq_ids", [])) for o in objectives}

    length_map: dict[tuple[int, str], int | None] = {}
    path_map: dict[tuple[int, str], str] = {}
    for m in metrics:
        key = _chapter_key(m)
        length_map[key] = m["reading_length"]
        path_map[key] = m.get("chapter_path", f"{m['edition_id']}:{m['chapter_code']}")

    # 章节 -> 目标权重；目标 -> 章节集合
    chapter_weights: dict[tuple[int, str], dict[int, float]] = {}
    obj_chapters: dict[int, list[tuple[int, str]]] = {}
    for lk in links:
        key = _chapter_key(lk)
        chapter_weights.setdefault(key, {})[lk["objective_id"]] = float(lk["weight"])
        obj_chapters.setdefault(lk["objective_id"], []).append(key)

    warnings: list[str] = []

    # 1) 先修闭包与成环检测（回路涉及闭包内目标才阻断本计划）
    closure = prerequisite_closure(requested_ids, prereq_map)
    all_cycles = find_cycles(prereq_map, code_map)
    cycles = [c for c in all_cycles
              if any(code_map.get(i) in c for i in closure)]
    if cycles:
        return {"status": "failed", "cycle_codes": cycles,
                "error": "先修依赖成环，涉及目标：" + "；".join(
                    " → ".join(c) for c in cycles)}

    selected: dict[tuple[int, str], dict] = {}
    covered: set[int] = set()
    total_length = 0

    def add_chapter(key: tuple[int, str], reason: str, ratio: float | None,
                    added_weight: float | None, charged: bool) -> None:
        nonlocal total_length
        if key in selected:
            return
        length = length_map.get(key)
        if charged and length is not None:
            total_length += length  # 已选章节不重复计费：仅首次选入计费
        selected[key] = {
            "edition_id": key[0], "chapter_code": key[1],
            "chapter_path": path_map.get(key, key[1]),
            "reading_length": length,
            "reason": reason, "ratio": round(ratio, 4) if ratio is not None else None,
            "added_weight": added_weight,
            "charged_length": charged and length is not None,
        }
        for oid in chapter_weights.get(key, {}):
            covered.add(oid)

    # 2) 已有基础：锁定章节覆盖的目标视为已掌握，不计阅读量
    for key in known_locks:
        if key in required_locks:
            warnings.append(
                f"章节 {path_map.get(key, key)} 同时被锁定为必读与已有基础，按必读处理并计入篇幅。")
            continue
        if key not in length_map:
            warnings.append(f"已有基础章节 {key} 不在计划固定版本目录内，已忽略。")
            continue
        add_chapter(key, "already_known", None, None, charged=False)

    # 3) 必读锁定：无条件保留，篇幅未知也保留但无法计入预算
    for key in required_locks:
        if key not in length_map:
            warnings.append(
                f"必读章节 {key} 不在计划固定版本目录内，仍保留锁定但无法核算篇幅。")
            length_map.setdefault(key, None)
            path_map.setdefault(key, f"{key[0]}:{key[1]}")
        add_chapter(key, "required", None, None, charged=True)
        if length_map.get(key) is None:
            warnings.append(
                f"必读章节 {path_map.get(key, key)} 篇幅未知，无法计入阅读预算，"
                "已按锁定保留且不自动补选其他章节替代。")

    # 4) 贪心选章（纯函数，便于复现）
    def greedy(cap: int | None) -> tuple[dict, set[int], int, list[dict]]:
        sel = dict(selected)
        cov = set(covered)
        cost = total_length
        trace: list[dict] = []
        while True:
            need = closure - cov
            if not need:
                break
            if is_cancelled():
                raise JobCancelled()
            best: dict | None = None
            for key, weights in chapter_weights.items():
                if key in sel:
                    continue
                length = length_map.get(key)
                if length is None:
                    continue  # 篇幅未知不可自动选入，不能当零成本
                added = sum(w for oid, w in weights.items() if oid in need)
                if added <= 0:
                    continue
                if cap is not None and cost + length > cap:
                    continue  # 超预算章节本轮跳过，给更小的高性价比章节机会
                ratio = added / length
                cand = {"key": key, "length": length, "added": added, "ratio": ratio}
                if best is None or (-ratio, length, key) < (
                        -best["ratio"], best["length"], best["key"]):
                    best = cand
            if best is None:
                break
            key = best["key"]
            sel[key] = {
                "edition_id": key[0], "chapter_code": key[1],
                "chapter_path": path_map.get(key, key[1]),
                "reading_length": best["length"], "reason": "greedy",
                "ratio": round(best["ratio"], 4), "added_weight": round(best["added"], 2),
                "charged_length": True,
            }
            cost += best["length"]
            cov.update(chapter_weights[key].keys())
            trace.append({"chapter_path": path_map.get(key, key[1]),
                          "added_weight": round(best["added"], 2),
                          "length": best["length"], "ratio": round(best["ratio"], 4)})
        return sel, cov, cost, trace

    if total_length > budget:
        warnings.append(
            f"锁定必读章节合计 {total_length} 字已超出阅读上限 {budget} 字："
            "保留全部必读内容，不删除必读章节凑可行；超出期间不再自动选入新章节。")

    sel, cov, cost, trace = greedy(budget)
    selected, covered, total_length = sel, cov, cost

    def effective_coverage(sel_keys: set[tuple[int, str]]) -> set[int]:
        """有效覆盖：目标自身章节已选，且全部先修（递归）也有效覆盖。

        仅机械命中而先修未覆盖的目标不算覆盖，诊断为“缺先修”，
        避免向尚未学先修的学生推荐后续章节。
        """
        mechanical = {oid for key in sel_keys
                      for oid in chapter_weights.get(key, {})}
        effective: set[int] = set()
        changed = True
        while changed:
            changed = False
            for oid in closure:
                if oid in effective:
                    continue
                if oid in mechanical and prereq_map.get(oid, set()) <= effective:
                    effective.add(oid)
                    changed = True
        return effective

    effective = effective_coverage(set(selected.keys()))

    # 5) 无预算上限再跑一遍，得到“补齐全部可覆盖目标”需要的总阅读量
    unlimited_sel, _, unlimited_cost, _ = greedy(float("inf"))
    unlimited_effective = effective_coverage(set(unlimited_sel.keys()))
    full_coverable = closure <= unlimited_effective
    extra_to_full = (max(0, unlimited_cost - budget)
                     if full_coverable else None)

    # 6) 未覆盖目标逐项诊断
    uncovered: list[dict] = []
    remaining_budget = max(0, budget - total_length)
    for oid in sorted(closure - effective):
        obj = obj_by_id[oid]
        reasons: list[str] = []
        detail: dict[str, Any] = {
            "objective_id": oid, "code": obj["code"], "title": obj["title"],
            "is_requested": oid in set(requested_ids),
            "reasons": reasons, "reason_labels": [],
            "prereq_missing": [], "cycle_codes": [],
            "candidate_chapters": [],
            "cheapest_selectable": None, "extra_length_to_cover": None,
            "remaining_budget": remaining_budget,
        }
        in_cycle = any(code_map.get(oid) in c for c in all_cycles)
        if in_cycle:
            reasons.append("cycle")
            detail["reason_labels"].append("缺先修：先修依赖成环，无法确定学习顺序")
            detail["cycle_codes"] = next(
                c for c in all_cycles if code_map.get(oid) in c)

        missing_prereqs = [
            code_map[p] for p in prereq_map.get(oid, set())
            if p not in effective and p in closure]
        if missing_prereqs:
            reasons.append("missing_prereq")
            detail["prereq_missing"] = sorted(missing_prereqs)
            detail["reason_labels"].append(
                "缺先修：先修目标 " + "、".join(sorted(missing_prereqs)) + " 尚未有效覆盖，"
                "该目标即使章节已选也不计入覆盖")

        chapters = obj_chapters.get(oid, [])
        known_length_chapters = [k for k in chapters if length_map.get(k) is not None]
        if not chapters:
            reasons.append("no_material")
            detail["reason_labels"].append("缺教材：已审覆盖关联中没有任何章节覆盖该目标")
        elif not known_length_chapters:
            reasons.append("no_material")
            detail["reason_labels"].append(
                "缺教材：关联章节 " + "、".join(path_map.get(k, k[1]) for k in chapters)
                + " 篇幅均未知，不可自动选入")

        for k in chapters:
            detail["candidate_chapters"].append({
                "edition_id": k[0], "chapter_code": k[1],
                "chapter_path": path_map.get(k, k[1]),
                "reading_length": length_map.get(k),
                "excluded_reason": (
                    "篇幅未知，不可自动选入" if length_map.get(k) is None
                    else ("剩余阅读量不足" if length_map[k] > remaining_budget
                          else "未被贪心选中")),
            })
        if known_length_chapters:
            cheap_key = min(known_length_chapters, key=lambda k: length_map[k])
            cheap_len = length_map[cheap_key]
            detail["cheapest_selectable"] = {
                "edition_id": cheap_key[0], "chapter_code": cheap_key[1],
                "chapter_path": path_map.get(cheap_key, cheap_key[1]),
                "reading_length": cheap_len}
            detail["extra_length_to_cover"] = max(0, cheap_len - remaining_budget)
            if not reasons:
                reasons.append("budget")
                detail["reason_labels"].append(
                    f"预算不足：最省的覆盖章节还需 {cheap_len} 字，"
                    f"当前仅剩 {remaining_budget} 字，至少再增加 "
                    f"{detail['extra_length_to_cover']} 字可直接补齐该目标")
        uncovered.append(detail)

    return {
        "status": "ready",
        "selected": list(selected.values()),
        "total_length": total_length,
        "covered_objective_ids": sorted(effective),
        "uncovered": uncovered,
        "warnings": warnings,
        "greedy_trace": trace,
        "extra_to_full_coverage": extra_to_full,
        "remaining_budget": remaining_budget,
        "algorithm_note": ALGORITHM_NOTE,
    }


# ============================================================
# 目标与先修关系
# ============================================================

def list_objectives(conn, subject: str | None = None) -> list[dict]:
    sql = ("SELECT o.*, coalesce(array_remove(array_agg(p.code) "
           "FILTER (WHERE p.code IS NOT NULL), NULL), '{}') AS prereq_codes "
           "FROM learning_objectives o "
           "LEFT JOIN objective_prereqs r ON r.objective_id=o.objective_id "
           "LEFT JOIN learning_objectives p ON p.objective_id=r.requires_objective_id ")
    params: tuple = ()
    if subject:
        sql += "WHERE o.subject=%s "
        params = (subject,)
    sql += "GROUP BY o.objective_id ORDER BY o.code"
    rows = conn.execute(sql, params).fetchall()
    out = [dict(r) for r in rows]
    cycles = _all_cycles(conn)
    cyclic = {code for cyc in cycles for code in cyc}
    for r in out:
        r["in_cycle"] = r["code"] in cyclic
    return out


def _all_cycles(conn) -> list[list[str]]:
    objs = conn.execute(
        "SELECT objective_id, code FROM learning_objectives").fetchall()
    code_map = {r["objective_id"]: r["code"] for r in objs}
    prereq_map: dict[int, set[int]] = {oid: set() for oid in code_map}
    for r in conn.execute(
            "SELECT objective_id, requires_objective_id FROM objective_prereqs"):
        prereq_map.setdefault(r["objective_id"], set()).add(r["requires_objective_id"])
    return find_cycles(prereq_map, code_map)


def create_objective(conn, req, username: str) -> dict:
    if req.code in req.prereq_codes:
        raise PlanningError("目标不能以自身为先修")
    row = conn.execute(
        "INSERT INTO learning_objectives(code, title, subject, stage, description, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
        (req.code, req.title, req.subject, req.stage, req.description, username),
    ).fetchone()
    for prereq_code in req.prereq_codes:
        pre = conn.execute("SELECT objective_id FROM learning_objectives WHERE code=%s",
                           (prereq_code,)).fetchone()
        if not pre:
            raise PlanningError(f"先修目标 {prereq_code} 不存在，请先录入")
        conn.execute(
            "INSERT INTO objective_prereqs(objective_id, requires_objective_id) "
            "VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (row["objective_id"], pre["objective_id"]))
    cycles = _all_cycles(conn)
    if cycles:
        # 仅当新目标自身或其先修闭包落入回路时才拒绝；库中其他无关回路不应阻断录入
        full_map: dict[int, set[int]] = {}
        for r in conn.execute(
                "SELECT objective_id, requires_objective_id FROM objective_prereqs"):
            full_map.setdefault(r["objective_id"], set()).add(r["requires_objective_id"])
        closure = prerequisite_closure([row["objective_id"]], full_map)
        id_to_code = {r["objective_id"]: r["code"] for r in conn.execute(
            "SELECT objective_id, code FROM learning_objectives").fetchall()}
        blocking = [c for c in cycles
                    if any(id_to_code.get(oid) in c for oid in closure)]
        if blocking:
            raise CycleError(blocking)
    return get_objective(conn, row["objective_id"])


def get_objective(conn, objective_id: int) -> dict:
    row = conn.execute(
        "SELECT o.*, coalesce(array_remove(array_agg(p.code) "
        "FILTER (WHERE p.code IS NOT NULL), NULL), '{}') AS prereq_codes "
        "FROM learning_objectives o "
        "LEFT JOIN objective_prereqs r ON r.objective_id=o.objective_id "
        "LEFT JOIN learning_objectives p ON p.objective_id=r.requires_objective_id "
        "WHERE o.objective_id=%s GROUP BY o.objective_id",
        (objective_id,)).fetchone()
    if not row:
        raise PlanningError("目标不存在")
    return dict(row)


# ============================================================
# 搜索候选（未审核命中只作候选，不直接参与规划）
# ============================================================

def chapter_candidates(conn, objective_code: str,
                       edition_ids: list[int], limit: int = 8) -> list[dict]:
    obj = conn.execute("SELECT * FROM learning_objectives WHERE code=%s",
                       (objective_code,)).fetchone()
    if not obj:
        raise PlanningError(f"目标 {objective_code} 不存在")
    gens = conn.execute(
        "SELECT e.edition_id, g.generation_id FROM textbook_editions e "
        "JOIN index_generations g ON g.edition_id=e.edition_id "
        "WHERE e.edition_id = ANY(%s) AND g.status IN ('ready','archived')",
        (edition_ids,)).fetchall()
    if not gens:
        return []
    gen_ids = [g["generation_id"] for g in gens]
    norm, _ = textmod.normalize(f"{obj['title']} {obj['description']}".strip())
    terms = sorted(set(textmod.query_terms(norm)))
    if not terms:
        return []
    postings = conn.execute(
        "SELECT p.unit_id, p.term, p.tf FROM postings p "
        "WHERE p.generation_id = ANY(%s) AND p.term = ANY(%s)",
        (gen_ids, terms)).fetchall()
    units = conn.execute(
        "SELECT unit_id, edition_id, generation_id, chapter_code, section_path, title_path "
        "FROM structural_units WHERE generation_id = ANY(%s)",
        (gen_ids,)).fetchall()
    unit_map = {u["unit_id"]: dict(u) for u in units}
    agg: dict[tuple[int, str], dict] = {}
    for p in postings:
        u = unit_map.get(p["unit_id"])
        if not u:
            continue
        key = (u["edition_id"], u["chapter_code"])
        bucket = agg.setdefault(key, {"score": 0, "terms": set(), "units": set(),
                                      "chapter_path": u["section_path"].split(" / ")[0]})
        bucket["score"] += p["tf"]
        bucket["terms"].add(p["term"])
        bucket["units"].add(u["title_path"])
    existing = {
        (r["edition_id"], r["chapter_code"]) for r in conn.execute(
            "SELECT edition_id, chapter_code FROM objective_chapter_links "
            "WHERE objective_id=%s", (obj["objective_id"],)).fetchall()}
    out = []
    for (edition_id, chapter_code), b in agg.items():
        if (edition_id, chapter_code) in existing:
            continue
        out.append({"edition_id": edition_id, "chapter_code": chapter_code,
                    "chapter_path": b["chapter_path"], "score": b["score"],
                    "matched_terms": sorted(b["terms"]),
                    "matched_units": len(b["units"])})
    out.sort(key=lambda r: (-r["score"], r["edition_id"], r["chapter_code"]))
    return out[:limit]


def upsert_link(conn, body: dict, username: str) -> dict:
    obj = conn.execute("SELECT * FROM learning_objectives WHERE code=%s",
                       (body["objective_code"],)).fetchone()
    if not obj:
        raise PlanningError(f"目标 {body['objective_code']} 不存在")
    edition = conn.execute(
        "SELECT e.* FROM textbook_editions e JOIN index_generations g "
        "ON g.edition_id=e.edition_id WHERE e.edition_id=%s "
        "AND g.status IN ('ready','archived')",
        (body["edition_id"],)).fetchone()
    if not edition:
        raise PlanningError("版本不存在或其索引不可用，不能建立覆盖关联")
    origin = body.get("origin", "manual")
    if origin not in ("manual", "search_candidate"):
        raise PlanningError("origin 仅支持 manual / search_candidate")
    row = conn.execute(
        "INSERT INTO objective_chapter_links"
        "(objective_id, edition_id, chapter_code, weight, status, origin, evidence, created_by) "
        "VALUES (%s,%s,%s,%s,'pending',%s,%s,%s) "
        "ON CONFLICT (objective_id, edition_id, chapter_code) DO UPDATE SET "
        "weight=EXCLUDED.weight, origin=EXCLUDED.origin, evidence=EXCLUDED.evidence, "
        "status='pending', reviewed_by=NULL, reviewed_at=NULL "
        "RETURNING *",
        (obj["objective_id"], body["edition_id"], body["chapter_code"],
         body.get("weight", 1.0), origin,
         json.dumps(body.get("evidence", {}), ensure_ascii=False), username),
    ).fetchone()
    return _link_dict(conn, row)


def review_link(conn, link_id: int, decision: str, username: str) -> dict:
    if decision not in ("approved", "rejected"):
        raise PlanningError("status 必须是 approved 或 rejected")
    row = conn.execute(
        "UPDATE objective_chapter_links SET status=%s, reviewed_by=%s, reviewed_at=now() "
        "WHERE link_id=%s RETURNING *", (decision, username, link_id)).fetchone()
    if not row:
        raise PlanningError("覆盖关联不存在")
    return _link_dict(conn, row)


def list_links(conn, objective_code: str | None, status: str | None) -> list[dict]:
    sql = ("SELECT l.*, o.code AS objective_code, o.title AS objective_title "
           "FROM objective_chapter_links l JOIN learning_objectives o "
           "ON o.objective_id=l.objective_id WHERE 1=1 ")
    params: list[Any] = []
    if objective_code:
        sql += "AND o.code=%s "
        params.append(objective_code)
    if status and status != "all":
        sql += "AND l.status=%s "
        params.append(status)
    sql += "ORDER BY l.link_id DESC"
    return [_link_dict(conn, r) for r in conn.execute(sql, params).fetchall()]


def _link_dict(conn, row: dict) -> dict:
    obj = conn.execute("SELECT code FROM learning_objectives WHERE objective_id=%s",
                       (row["objective_id"],)).fetchone()
    d = dict(row)
    d["objective_code"] = d.get("objective_code") or (obj["code"] if obj else "")
    return d


# ============================================================
# 章节篇幅
# ============================================================

def list_metrics(conn, edition_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.edition_id, s.chapter_code, s.chapter_path, m.reading_length, m.source "
        "FROM chapter_snapshots s LEFT JOIN chapter_metrics m "
        "ON m.edition_id=s.edition_id AND m.chapter_code=s.chapter_code "
        "WHERE s.edition_id=%s ORDER BY s.ordinal, s.chapter_code",
        (edition_id,)).fetchall()
    return [dict(r) for r in rows]


def upsert_metric(conn, edition_id: int, chapter_code: str,
                  reading_length: int | None, username: str) -> dict:
    exists = conn.execute(
        "SELECT 1 FROM chapter_snapshots WHERE edition_id=%s AND chapter_code=%s",
        (edition_id, chapter_code)).fetchone()
    if not exists:
        raise PlanningError("该版本目录中不存在此章节")
    row = conn.execute(
        "INSERT INTO chapter_metrics(edition_id, chapter_code, reading_length, source, updated_by) "
        "VALUES (%s,%s,%s,'manual',%s) "
        "ON CONFLICT (edition_id, chapter_code) DO UPDATE SET "
        "reading_length=EXCLUDED.reading_length, source='manual', updated_by=EXCLUDED.updated_by, "
        "updated_at=now() RETURNING *",
        (edition_id, chapter_code, reading_length, username)).fetchone()
    return dict(row)


# ============================================================
# 计划创建、快照与后台作业
# ============================================================

def _editions_for_subject(conn, subject: str) -> list[dict]:
    return conn.execute(
        "SELECT e.edition_id, e.generation, e.isbn, e.status, e.supersede_reason, "
        "t.title FROM textbook_editions e JOIN textbooks t ON t.isbn=e.isbn "
        "JOIN index_generations g ON g.edition_id=e.edition_id "
        "WHERE t.subject=%s AND e.status='active' AND g.status='ready' ORDER BY e.edition_id",
        (subject,)).fetchall()


def create_plan(conn, req, username: str, spawn: bool = True) -> dict:
    objectives = conn.execute(
        "SELECT * FROM learning_objectives WHERE code = ANY(%s) ORDER BY code",
        (req.objective_codes,)).fetchall()
    found = {o["code"]: o for o in objectives}
    missing = sorted(set(req.objective_codes) - set(found))
    if missing:
        raise PlanningError("教学目标不存在：" + "、".join(missing))
    subjects = {o["subject"] for o in objectives}
    if len(subjects) != 1:
        raise PlanningError("同一计划的教学目标必须属于同一学科")
    subject = next(iter(subjects))

    cycles = _all_cycles(conn)
    full_map = {o["objective_id"]: set(o["prereq_ids"]) for o in
                conn.execute(
                    "SELECT o.objective_id, coalesce(array_agg(r.requires_objective_id) "
                    "FILTER (WHERE r.requires_objective_id IS NOT NULL), '{}') AS prereq_ids "
                    "FROM learning_objectives o LEFT JOIN objective_prereqs r "
                    "ON r.objective_id=o.objective_id GROUP BY o.objective_id").fetchall()}
    closure_ids = prerequisite_closure(
        [found[c]["objective_id"] for c in req.objective_codes], full_map)
    id_to_code = {o["objective_id"]: o["code"] for o in objectives}
    blocking = [c for c in cycles
                if any(id_to_code.get(oid) in c for oid in closure_ids)]
    if blocking:
        raise CycleError(blocking)

    if req.edition_ids:
        editions = conn.execute(
            "SELECT e.*, g.status AS gen_status FROM textbook_editions e "
            "JOIN index_generations g ON g.edition_id=e.edition_id "
            "WHERE e.edition_id = ANY(%s)", (req.edition_ids,)).fetchall()
        if len(editions) != len(set(req.edition_ids)):
            raise PlanningError("存在无效的教材版本")
        bad = [e["edition_id"] for e in editions if e["gen_status"] not in
               ("ready", "archived")]
        if bad:
            raise PlanningError(f"版本 {bad} 索引不可用，无法固定到计划")
        if any(e["subject"] != subject for e in editions):
            raise PlanningError("教材版本与目标学科不一致")
    else:
        editions = _editions_for_subject(conn, subject)
        if not editions:
            raise PlanningError("该学科当前没有现行可用教材版本")
    edition_ids = [e["edition_id"] for e in editions]

    # 快照：目标全图（含先修）、仅已审关联、目录与篇幅（含未知）
    obj_snapshot = [
        {"objective_id": o["objective_id"], "code": o["code"], "title": o["title"],
         "prereq_ids": [r["requires_objective_id"] for r in conn.execute(
             "SELECT requires_objective_id FROM objective_prereqs "
             "WHERE objective_id=%s", (o["objective_id"],)).fetchall()]}
        for o in conn.execute("SELECT * FROM learning_objectives ORDER BY objective_id")
        .fetchall()]
    link_rows = conn.execute(
        "SELECT objective_id, edition_id, chapter_code, weight "
        "FROM objective_chapter_links WHERE status='approved' "
        "AND edition_id = ANY(%s)", (edition_ids,)).fetchall()
    links_snapshot = [{**dict(r), "weight": float(r["weight"])} for r in link_rows]
    metrics_snapshot = list_metrics(conn, edition_ids[0])
    for eid in edition_ids[1:]:
        metrics_snapshot.extend(list_metrics(conn, eid))

    plan = conn.execute(
        "INSERT INTO reading_plans(name, subject, reading_budget, status, created_by, "
        " edition_ids, links_snapshot, objectives_snapshot, metrics_snapshot, job_token) "
        "VALUES (%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s) RETURNING *",
        (req.name, subject, req.reading_budget, username, edition_ids,
         json.dumps(links_snapshot, ensure_ascii=False),
         json.dumps(obj_snapshot, ensure_ascii=False),
         json.dumps(metrics_snapshot, ensure_ascii=False),
         str(uuid.uuid4()))).fetchone()

    for code in req.objective_codes:
        conn.execute(
            "INSERT INTO plan_objectives(plan_id, objective_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING", (plan["plan_id"], found[code]["objective_id"]))
    _replace_locks(conn, plan["plan_id"], req.locked_required, req.locked_known)
    conn.execute(
        "INSERT INTO plan_events(plan_id, event_type, detail, created_by) "
        "VALUES (%s,'created','计划创建，目标与已审关联、教材版本已快照固定',%s)",
        (plan["plan_id"], username))
    conn.commit()

    if spawn:
        spawn_plan_job(plan["plan_id"])
    return get_plan(conn, plan["plan_id"])


def _replace_locks(conn, plan_id: int, required: list[dict],
                   known: list[dict]) -> None:
    conn.execute("DELETE FROM plan_locked_chapters WHERE plan_id=%s", (plan_id,))
    for item in required or []:
        conn.execute(
            "INSERT INTO plan_locked_chapters(plan_id, edition_id, chapter_code, lock_type) "
            "VALUES (%s,%s,%s,'required') ON CONFLICT DO NOTHING",
            (plan_id, item["edition_id"], item["chapter_code"]))
    for item in known or []:
        conn.execute(
            "INSERT INTO plan_locked_chapters(plan_id, edition_id, chapter_code, lock_type) "
            "VALUES (%s,%s,%s,'already_known') ON CONFLICT DO NOTHING",
            (plan_id, item["edition_id"], item["chapter_code"]))


def _load_plan_inputs(conn, plan: dict) -> tuple[list[int], list[tuple[int, str]],
                                                 list[tuple[int, str]]]:
    requested = [r["objective_id"] for r in conn.execute(
        "SELECT objective_id FROM plan_objectives WHERE plan_id=%s ORDER BY objective_id",
        (plan["plan_id"],)).fetchall()]
    required = [(r["edition_id"], r["chapter_code"]) for r in conn.execute(
        "SELECT edition_id, chapter_code FROM plan_locked_chapters "
        "WHERE plan_id=%s AND lock_type='required'", (plan["plan_id"],)).fetchall()]
    known = [(r["edition_id"], r["chapter_code"]) for r in conn.execute(
        "SELECT edition_id, chapter_code FROM plan_locked_chapters "
        "WHERE plan_id=%s AND lock_type='already_known'", (plan["plan_id"],)).fetchall()]
    return requested, required, known


def spawn_plan_job(plan_id: int) -> None:
    threading.Thread(target=execute_plan_job, args=(plan_id,),
                     daemon=True, name=f"reading-plan-{plan_id}").start()


def execute_plan_job(plan_id: int) -> None:
    """后台作业执行体（也可同步调用，便于播种与测试）。

    独立连接；令牌不匹配或已取消时，计算结果一律丢弃。
    """
    try:
        with tx() as conn:
            plan = conn.execute(
                "SELECT * FROM reading_plans WHERE plan_id=%s FOR UPDATE",
                (plan_id,)).fetchone()
            if not plan or plan["status"] != "queued" or plan["cancel_requested"]:
                return
            token = plan["job_token"]
            conn.execute(
                "UPDATE reading_plans SET status='running' "
                "WHERE plan_id=%s AND job_token=%s", (plan_id, token))
            conn.commit()

            requested, required, known = _load_plan_inputs(conn, plan)
            snapshot = {"objectives": plan["objectives_snapshot"],
                        "links": plan["links_snapshot"],
                        "metrics": plan["metrics_snapshot"]}
            cancel_flag = lambda: _cancel_flag(plan_id, token)  # noqa: E731
            result = run_planning(snapshot, requested, required, known,
                                  plan["reading_budget"], cancel_flag)

        with tx() as conn:
            cur = conn.execute(
                "SELECT * FROM reading_plans WHERE plan_id=%s AND job_token=%s",
                (plan_id, token)).fetchone()
            if not cur or cur["cancel_requested"]:
                conn.execute(
                    "INSERT INTO plan_events(plan_id, event_type, detail) "
                    "VALUES (%s,'result_discarded','作业已取消或被新作业取代，旧计算结果丢弃')",
                    (plan_id,))
                conn.commit()
                return
            if result["status"] == "failed":
                conn.execute(
                    "UPDATE reading_plans SET status='failed', error=%s, computed_at=now() "
                    "WHERE plan_id=%s AND job_token=%s AND cancel_requested=false",
                    (result["error"], plan_id, token))
                conn.execute(
                    "INSERT INTO plan_events(plan_id, event_type, detail) VALUES (%s,'failed',%s)",
                    (plan_id, result["error"]))
                conn.commit()
                return
            deprecation = _deprecation_note(conn, cur)
            new_status = "needs_review" if deprecation and cur["status"] != "confirmed" else "ready"
            updated = conn.execute(
                "UPDATE reading_plans SET status=%s, selected_chapters=%s, total_length=%s, "
                "covered_objective_ids=%s, uncovered=%s, warnings=%s, algorithm_note=%s, "
                "review_reason=%s, computed_at=now() "
                "WHERE plan_id=%s AND job_token=%s AND cancel_requested=false",
                (new_status,
                 json.dumps(result["selected"], ensure_ascii=False),
                 result["total_length"],
                 result["covered_objective_ids"],
                 json.dumps(result["uncovered"], ensure_ascii=False),
                 json.dumps(result["warnings"] + ([deprecation] if deprecation else []),
                            ensure_ascii=False),
                 result["algorithm_note"],
                 deprecation if new_status == "needs_review" else cur["review_reason"],
                 plan_id, token)).rowcount
            if updated:
                conn.execute(
                    "INSERT INTO plan_events(plan_id, event_type, detail) "
                    "VALUES (%s,'computed',%s)",
                    (plan_id,
                     f"贪心近似选章完成：选入 {len(result['selected'])} 章，"
                     f"合计 {result['total_length']} 字，未覆盖目标 "
                     f"{len(result['uncovered'])} 项"))
            else:
                conn.execute(
                    "INSERT INTO plan_events(plan_id, event_type, detail) "
                    "VALUES (%s,'result_discarded','写入时发现作业已失效，旧计算结果丢弃')",
                    (plan_id,))
            conn.commit()
    except JobCancelled:
        with tx() as conn:
            conn.execute(
                "INSERT INTO plan_events(plan_id, event_type, detail) "
                "VALUES (%s,'result_discarded','作业中途取消，未写入选择结果')", (plan_id,))
            conn.commit()
    except Exception as exc:  # 后台失败留痕，不静默吞掉
        with tx() as conn:
            conn.execute(
                "UPDATE reading_plans SET status='failed', error=%s "
                "WHERE plan_id=%s AND cancel_requested=false", (str(exc), plan_id))
            conn.execute(
                "INSERT INTO plan_events(plan_id, event_type, detail) "
                "VALUES (%s,'failed',%s)", (plan_id, f"后台计算异常：{exc}"))
            conn.commit()


def _cancel_flag(plan_id: int, token: str) -> bool:
    with tx() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM reading_plans WHERE plan_id=%s AND job_token=%s",
            (plan_id, token)).fetchone()
        return bool(row and row["cancel_requested"])


def _deprecation_note(conn, plan: dict) -> str:
    rows = conn.execute(
        "SELECT edition_id, supersede_reason FROM textbook_editions "
        "WHERE edition_id = ANY(%s) AND status='deprecated'",
        (plan["edition_ids"],)).fetchall()
    if not rows:
        return ""
    return ("计划固定的教材版本已停用：" + "；".join(
        f"版本 {r['edition_id']}（{r['supersede_reason'] or '已被新版替代'}）" for r in rows)
        + "。已发计划保留原出处正文，不自动替换，请复核是否改用新版。")


def cancel_plan(conn, plan_id: int) -> dict:
    plan = conn.execute("SELECT * FROM reading_plans WHERE plan_id=%s",
                        (plan_id,)).fetchone()
    if not plan:
        raise PlanningError("计划不存在")
    if plan["status"] not in ("queued", "running"):
        raise PlanningError(f"计划当前状态 {plan['status']}，无法取消")
    conn.execute(
        "UPDATE reading_plans SET cancel_requested=true, status='cancelled' "
        "WHERE plan_id=%s", (plan_id,))
    conn.execute(
        "INSERT INTO plan_events(plan_id, event_type, detail) "
        "VALUES (%s,'cancelled','教研员请求取消，进行中的旧计算返回后不得替换当前选择')",
        (plan_id,))
    conn.commit()
    return get_plan(conn, plan_id)


def recompute_plan(conn, plan_id: int, changes: dict | None, username: str,
                   spawn: bool = True) -> dict:
    plan = conn.execute("SELECT * FROM reading_plans WHERE plan_id=%s FOR UPDATE",
                        (plan_id,)).fetchone()
    if not plan:
        raise PlanningError("计划不存在")
    if plan["status"] in ("queued", "running"):
        raise PlanningError("计算进行中，请先取消再重算")
    if plan["status"] == "confirmed":
        raise PlanningError("计划已确认发送，出处已固定，不可重算；如需调整请新建计划")
    changes = changes or {}
    budget = changes.get("reading_budget") or plan["reading_budget"]
    if changes.get("locked_required") is not None or changes.get("locked_known") is not None:
        _replace_locks(conn, plan_id,
                       changes.get("locked_required", _current_locks(conn, plan_id, "required")),
                       changes.get("locked_known", _current_locks(conn, plan_id, "already_known")))
    token = str(uuid.uuid4())
    # 旧选择保留在 selected_chapters 中，直到新作业成功写入（令牌保护）
    conn.execute(
        "UPDATE reading_plans SET status='queued', job_token=%s, cancel_requested=false, "
        "reading_budget=%s, error='', review_reason='' WHERE plan_id=%s",
        (token, budget, plan_id))
    conn.execute(
        "INSERT INTO plan_events(plan_id, event_type, detail, created_by) "
        "VALUES (%s,'recompute','按同一组已审快照资料重新计算，旧结果保留至新作业完成',%s)",
        (plan_id, username))
    conn.commit()
    if spawn:
        spawn_plan_job(plan_id)
    return get_plan(conn, plan_id)


def _current_locks(conn, plan_id: int, lock_type: str) -> list[dict]:
    return [{"edition_id": r["edition_id"], "chapter_code": r["chapter_code"]}
            for r in conn.execute(
                "SELECT edition_id, chapter_code FROM plan_locked_chapters "
                "WHERE plan_id=%s AND lock_type=%s", (plan_id, lock_type)).fetchall()]


def confirm_plan(conn, plan_id: int) -> dict:
    plan = conn.execute("SELECT * FROM reading_plans WHERE plan_id=%s",
                        (plan_id,)).fetchone()
    if not plan:
        raise PlanningError("计划不存在")
    if plan["status"] not in ("ready", "needs_review"):
        raise PlanningError(f"计划状态 {plan['status']}，暂不能确认发送")
    conn.execute(
        "UPDATE reading_plans SET status='confirmed', confirmed_at=now() "
        "WHERE plan_id=%s", (plan_id,))
    conn.execute(
        "INSERT INTO plan_events(plan_id, event_type, detail) "
        "VALUES (%s,'confirmed','计划已确认发送给教师，所选出处与正文固定保留')",
        (plan_id,))
    conn.commit()
    return get_plan(conn, plan_id)


def mark_editions_deprecated(conn, edition_ids: list[int], reason: str) -> None:
    """教材停用联动：未确认计划转待复核并取消在途作业；已确认计划保留出处仅提示。"""
    if not edition_ids:
        return
    plans = conn.execute(
        "SELECT * FROM reading_plans WHERE edition_ids && %s::bigint[]",
        (edition_ids,)).fetchall()
    for plan in plans:
        note = _deprecation_note(conn, plan) or reason
        if plan["status"] == "confirmed":
            warnings = list(plan["warnings"])
            if note not in warnings:
                warnings.append(note)
                conn.execute(
                    "UPDATE reading_plans SET warnings=%s WHERE plan_id=%s",
                    (json.dumps(warnings, ensure_ascii=False), plan["plan_id"]))
                conn.execute(
                    "INSERT INTO plan_events(plan_id, event_type, detail) "
                    "VALUES (%s,'edition_deprecated','%s')",
                    (plan["plan_id"], "已确认计划：" + note))
        elif plan["status"] != "needs_review":
            conn.execute(
                "UPDATE reading_plans SET status='needs_review', review_reason=%s, "
                "cancel_requested=true WHERE plan_id=%s",
                (note, plan["plan_id"]))
            conn.execute(
                "INSERT INTO plan_events(plan_id, event_type, detail) "
                "VALUES (%s,'edition_deprecated','%s')",
                (plan["plan_id"], "未确认计划转待复核：" + note))


def list_plans(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT plan_id, name, subject, reading_budget, status, total_length, "
        "cardinality(covered_objective_ids) AS covered_count, "
        "jsonb_array_length(uncovered) AS uncovered_count, created_by, created_at, "
        "computed_at, review_reason FROM reading_plans ORDER BY plan_id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = r["created_at"].isoformat()
        d["computed_at"] = r["computed_at"].isoformat() if r["computed_at"] else None
        out.append(d)
    return out


def get_plan(conn, plan_id: int) -> dict:
    plan = conn.execute("SELECT * FROM reading_plans WHERE plan_id=%s",
                        (plan_id,)).fetchone()
    if not plan:
        raise PlanningError("计划不存在")
    d = dict(plan)
    d["created_at"] = plan["created_at"].isoformat()
    d["computed_at"] = plan["computed_at"].isoformat() if plan["computed_at"] else None
    d["confirmed_at"] = plan["confirmed_at"].isoformat() if plan["confirmed_at"] else None
    requested, required, known = _load_plan_inputs(conn, plan)
    obj_rows = conn.execute(
        "SELECT o.objective_id, o.code, o.title FROM plan_objectives po "
        "JOIN learning_objectives o ON o.objective_id=po.objective_id "
        "WHERE po.plan_id=%s ORDER BY o.code", (plan_id,)).fetchall()
    d["requested_objectives"] = [dict(r) for r in obj_rows]
    d["locked_required"] = [{"edition_id": e, "chapter_code": c} for e, c in required]
    d["locked_known"] = [{"edition_id": e, "chapter_code": c} for e, c in known]
    d["edition_statuses"] = [dict(r) for r in conn.execute(
        "SELECT e.edition_id, e.isbn, e.generation, e.status, e.supersede_reason, t.title "
        "FROM textbook_editions e JOIN textbooks t ON t.isbn=e.isbn "
        "WHERE e.edition_id = ANY(%s) ORDER BY e.edition_id", (plan["edition_ids"],))]
    d["deprecation_note"] = _deprecation_note(conn, plan)
    d["events"] = [
        {**dict(r), "created_at": r["created_at"].isoformat()}
        for r in conn.execute(
            "SELECT * FROM plan_events WHERE plan_id=%s ORDER BY event_id",
            (plan_id,)).fetchall()]
    return d
