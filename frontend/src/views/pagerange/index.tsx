import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/pagerange'
const statuses = ['已生成', '待确认', '消费中', '已消费', '已废弃']
const modes = ['页码', '偏移量', '双模式']
const columns = ['范围编号', '规则编号', '规则版本', '翻页模式', '页数', '总条数', '终止依据', '结果版本', '状态']

type Row = Record<string, any>

const emptyComputeForm = {
  规则编号: '',
  规则版本: '',
  翻页模式: '跟随规则',
  页大小: '',
  总条数: '',
  空页信号: '',
  查询条件: '{}',
}

export default function PagerangeView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState<Row>({})
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [computeForm, setComputeForm] = useState<Record<string, string>>(emptyComputeForm)
  const [detail, setDetail] = useState<Row | null>(null)
  const [locateInput, setLocateInput] = useState('')
  const [locateResult, setLocateResult] = useState<Row | null>(null)
  const [replayReport, setReplayReport] = useState<Row | null>(null)

  const stats = useMemo(
    () => [
      { label: '范围总数', value: summary['范围总数'] ?? 0 },
      { label: '已生成', value: summary['已生成'] ?? 0 },
      { label: '待确认扩版', value: summary['待确认'] ?? 0 },
      { label: '已消费', value: summary['已消费'] ?? 0 },
    ],
    [summary],
  )

  const reloadSummary = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/summary`)
      if (response.ok) {
        setSummary(await response.json())
      }
    } catch {
      /* 汇总失败不阻断列表 */
    }
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (filters.keyword) query.set('keyword', filters.keyword)
    if (filters.status) query.set('status', filters.status)
    if (filters.mode) query.set('mode', filters.mode)
    if (filters.condition) query.set('condition', filters.condition)
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('翻页采集范围列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      await reloadSummary()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '翻页采集范围列表读取失败')
    }
  }, [filters, reloadSummary])

  useEffect(() => {
    void reload()
  }, [reload])

  async function computeRange() {
    setErrorMessage('')
    setNotice('')
    const values: Record<string, unknown> = { 规则编号: computeForm.规则编号 }
    if (computeForm.规则版本) values.规则版本 = computeForm.规则版本
    if (computeForm.翻页模式 && computeForm.翻页模式 !== '跟随规则') values.翻页模式 = computeForm.翻页模式
    if (computeForm.页大小) values.页大小 = computeForm.页大小
    if (computeForm.总条数) values.总条数 = computeForm.总条数
    if (computeForm.空页信号) values.空页信号 = computeForm.空页信号
    if (computeForm.查询条件) values.查询条件 = computeForm.查询条件
    try {
      const response = await request(`${ENDPOINT}/compute`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '范围生成失败')
      }
      setNotice(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '范围生成失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    setLocateResult(null)
    setReplayReport(null)
    setLocateInput('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('范围明细读取失败')
      }
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '范围明细读取失败')
    }
  }

  async function runAction(row: Row, action: string) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '动作未生效')
      }
      setNotice(payload.message)
      if (detail && detail.id === row.id) {
        await openDetail(row)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '动作执行失败')
    }
  }

  async function replay(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/replay`)
      if (!response.ok) {
        throw new Error('重放校验失败')
      }
      setReplayReport(await response.json())
      if (!detail || detail.id !== row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重放校验失败')
    }
  }

  async function locate() {
    setErrorMessage('')
    if (!detail) return
    const pageNo = locateInput.trim()
    if (!pageNo) {
      setErrorMessage('请输入要定位的页码')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${detail.id}/locate?page_no=${encodeURIComponent(pageNo)}`)
      if (!response.ok) {
        throw new Error('定位失败')
      }
      setLocateResult(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '定位失败')
    }
  }

  return (
    <section className="page" data-module="pagerange">
      <header className="page-head">
        <div>
          <h2>翻页采集范围</h2>
          <p className="page-desc">
            读取页码参数、偏移量参数与总条数，校验双模式边界并生成有序、去重、可重放的页面清单；
            规则版本与终止依据全程可追溯。
          </p>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form
        className="filter-bar"
        onSubmit={(event) => {
          event.preventDefault()
          void computeRange()
        }}
      >
        <label className="filter-item">
          <span>规则编号</span>
          <input
            value={computeForm.规则编号}
            placeholder="如 PAGE-0003"
            onChange={(event) => setComputeForm((prev) => ({ ...prev, 规则编号: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>规则版本</span>
          <input
            value={computeForm.规则版本}
            placeholder="留空跟随规则"
            onChange={(event) => setComputeForm((prev) => ({ ...prev, 规则版本: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>翻页模式</span>
          <select
            value={computeForm.翻页模式}
            onChange={(event) => setComputeForm((prev) => ({ ...prev, 翻页模式: event.target.value }))}
          >
            <option value="跟随规则">跟随规则</option>
            {modes.map((mode) => (
              <option key={mode} value={mode}>{mode}</option>
            ))}
          </select>
        </label>
        <label className="filter-item">
          <span>页大小</span>
          <input
            value={computeForm.页大小}
            placeholder="留空跟随规则"
            onChange={(event) => setComputeForm((prev) => ({ ...prev, 页大小: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>总条数</span>
          <input
            value={computeForm.总条数}
            placeholder="留空=未知"
            onChange={(event) => setComputeForm((prev) => ({ ...prev, 总条数: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>空页信号</span>
          <input
            value={computeForm.空页信号}
            placeholder="逗号分隔，如 4,5"
            onChange={(event) => setComputeForm((prev) => ({ ...prev, 空页信号: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>查询条件(JSON)</span>
          <input
            value={computeForm.查询条件}
            placeholder='{"类目":"图书"}'
            onChange={(event) => setComputeForm((prev) => ({ ...prev, 查询条件: event.target.value }))}
          />
        </label>
        <button className="btn primary" type="submit">生成/重算范围</button>
      </form>

      <form
        className="filter-bar"
        onSubmit={(event) => {
          event.preventDefault()
          void reload()
        }}
      >
        <label className="filter-item">
          <span>范围/规则编号</span>
          <input
            value={filters.keyword ?? ''}
            placeholder="按范围编号或规则编号检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, keyword: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => (
              <option key={status} value={status}>{status}</option>
            ))}
          </select>
        </label>
        <label className="filter-item">
          <span>翻页模式</span>
          <select
            value={filters.mode ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, mode: event.target.value }))}
          >
            <option value="">全部模式</option>
            {modes.map((mode) => (
              <option key={mode} value={mode}>{mode}</option>
            ))}
          </select>
        </label>
        <label className="filter-item">
          <span>查询条件包含</span>
          <input
            value={filters.condition ?? ''}
            placeholder="按查询条件内容检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, condition: event.target.value }))}
          />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{row['范围编号']}</td>
              <td>{row['规则编号']}</td>
              <td>{row['规则版本']}</td>
              <td>{row['翻页模式']}</td>
              <td>{row['页数']}</td>
              <td>{row['总条数'] ?? '未知'}</td>
              <td>{row['终止依据']}</td>
              <td>{row['结果版本'] ?? '—'}</td>
              <td>{row['status']}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>页面清单</button>
                <button className="link" type="button" onClick={() => void replay(row)}>重放校验</button>
                {row['status'] === '待确认' && (
                  <button className="link" type="button" onClick={() => void runAction(row, '确认扩版')}>确认扩版</button>
                )}
                {row['status'] !== '已废弃' && (
                  <button className="link" type="button" onClick={() => void runAction(row, '废弃范围')}>废弃范围</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无翻页采集范围，可先在上方生成</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="panel">
          <h3 className="panel-title">
            范围 {detail['范围编号']}（规则 {detail['规则编号']} / {detail['规则版本']} / {detail['翻页模式']}）
          </h3>
          <p className="page-desc">
            终止依据：{detail['终止依据']}
            {detail['空页终止原因'] ? `；空页终止原因：${detail['空页终止原因']}` : ''}
            {detail['冲突裁决说明'] ? `；冲突裁决：${detail['冲突裁决说明']}` : ''}
            {detail['扩版说明'] ? `；扩版说明：${detail['扩版说明']}` : ''}
          </p>
          {detail['双边界对照'] && (
            <p className="page-desc">
              双边界对照：页码边界 {detail['双边界对照']['页码边界页数']} 页（{detail['双边界对照']['页码边界终止']}），
              偏移量边界 {detail['双边界对照']['偏移量边界页数']} 页（{detail['双边界对照']['偏移量边界终止']}）
            </p>
          )}

          <div className="filter-bar">
            <label className="filter-item">
              <span>定位页码</span>
              <input
                value={locateInput}
                placeholder="输入页码定位当前分页结果"
                onChange={(event) => setLocateInput(event.target.value)}
              />
            </label>
            <button className="btn" type="button" onClick={() => void locate()}>定位</button>
            {locateResult && (
              <span className="page-desc">
                {locateResult['命中']
                  ? `第 ${locateResult['序号']} 页 / 共 ${locateResult['总页数']} 页（进度 ${locateResult['进度']}），上一页 ${locateResult['上一页'] ?? '—'}，下一页 ${locateResult['下一页'] ?? '—'}`
                  : locateResult['说明']}
              </span>
            )}
          </div>

          {replayReport && (
            <p className="page-desc">
              重放校验：{replayReport['说明']}（有序 {String(replayReport['有序'])}、无重复 {String(replayReport['无重复'])}、
              未越界 {String(replayReport['未越界'])}、重放一致 {String(replayReport['重放一致'])}，
              存储 {replayReport['存储页数']} 页 / 重放 {replayReport['重放页数']} 页）
            </p>
          )}

          <table className="data-table">
            <thead>
              <tr>
                <th>序号</th>
                <th>页码</th>
                <th>偏移量</th>
                <th>请求参数</th>
              </tr>
            </thead>
            <tbody>
              {(detail['页面清单'] ?? []).map((page: Row) => (
                <tr key={page['序号']}>
                  <td>{page['序号']}</td>
                  <td>{page['页码']}</td>
                  <td>{page['偏移量']}</td>
                  <td><code>{JSON.stringify(page['请求参数'])}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 份翻页采集范围</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
