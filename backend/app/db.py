"""PostgreSQL 连接池与 schema 初始化（启动时幂等建表）。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None

DDL = r"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.users (
    username     text PRIMARY KEY,
    display_name text NOT NULL,
    role         text NOT NULL CHECK (role IN ('editor', 'teacher'))
);

CREATE TABLE IF NOT EXISTS {schema}.textbooks (
    isbn                    text PRIMARY KEY,
    title                   text NOT NULL,
    subject                 text NOT NULL,
    stage                   text NOT NULL,
    volume                  text NOT NULL DEFAULT '',
    semester                text NOT NULL DEFAULT '',
    publisher               text NOT NULL DEFAULT '',
    current_generation_id   bigint,
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.textbook_editions (
    edition_id              bigserial PRIMARY KEY,
    isbn                    text NOT NULL REFERENCES {schema}.textbooks(isbn),
    generation              integer NOT NULL,
    title                   text NOT NULL,
    stage                   text NOT NULL,
    subject                 text NOT NULL,
    volume                  text NOT NULL DEFAULT '',
    semester                text NOT NULL DEFAULT '',
    status                  text NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft', 'active', 'deprecated', 'rejected')),
    supersede_reason        text NOT NULL DEFAULT '',
    superseded_by           bigint,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (isbn, generation)
);

CREATE TABLE IF NOT EXISTS {schema}.replacement_requests (
    request_id      bigserial PRIMARY KEY,
    isbn            text NOT NULL,
    old_edition_id  bigint,
    new_edition_id  bigint NOT NULL,
    reason          text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by      text,
    decided_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.index_generations (
    generation_id   bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    isbn            text NOT NULL,
    job_token       text NOT NULL,
    status          text NOT NULL DEFAULT 'building'
                    CHECK (status IN ('building', 'ready', 'failed', 'abandoned',
                                      'archived', 'retired')),
    total_chapters  integer NOT NULL DEFAULT 0,
    ok_chapters     integer NOT NULL DEFAULT 0,
    failed_chapters integer NOT NULL DEFAULT 0,
    total_units     integer NOT NULL DEFAULT 0,
    total_terms     integer NOT NULL DEFAULT 0,
    total_postings  integer NOT NULL DEFAULT 0,
    error           text NOT NULL DEFAULT '',
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

CREATE TABLE IF NOT EXISTS {schema}.chapter_snapshots (
    id              bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL,
    chapter_path    text NOT NULL,
    ordinal         integer NOT NULL,
    status          text NOT NULL CHECK (status IN ('ok', 'error')),
    error           text NOT NULL DEFAULT '',
    payload         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    UNIQUE (generation_id, chapter_code)
);

CREATE TABLE IF NOT EXISTS {schema}.structural_units (
    unit_id         bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL,
    unit_code       text NOT NULL,
    unit_type       text NOT NULL CHECK (unit_type IN ('paragraph', 'example', 'figure_note')),
    section_path    text NOT NULL,
    title_path      text NOT NULL,
    ordinal_in_book integer NOT NULL,
    parent_unit_id  bigint,
    text            text NOT NULL,
    conditions      jsonb NOT NULL DEFAULT '[]'::jsonb,
    content_hash    text NOT NULL,
    norm_text       text NOT NULL,
    norm_mapping    jsonb NOT NULL,
    sentence_ranges jsonb NOT NULL,
    UNIQUE (edition_id, unit_code)
);
CREATE INDEX IF NOT EXISTS idx_units_generation ON {schema}.structural_units (generation_id);
CREATE INDEX IF NOT EXISTS idx_units_edition ON {schema}.structural_units (edition_id);

CREATE TABLE IF NOT EXISTS {schema}.index_chapter_reports (
    id              bigserial PRIMARY KEY,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL,
    stage           text NOT NULL CHECK (stage IN ('received', 'parsed', 'terms', 'verified')),
    status          text NOT NULL CHECK (status IN ('ok', 'error')),
    counts          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    error           text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reports_gen ON {schema}.index_chapter_reports (generation_id);

CREATE TABLE IF NOT EXISTS {schema}.postings (
    generation_id   bigint NOT NULL,
    term            text NOT NULL,
    unit_id         bigint NOT NULL,
    tf              integer NOT NULL,
    positions       jsonb NOT NULL,
    PRIMARY KEY (generation_id, term, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_postings_unit ON {schema}.structural_units (generation_id, unit_id);
CREATE INDEX IF NOT EXISTS idx_postings_term ON {schema}.postings (generation_id, term);

CREATE TABLE IF NOT EXISTS {schema}.term_dfs (
    generation_id   bigint NOT NULL,
    term            text NOT NULL,
    df              integer NOT NULL,
    collection_tf   integer NOT NULL,
    PRIMARY KEY (generation_id, term)
);

CREATE TABLE IF NOT EXISTS {schema}.index_stats (
    generation_id   bigint PRIMARY KEY,
    doc_count       integer NOT NULL,
    avgdl           numeric NOT NULL,
    bm25_k1         numeric NOT NULL,
    bm25_b          numeric NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.synonyms (
    synonym_id      bigserial PRIMARY KEY,
    subject         text NOT NULL,
    term_norm       text NOT NULL,
    term_display    text NOT NULL,
    synonym_norm    text NOT NULL,
    synonym_display text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    created_by      text NOT NULL,
    reviewed_by     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    reviewed_at     timestamptz,
    UNIQUE (subject, term_norm, synonym_norm)
);

CREATE TABLE IF NOT EXISTS {schema}.chapter_corrections (
    correction_id   bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    chapter_code    text NOT NULL,
    payload         jsonb NOT NULL,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (edition_id, chapter_code)
);

CREATE TABLE IF NOT EXISTS {schema}.parse_issues (
    issue_id        bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    generation_id   bigint NOT NULL,
    chapter_code    text NOT NULL DEFAULT '',
    unit_code       text NOT NULL DEFAULT '',
    issue_type      text NOT NULL CHECK (issue_type IN (
                        'missing_figure', 'duplicate_id', 'missing_premise',
                        'parse_error', 'chapter_error')),
    detail          text NOT NULL,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_issues_gen ON {schema}.parse_issues (generation_id);

CREATE TABLE IF NOT EXISTS {schema}.search_sessions (
    session_id      uuid PRIMARY KEY,
    username        text NOT NULL,
    generation_id   bigint NOT NULL,
    query_text      text NOT NULL,
    filters         jsonb NOT NULL,
    query_terms     jsonb NOT NULL,
    expanded_terms  jsonb NOT NULL,
    synonym_enabled boolean NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.query_log (
    id              bigserial PRIMARY KEY,
    username        text NOT NULL,
    query_text      text NOT NULL,
    query_terms     jsonb NOT NULL,
    expanded_terms  jsonb NOT NULL,
    filters         jsonb NOT NULL,
    generation_id   bigint,
    hit_count       integer NOT NULL,
    no_hit_reason   text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.citations (
    citation_id     bigserial PRIMARY KEY,
    username        text NOT NULL,
    generation_id   bigint NOT NULL,
    edition_id      bigint NOT NULL,
    unit_id         bigint NOT NULL,
    unit_code       text NOT NULL,
    quoted_text     text NOT NULL,
    content_hash    text NOT NULL,
    locator         jsonb NOT NULL,
    last_status     text NOT NULL DEFAULT 'ok'
                    CHECK (last_status IN ('ok', 'drifted', 'edition_unavailable')),
    last_detail     text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz
);

CREATE TABLE IF NOT EXISTS {schema}.citation_checks (
    id              bigserial PRIMARY KEY,
    citation_id     bigint NOT NULL,
    status          text NOT NULL,
    detail          text NOT NULL,
    checked_at      timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- 课程阅读规划：教学目标、先修关系、覆盖关联、章节篇幅、计划
-- ============================================================

-- 教学目标（教研员录入，先修关系另表）
CREATE TABLE IF NOT EXISTS {schema}.learning_objectives (
    objective_id    bigserial PRIMARY KEY,
    code            text NOT NULL UNIQUE,
    title           text NOT NULL,
    subject         text NOT NULL,
    stage           text NOT NULL DEFAULT '',
    description     text NOT NULL DEFAULT '',
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- 目标先修关系：objective_id 的学习以 requires_objective_id 为前提
CREATE TABLE IF NOT EXISTS {schema}.objective_prereqs (
    objective_id            bigint NOT NULL REFERENCES {schema}.learning_objectives(objective_id)
                            ON DELETE CASCADE,
    requires_objective_id   bigint NOT NULL REFERENCES {schema}.learning_objectives(objective_id)
                            ON DELETE CASCADE,
    PRIMARY KEY (objective_id, requires_objective_id),
    CHECK (objective_id <> requires_objective_id)
);

-- 章节篇幅：长度可能未知（reading_length IS NULL），未知篇幅不可被自动选入
CREATE TABLE IF NOT EXISTS {schema}.chapter_metrics (
    metric_id       bigserial PRIMARY KEY,
    edition_id      bigint NOT NULL,
    chapter_code    text NOT NULL,
    reading_length  integer,
    source          text NOT NULL DEFAULT 'manual'
                    CHECK (source IN ('payload', 'manual')),
    updated_by      text NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (edition_id, chapter_code)
);

-- 目标-章节覆盖关联：搜索命中仅为候选(pending)，教师审核通过(approved)后才可用于规划
CREATE TABLE IF NOT EXISTS {schema}.objective_chapter_links (
    link_id         bigserial PRIMARY KEY,
    objective_id    bigint NOT NULL REFERENCES {schema}.learning_objectives(objective_id)
                    ON DELETE CASCADE,
    edition_id      bigint NOT NULL,
    chapter_code    text NOT NULL,
    weight          numeric(10,2) NOT NULL DEFAULT 1 CHECK (weight > 0),
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    origin          text NOT NULL DEFAULT 'manual'
                    CHECK (origin IN ('manual', 'search_candidate')),
    evidence        jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_by      text NOT NULL,
    reviewed_by     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    reviewed_at     timestamptz,
    UNIQUE (objective_id, edition_id, chapter_code)
);
CREATE INDEX IF NOT EXISTS idx_links_objective ON {schema}.objective_chapter_links (objective_id);
CREATE INDEX IF NOT EXISTS idx_links_status ON {schema}.objective_chapter_links (status);

-- 阅读规划主表：目标集合、教材版本与已审关联在创建时整体快照固定
CREATE TABLE IF NOT EXISTS {schema}.reading_plans (
    plan_id             bigserial PRIMARY KEY,
    name                text NOT NULL,
    subject             text NOT NULL,
    reading_budget      integer NOT NULL CHECK (reading_budget > 0),
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'queued', 'running', 'ready',
                                          'failed', 'cancelled', 'confirmed',
                                          'needs_review')),
    created_by          text NOT NULL,
    -- 计划固定的教材版本（edition_id 集合）与创建时刻已审关联快照
    edition_ids         bigint[] NOT NULL DEFAULT '{{}}',
    links_snapshot      jsonb NOT NULL DEFAULT '[]'::jsonb,
    objectives_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- 创建时刻章节目录与篇幅快照（含未知篇幅），重算沿用，不随后续改价变动
    metrics_snapshot    jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- 计算结果
    selected_chapters   jsonb NOT NULL DEFAULT '[]'::jsonb,
    total_length        integer NOT NULL DEFAULT 0,
    covered_objective_ids  bigint[] NOT NULL DEFAULT '{{}}',
    uncovered           jsonb NOT NULL DEFAULT '[]'::jsonb,
    warnings            jsonb NOT NULL DEFAULT '[]'::jsonb,
    algorithm_note      text NOT NULL DEFAULT '',
    -- 后台作业控制：计算令牌与取消标志，过期作业不得覆盖当前选择
    job_token           uuid,
    cancel_requested    boolean NOT NULL DEFAULT false,
    error               text NOT NULL DEFAULT '',
    -- 停用联动
    review_reason       text NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now(),
    computed_at         timestamptz,
    confirmed_at        timestamptz
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON {schema}.reading_plans (status);

-- 计划内目标（计划创建时固定，重算只采用同一组目标/已审资料）
CREATE TABLE IF NOT EXISTS {schema}.plan_objectives (
    plan_id         bigint NOT NULL REFERENCES {schema}.reading_plans(plan_id) ON DELETE CASCADE,
    objective_id    bigint NOT NULL,
    PRIMARY KEY (plan_id, objective_id)
);

-- 教研员锁定：必读章节(required)与学生已有基础(already_known)
CREATE TABLE IF NOT EXISTS {schema}.plan_locked_chapters (
    id              bigserial PRIMARY KEY,
    plan_id         bigint NOT NULL REFERENCES {schema}.reading_plans(plan_id) ON DELETE CASCADE,
    edition_id      bigint NOT NULL,
    chapter_code    text NOT NULL,
    lock_type       text NOT NULL CHECK (lock_type IN ('required', 'already_known')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, edition_id, chapter_code, lock_type)
);

-- 计划状态流转留痕（取消、停用触发复核等）
CREATE TABLE IF NOT EXISTS {schema}.plan_events (
    event_id        bigserial PRIMARY KEY,
    plan_id         bigint NOT NULL REFERENCES {schema}.reading_plans(plan_id) ON DELETE CASCADE,
    event_type      text NOT NULL,
    detail          text NOT NULL DEFAULT '',
    created_by      text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_plan_events ON {schema}.plan_events (plan_id);
"""


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={
                "row_factory": dict_row,
                "options": f"-c search_path={settings.search_schema}",
            },
            check=ConnectionPool.check_connection,
        )
    return _pool


@contextmanager
def tx() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    with get_pool().connection() as conn:
        yield conn


def init_schema() -> None:
    with tx() as conn:
        conn.execute(DDL.format(schema=settings.search_schema))
        # 幂等迁移：为已存在的旧表补齐后加字段
        conn.execute(
            "ALTER TABLE " + settings.search_schema +
            ".structural_units ADD COLUMN IF NOT EXISTS conditions jsonb "
            "NOT NULL DEFAULT '[]'::jsonb")
        conn.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(settings.search_schema)
            )
        )
        conn.execute(
            "INSERT INTO users(username, display_name, role) VALUES "
            "('editor1', '王编辑', 'editor'), ('teacher1', '李老师', 'teacher') "
            "ON CONFLICT (username) DO NOTHING"
        )
        conn.commit()
