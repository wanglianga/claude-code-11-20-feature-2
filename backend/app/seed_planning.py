"""课程阅读规划演示数据（幂等）。

在教材代次构建并启用之后播种，场景与需求样本一一对应：
- 一章覆盖多目标：初中 CH19 同时覆盖三个目标；
- 依赖回路：OBJ-CYC-X 与 OBJ-CYC-Y 互为先修，建计划时被拦截并指出涉及目标；
- 锁定超额：预算 1000 字却锁定 4000 字必读章，保留冲突不删必读；
- 篇幅未知：CH20 未登记篇幅，即便有关联也不可自动选入；
- 缺教材：OBJ-NM 只有未审核候选关联，已审关联为空；
- 缺先修：只选“利用导数研究单调性”而未学导数，闭包补入导数目标后仍受预算限制。
"""
from __future__ import annotations

import json

from . import planning
from .db import tx
from .models import ObjectiveIn, PlanCreate


def _active_edition(conn, isbn: str) -> int:
    row = conn.execute(
        "SELECT e.edition_id FROM textbook_editions e "
        "JOIN index_generations g ON g.edition_id=e.edition_id "
        "WHERE e.isbn=%s AND e.status='active' AND g.status='ready'",
        (isbn,)).fetchone()
    if not row:
        raise RuntimeError(f"播种规划数据前需先启用 {isbn} 的现行代次")
    return row["edition_id"]


def _objective(conn, code: str, title: str, prereq_codes: list[str]) -> None:
    planning.create_objective(conn, ObjectiveIn(
        code=code, title=title, subject="数学", stage="高中",
        description=title, prereq_codes=prereq_codes), "seed")


def _approved_link(conn, objective_code: str, edition_id: int,
                   chapter_code: str, weight: float) -> None:
    obj = conn.execute("SELECT objective_id FROM learning_objectives WHERE code=%s",
                       (objective_code,)).fetchone()
    conn.execute(
        "INSERT INTO objective_chapter_links"
        "(objective_id, edition_id, chapter_code, weight, status, origin, "
        " evidence, created_by, reviewed_by, reviewed_at) "
        "VALUES (%s,%s,%s,%s,'approved','manual','{}'::jsonb,'seed','seed',now()) "
        "ON CONFLICT (objective_id, edition_id, chapter_code) DO UPDATE SET "
        "status='approved', weight=EXCLUDED.weight, reviewed_by='seed', reviewed_at=now()",
        (obj["objective_id"], edition_id, chapter_code, weight))


def _pending_link(conn, objective_code: str, edition_id: int,
                  chapter_code: str) -> None:
    obj = conn.execute("SELECT objective_id FROM learning_objectives WHERE code=%s",
                       (objective_code,)).fetchone()
    conn.execute(
        "INSERT INTO objective_chapter_links"
        "(objective_id, edition_id, chapter_code, weight, status, origin, "
        " evidence, created_by) "
        "VALUES (%s,%s,%s,1,'pending','search_candidate',%s,'seed') "
        "ON CONFLICT DO NOTHING",
        (obj["objective_id"], edition_id, chapter_code,
         json.dumps({"note": "检索命中候选，未经教师审核，不参与规划"},
                    ensure_ascii=False)))


