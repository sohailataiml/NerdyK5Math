import { motion } from 'framer-motion'
import { spring } from '../lib/motion'

const PARTICLE_COLORS = [
  'var(--color-fruit-yellow)',
  'var(--color-fruit-orange)',
  'var(--color-jungle-light)',
  'var(--color-sky-blue)',
  'var(--color-fruit-purple)',
]

const PARTICLE_COUNT = 28

/** Stagger particle burst from centre, per the spec. */
function Confetti() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      {Array.from({ length: PARTICLE_COUNT }, (_, i) => {
        const angle = (i / PARTICLE_COUNT) * Math.PI * 2
        const distance = 140 + (i % 5) * 42
        return (
          <motion.span
            key={i}
            initial={{ x: 0, y: 0, scale: 0, opacity: 1 }}
            animate={{
              x: Math.cos(angle) * distance,
              y: Math.sin(angle) * distance + 60,
              scale: [0, 1, 0.9],
              opacity: [1, 1, 0],
              rotate: (i % 2 === 0 ? 1 : -1) * 220,
            }}
            transition={{ duration: 1.4, delay: i * 0.015, ease: 'easeOut' }}
            className="absolute left-1/2 top-1/2 h-3 w-2 rounded-[2px]"
            style={{ backgroundColor: PARTICLE_COLORS[i % PARTICLE_COLORS.length] }}
          />
        )
      })}
    </div>
  )
}

/**
 * The correct-answer moment.
 *
 * Celebrating a right answer is fine and is not what §11.5 warns about — what it
 * rules out is a running score or streak that turns every session into something
 * to protect. This fires, it is loud, and then it is over.
 */
export function Celebration({ message, onNext }: { message: string; onNext: () => void }) {
  return (
    <motion.section
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={spring}
      className="relative flex w-full max-w-[640px] flex-col items-center gap-6 rounded-card border-4 border-fruit-yellow bg-bark-dark p-10 text-center shadow-2xl"
      role="status"
    >
      <Confetti />

      <motion.h2
        initial={{ scale: 0.5, rotate: -8 }}
        animate={{ scale: 1, rotate: 0 }}
        transition={{ ...spring, delay: 0.1 }}
        className="relative font-display text-hero font-black text-fruit-yellow text-shadow-deep"
      >
        Correct!
      </motion.h2>

      <p className="relative font-body text-body font-bold text-sky-blue">{message}</p>

      <motion.button
        onClick={onNext}
        whileHover={{ scale: 1.04 }}
        whileTap={{ scale: 0.97 }}
        className="relative rounded-control bg-jungle-light px-8 py-3 font-display text-button font-extrabold text-jungle-dark shadow-lg focus:outline-none focus:ring-4 focus:ring-jungle-light/50"
      >
        Next question
      </motion.button>
    </motion.section>
  )
}

/**
 * The end of a session where the child did not get there.
 *
 * Deliberately not a failure screen, and this is the single most important
 * screen in the client to get right. §11.5: a child who reads the end of a
 * session as punishment stops asking for hints, and hint-seeking is the one
 * behaviour the whole system exists to reward. No red, no cross, no score. The
 * palette is the same warm bark and amber as everywhere else.
 *
 * The wording follows the server's `message`, which itself follows the record —
 * it says a teacher will look only when a review row was actually written.
 */
export function GoingToTeacher({ message, onNext }: { message: string; onNext: () => void }) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={spring}
      className="flex w-full max-w-[640px] flex-col items-center gap-6 rounded-card border-4 border-bark-mid bg-bark-dark p-10 text-center shadow-2xl"
      role="status"
    >
      <span aria-hidden="true" className="text-[56px] leading-none">
        🌿
      </span>
      <h2 className="font-display text-heading font-black text-fruit-amber">
        Good sticking with it
      </h2>
      <p className="max-w-[26rem] font-body text-body font-bold text-sky-blue">{message}</p>
      <motion.button
        onClick={onNext}
        whileHover={{ scale: 1.04 }}
        whileTap={{ scale: 0.97 }}
        className="rounded-control bg-fruit-amber px-8 py-3 font-display text-button font-extrabold text-bark-dark shadow-lg focus:outline-none focus:ring-4 focus:ring-fruit-yellow/50"
      >
        Try another one
      </motion.button>
    </motion.section>
  )
}
