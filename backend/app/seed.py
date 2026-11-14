"""演示数据自动播种：构建各代次、批准现行版、预置同义词。

仅在空库时执行一次，保证幂等。场景覆盖：
- 初中数学旧版（gen1）与新版（gen2，删除旧例题）：默认现行稿为新版，旧版停用归档；
- 高中数学（同名“函数”，集合映射前提）；
- 物理册仅提交一章，模拟构建中断 -> 整本 failed，不可启用；
- 已审同义词（可直接扩展）与一条待审同义词（编辑审核流程）。
"""
from __future__ import annotations

import os

from . import importer
from .config import settings
from .db import tx

DATA = settings.data_dir


def _path(*parts: str) -> str:
    return os.path.join(DATA, *parts)


def _approve(conn, edition_id: int) -> None:
    req = conn.execute(
        "SELECT * FROM replacement_requests WHERE new_edition_id=%s "
        "AND status='pending' ORDER BY request_id", (edition_id,)).fetchone()
    if not req:
        return
    new_gen = conn.execute(
        "SELECT g.generation_id FROM index_generations g "
        "JOIN textbook_editions e ON e.edition_id=g.edition_id "
        "WHERE g.edition_id=%s AND g.status='ready'", (edition_id,)).fetchone()
    old = conn.execute(
        "SELECT e.edition_id, g.generation_id FROM textbook_editions e "
        "JOIN index_generations g ON g.edition_id=e.edition_id "
        "WHERE e.isbn=%s AND e.status='active'", (req["isbn"],)).fetchall()
    for o in old:
        conn.execute(
            "UPDATE textbook_editions SET status='deprecated', superseded_by=%s, "
            "supersede_reason=coalesce(nullif(supersede_reason,''),%s) "
            "WHERE edition_id=%s",
            (edition_id, req["reason"] or "已由编辑批准的新版替代", o["edition_id"]))
        conn.execute(
            "UPDATE index_generations SET status='archived' WHERE generation_id=%s",
            (o["generation_id"],))
    conn.execute("UPDATE textbook_editions SET status='active' WHERE edition_id=%s",
                 (edition_id,))
    conn.execute("UPDATE textbooks SET current_generation_id=%s WHERE isbn=%s",
                 (new_gen["generation_id"], req["isbn"]))
    conn.execute(
        "UPDATE replacement_requests SET status='approved', decided_by='seed', "
        "decided_at=now() WHERE request_id=%s", (req["request_id"],))


def _synonym(conn, subject: str, term: str, syn: str, status: str) -> None:
    from .text import normalize
    tn, _ = normalize(term)
    sn, sd = normalize(syn)
    conn.execute(
        "INSERT INTO synonyms"
        "(subject, term_norm, term_display, synonym_norm, synonym_display, "
        " status, created_by, reviewed_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,'seed',%s) "
        "ON CONFLICT (subject, term_norm, synonym_norm) DO NOTHING",
        (subject, tn, term, sn, syn, status,
         "seed" if status == "approved" else None),
    )


def seed_all() -> None:
    with tx() as conn:
        already = conn.execute("SELECT count(*) AS c FROM textbooks").fetchone()["c"]
    if already:
        return

    # 1. 初中数学旧版 -> 批准为现行
    r1 = importer.build_from_payload(_path("9787100000011", "payload.json"))
    with tx() as conn:
        _approve(conn, _edition_id(conn, r1["generation_id"]))
        conn.commit()

    # 2. 初中数学新版 -> 批准切换，旧版停用归档
    r2 = importer.build_from_payload(_path("9787100000011-gen2", "payload.json"))
    with tx() as conn:
        _approve(conn, _edition_id(conn, r2["generation_id"]))
        conn.commit()

    # 3. 高中数学 -> 批准为现行
    r3 = importer.build_from_payload(_path("9787100000028", "payload.json"))
    with tx() as conn:
        _approve(conn, _edition_id(conn, r3["generation_id"]))
        conn.commit()

    # 4. 物理册构建中断：只提交第 13 章，第 14 章缺失 -> 整本 failed
    importer.build_from_payload(
        _path("9787100000035", "payload.json"), chapter_codes=["W-CH13"])

    # 5. 同义词：已审两条 + 待审一条
    with tx() as conn:
        _synonym(conn, "数学", "因变量", "函数值", "approved")
        _synonym(conn, "数学", "取值范围", "定义域", "approved")
        _synonym(conn, "物理", "热运动", "无规则运动", "approved")
        _synonym(conn, "数学", "正比例", "正比关系", "pending")
        conn.commit()

    # 6. 课程阅读规划演示数据（目标、先修回路、覆盖审核样本与六个计划样本）
    from .seed_planning import seed_planning
    seed_planning()


def _edition_id(conn, generation_id: int) -> int:
    return conn.execute(
        "SELECT edition_id FROM index_generations WHERE generation_id=%s",
        (generation_id,)).fetchone()["edition_id"]
