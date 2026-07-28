/**
 * The student API, typed to match `services/api/student.py` exactly.
 *
 * Two things this file deliberately does not have, and both are load-bearing:
 *
 * **No `correctAnswer` anywhere.** The server never sends one — `_SafeProblem`
 * makes that structural on its side — and there is no field here to receive one
 * if that ever changed. A child with the network tab open is still a child in
 * the pilot.
 *
 * **No client-side grading.** `submitAnswer` sends what the child typed and is
 * told whether it was right. The client has nothing to compare against, which is
 * the same rule stated from this end.
 */

export interface Visual {
  /** `ten_frame` when both parts fit in ten and it is addition; `number_line`
   *  otherwise. The server picks, because a ten-frame showing `13 - 8` is a
   *  picture that contradicts the question. */
  kind: 'ten_frame' | 'number_line'
  left: number
  right: number
  operation: string
}

export interface SafeProblem {
  id: string
  prompt: string
  grade_band: string
  visual: Visual | null
}

export interface SessionStarted {
  session_id: string
  problem: SafeProblem
  attempt: number
  max_hint_level: number
}

export interface AnswerResponse {
  correct: boolean
  hint: string | null
  hint_level: number | null
  attempt: number
  done: boolean
  going_to_teacher: boolean
  message: string | null
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * Pilot-grade identity, matching `services/api/auth.py`: the principal id comes
 * from `?as=` and is sent as a header. There is no password and no token — the
 * backend says so in its own docstring, and this client is not the place to
 * imply otherwise.
 */
export function principalFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('as')
}

async function call<T>(path: string, principal: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-principal-id': principal,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })

  if (!response.ok) {
    // The child never sees this string; `App` maps status to something a
    // seven-year-old can act on. Keeping the server's detail here means the
    // console is still useful to whoever is helping them.
    let detail = response.statusText
    try {
      const parsed: unknown = await response.json()
      if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
        detail = String((parsed as { detail: unknown }).detail)
      }
    } catch {
      /* a non-JSON error body is still an error */
    }
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}

export function startSession(principal: string): Promise<SessionStarted> {
  return call<SessionStarted>('/student/session', principal)
}

export function submitAnswer(
  principal: string,
  sessionId: string,
  answer: string,
): Promise<AnswerResponse> {
  return call<AnswerResponse>(`/student/session/${sessionId}/answer`, principal, { answer })
}
