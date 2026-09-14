/**
 * 取数 hook：够用就好的那一档。
 *
 * 没上任何数据请求库——控制台每个面板就是「进页面拉一次 + 手动刷新」，
 * 用 `useEffect` + 一个自增的 nonce 就能覆盖，省掉几 KB 依赖。
 */
import { useCallback, useEffect, useRef, useState } from 'preact/hooks'
import { apiGet, describeError } from './bridge'

export interface QueryState<T> {
  data: T | null
  error: string
  loading: boolean
  reload: () => void
}

export function useQuery<T>(
  endpoint: string,
  params?: Record<string, unknown>,
  options: { enabled?: boolean; nonce?: number } = {},
): QueryState<T> {
  const enabled = options.enabled !== false
  // 外部刷新令牌：顶栏的「刷新」按钮会把它加一，各面板据此重新取数。
  const external = options.nonce ?? 0
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(enabled)
  const [nonce, setNonce] = useState(0)
  const [settledKey, setSettledKey] = useState('')
  // 参数是对象字面量，直接进依赖数组会每渲染都变；序列化后当键用。
  const key = JSON.stringify(params ?? {})
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setError('')
    const parsed = key === '{}' ? undefined : (JSON.parse(key) as Record<string, unknown>)
    apiGet<T>(endpoint, parsed)
      .then((result) => {
        if (cancelled || !alive.current) return
        setData(result)
        setSettledKey(key)
      })
      .catch((failure: unknown) => {
        if (cancelled || !alive.current) return
        setError(describeError(failure))
      })
      .finally(() => {
        if (cancelled || !alive.current) return
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [endpoint, key, nonce, external, enabled])

  const reload = useCallback(() => setNonce((value) => value + 1), [])
  // 参数变了但新结果还没回来时，旧的 data 属于上一个查询，不能再拿去渲染。
  const stale = settledKey !== key && Boolean(data)
  return { data: stale ? data : data, error, loading, reload }
}

/** 轮询：只在页面可见时跑，切到后台就停，别在用户不看的时候空烧请求。 */
export function useInterval(callback: () => void, ms: number | null): void {
  const saved = useRef(callback)
  useEffect(() => {
    saved.current = callback
  }, [callback])
  useEffect(() => {
    if (ms === null) return
    const tick = () => {
      if (document.visibilityState === 'visible') saved.current()
    }
    const timer = window.setInterval(tick, ms)
    return () => window.clearInterval(timer)
  }, [ms])
}
