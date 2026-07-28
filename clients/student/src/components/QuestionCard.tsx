import { motion } from 'framer-motion'
import type { FormEvent } from 'react'
import type { SafeProblem } from '../api/client'
import { questionEntrance } from '../lib/motion'
import { Visual } from './Visual'

/**
 * The wooden sign the question is carved into. Card internal gap: 24px.
 *
 * The input is never disabled on a wrong answer and never cleared without the
 * child's say-so — a box that empties itself after a wrong try makes them retype
 * what they were about to correct.
 */
export function QuestionCard({
  problem,
  answer,
  onAnswerChange,
  onSubmit,
  busy,
  locked,
}: {
  problem: SafeProblem
  answer: string
  onAnswerChange: (value: string) => void
  onSubmit: () => void
  busy: boolean
  locked: boolean
}) {
  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (!busy && !locked && answer.trim()) onSubmit()
  }

  return (
    <motion.section
      variants={questionEntrance}
      initial="hidden"
      animate="visible"
      exit="exit"
      aria-labelledby="question-heading"
      className="flex w-full max-w-[640px] flex-col gap-6 rounded-card border-4 border-bark-mid bg-bark-dark p-8 shadow-2xl"
    >
      <div className="flex flex-col gap-2">
        <span className="font-body text-caption font-semibold uppercase tracking-widest text-fruit-yellow">
          {problem.grade_band}
        </span>
        <h1
          id="question-heading"
          className="font-display text-question font-extrabold text-ink text-shadow-deep"
        >
          {problem.prompt}
        </h1>
      </div>

      <Visual visual={problem.visual} />

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <label
          htmlFor="answer"
          className="font-display text-subheading font-extrabold text-sky-blue"
        >
          Your answer
        </label>
        <div className="flex gap-3">
          <input
            id="answer"
            name="answer"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            autoFocus
            maxLength={500}
            value={answer}
            disabled={locked}
            onChange={(event) => onAnswerChange(event.target.value)}
            placeholder="Type it here"
            className="min-w-0 flex-1 rounded-control border-2 border-bark-mid bg-white px-4 py-3 font-display text-question font-extrabold text-bark-dark placeholder:font-body placeholder:text-body placeholder:font-bold placeholder:text-muted focus:border-fruit-yellow focus:outline-none focus:ring-4 focus:ring-fruit-yellow/40 disabled:opacity-60"
          />
          <motion.button
            type="submit"
            disabled={busy || locked || !answer.trim()}
            whileHover={{ scale: busy || locked ? 1 : 1.04 }}
            whileTap={{ scale: busy || locked ? 1 : 0.97 }}
            className="rounded-control bg-fruit-amber px-7 py-3 font-display text-button font-extrabold text-bark-dark shadow-lg transition-colors hover:bg-fruit-yellow focus:outline-none focus:ring-4 focus:ring-fruit-yellow/50 disabled:cursor-not-allowed disabled:bg-muted disabled:text-bark-dark/60"
          >
            {busy ? 'Thinking…' : 'Check it'}
          </motion.button>
        </div>
      </form>
    </motion.section>
  )
}
