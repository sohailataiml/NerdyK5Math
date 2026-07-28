import { motion } from 'framer-motion'
import { spring } from '../lib/motion'

/** h:84, horizontal, px:32, py:16 — per the spec's layout table. */
export function Header({ children }: { children: React.ReactNode }) {
  return (
    <header className="flex h-[84px] shrink-0 items-center justify-between gap-8 border-b border-bark-mid/40 bg-bark-dark/50 px-8 py-4 backdrop-blur-sm">
      {children}
    </header>
  )
}

export function ProfileBadge({ name }: { name: string }) {
  const initial = name.trim().charAt(0).toUpperCase() || '?'
  return (
    <div className="flex items-center gap-3 rounded-card border border-bark-mid/60 bg-bark-dark px-3 py-2">
      <div
        className="grid h-9 w-9 place-items-center rounded-full bg-fruit-amber font-display text-button font-extrabold text-bark-dark"
        aria-hidden="true"
      >
        {initial}
      </div>
      <span className="font-display text-subheading font-extrabold text-ink">{name}</span>
    </div>
  )
}

/**
 * Stepping stones across a river. `total` is the number of tries this problem
 * allows; `reached` is how far the child has come.
 *
 * Deliberately framed as distance travelled rather than tries used up. Both
 * render the same three stones, but one says "you are getting there" and the
 * other says "you are running out", and §11.5 is explicit that escalation must
 * never read as a loss state.
 */
export function ProgressTrail({ reached, total }: { reached: number; total: number }) {
  return (
    <div
      className="flex items-center gap-2"
      role="img"
      aria-label={`Stepping stone ${Math.min(reached + 1, total)} of ${total}`}
    >
      {Array.from({ length: total }, (_, index) => {
        const filled = index < reached
        const current = index === reached
        return (
          <div key={index} className="flex items-center gap-2">
            {index > 0 && (
              <span
                className={`h-[3px] w-6 rounded-full ${filled ? 'bg-jungle-light' : 'bg-jungle-mid/30'}`}
              />
            )}
            <motion.span
              animate={filled || current ? { scale: 1 } : { scale: 0.82 }}
              transition={spring}
              className={[
                'grid h-7 w-7 place-items-center rounded-full border-2 font-body text-caption font-bold',
                filled
                  ? 'border-jungle-light bg-jungle-light text-jungle-dark'
                  : current
                    ? 'border-fruit-yellow bg-jungle-dark text-fruit-yellow'
                    : 'border-jungle-mid/40 bg-jungle-dark/60 text-muted',
              ].join(' ')}
            >
              {filled ? '✓' : index + 1}
            </motion.span>
          </div>
        )
      })}
    </div>
  )
}

/**
 * The spec's `<StatsGroup />`, minus score and streak.
 *
 * Both were specified and neither is built, for a reason that is not a
 * shortcut. `services/api/student.py` returns no score and no streak, and it
 * returns none because §11.5 rules out score, streak, and timer on this surface:
 * a child racing a counter stops asking for hints, and hint-seeking is the one
 * behaviour this system exists to reward. Adding them would have meant inventing
 * the numbers client-side — a scoreboard the record cannot corroborate, shown to
 * a seven-year-old.
 *
 * What is left is honest and comes from the API: how much help is still
 * available. Phrased as remaining, never as spent.
 */
export function StatsGroup({ hintsLeft, total }: { hintsLeft: number; total: number }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex items-center gap-2 rounded-card border border-bark-mid/60 bg-bark-dark px-3 py-2">
        <span aria-hidden="true" className="text-[18px] leading-none">
          🍊
        </span>
        <div className="flex flex-col leading-tight">
          <span className="font-body text-caption font-semibold text-muted">help fruit</span>
          <span className="font-display text-button font-extrabold text-fruit-orange">
            {hintsLeft} of {total}
          </span>
        </div>
      </div>
    </div>
  )
}
