import { AnimatePresence, motion } from 'framer-motion'
import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  principalFromUrl,
  startSession,
  submitAnswer,
  type AnswerResponse,
  type SessionStarted,
} from './api/client'
import { Celebration, GoingToTeacher } from './components/Celebration'
import { HangingVines } from './components/HangingVines'
import { Header, ProfileBadge, ProgressTrail, StatsGroup } from './components/Header'
import { Mascot, type MascotMood } from './components/Mascot'
import { QuestionCard } from './components/QuestionCard'

/**
 * The spec's app states, plus the two the backend can actually produce that the
 * spec did not name.
 *
 * `going-to-teacher` is the one that matters. `services/api/student.py` ends a
 * session that way whenever no hint could be cleared or the hint levels ran out,
 * and it is explicitly not a loss state — so it needs its own screen rather than
 * being folded into `celebration` as its sad twin.
 */
type Phase = 'loading' | 'question' | 'hint-revealed' | 'celebration' | 'going-to-teacher' | 'error'

export function App() {
  const [principal] = useState(principalFromUrl)
  const [session, setSession] = useState<SessionStarted | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [answer, setAnswer] = useState('')
  const [hints, setHints] = useState<string[]>([])
  const [last, setLast] = useState<AnswerResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const begin = useCallback(async () => {
    if (!principal) {
      setError('missing-principal')
      setPhase('error')
      return
    }
    setPhase('loading')
    setAnswer('')
    setHints([])
    setLast(null)
    try {
      setSession(await startSession(principal))
      setPhase('question')
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 403
          ? 'not-a-student'
          : caught instanceof ApiError
            ? caught.message
            : 'offline',
      )
      setPhase('error')
    }
  }, [principal])

  useEffect(() => {
    void begin()
  }, [begin])

  async function send() {
    if (!principal || !session) return
    setBusy(true)
    try {
      const result = await submitAnswer(principal, session.session_id, answer)
      setLast(result)

      if (result.correct) {
        setPhase('celebration')
      } else if (result.done) {
        // Out of hints, or nothing could be cleared. Still show the last hint
        // if one arrived — the child asked for help and it exists.
        if (result.hint) setHints((prior) => [...prior, result.hint as string])
        setPhase('going-to-teacher')
      } else if (result.hint) {
        setHints((prior) => [...prior, result.hint as string])
        setAnswer('')
        setPhase('hint-revealed')
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'offline')
      setPhase('error')
    } finally {
      setBusy(false)
    }
  }

  const maxHints = session?.max_hint_level ?? 3

  const mood: MascotMood = busy
    ? 'thinking'
    : phase === 'celebration'
      ? 'celebrating'
      : phase === 'going-to-teacher'
        ? 'warm'
        : phase === 'hint-revealed'
          ? 'pointing'
          : 'encouraging'

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden bg-jungle-dark jungle-canopy">
      <Header>
        <ProfileBadge name="Explorer" />
        <ProgressTrail reached={hints.length} total={maxHints} />
        <StatsGroup hintsLeft={Math.max(maxHints - hints.length, 0)} total={maxHints} />
      </Header>

      <HangingVines maxHintLevel={maxHints} hintsShown={hints} />

      {/* Main area: px:80, py:40, gap:48 between mascot and question card. */}
      <main className="flex min-h-0 flex-1 items-center justify-center gap-12 px-20 py-10">
        {phase !== 'error' && <Mascot mood={mood} />}

        <AnimatePresence mode="wait">
          {phase === 'loading' && (
            <motion.p
              key="loading"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="font-display text-subheading font-extrabold text-sky-blue"
            >
              Finding you a question…
            </motion.p>
          )}

          {(phase === 'question' || phase === 'hint-revealed') && session && (
            <QuestionCard
              key={`${session.session_id}-${hints.length}`}
              problem={session.problem}
              answer={answer}
              onAnswerChange={setAnswer}
              onSubmit={() => void send()}
              busy={busy}
              locked={false}
            />
          )}

          {phase === 'celebration' && (
            <Celebration
              key="celebration"
              message={last?.message ?? 'You got it.'}
              onNext={() => void begin()}
            />
          )}

          {phase === 'going-to-teacher' && (
            <GoingToTeacher
              key="teacher"
              message={last?.message ?? "Let's come back to this one."}
              onNext={() => void begin()}
            />
          )}

          {phase === 'error' && <ErrorPanel key="error" reason={error} onRetry={() => void begin()} />}
        </AnimatePresence>
      </main>
    </div>
  )
}

/**
 * Errors phrased for whoever is sitting with the child, not for the child.
 *
 * A seven-year-old cannot act on "403 this surface is for students", and a
 * stack-trace-shaped message reads to them as something they broke. The adult
 * gets the detail; the child gets a jungle that is briefly quiet.
 */
function ErrorPanel({ reason, onRetry }: { reason: string | null; onRetry: () => void }) {
  const help =
    reason === 'missing-principal'
      ? 'Add ?as=<student-principal-id> to the address. Run `python -m scripts.seed_pilot` to list them.'
      : reason === 'not-a-student'
        ? 'That id is not a student. The student page needs a student principal; teachers have their own console.'
        : reason === 'offline'
          ? 'The tutor server is not answering. Start it with `python -m scripts.serve`.'
          : (reason ?? 'Something went wrong.')

  return (
    <motion.section
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex max-w-[520px] flex-col items-center gap-4 rounded-card border-4 border-bark-mid bg-bark-dark p-10 text-center"
    >
      <span aria-hidden="true" className="text-[44px] leading-none">
        🌙
      </span>
      <h2 className="font-display text-heading font-black text-fruit-amber">
        The jungle is quiet just now
      </h2>
      <p className="font-body text-body font-bold text-muted">{help}</p>
      <button
        onClick={onRetry}
        className="rounded-control bg-fruit-amber px-6 py-3 font-display text-button font-extrabold text-bark-dark"
      >
        Try again
      </button>
    </motion.section>
  )
}
