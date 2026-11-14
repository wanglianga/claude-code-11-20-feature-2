"""课程阅读规划：成环检测、先修闭包、贪心近似选章、锁定/篇幅约束、
未覆盖诊断、快照固定、取消令牌与教材停用联动。"""
import pytest

from app import planning, seed_planning
from app.db import tx
from app.models import ObjectiveIn, PlanCreate


# -------------------------------------------------------------
# 纯函数：图算法
# -------------------------------------------------------------

def test_find_cycles_detects_and_reports_involved_objectives():
    code_map = {1: "A", 2: "B", 3: "C", 4: "D"}
    prereq_map = {1: {2}, 2: {1}, 3: {4}, 4: set()}
    cycles = planning.find_cycles(prereq_map, code_map)
    assert cycles == [["A", "B", "A"]]


def test_acyclic_graph_has_no_cycle_and_closure_is_transitive():
    prereq_map = {1: {2}, 2: {3}, 3: set(), 4: set()}
    assert planning.find_cycles(prereq_map, {1: "A", 2: "B", 3: "C"}) == []
    assert planning.prerequisite_closure([1], prereq_map) == {1, 2, 3}


# -------------------------------------------------------------
# 纯函数：规划算法
# -------------------------------------------------------------

def _snapshot():
    return {
        "objectives": [
            {"objective_id": 1, "code": "O1", "title": "目标1", "prereq_ids": []},
            {"objective_id": 2, "code": "O2", "title": "目标2", "prereq_ids": [1]},
            {"objective_id": 3, "code": "O3", "title": "目标3", "prereq_ids": []},
        ],
        "links": [
            {"objective_id": 1, "edition_id": 9, "chapter_code": "C1", "weight": 1.0},
            {"objective_id": 2, "edition_id": 9, "chapter_code": "C1", "weight": 1.0},
            {"objective_id": 3, "edition_id": 9, "chapter_code": "C2", "weight": 1.0},
        ],
        "metrics": [
            {"edition_id": 9, "chapter_code": "C1", "chapter_path": "C1 第一章",
             "reading_length": 1000},
            {"edition_id": 9, "chapter_code": "C2", "chapter_path": "C2 第二章",
             "reading_length": 2000},
            {"edition_id": 9, "chapter_code": "C3", "chapter_path": "C3 第三章",
             "reading_length": None},
        ],
    }


def test_one_chapter_covers_multiple_objectives_charged_once():
    res = planning.run_planning(_snapshot(), [1, 2], [], [], 5000)
    assert res["status"] == "ready"
    assert len(res["selected"]) == 1
    assert res["selected"][0]["chapter_code"] == "C1"
    assert res["total_length"] == 1000  # 一章覆盖两目标，只计一次篇幅
    assert set(res["covered_objective_ids"]) == {1, 2}


def test_greedy_prefers_higher_ratio_and_respects_budget():
    snap = _snapshot()
    # C2 性价比 1/2000；新增 C4 性价比 2/500，应先选 C4
    snap["links"].append(
        {"objective_id": 3, "edition_id": 9, "chapter_code": "C4", "weight": 2.0})
    snap["metrics"].append(
        {"edition_id": 9, "chapter_code": "C4", "chapter_path": "C4 第四章",
         "reading_length": 500})
    res = planning.run_planning(snap, [3], [], [], 600)
    codes = [c["chapter_code"] for c in res["selected"]]
    assert codes == ["C4"]
    assert res["greedy_trace"][0]["ratio"] == pytest.approx(2 / 500)


def test_unknown_length_chapter_never_auto_selected():
    snap = _snapshot()
    # O3 只保留篇幅未知的 C3
    snap["links"] = [lk for lk in snap["links"] if lk["objective_id"] != 3]
    snap["links"].append(
        {"objective_id": 3, "edition_id": 9, "chapter_code": "C3", "weight": 1.0})
    res = planning.run_planning(snap, [3], [], [], 9999)
    assert all(c["chapter_code"] != "C3" for c in res["selected"])
    unc = next(u for u in res["uncovered"] if u["code"] == "O3")
    assert "no_material" in unc["reasons"]
    assert unc["candidate_chapters"][0]["excluded_reason"] == "篇幅未知，不可自动选入"