def seed_planning() -> None:
    with tx() as conn:
        if conn.execute("SELECT count(*) AS c FROM learning_objectives").fetchone()["c"]:
            return
        mid_edition = _active_edition(conn, "9787100000011")   # 初中数学现行版
        high_edition = _active_edition(conn, "9787100000028")  # 高中数学现行版

        # ---- 教学目标与先修链 ----
        _objective(conn, "OBJ-A1", "一次函数的概念", [])
        _objective(conn, "OBJ-A2", "一次函数的图象与性质", ["OBJ-A1"])
        _objective(conn, "OBJ-A3", "函数与方程的联系", ["OBJ-A2"])
        _objective(conn, "OBJ-M1", "函数建模入门", ["OBJ-A1"])
        _objective(conn, "OBJ-D1", "导数的概念与几何意义", [])
        _objective(conn, "OBJ-D2", "利用导数研究函数单调性", ["OBJ-D1"])
        _objective(conn, "OBJ-NM", "统计概率综合建模", [])

        # ---- 依赖回路（绕过服务端成环拦截，直接构造回路样本）----
        for code, title in (("OBJ-CYC-X", "循环样本·变化率"),
                            ("OBJ-CYC-Y", "循环样本·极限思想")):
            conn.execute(
                "INSERT INTO learning_objectives(code, title, subject, stage, "
                " description, created_by) VALUES (%s,%s,'数学','高中',%s,'seed') "
                "ON CONFLICT DO NOTHING", (code, title, title))
        ids = {r["code"]: r["objective_id"] for r in conn.execute(
            "SELECT objective_id, code FROM learning_objectives "
            "WHERE code IN ('OBJ-CYC-X','OBJ-CYC-Y')").fetchall()}
        for a, b in (("OBJ-CYC-X", "OBJ-CYC-Y"), ("OBJ-CYC-Y", "OBJ-CYC-X")):
            conn.execute(
                "INSERT INTO objective_prereqs(objective_id, requires_objective_id) "
                "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (ids[a], ids[b]))

        # ---- 章节篇幅：CH20 故意留空（篇幅未知）----
        planning.upsert_metric(conn, mid_edition, "CH19", 3000, "seed")
        planning.upsert_metric(conn, high_edition, "CH3", 4000, "seed")

        # ---- 已审覆盖关联（CH19 一章覆盖多目标）----
        _approved_link(conn, "OBJ-A1", mid_edition, "CH19", 1.0)
        _approved_link(conn, "OBJ-A2", mid_edition, "CH19", 1.0)
        _approved_link(conn, "OBJ-M1", mid_edition, "CH19", 1.0)
        _approved_link(conn, "OBJ-A3", high_edition, "CH3", 2.0)
        _approved_link(conn, "OBJ-D1", high_edition, "CH3", 1.0)
        _approved_link(conn, "OBJ-D2", high_edition, "CH3", 1.0)
        # 未审核候选：OBJ-NM 命中篇幅未知的 CH20，双重演示“候选不入规划/未知篇幅不选”
        _pending_link(conn, "OBJ-NM", mid_edition, "CH20")
        conn.commit()

    # ---- 计划样本（同步计算，保证播种结束即可见结果）----
    plans = [
        PlanCreate(
            name="样本1·一章覆盖多目标",
            objective_codes=["OBJ-A1", "OBJ-A2", "OBJ-M1"],
            reading_budget=5000),
        PlanCreate(
            name="样本2·预算不足的函数进阶",
            objective_codes=["OBJ-A1", "OBJ-A2", "OBJ-A3"],
            reading_budget=5000),
        PlanCreate(
            name="样本3·锁定必读超额（保留冲突）",
            objective_codes=["OBJ-A1"],
            reading_budget=1000,
            locked_required=[{"edition_id": high_edition, "chapter_code": "CH3"}]),
        PlanCreate(
            name="样本4·先修缺口（尚未学导数）",
            objective_codes=["OBJ-D2"],
            reading_budget=3000),
        PlanCreate(
            name="样本5·缺教材与篇幅未知",
            objective_codes=["OBJ-NM"],
            reading_budget=5000),
        PlanCreate(
            name="样本6·已有基础免阅读量",
            objective_codes=["OBJ-A1", "OBJ-A2"],
            reading_budget=100,
            locked_known=[{"edition_id": mid_edition, "chapter_code": "CH19"}]),
    ]
    for req in plans:
        with tx() as conn:
            plan = planning.create_plan(conn, req, "seed", spawn=False)
            conn.commit()
        planning.execute_plan_job(plan["plan_id"])
