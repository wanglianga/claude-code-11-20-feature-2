"""FastAPI 入口：认证、检索、原文、引用、同义词、构建与编辑管理路由。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import importer, planning, search as searchmod
from .config import settings
from .db import init_schema, tx
from .models import (
    BuildReport, BuildRequest, ChapterLinkIn, ChapterLinkReview, ChapterMetricIn,
    CitationCreate, CitationOut, LoginRequest, ObjectiveIn, PlanCreate,
    PlanRecompute, ReplacementIn, SearchRequest, SynonymIn, SynonymOut,
    TextbookOut, UnitOut, UserOut,
)


# ---------------------------------------------------------------- 简易令牌

def make_token(username: str) -> str:
    payload = {"u": username, "exp": int(time.time()) + settings.token_ttl_hours * 3600}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode()
    sig = hmac.new(settings.token_secret.encode(), body.encode(),
                   hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 令牌")
    token = authorization[7:]
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(401, "令牌格式错误")
    expect = hmac.new(settings.token_secret.encode(), body.encode(),
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        raise HTTPException(401, "令牌签名无效")
    payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    if payload["exp"] < int(time.time()):
        raise HTTPException(401, "令牌已过期")
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=%s",
                           (payload["u"],)).fetchone()
    if not row:
        raise HTTPException(401, "用户不存在")
    return dict(row)


def require_editor(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "editor":
        raise HTTPException(403, "仅编辑可执行该操作")
    return user


def require_teacher(user: dict = Depends(current_user)) -> dict:
    # 教研员/教师侧操作：目标录入、覆盖关联审核、阅读规划
    if user["role"] != "teacher":
        raise HTTPException(403, "仅教研员（教师账号）可执行该操作")
    return user


# ---------------------------------------------------------------- 生命周期

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    if settings.auto_seed:
        from .seed import seed_all
        seed_all()
    yield


app = FastAPI(title="教材检索与引用核查平台 API", version="1.0.0",
              lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)


# ---------------------------------------------------------------- 认证与元数据

@app.post("/api/auth/login", response_model=dict)
def login(req: LoginRequest):
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=%s",
                           (req.username,)).fetchone()
    if not row:
        raise HTTPException(401, f"用户 {req.username} 不存在，可用 editor1 / teacher1")
    return {"token": make_token(row["username"]),
            "user": UserOut(**row).model_dump()}


@app.get("/api/meta")
def meta(user: dict = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT DISTINCT subject, stage FROM textbooks ORDER BY 1,2").fetchall()
    return {"subjects": sorted({r["subject"] for r in rows}),
            "stages": sorted({r["stage"] for r in rows})}


# ---------------------------------------------------------------- 教材与版本

@app.get("/api/textbooks")
def list_textbooks(user: dict = Depends(current_user)):
    with tx() as conn:
        books = conn.execute("SELECT * FROM textbooks ORDER BY isbn").fetchall()
        out: list[dict] = []
        for b in books:
            editions = conn.execute(
                "SELECT e.*, g.status AS gen_status, g.generation_id "
                "FROM textbook_editions e "
                "JOIN index_generations g ON g.edition_id=e.edition_id "
                "WHERE e.isbn=%s ORDER BY e.generation DESC", (b["isbn"],)).fetchall()
            active = next((e for e in editions if e["status"] == "active"), None)
            out.append({
                **TextbookOut(
                    isbn=b["isbn"], title=b["title"], subject=b["subject"],
                    stage=b["stage"], volume=b["volume"], semester=b["semester"],
                    publisher=b["publisher"],
                    active_generation_id=active["generation_id"] if active else None,
                    active_edition_id=active["edition_id"] if active else None,
                    active_status=active["gen_status"] if active else None,
                    has_older=any(e["status"] == "deprecated" for e in editions),
                ).model_dump(),
                "editions": [dict(e) for e in editions],
            })
        return out


# ---------------------------------------------------------------- 检索与原文

@app.post("/api/search")
def do_search(req: SearchRequest, user: dict = Depends(current_user)):
    with tx() as conn:
        return searchmod.search(conn, req, user["username"])


@app.get("/api/units/{unit_id}")
def get_unit(unit_id: int, session_id: str | None = Query(default=None),
             user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            return searchmod.unit_detail(conn, unit_id, session_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except PermissionError as exc:
            raise HTTPException(409, str(exc))


# ---------------------------------------------------------------- 引用

@app.post("/api/citations")
def add_citation(req: CitationCreate, user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            row = searchmod.create_citation(conn, req, user["username"])
            conn.commit()
            return row
        except PermissionError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))


@app.get("/api/citations")
def list_citations(user: dict = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM citations WHERE username=%s ORDER BY citation_id DESC",
            (user["username"],)).fetchall()
        return [searchmod._citation_dict(r) for r in rows]


@app.post("/api/citations/{citation_id}/check")
def recheck_citation(citation_id: int, user: dict = Depends(current_user)):
    with tx() as conn:
        try:
            row = searchmod.check_citation(conn, citation_id)
            conn.commit()
            return row
        except LookupError as exc:
            raise HTTPException(404, str(exc))


# ---------------------------------------------------------------- 同义词

@app.get("/api/synonyms")
def list_synonyms(status: str = "all", user: dict = Depends(current_user)):
    sql = "SELECT * FROM synonyms"
    params: tuple = ()
    if status != "all":
        sql += " WHERE status=%s"
        params = (status,)
    sql += " ORDER BY synonym_id DESC"
    with tx() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [SynonymOut(**r).model_dump() for r in rows]


@app.post("/api/synonyms")
def create_synonym(req: SynonymIn, user: dict = Depends(current_user)):
    from .text import normalize
    term_norm, _ = normalize(req.term_display.strip())
    syn_norm, _ = normalize(req.synonym_display.strip())
    if not term_norm or not syn_norm:
        raise HTTPException(422, "术语与同义词不能为空")
    with tx() as conn:
        try:
            row = conn.execute(
                "INSERT INTO synonyms"
                "(subject, term_norm, term_display, synonym_norm, synonym_display, "
                " status, created_by) VALUES (%s,%s,%s,%s,%s,'pending',%s) RETURNING *",
                (req.subject, term_norm, req.term_display.strip(), syn_norm,
                 req.synonym_display.strip(), user["username"]),
            ).fetchone()
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"该学科同义词已存在：{exc}")
    return SynonymOut(**row).model_dump()


@app.post("/api/synonyms/{synonym_id}/review")
def review_synonym(synonym_id: int, body: dict,
                   editor: dict = Depends(require_editor)):
    decision = body.get("status")
    if decision not in ("approved", "rejected"):
        raise HTTPException(422, "status 必须是 approved 或 rejected")
    with tx() as conn:
        row = conn.execute(
            "UPDATE synonyms SET status=%s, reviewed_by=%s, reviewed_at=now() "
            "WHERE synonym_id=%s RETURNING *",
            (decision, editor["username"], synonym_id),
        ).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "同义词不存在")
    return SynonymOut(**row).model_dump()


# ---------------------------------------------------------------- 构建与代次

@app.post("/api/admin/build", response_model=BuildReport)
def build_book(req: BuildRequest, editor: dict = Depends(require_editor)):
    payload_path = os.path.join(
        settings.data_dir, req.payload_rel or f"{req.isbn}/payload.json")
    if not os.path.exists(payload_path):
        raise HTTPException(404, f"数据包不存在：{payload_path}")
    try:
        report = importer.build_from_payload(
            payload_path, chapter_codes=req.chapter_codes,
            username=editor["username"])
        return report
    except importer.BuildError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/admin/generations")
def list_generations(editor: dict = Depends(require_editor)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT g.*, e.generation AS edition_generation, t.title, t.isbn "
            "FROM index_generations g "
            "JOIN textbook_editions e ON e.edition_id=g.edition_id "
            "JOIN textbooks t ON t.isbn=e.isbn "
            "ORDER BY g.generation_id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["started_at"] = r["started_at"].isoformat()
            d["finished_at"] = r["finished_at"].isoformat() if r["finished_at"] else None
            out.append(d)
        return out


@app.get("/api/admin/reports/{generation_id}")
def build_report(generation_id: int, editor: dict = Depends(require_editor)):
    with tx() as conn:
        gen = conn.execute(
            "SELECT * FROM index_generations WHERE generation_id=%s",
            (generation_id,)).fetchone()
        if not gen:
            raise HTTPException(404, "代次不存在")
        reports = conn.execute(
            "SELECT chapter_code, stage, status, counts, error FROM index_chapter_reports "
            "WHERE generation_id=%s ORDER BY id", (generation_id,)).fetchall()
        issues = conn.execute(
            "SELECT * FROM parse_issues WHERE generation_id=%s ORDER BY issue_id",
            (generation_id,)).fetchall()
        return {"generation": dict(gen),
                "stage_reports": [dict(r) for r in reports],
                "issues": [dict(i) for i in issues]}


@app.get("/api/admin/issues")
def list_issues(status: str = "open", editor: dict = Depends(require_editor)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM parse_issues WHERE status=%s ORDER BY generation_id, issue_id",
            (status,)).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/admin/issues/{issue_id}/resolve")
def resolve_issue(issue_id: int, editor: dict = Depends(require_editor)):
    with tx() as conn:
        row = conn.execute(
            "UPDATE parse_issues SET status='resolved' WHERE issue_id=%s RETURNING *",
            (issue_id,)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "问题不存在")
    return dict(row)


@app.post("/api/admin/corrections")
def submit_correction(body: dict, editor: dict = Depends(require_editor)):
    """编辑提交坏章修正稿：登记修正并关闭该章待修正问题。

    修正稿只作存档与问题闭环；重新入索引需以新一代次数据包重新构建，
    失败代次始终不会被直接“补章”变成可用，避免半新半旧的混合索引。
    """
    edition_id = body.get("edition_id")
    chapter_code = body.get("chapter_code")
    payload = body.get("payload")
    if not edition_id or not chapter_code or payload is None:
        raise HTTPException(422, "edition_id、chapter_code、payload 均为必填")
    with tx() as conn:
        edition = conn.execute(
            "SELECT * FROM textbook_editions WHERE edition_id=%s",
            (edition_id,)).fetchone()
        if not edition:
            raise HTTPException(404, "版本不存在")
        conn.execute(
            "INSERT INTO chapter_corrections(edition_id, chapter_code, payload, created_by) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (edition_id, chapter_code) DO UPDATE SET "
            "payload=EXCLUDED.payload, created_by=EXCLUDED.created_by, created_at=now()",
            (edition_id, chapter_code,
             json.dumps(payload, ensure_ascii=False), editor["username"]),
        )
        closed = conn.execute(
            "UPDATE parse_issues SET status='resolved' "
            "WHERE edition_id=%s AND chapter_code=%s AND status='open' RETURNING issue_id",
            (edition_id, chapter_code)).fetchall()
        conn.commit()
    return {"edition_id": edition_id, "chapter_code": chapter_code,
            "resolved_issue_ids": [r["issue_id"] for r in closed],
            "note": "修正稿已存档；请用提升 generation 的新数据包重新构建后再申请启用。"}


# ---------------------------------------------------------------- 替代批准与原子切换

@app.post("/api/admin/replacement/request")
def request_replacement(req: ReplacementIn, editor: dict = Depends(require_editor)):
    with tx() as conn:
        row = conn.execute(
            "INSERT INTO replacement_requests"
            "(isbn, old_edition_id, new_edition_id, reason, status) "
            "VALUES (%s,%s,%s,%s,'pending') RETURNING *",
            (req.isbn, req.old_edition_id, req.new_edition_id, req.reason),
        ).fetchone()
        conn.commit()
    return dict(row)


@app.get("/api/admin/replacement")
def list_replacements(editor: dict = Depends(require_editor)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM replacement_requests ORDER BY request_id DESC").fetchall()
        return [dict(r) for r in rows]


@app.post("/api/admin/replacement/{request_id}/approve")
def approve_replacement(request_id: int, editor: dict = Depends(require_editor)):
    """原子切换现行索引：旧版停用归档，新版启用；旧作业不可能覆盖更新的新稿。"""
    with tx() as conn:
        req = conn.execute(
            "SELECT * FROM replacement_requests WHERE request_id=%s FOR UPDATE",
            (request_id,)).fetchone()
        if not req:
            raise HTTPException(404, "替代申请不存在")
        if req["status"] != "pending":
            raise HTTPException(409, f"申请已处理：{req['status']}")

        new_gen = conn.execute(
            "SELECT g.*, e.generation AS edition_generation FROM index_generations g "
            "JOIN textbook_editions e ON e.edition_id=g.edition_id "
            "WHERE g.edition_id=%s FOR UPDATE", (req["new_edition_id"],)).fetchone()
        if not new_gen or new_gen["status"] != "ready":
            raise HTTPException(409, "新稿索引未就绪（构建未成功或不齐全），拒绝切换")

        # 陈旧作业保护：只允许启用该 ISBN 下最大代次的就绪稿
        newest = conn.execute(
            "SELECT max(e.generation) AS m FROM textbook_editions e "
            "JOIN index_generations g ON g.edition_id=e.edition_id "
            "WHERE e.isbn=%s AND g.status='ready'", (req["isbn"],)).fetchone()
        if newest["m"] is not None and new_gen["edition_generation"] < newest["m"]:
            conn.execute(
                "UPDATE replacement_requests SET status='rejected', decided_by=%s, "
                "decided_at=now() WHERE request_id=%s",
                (editor["username"], request_id))
            conn.commit()
            raise HTTPException(409, "存在更新的就绪代次，旧作业不得覆盖新稿，申请已拒绝")

        old = conn.execute(
            "SELECT e.edition_id, g.generation_id FROM textbook_editions e "
            "JOIN index_generations g ON g.edition_id=e.edition_id "
            "WHERE e.isbn=%s AND e.status='active' FOR UPDATE",
            (req["isbn"],)).fetchall()
        for o in old:
            conn.execute(
                "UPDATE textbook_editions SET status='deprecated', "
                "superseded_by=%s, supersede_reason=coalesce(nullif(supersede_reason,''),%s) "
                "WHERE edition_id=%s",
                (req["new_edition_id"], req["reason"] or "已由编辑批准的新版替代",
                 o["edition_id"]))
            conn.execute(
                "UPDATE index_generations SET status='archived' WHERE generation_id=%s",
                (o["generation_id"],))
        conn.execute(
            "UPDATE textbook_editions SET status='active' WHERE edition_id=%s",
            (req["new_edition_id"],))
        conn.execute(
            "UPDATE textbooks SET current_generation_id=%s WHERE isbn=%s",
            (new_gen["generation_id"], req["isbn"]))
        conn.execute(
            "UPDATE replacement_requests SET status='approved', decided_by=%s, "
            "decided_at=now() WHERE request_id=%s",
            (editor["username"], request_id))
        # 教材停用联动：未确认阅读计划转待复核，已确认计划保留出处仅提示更新
        planning.mark_editions_deprecated(
            conn, [o["edition_id"] for o in old],
            req["reason"] or "已由编辑批准的新版替代")
        conn.commit()
        return {"ok": True, "isbn": req["isbn"],
                "active_generation_id": new_gen["generation_id"]}


@app.post("/api/admin/replacement/{request_id}/reject")
def reject_replacement(request_id: int, body: dict,
                       editor: dict = Depends(require_editor)):
    with tx() as conn:
        row = conn.execute(
            "UPDATE replacement_requests SET status='rejected', decided_by=%s, "
            "decided_at=now() WHERE request_id=%s RETURNING *",
            (editor["username"], request_id)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "替代申请不存在")
    return dict(row)


@app.post("/api/admin/generations/{generation_id}/retire")
def retire_generation(generation_id: int, editor: dict = Depends(require_editor)):
    """将已归档代次标记为不可访问，用于演示引用固定代次失效后的诚实失败。"""
    with tx() as conn:
        row = conn.execute(
            "UPDATE index_generations SET status='retired' "
            "WHERE generation_id=%s AND status='archived' RETURNING *",
            (generation_id,)).fetchone()
        conn.commit()
    if not row:
        raise HTTPException(409, "仅已归档(archived)的代次可退役")
    with tx() as conn:
        planning.mark_editions_deprecated(
            conn, [row["edition_id"]], "计划固定代次已退役，出处原文不可访问")
        conn.commit()
    return dict(row)


# ---------------------------------------------------------------- 课程阅读规划

def _planning_error(exc: Exception) -> HTTPException:
    if isinstance(exc, planning.CycleError):
        return HTTPException(422, {"message": str(exc), "cycles": exc.cycles})
    return HTTPException(422, str(exc))


@app.get("/api/planning/objectives")
def planning_objectives(subject: str | None = None,
                        user: dict = Depends(require_teacher)):
    with tx() as conn:
        return planning.list_objectives(conn, subject)


@app.post("/api/planning/objectives")
def create_objective(req: ObjectiveIn, user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            row = planning.create_objective(conn, req, user["username"])
            conn.commit()
            return row
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.get("/api/planning/cycles")
def planning_cycles(user: dict = Depends(require_teacher)):
    with tx() as conn:
        return {"cycles": planning._all_cycles(conn)}


@app.get("/api/planning/editions/{edition_id}/metrics")
def edition_metrics(edition_id: int, user: dict = Depends(require_teacher)):
    with tx() as conn:
        return planning.list_metrics(conn, edition_id)


@app.post("/api/planning/metrics")
def set_metric(req: ChapterMetricIn, user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            row = planning.upsert_metric(
                conn, req.edition_id, req.chapter_code,
                req.reading_length, user["username"])
            conn.commit()
            return row
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.get("/api/planning/links")
def planning_links(objective_code: str | None = None, status: str = "all",
                   user: dict = Depends(require_teacher)):
    with tx() as conn:
        return planning.list_links(conn, objective_code, status)


@app.post("/api/planning/links")
def create_link(req: ChapterLinkIn, user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            row = planning.upsert_link(conn, req.model_dump(), user["username"])
            conn.commit()
            return row
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.get("/api/planning/objectives/{objective_code}/candidates")
def link_candidates(objective_code: str,
                    edition_ids: list[int] = Query(default=[]),
                    user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            return planning.chapter_candidates(conn, objective_code, edition_ids)
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.post("/api/planning/links/{link_id}/review")
def review_link(link_id: int, req: ChapterLinkReview,
                user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            row = planning.review_link(conn, link_id, req.status, user["username"])
            conn.commit()
            return row
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.get("/api/planning/plans")
def list_plans(user: dict = Depends(require_teacher)):
    with tx() as conn:
        return planning.list_plans(conn)


@app.post("/api/planning/plans")
def create_plan(req: PlanCreate, user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            return planning.create_plan(conn, req, user["username"])
        except planning.CycleError as exc:
            raise _planning_error(exc)
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.get("/api/planning/plans/{plan_id}")
def get_plan(plan_id: int, user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            return planning.get_plan(conn, plan_id)
        except planning.PlanningError as exc:
            raise HTTPException(404, str(exc))


@app.post("/api/planning/plans/{plan_id}/cancel")
def cancel_plan(plan_id: int, user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            row = planning.cancel_plan(conn, plan_id)
            return row
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.post("/api/planning/plans/{plan_id}/recompute")
def recompute_plan(plan_id: int, req: PlanRecompute,
                   user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            return planning.recompute_plan(conn, plan_id,
                                           req.model_dump(exclude_none=True),
                                           user["username"])
        except planning.PlanningError as exc:
            raise _planning_error(exc)


@app.post("/api/planning/plans/{plan_id}/confirm")
def confirm_plan(plan_id: int, user: dict = Depends(require_teacher)):
    with tx() as conn:
        try:
            row = planning.confirm_plan(conn, plan_id)
            return row
        except planning.PlanningError as exc:
            raise _planning_error(exc)


# ---------------------------------------------------------------- 前端静态资源

static_dir = os.getenv("STATIC_DIR", "/workspace/20/frontend/dist")
if os.path.isdir(static_dir):
    from fastapi.staticfiles import StaticFiles
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")),
              name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):  # pragma: no cover - 仅容器内生效
        from fastapi.responses import FileResponse
        target = os.path.join(static_dir, full_path)
        if full_path and os.path.isfile(target):
            return FileResponse(target)
        return FileResponse(os.path.join(static_dir, "index.html"))