def test_unknown_length_required_lock_is_kept_but_not_priced():
    res = planning.run_planning(
        _snapshot(), [1], [(9, "C3")], [], 500)
    locked = next(c for c in res["selected"] if c["chapter_code"] == "C3")
    assert locked["reason"] == "required"
    assert locked["reading_length"] is None
    assert any("篇幅未知" in w for w in res["warnings"])


def test_locked_required_overrun_keeps_conflict_instead_of_dropping():
    res = planning.run_planning(
        _snapshot(), [1], [(9, "C2")], [], 1000)  # C2=2000 > 预算 1000
    assert res["total_length"] == 2000
    assert any("超出阅读上限" in w and "保留全部必读内容" in w for w in res["warnings"])
    # 超额期间不再自动选章：O1 虽可由 C1(1000) 覆盖，也不补选
    assert [c["chapter_code"] for c in res["selected"]] == ["C2"]


def test_already_known_lock_covers_without_reading_cost():
    res = planning.run_planning(
        _snapshot(), [1, 2], [], [(9, "C1")], 100)
    assert res["total_length"] == 0
    assert set(res["covered_objective_ids"]) == {1, 2}
    assert res["selected"][0]["reason"] == "already_known"


def test_budget_diagnosis_states_extra_length_needed():
    res = planning.run_planning(_snapshot(), [3], [], [], 1500)  # C2 需要 2000
    unc = next(u for u in res["uncovered"] if u["code"] == "O3")
    assert unc["reasons"] == ["budget"]
    assert unc["extra_length_to_cover"] == 500
    assert unc["cheapest_selectable"]["chapter_code"] == "C2"


def test_missing_prereq_is_reported_per_objective():
    # 只要 O2（先修 O1），预算只够一章但两章同章可覆盖 -> 构造先修未覆盖：
    snap = _snapshot()
    snap["links"] = [lk for lk in snap["links"] if lk["objective_id"] != 1]
    res = planning.run_planning(snap, [2], [], [], 5000)
    unc = {u["code"]: u for u in res["uncovered"]}
    assert "missing_prereq" in unc["O2"]["reasons"]
    assert unc["O2"]["prereq_missing"] == ["O1"]
    assert "O1" in unc  # 闭包补入的先修目标同样逐项列出


def test_cancel_callback_aborts_greedy():
    with pytest.raises(planning.JobCancelled):
        planning.run_planning(_snapshot(), [1, 2, 3], [], [], 5000,
                              is_cancelled=lambda: True)


def test_result_is_marked_approximate_not_shortest_path():
    res = planning.run_planning(_snapshot(), [1], [], [], 5000)
    assert "近似" in res["algorithm_note"]
    assert "最短阅读路线" in res["algorithm_note"]


# -------------------------------------------------------------
# 服务层 + API（基于完整演示播种）
# -------------------------------------------------------------

@pytest.fixture()
def planning_seeded(seeded):
    seed_planning.seed_planning()
    return seeded


def _create_plan_sync(req):
    with tx() as conn:
        plan = planning.create_plan(conn, req, "teacher1", spawn=False)
        conn.commit()
    planning.execute_plan_job(plan["plan_id"])
    with tx() as conn:
        return planning.get_plan(conn, plan["plan_id"])


def test_seed_plans_cover_required_scenarios(planning_seeded):
    with tx() as conn:
        plans = {r["name"]: r for r in conn.execute(
            "SELECT name, status, total_length, reading_budget, "
            "cardinality(covered_objective_ids) AS cov, "
            "jsonb_array_length(uncovered) AS unc, warnings "
            "FROM reading_plans ORDER BY plan_id").fetchall()}
    multi = plans["样本1·一章覆盖多目标"]
    assert multi["status"] == "ready" and multi["total_length"] == 3000 and multi["cov"] == 3
    overrun = plans["样本3·锁定必读超额（保留冲突）"]
    assert overrun["total_length"] > overrun["reading_budget"]
    assert any("超出阅读上限" in w for w in overrun["warnings"])
    no_mat = plans["样本5·缺教材与篇幅未知"]
    assert no_mat["unc"] == 1


