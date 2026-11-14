import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'

const LINK_STATUS = { pending: '待审候选', approved: '已审通过', rejected: '已驳回' }

export default function PlanningObjectives() {
  const [objectives, setObjectives] = useState([])
  const [cycles, setCycles] = useState([])
  const [books, setBooks] = useState([])
  const [selectedCode, setSelectedCode] = useState('')
  const [links, setLinks] = useState([])
  const [candidates, setCandidates] = useState([])
  const [metricsByEdition, setMetricsByEdition] = useState({})
  const [form, setForm] = useState({
    code: '', title: '', subject: '数学', stage: '高中',
    description: '', prereq_codes: '',
  })
  const [linkForm, setLinkForm] = useState({ edition_id: '', chapter_code: '', weight: 1 })
  const [msg, setMsg] = useState('')

  async function loadAll() {
    const [obs, cyc, bks] = await Promise.all([
      api.get('/api/planning/objectives'),
      api.get('/api/planning/cycles'),
      api.get('/api/textbooks'),
    ])
    setObjectives(obs)
    setCycles(cyc.cycles)
    setBooks(bks.filter((b) => b.active_generation_id))
    if (!selectedCode && obs.length) setSelectedCode(obs[0].code)
  }
  useEffect(() => { loadAll() }, [])

  const activeEditions = useMemo(
    () => books.flatMap((b) => b.editions
      .filter((e) => e.status === 'active')
      .map((e) => ({ ...e, book_title: b.title, isbn: b.isbn }))),
    [books])

  async function loadMetrics(editionId) {
    if (!editionId || metricsByEdition[editionId]) return
    setMetricsByEdition({
      ...metricsByEdition,
      [editionId]: await api.get(`/api/planning/editions/${editionId}/metrics`),
    })
  }
  useEffect(() => { activeEditions.forEach((e) => loadMetrics(e.edition_id)) }, [activeEditions])

  async function loadDetail(code) {
    setSelectedCode(code)
    setCandidates([])
    const [lk] = await Promise.all([
      api.get(`/api/planning/links?objective_code=${code}&status=all`),
    ])
    setLinks(lk)
  }
  useEffect(() => { if (selectedCode) loadDetail(selectedCode) }, [selectedCode])

  async function createObjective(e) {
    e.preventDefault()
    setMsg('')
    try {
      const body = {
        ...form,
        prereq_codes: form.prereq_codes.split(/[，,]/).map((s) => s.trim()).filter(Boolean),
      }
      await api.post('/api/planning/objectives', body)
      setForm({ ...form, code: '', title: '', description: '', prereq_codes: '' })
      setMsg('目标已录入；若先修成环，系统会拦截并指出涉及目标。')
      await loadAll()
    } catch (err) { setMsg(err.message) }
  }

  async function searchCandidates() {
    setMsg('')
    const ids = activeEditions.map((e) => e.edition_id)
    try {
      const rows = await api.get(
        `/api/planning/objectives/${selectedCode}/candidates?`
        + ids.map((i) => `edition_ids=${i}`).join('&'))
      setCandidates(rows)
      if (!rows.length) setMsg('现行版本中没有新的章节候选（已有关联的章节不重复列出）。')
    } catch (err) { setMsg(err.message) }
  }

  async function addCandidate(c, origin) {
    await api.post('/api/planning/links', {
      objective_code: selectedCode,
      edition_id: c.edition_id, chapter_code: c.chapter_code,
      weight: 1, origin,
      evidence: origin === 'search_candidate'
        ? { score: c.score, matched_terms: c.matched_terms } : {},
    })
    await loadDetail(selectedCode)
    setCandidates(candidates.filter((x) =>
      !(x.edition_id === c.edition_id && x.chapter_code === c.chapter_code)))
  }

  async function review(id, status) {
    await api.post(`/api/planning/links/${id}/review`, { status })
    await loadDetail(selectedCode)
  }

  async function addManualLink(e) {
    e.preventDefault()
    if (!linkForm.edition_id || !linkForm.chapter_code) return
    await api.post('/api/planning/links', {
      objective_code: selectedCode, ...linkForm,
      edition_id: Number(linkForm.edition_id), weight: Number(linkForm.weight),
      origin: 'manual',
    })
    setLinkForm({ ...linkForm, chapter_code: '' })
    await loadDetail(selectedCode)
  }

  async function saveLength(editionId, chapterCode, raw) {
    const value = raw.trim() === '' ? null : Number(raw)
    await api.post('/api/planning/metrics', {
      edition_id: editionId, chapter_code: chapterCode, reading_length: value,
    })
    setMsg(`章节 ${chapterCode} 篇幅已${value === null ? '标记为未知（不可自动选入）' : `更新为 ${value} 字`}`)
    setMetricsByEdition({
      ...metricsByEdition,
      [editionId]: await api.get(`/api/planning/editions/${editionId}/metrics`),
    })
  }

  const currentLinks = links
  const editionName = (id) =>
    activeEditions.find((e) => e.edition_id === id)?.book_title || `版本${id}`

  return (
    <div className="planning-page">
      <h2>教学目标与章节覆盖审核</h2>

      {cycles.length > 0 && (
        <section className="card cycle-card">
          <h3>检测到先修依赖回路（涉及目标无法进入规划）</h3>
          {cycles.map((c, i) => (
            <div key={i} className="cycle-path">🔁 {c.join(' → ')}</div>
          ))}
          <p className="muted small">请先解除回路（调整先修关系），再创建阅读规划。</p>
        </section>
      )}

      <section className="card">
        <h3>录入教学目标与先修关系</h3>
        <form className="syn-form" onSubmit={createObjective}>
          <input placeholder="目标编码（如 OBJ-A1）" required value={form.code}
            onChange={(e) => setForm({ ...form, code: e.target.value })} />
          <input placeholder="目标标题" required value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <select value={form.subject}
            onChange={(e) => setForm({ ...form, subject: e.target.value })}>
            <option>数学</option><option>物理</option>
          </select>
          <input placeholder="学段" value={form.stage}
            onChange={(e) => setForm({ ...form, stage: e.target.value })} />
          <input className="wide-input" placeholder="先修目标编码，逗号分隔（如 OBJ-A1,OBJ-A2）"
            value={form.prereq_codes}
            onChange={(e) => setForm({ ...form, prereq_codes: e.target.value })} />
          <input className="wide-input" placeholder="目标描述（也用于生成章节检索候选）"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <button className="btn primary" type="submit">录入目标</button>
        </form>
        {msg && <div className="notice">{msg}</div>}
      </section>

      <section className="card">
        <h3>目标清单（点击选择，维护章节覆盖）</h3>
        <table className="grid">
          <thead><tr><th>编码</th><th>目标</th><th>学科/学段</th><th>先修</th><th>状态</th></tr></thead>
          <tbody>
            {objectives.map((o) => (
              <tr key={o.objective_id}
                className={o.code === selectedCode ? 'row-selected' : ''}
                onClick={() => loadDetail(o.code)} style={{ cursor: 'pointer' }}>
                <td>{o.code}</td><td>{o.title}</td>
                <td className="small">{o.subject}/{o.stage}</td>
                <td className="small">{o.prereq_codes.join('、') || '—'}</td>
                <td>{o.in_cycle
                  ? <span className="badge bad">先修成环</span>
                  : <span className="badge ok">正常</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {selectedCode && (
        <section className="card">
          <h3>目标 {selectedCode} 的章节覆盖关联（未审核命中仅作候选）</h3>
          <div className="filter-row">
            <button className="btn" onClick={searchCandidates}>按目标文本检索章节候选</button>
            <span className="muted small">检索命中不会自动用于规划，审核通过后才进入已审关联。</span>
          </div>
          {candidates.length > 0 && (
            <table className="grid">
              <thead><tr><th>候选章节</th><th>命中词项/次数</th><th>操作</th></tr></thead>
              <tbody>
                {candidates.map((c) => (
                  <tr key={`${c.edition_id}-${c.chapter_code}`}>
                    <td>{editionName(c.edition_id)} · {c.chapter_path}</td>
                    <td className="small">{c.matched_terms.join('、')}（tf合计 {c.score}）</td>
                    <td><button className="btn ghost"
                      onClick={() => addCandidate(c, 'search_candidate')}>登记为候选关联</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <form className="syn-form" onSubmit={addManualLink} style={{ marginTop: 12 }}>
            <select required value={linkForm.edition_id}
              onChange={(e) => setLinkForm({ ...linkForm, edition_id: e.target.value, chapter_code: '' })}>
              <option value="">选择教材版本</option>
              {activeEditions.map((e) => (
                <option key={e.edition_id} value={e.edition_id}>
                  {e.book_title} · 第{e.generation}版
                </option>
              ))}
            </select>
            <select required value={linkForm.chapter_code}
              onChange={(e) => setLinkForm({ ...linkForm, chapter_code: e.target.value })}>
              <option value="">选择章节</option>
              {(metricsByEdition[linkForm.edition_id] || []).map((m) => (
                <option key={m.chapter_code} value={m.chapter_code}>
                  {m.chapter_path}{m.reading_length === null ? '（篇幅未知）' : `（${m.reading_length}字）`}
                </option>
              ))}
            </select>
            <input type="number" min="0.1" step="0.1" style={{ width: 110 }}
              value={linkForm.weight}
              onChange={(e) => setLinkForm({ ...linkForm, weight: e.target.value })} />
            <button className="btn primary" type="submit">手工登记关联</button>
          </form>

          <table className="grid" style={{ marginTop: 12 }}>
            <thead><tr><th>教材</th><th>章节</th><th>权重</th><th>来源/状态</th><th>审核</th></tr></thead>
            <tbody>
              {currentLinks.map((l) => (
                <tr key={l.link_id}>
                  <td className="small">{editionName(l.edition_id)}</td>
                  <td>{l.chapter_code}</td>
                  <td>{Number(l.weight)}</td>
                  <td>
                    <span className="muted small">{l.origin === 'search_candidate' ? '检索候选' : '手工登记'} · </span>
                    <span className={`badge syn-${l.status}`}>{LINK_STATUS[l.status]}</span>
                  </td>
                  <td>
                    {l.status !== 'approved' && (
                      <button className="btn ghost" onClick={() => review(l.link_id, 'approved')}>通过</button>)}
                    {l.status !== 'rejected' && (
                      <button className="btn ghost danger" onClick={() => review(l.link_id, 'rejected')}>驳回</button>)}
                  </td>
                </tr>
              ))}
              {currentLinks.length === 0 && (
                <tr><td colSpan="5" className="muted">暂无关联；该目标若没有任何已审关联，规划时将诊断为“缺教材”。</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <section className="card">
        <h3>章节篇幅维护（留空 = 篇幅未知，规划不可自动选入，不会按零成本处理）</h3>
        {activeEditions.map((e) => (
          <div key={e.edition_id} style={{ marginBottom: 14 }}>
            <strong>{e.book_title} · 第{e.generation}版</strong>
            <table className="grid" style={{ marginTop: 6 }}>
              <thead><tr><th>章节</th><th>阅读篇幅（字）</th><th>来源</th><th></th></tr></thead>
              <tbody>
                {(metricsByEdition[e.edition_id] || []).map((m) => (
                  <MetricRow key={m.chapter_code} m={m}
                    onSave={(raw) => saveLength(e.edition_id, m.chapter_code, raw)} />
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </section>
    </div>
  )
}

function MetricRow({ m, onSave }) {
  const [val, setVal] = useState(m.reading_length === null ? '' : String(m.reading_length))
  return (
    <tr>
      <td>{m.chapter_path}</td>
      <td>
        <input type="number" min="1" style={{ width: 140 }} placeholder="未知"
          value={val} onChange={(e) => setVal(e.target.value)} />
      </td>
      <td className="small muted">{m.source === 'payload' ? '数据包声明' : '人工维护'}</td>
      <td><button className="btn ghost" onClick={() => onSave(val)}>保存篇幅</button></td>
    </tr>
  )
}
