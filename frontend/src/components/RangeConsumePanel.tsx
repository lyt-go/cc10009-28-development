import { useCallback, useEffect, useState } from 'react'

import { request } from '@/api/client'

type Row = Record<string, any>

interface Props {
  consumer: 'urlfilter' | 'crawljob'
  title: string
}

/** 翻页范围消费面板：消费可重放翻页采集范围，并展示本侧与对侧的消费回执清单。 */
export default function RangeConsumePanel({ consumer, title }: Props) {
  const [items, setItems] = useState<Row[]>([])
  const [summary, setSummary] = useState<Row>({})
  const [rangeCode, setRangeCode] = useState('')
  const [jobCode, setJobCode] = useState('')
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')

  const reload = useCallback(async () => {
    try {
      const response = await request(`/api/${consumer}/range-manifest`)
      if (!response.ok) {
        throw new Error('消费清单读取失败')
      }
      const payload = await response.json()
      setItems(payload.items ?? [])
      setSummary(payload.summary ?? {})
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '消费清单读取失败')
    }
  }, [consumer])

  useEffect(() => {
    void reload()
  }, [reload])

  async function consume() {
    setMessage('')
    setErrorMessage('')
    if (!rangeCode.trim()) {
      setErrorMessage('请输入要消费的范围编号，如 PR-0001')
      return
    }
    const values: Record<string, string> = { 范围编号: rangeCode.trim() }
    if (jobCode.trim()) {
      values.作业编号 = jobCode.trim()
    }
    try {
      const response = await request(`/api/${consumer}/consume-range`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '消费失败')
      }
      setMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '消费失败')
    }
  }

  return (
    <section className="panel">
      <h3 className="panel-title">{title}</h3>
      <p className="page-desc">
        消费范围数 {summary['消费范围数'] ?? 0}，本侧已消费 {summary['本侧已消费'] ?? 0}，
        对侧已消费 {summary['对侧已消费'] ?? 0}，双侧已完成 {summary['双侧已完成'] ?? 0}。
        重复消费只生效一次，并发回执不会多出第二份结果。
      </p>
      <div className="filter-bar">
        <label className="filter-item">
          <span>范围编号</span>
          <input
            value={rangeCode}
            placeholder="如 PR-0001"
            onChange={(event) => setRangeCode(event.target.value)}
          />
        </label>
        <label className="filter-item">
          <span>作业编号（可选）</span>
          <input
            value={jobCode}
            placeholder="关联的抓取作业"
            onChange={(event) => setJobCode(event.target.value)}
          />
        </label>
        <button className="btn primary" type="button" onClick={() => void consume()}>消费范围</button>
      </div>
      {message && <p className="page-desc">{message}</p>}
      {errorMessage && <p className="error-text">{errorMessage}</p>}
      <table className="data-table">
        <thead>
          <tr>
            <th>范围编号</th>
            <th>规则编号</th>
            <th>规则版本</th>
            <th>页数</th>
            <th>终止依据</th>
            <th>结果版本</th>
            <th>范围状态</th>
            <th>本侧回执</th>
            <th>对侧回执</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item['范围编号']}>
              <td>{item['范围编号']}</td>
              <td>{item['规则编号']}</td>
              <td>{item['规则版本']}</td>
              <td>{item['页数']}</td>
              <td>{item['终止依据']}</td>
              <td>{item['结果版本'] ?? '—'}</td>
              <td>{item['范围状态']}</td>
              <td>{item['本侧回执'] ? item['本侧回执']['回执编号'] : '未消费'}</td>
              <td>{item['对侧回执'] ? `${item['对侧回执']['回执编号']}` : `待${item['对侧消费方']}消费`}</td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={9}>暂无消费记录，可先在翻页范围页面生成范围</td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  )
}