def test_objective_cycle_flag_and_cycle_api(client, planning_seeded,
                                            teacher_token, auth_headers):
    h = auth_headers(teacher_token)
    cyc = client.get("/api/planning/cycles", headers=h).json()["cycles"]
    assert any("OBJ-CYC-X" in c and "OBJ-CYC-Y" in c for c in cyc)
    resp = client.post("/api/planning/plans", headers=h, json={
        "name": "回路计划", "objective_codes": ["OBJ-CYC-X"],
        "reading_budget": 9999})
    assert resp.status_code == 422
    assert resp.json()["detail"]["cycles"]


def test_pending_search_link_is_only_candidate(planning_seeded):
    with tx() as conn:
        plan = _create_plan_sync(PlanCreate(
            name="候选不入规划", objective_codes=["OBJ-NM"], reading_budget=9999))
        unc = plan["uncovered"]
    assert len(unc) == 1 and unc[0]["reasons"] == ["no_material"]
    assert plan["selected_chapters"] == []


def test_review_approved_link_enters_next_plan_snapshot(planning_seeded):
    with tx() as conn:
        link = conn.execute(
            "SELECT link_id FROM objective_chapter_links WHERE status='pending'").fetchone()
        planning.review_link(conn, link["link_id"], "approved", "teacher1")
        conn.commit()
        # 补一个已知篇幅，使 CH20 可被选入
        edition = conn.execute(
            "SELECT edition_id FROM objective_chapter_links WHERE link_id=%s",
            (link["link_id"],)).fetchone()["edition_id"]
        planning.upsert_metric(conn, edition, "CH20", 800, "teacher1")
        conn.commit()
    plan = _create_plan_sync(PlanCreate(
        name="审核后可用", objective_codes=["OBJ-NM"], reading_budget=9999))
    assert plan["status"] == "ready"
    assert any(c["chapter_code"] == "CH20" for c in plan["selected_chapters"])


def test_plan_snapshot_is_fixed_recompute_ignores_later_link_changes(planning_seeded):
    plan = _create_plan_sync(PlanCreate(
        name="快照固定", objective_codes=["OBJ-A1", "OBJ-A2", "OBJ-M1"],
        reading_budget=5000))
    before = [(c["edition_id"], c["chapter_code"]) for c in plan["selected_chapters"]]
    with tx() as conn:
        # 计划创建后再新增已审关联，重算不得采用
        conn.execute(
            "INSERT INTO objective_chapter_links"
            "(objective_id, edition_id, chapter_code, weight, status, origin, created_by) "
            "SELECT o.objective_id, e.edition_id, 'CH20', 5, 'approved', 'manual', 'teacher1' "
            "FROM learning_objectives o, textbook_editions e "
            "WHERE o.code='OBJ-A1' AND e.status='active' "
            "ON CONFLICT DO NOTHING")
        conn.commit()
        planning.recompute_plan(conn, plan["plan_id"], None, "teacher1", spawn=False)
        conn.commit()
    planning.execute_plan_job(plan["plan_id"])
    with tx() as conn:
        again = planning.get_plan(conn, plan["plan_id"])
    assert [(c["edition_id"], c["chapter_code"]) for c in again["selected_chapters"]] == before


def test_cancelled_job_result_never_replaces_selection(planning_seeded):
    with tx() as conn:
        plan = planning.create_plan(conn, PlanCreate(
            name="取消样本", objective_codes=["OBJ-A1"], reading_budget=5000),
            "teacher1", spawn=False)
        pid = plan["plan_id"]
        planning.cancel_plan(conn, pid)
        conn.commit()
    # 作业若在取消后才返回，写入被令牌/取消标志拦截
    planning.execute_plan_job(pid)
    with tx() as conn:
        row = conn.execute(
            "SELECT status, selected_chapters FROM reading_plans WHERE plan_id=%s",
            (pid,)).fetchone()
    assert row["status"] == "cancelled"
    assert row["selected_chapters"] == []


def test_recompute_keeps_old_selection_until_new_job_finishes(planning_seeded):
    plan = _create_plan_sync(PlanCreate(
        name="重算保留旧选择", objective_codes=["OBJ-A1"], reading_budget=5000))
    assert plan["selected_chapters"]
    with tx() as conn:
        planning.recompute_plan(conn, plan["plan_id"],
                                {"reading_budget": 5000}, "teacher1", spawn=False)
        conn.commit()
        queued = planning.get_plan(conn, plan["plan_id"])
    # 排队期间旧选择仍在，未被清空
    assert queued["status"] == "queued"
    assert queued["selected_chapters"] == plan["selected_chapters"]


