import { AnimatePresence, motion } from 'framer-motion'
import { Fruit, type FruitKind } from './Fruit'
import { fruitReveal, spring } from '../lib/motion'

export type FruitState = 'locked' | 'open'

/** One species per hint level, so a child can tell them apart at a glance
 *  rather than by counting position. */
const SPECIES: readonly FruitKind[] = ['orange', 'mango', 'passionfruit']

/**
 * One hint slot hanging from the canopy.
 *
 * A locked fruit is dimmed but never crossed out or greyed to nothing. It is a
 * hint the child has not needed yet, not one they have been denied — and on this
 * surface that distinction is the whole design.
 */
export function FruitHint({
  index,
  state,
  hint,
}: {
  index: number
  state: FruitState
  hint: string | null
}) {
  const kind = SPECIES[index % SPECIES.length] ?? 'orange'
  const open = state === 'open'

  return (
    <div className="flex w-[220px] flex-col items-center">
      {/* the vine */}
      <span
        aria-hidden="true"
        className="w-[3px] rounded-full bg-gradient-to-b from-jungle-mid/10 via-jungle-mid to-jungle-light"
        style={{ height: 34 + index * 12 }}
      />

      <motion.div
        variants={fruitReveal}
        initial="locked"
        animate={open ? 'opening' : 'locked'}
        className="relative -mt-1"
        role="img"
        aria-label={
          open ? `Hint ${index + 1}, ready` : `Hint ${index + 1}, not needed yet`
        }
      >
        <Fruit kind={kind} ripe={open} />
        {/* The level still has to be readable — a child choosing between three
            fruits needs to know which is the first. Kept small and off the
            fruit's face so it does not turn the illustration back into a
            button. */}
        <span
          className={[
            'absolute bottom-1 -right-1 grid h-6 w-6 place-items-center rounded-full',
            'border-2 border-jungle-dark font-display text-caption font-extrabold',
            open ? 'bg-fruit-yellow text-bark-dark' : 'bg-jungle-mid/70 text-sky-blue',
          ].join(' ')}
        >
          {index + 1}
        </span>
      </motion.div>

      <AnimatePresence>
        {open && hint && (
          <motion.p
            initial={{ opacity: 0, y: -8, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8 }}
            transition={spring}
            className="mt-3 rounded-card border border-bark-mid/60 bg-bark-dark/95 px-4 py-3 text-center font-body text-hint font-bold text-ink shadow-lg"
          >
            {hint}
          </motion.p>
        )}
      </AnimatePresence>
    </div>
  )
}

/** Three fruit hint slots across the top, per the spec's component tree. */
export function HangingVines({
  maxHintLevel,
  hintsShown,
}: {
  maxHintLevel: number
  /** Hint text by level, in the order the child received them. */
  hintsShown: readonly (string | null)[]
}) {
  return (
    <div
      className="flex shrink-0 items-start justify-center gap-6 px-8"
      aria-label="Hints from the canopy"
    >
      {Array.from({ length: maxHintLevel }, (_, index) => (
        <FruitHint
          key={index}
          index={index}
          state={index < hintsShown.length ? 'open' : 'locked'}
          hint={hintsShown[index] ?? null}
        />
      ))}
    </div>
  )
}
