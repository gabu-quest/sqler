import { ref, type Ref } from 'vue'

/**
 * Custom error class that preserves HTTP status codes from API responses.
 * Use `instanceof ApiError` to check for API errors and access the status code.
 */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

interface ApiOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  headers?: Record<string, string>
  etag?: string
}

interface ApiResult<T> {
  data: Ref<T | null>
  error: Ref<string | null>
  loading: Ref<boolean>
  etag: Ref<string | null>
  execute: () => Promise<T | null>
}

export function useApi<T>(url: string | (() => string), options: ApiOptions = {}): ApiResult<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const error = ref<string | null>(null)
  const loading = ref(false)
  const etag = ref<string | null>(null)

  const execute = async (): Promise<T | null> => {
    loading.value = true
    error.value = null

    try {
      const resolvedUrl = typeof url === 'function' ? url() : url
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...options.headers
      }

      if (options.etag) {
        headers['If-Match'] = options.etag
      }

      const response = await fetch(resolvedUrl, {
        method: options.method || 'GET',
        headers,
        body: options.body ? JSON.stringify(options.body) : undefined
      })

      // Store ETag from response
      const responseEtag = response.headers.get('ETag')
      if (responseEtag) {
        etag.value = responseEtag
      }

      if (response.status === 304) {
        // Not Modified - data unchanged
        return data.value
      }

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ detail: response.statusText }))
        throw new ApiError(response.status, errorData.detail || response.statusText)
      }

      const result = await response.json()
      data.value = result
      return result
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'Unknown error'
      return null
    } finally {
      loading.value = false
    }
  }

  return { data, error, loading, etag, execute }
}

// Convenience wrapper for immediate execution
export async function fetchApi<T>(url: string, options: ApiOptions = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...options.headers
  }

  if (options.etag) {
    headers['If-Match'] = options.etag
  }

  const response = await fetch(url, {
    method: options.method || 'GET',
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined
  })

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: response.statusText }))
    throw new ApiError(response.status, errorData.detail || response.statusText)
  }

  return response.json()
}