def test_deprecation_confirmed_plan_kept_unconfirmed_needs_review(client, planning_seeded,
                                                                  teacher_token,
                                                                  auth_headers):
    h = auth_headers(teacher_token)
    with tx() as conn:
        archived = conn.execute(
            "SELECT e.edition_id, g.generation_id FROM textbook_editions e "
            "JOIN index_generations g ON g.edition_id=e.edition_id "
            "WHERE g.status='archived' ORDER BY e.edition_id LIMIT 1").fetchone()
    # 两个计划都固定到即将退役的旧版：一个确认、一个不确认
    confirmed = _create_plan_sync(PlanCreate(
        name="停用-已确认", objective_codes=["OBJ-A1"], reading_budget=9999,
        edition_ids=[archived["edition_id"]]))
    with tx() as conn:
        planning.confirm_plan(conn, confirmed["plan_id"])
        conn.commit()
    unconfirmed = _create_plan_sync(PlanCreate(
        name="停用-未确认", objective_codes=["OBJ-A1"], reading_budget=9999,
        edition_ids=[archived["edition_id"]]))

    resp = client.post(
        f"/api/admin/generations/{archived['generation_id']}/retire",
        headers=auth_headers(client.post("/api/auth/login",
                                         json={"username": "editor1"}).json()["token"]))
    assert resp.status_code == 200
    with tx() as conn:
        p1 = planning.get_plan(conn, confirmed["plan_id"])
        p2 = planning.get_plan(conn, unconfirmed["plan_id"])
    assert p1["status"] == "confirmed"  # 已发计划保留原出处
    assert any("停用" in w or "退役" in w for w in p1["warnings"])
    assert p2["status"] == "needs_review"
    assert "复核" in p2["review_reason"]


def test_teacher_role_guards_planning_api(client, seeded, editor_token, auth_headers):
    r = client.get("/api/planning/objectives", headers=auth_headers(editor_token))
    assert r.status_code == 403


def test_metrics_unknown_rejected_for_unknown_chapter(client, planning_seeded,
                                                      teacher_token, auth_headers):
    h = auth_headers(teacher_token)
    r = client.post("/api/planning/metrics", headers=h, json={
        "edition_id": 1, "chapter_code": "NO-SUCH-CH", "reading_length": 100})
    assert r.status_code == 422


def test_cycle_objective_creation_api_rejected(client, planning_seeded,
                                               teacher_token, auth_headers):
    h = auth_headers(teacher_token)
    r = client.post("/api/planning/objectives", headers=h, json={
        "code": "OBJ-LOOP-A", "title": "接口成环A", "subject": "数学",
        "prereq_codes": ["OBJ-LOOP-B"]})
    # 先修不存在 -> 422
    assert r.status_code == 422
    client.post("/api/planning/objectives", headers=h, json={
        "code": "OBJ-LOOP-B", "title": "接口成环B", "subject": "数学",
        "prereq_codes": []})
    r2 = client.post("/api/planning/objectives", headers=h, json={
        "code": "OBJ-LOOP-A", "title": "接口成环A", "subject": "数学",
        "prereq_codes": ["OBJ-LOOP-B"]})
    assert r2.status_code == 200
    r3 = client.post("/api/planning/objectives", headers=h, json={
        "code": "OBJ-LOOP-C", "title": "接口成环C", "subject": "数学",
        "prereq_codes": ["OBJ-LOOP-A"]})
    assert r3.status_code == 200
    # 让 B 反向依赖 C 形成 B->C->... 回路：B 已存在，直接服务层校验
    with tx() as conn:
        b = conn.execute("SELECT objective_id FROM learning_objectives "
                         "WHERE code='OBJ-LOOP-B'").fetchone()
        c = conn.execute("SELECT objective_id FROM learning_objectives "
                         "WHERE code='OBJ-LOOP-C'").fetchone()
        conn.execute("INSERT INTO objective_prereqs VALUES (%s,%s)",
                     (b["objective_id"], c["objective_id"]))
        with pytest.raises(planning.CycleError):
            planning.create_objective(conn, ObjectiveIn(
                code="OBJ-LOOP-D", title="触发校验", subject="数学",
                prereq_codes=["OBJ-LOOP-B"]), "teacher1")
