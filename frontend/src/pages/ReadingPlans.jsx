import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'

const PLAN_STATUS = {
  draft: '草稿', queued: '排队中', running: '计算中', ready: '方案就绪',
  failed: '计算失败', cancelled: '已取消', confirmed: '已确认发送',
  needs_review: '待复核（教材停用）',
}
const REASON_LABEL = {
  required: '锁定必读', already_known: '已有基础（不计阅读量）', greedy: '贪心选入',
}

export default function ReadingPlans() {
  const [plans, setPlans] = useState([])
  const [objectives, setObjectives] = useState([])
  const [books, setBooks] = useState([])
  const [detailId, setDetailId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [form, setForm] = useState({
    name: '', objective_codes: [], reading_budget: 3000,
    edition_ids: [], locked_required: [], locked_known: [],
  })
  const [msg, setMsg] = useState('')

  async function loadList() {
    setPlans(await api.get('/api/planning/plans'))
  }
  useEffect(() => {
    loadList()
    api.get('/api/planning/objectives').then(setObjectives)
    api.get('/api/textbooks').then(setBooks)
  }, [])

  const activeEditions = useMemo(
    () => books.flatMap((b) => b.editions
      .filter((e) => e.status === 'active')
      .map((e) => ({ ...e, book_title: b.title, isbn: b.isbn }))),
    [books])

  const editionName = (id) =>
    activeEditions.find((e) => e.edition_id === id)?.book_title
    || detail?.edition_statuses.find((e) => e.edition_id === id)?.title
    || `版本${id}`

  async function openPlan(id) {
    setDetailId(id)
    setDetail(await api.get(`/api/planning/plans/${id}`))
  }

  // 计算中自动轮询；取消后旧计算不覆盖当前选择
  useEffect(() => {
    if (!detail || !['queued', 'running'].includes(detail.status)) return
    const timer = setInterval(async () => {
      const fresh = await api.get(`/api/planning/plans/${detail.plan_id}`)
      setDetail(fresh)
      loadList()
    }, 800)
    return () => clearInterval(timer)
  }, [detail?.plan_id, detail?.status])

  function toggleObjective(code) {
    setForm((f) => ({
      ...f,
      objective_codes: f.objective_codes.includes(code)
        ? f.objective_codes.filter((c) => c !== code)
        : [...f.objective_codes, code],
    }))
  }

  function addLock(kind, editionId, chapterCode) {
    if (!editionId || !chapterCode) return
    const item = { edition_id: Number(editionId), chapter_code: chapterCode }
    setForm((f) => {
      const key = kind === 'required' ? 'locked_required' : 'locked_known'
      const other = kind === 'required' ? 'locked_known' : 'locked_required'
      return {
        ...f,
        [key]: f[key].some((x) => x.edition_id === item.edition_id
          && x.chapter_code === item.chapter_code)
          ? f[key]
          : [...f[key], item],
        [other]: f[other].filter((x) =>
          !(x.edition_id === item.edition_id && x.chapter_code === item.chapter_code)),
      }
    })
  }

  async function createPlan(e) {
    e.preventDefault()
    setMsg('')
    if (!form.objective_codes.length) { setMsg('请至少选择一个教学目标'); return }
    try {
      const plan = await api.post('/api/planning/plans', {
        ...form,
        edition_ids: form.edition_ids.map(Number),
        reading_budget: Number(form.reading_budget),
      })
      setMsg('计划已创建：目标、先修关系、已审关联与教材版本已快照固定，后台开始计算。')
      setForm({ name: '', objective_codes: [], reading_budget: 3000,
        edition_ids: [], locked_required: [], locked_known: [] })
      await loadList()
      openPlan(plan.plan_id)
    } catch (err) { setMsg(err.message) }
  }

  async function cancel() {
    setDetail(await api.post(`/api/planning/plans/${detail.plan_id}/cancel`))
    loadList()
  }
  async function confirm() {
    setDetail(await api.post(`/api/planning/plans/${detail.plan_id}/confirm`))
    loadList()
  }
  async function recompute(budget) {
    setDetail(await api.post(`/api/planning/plans/${detail.plan_id}/recompute`,
      { reading_budget: Number(budget) }))
    loadList()
  }

  const cyclic = objectives.filter((o) => o.in_cycle)

  return (
    <div className="planning-page">
      <h2>课程阅读规划</h2>

      {cyclic.length > 0 && (
        <div className="notice">
          存在先修成环目标：{cyclic.map((o) => o.code).join('、')}，
          含这些目标的计划会被拦截，请先在“目标与覆盖审核”页解除回路。
        </div>
      )}

      <section className="card">
        <h3>新建阅读规划</h3>
        <form onSubmit={createPlan}>
          <div className="syn-form">
            <input placeholder="计划名称" required value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <input type="number" min="1" style={{ width: 160 }}
              value={form.reading_budget}
              onChange={(e) => setForm({ ...form, reading_budget: e.target.value })}
              title="阅读量上限（字）" />
            <span className="muted small">阅读量上限（字），篇幅未知章节不会按零成本计入</span>
          </div>
          <div className="obj-picker">
            {objectives.map((o) => (
              <button type="button" key={o.objective_id}
                className={`chip ${form.objective_codes.includes(o.code) ? 'on' : ''} ${o.in_cycle ? 'chip-danger' : ''}`}
                title={o.in_cycle ? '该目标处于先修回路中' : (o.prereq_codes?.join('、') || '无先修')}
                onClick={() => !o.in_cycle && toggleObjective(o.code)}>
                {o.code} {o.title}{o.in_cycle ? ' 🔁' : ''}
              </button>
            ))}
          </div>
          <div className="syn-form" style={{ marginTop: 10 }}>
            <select multiple value={form.edition_ids.map(String)}
              onChange={(e) => setForm({
                ...form,
                edition_ids: Array.from(e.target.selectedOptions).map((o) => o.value),
              })}
              style={{ minWidth: 320, height: 70 }}>
              {activeEditions.map((ed) => (
                <option key={ed.edition_id} value={ed.edition_id}>
                  {ed.book_title} · 第{ed.generation}版（不选=该学科全部现行版）
                </option>
              ))}
            </select>
            <LockPicker editions={activeEditions} kind="required"
              locks={form.locked_required} onAdd={addLock} />
            <LockPicker editions={activeEditions} kind="known"
              locks={form.locked_known} onAdd={addLock} />
          </div>
          <div style={{ marginTop: 10 }}>
            <button className="btn primary" type="submit">创建并计算</button>
          </div>
        </form>
        {msg && <div className="notice">{msg}</div>}
      </section>

      <section className="card">
        <h3>计划列表</h3>
        <table className="grid">
          <thead><tr><th>#</th><th>名称</th><th>状态</th><th>阅读量/上限</th>
            <th>覆盖/未覆盖</th><th></th></tr></thead>
          <tbody>
            {plans.map((p) => (
              <tr key={p.plan_id} className={detailId === p.plan_id ? 'row-selected' : ''}>
                <td>{p.plan_id}</td>
                <td>{p.name}</td>
                <td><span className={`badge plan-${p.status}`}>{PLAN_STATUS[p.status]}</span>
                  {p.review_reason && <div className="small muted">{p.review_reason.slice(0, 40)}…</div>}</td>
                <td>{p.total_length} / {p.reading_budget}</td>
                <td>{p.covered_count} / {p.covered_count + p.uncovered_count}</td>
                <td><button className="btn ghost" onClick={() => openPlan(p.plan_id)}>查看</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail && <PlanDetail plan={detail} editionName={editionName}
        onCancel={cancel} onConfirm={confirm} onRecompute={recompute} />}
    </div>
  )
}

function LockPicker({ editions, kind, locks, onAdd }) {
  const [editionId, setEditionId] = useState('')
  const [chapter, setChapter] = useState('')
  const [metrics, setMetrics] = useState([])
  useEffect(() => {
    setChapter('')
    if (!editionId) { setMetrics([]); return }
    api.get(`/api/planning/editions/${editionId}/metrics`).then(setMetrics)
  }, [editionId])
  return (
    <div className="lock-picker">
      <div className="small muted">
        {kind === 'required' ? '锁定必读章（超额也保留，不删必读凑可行）' : '锁定已有基础（覆盖目标免阅读量）'}
      </div>
      <select value={editionId} onChange={(e) => setEditionId(e.target.value)}>
        <option value="">选择版本</option>
        {editions.map((e) => <option key={e.edition_id} value={e.edition_id}>
          {e.book_title} 第{e.generation}版</option>)}
      </select>
      <select value={chapter} onChange={(e) => setChapter(e.target.value)}>
        <option value="">选择章节</option>
        {metrics.map((m) => <option key={m.chapter_code} value={m.chapter_code}>
          {m.chapter_path}{m.reading_length === null ? '（篇幅未知）' : `（${m.reading_length}字）`}
        </option>)}
      </select>
      <button type="button" className="btn"
        onClick={() => onAdd(kind, editionId, chapter)}>加入锁定</button>
      <div className="small">
        {locks.map((l) => <span key={`${l.edition_id}-${l.chapter_code}`} className="lock-tag">
          {l.chapter_code}（版本{l.edition_id}）</span>)}
      </div>
    </div>
  )
}

function PlanDetail({ plan, editionName, onCancel, onConfirm, onRecompute }) {
  const [budget, setBudget] = useState(plan.reading_budget)
  useEffect(() => { setBudget(plan.reading_budget) }, [plan.plan_id, plan.reading_budget])
  const overrun = plan.total_length > plan.reading_budget
  return (
    <section className="card plan-detail">
      <h3>计划 #{plan.plan_id} {plan.name}</h3>
      <div className="plan-status-line">
        <span className={`badge plan-${plan.status}`}>{PLAN_STATUS[plan.status]}</span>
        <span className="muted small">
          固定版本：{plan.edition_statuses.map((e) =>
            `${e.title}第${e.generation}版[${e.status}]`).join('；')}
        </span>
      </div>

      {plan.status === 'failed' && <div className="error">{plan.error}</div>}
      {plan.review_reason && (
        <div className="no-hit">
          <strong>教材停用待复核：</strong>{plan.review_reason}
          {plan.status === 'confirmed' && <div>计划已发送给教师，所选出处与正文保留不变，仅提示更新。</div>}
        </div>
      )}
      {plan.warnings?.length > 0 && (
        <div className="warning-list">
          {plan.warnings.map((w, i) => <div key={i} className="warn-line">⚠️ {w}</div>)}
        </div>
      )}

      {['queued', 'running'].includes(plan.status) && (
        <div className="filter-row">
          <span className="muted">后台计算中（每 0.8 秒刷新）…</span>
          <button className="btn danger" onClick={onCancel}>取消计算</button>
        </div>
      )}
      {plan.status === 'cancelled' && (
        <div className="notice">已取消：即使旧计算稍后返回，也不会替换当前选择（作业令牌校验）。</div>
      )}

      {['ready', 'needs_review', 'confirmed'].includes(plan.status) && (
        <>
          <div className="budget-line">
            阅读量合计 <strong className={overrun ? 'text-bad' : ''}>{plan.total_length}</strong>
            / 上限 {plan.reading_budget} 字
            {overrun && <span className="badge bad">锁定超额 {plan.total_length - plan.reading_budget} 字，已保留冲突</span>}
            <span className="muted small">剩余 {Math.max(0, plan.reading_budget - plan.total_length)} 字</span>
          </div>

          <h4>选入章节（{plan.selected_chapters.length}）</h4>
          <table className="grid">
            <thead><tr><th>章节</th><th>篇幅</th><th>选入原因</th><th>新增覆盖权重</th><th>权重/篇幅</th></tr></thead>
            <tbody>
              {plan.selected_chapters.map((c, i) => (
                <tr key={`${c.edition_id}-${c.chapter_code}-${i}`}>
                  <td>{editionName(c.edition_id)} · {c.chapter_path}</td>
                  <td>{c.reading_length === null ? '未知（不计费）' : `${c.reading_length} 字`}
                    {!c.charged_length && c.reading_length !== null && <span className="muted small">（已有基础，零增量）</span>}</td>
                  <td><span className="badge">{REASON_LABEL[c.reason] || c.reason}</span></td>
                  <td>{c.added_weight ?? '—'}</td>
                  <td>{c.ratio ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4>目标覆盖情况</h4>
          <div className="obj-cov">
            <div><strong className="text-ok">已覆盖（{plan.covered_objective_ids.length}）：</strong>
              {plan.requested_objectives.filter((o) =>
                plan.covered_objective_ids.includes(o.objective_id)).map((o) =>
                <span key={o.objective_id} className="chip on">{o.code}</span>)}
            </div>
          </div>

          <h4>未覆盖项逐项解释（点击展开：为何未选 + 增加多少阅读量可补齐）</h4>
          {plan.uncovered.length === 0
            ? <div className="notice">全部目标已覆盖（含先修闭包）。</div>
            : plan.uncovered.map((u) => <UncoveredCard key={u.objective_id} u={u}
                editionName={editionName} budget={plan.reading_budget} />)}

          <div className="plan-actions">
            <div className="syn-form">
              <input type="number" min="1" style={{ width: 150 }} value={budget}
                onChange={(e) => setBudget(e.target.value)} />
              <button className="btn"
                disabled={plan.status === 'confirmed'}
                onClick={() => onRecompute(budget)}>
                按同一组已审资料重算
              </button>
              {plan.status !== 'confirmed'
                ? <button className="btn primary" onClick={onConfirm}>确认并发送给教师</button>
                : <span className="badge syn-approved">已确认，出处固定保留</span>}
            </div>
            <p className="muted small">{plan.algorithm_note}</p>
          </div>
        </>
      )}

      <details className="events">
        <summary>计划事件留痕（创建/计算/取消/停用联动）</summary>
        <table className="grid">
          <tbody>
            {plan.events.map((ev) => (
              <tr key={ev.event_id}>
                <td className="small muted">{ev.created_at}</td>
                <td className="small">{ev.event_type}</td>
                <td className="small">{ev.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  )
}

function UncoveredCard({ u, editionName, budget }) {
  return (
    <details className="uncovered-card">
      <summary>
        <span className="badge bad">{u.code}</span> {u.title}
        {!u.is_requested && <span className="muted small">（先修闭包补入的目标）</span>}
        {u.reason_labels.map((r, i) => <span key={i} className="reason-chip">{r.split('：')[0]}</span>)}
      </summary>
      <div className="unc-body">
        {u.reason_labels.map((r, i) => <div key={i}>• {r}</div>)}
        {u.cheapest_selectable && (
          <div className="notice">
            最省的可自动选入章节：{editionName(u.cheapest_selectable.edition_id)} ·
            {' '}{u.cheapest_selectable.chapter_path}（{u.cheapest_selectable.reading_length} 字）；
            当前剩余 {u.remaining_budget} 字，
            <strong> 至少再增加 {u.extra_length_to_cover} 字阅读量</strong> 可直接补齐该目标。
          </div>
        )}
        {u.candidate_chapters.length > 0 && (
          <table className="grid">
            <thead><tr><th>候选章节</th><th>篇幅</th><th>未选原因</th></tr></thead>
            <tbody>
              {u.candidate_chapters.map((c, i) => (
                <tr key={i}>
                  <td className="small">{editionName(c.edition_id)} · {c.chapter_path}</td>
                  <td>{c.reading_length === null ? '未知' : `${c.reading_length} 字`}</td>
                  <td className="small">{c.excluded_reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </details>
  )
}
