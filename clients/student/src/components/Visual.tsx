import { motion } from 'framer-motion'
import type { Visual as VisualData } from '../api/client'
import { spring } from '../lib/motion'

/**
 * The §11.2 representations, ported from the server-rendered page so the two
 * surfaces cannot disagree about what a child is shown.
 *
 * **Neither one draws the total.** A ten-frame with the sum filled in is an
 * answer leak wearing a diagram — it walks straight past the leak checker, which
 * inspects hint *text*. The frame shows what the child starts with and what they
 * are adding; where it lands is the question.
 *
 * The words are written once and used twice — as the `aria-label` and as the
 * visible caption. §11.5 requires a text-only equivalent, and two copies of the
 * same sentence drift until the screen-reader path describes a different picture
 * from the one on screen.
 */

const TEN_FRAME_CAPACITY = 10

interface Rendered {
  node: React.ReactNode
  words: string
}

function tenFrame(v: VisualData): Rendered {
  const have = v.left
  const adding = v.right
  const spill = have + adding > TEN_FRAME_CAPACITY ? have + adding - TEN_FRAME_CAPACITY : 0

  return {
    words:
      `A ten-frame with ${have} squares filled in, and ${adding} more to add.` +
      (spill > 0 ? ` ${spill} of them won't fit in the frame — where do they go?` : ''),
    node: (
      <div className="grid grid-cols-5 gap-2 rounded-control border-2 border-bark-mid bg-bark-dark/60 p-3">
        {Array.from({ length: TEN_FRAME_CAPACITY }, (_, i) => {
          const filled = i < have
          const isAdding = !filled && i < have + adding
          return (
            <motion.span
              key={i}
              initial={{ scale: 0.6, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              transition={{ ...spring, delay: i * 0.03 }}
              className={[
                'h-9 w-9 rounded-md border-2',
                filled
                  ? 'border-jungle-light bg-jungle-light'
                  : isAdding
                    ? 'border-dashed border-fruit-yellow bg-fruit-yellow/15'
                    : 'border-jungle-mid/35 bg-transparent',
              ].join(' ')}
            />
          )
        })}
      </div>
    ),
  }
}

function numberLine(v: VisualData): Rendered {
  const start = v.left
  const adding = v.operation === 'addition'
  const span = Math.max(start + (adding ? v.right : 0), TEN_FRAME_CAPACITY)
  const direction = adding ? 'forward' : 'back'

  return {
    words:
      `A number line from 0 to ${span}, with ${start} marked. ` +
      `Count ${direction} ${v.right} from ${start}.`,
    node: (
      <div className="w-full overflow-x-auto">
        <div className="flex min-w-max items-end gap-0 rounded-control border-2 border-bark-mid bg-bark-dark/60 px-4 pb-3 pt-8">
          {Array.from({ length: span + 1 }, (_, n) => {
            const major = n % 5 === 0
            const isStart = n === start
            return (
              <div key={n} className="relative flex w-8 flex-col items-center">
                {(major || isStart) && (
                  <span
                    className={[
                      'absolute -top-6 font-body text-caption font-bold',
                      isStart ? 'text-fruit-yellow' : 'text-muted',
                    ].join(' ')}
                  >
                    {n}
                  </span>
                )}
                <span
                  className={[
                    'w-[2px] rounded-full',
                    isStart
                      ? 'h-7 bg-fruit-yellow'
                      : major
                        ? 'h-5 bg-jungle-light/70'
                        : 'h-3 bg-jungle-mid/40',
                  ].join(' ')}
                />
              </div>
            )
          })}
          {/* The start is marked. The finish deliberately is not. */}
        </div>
      </div>
    ),
  }
}

export function Visual({ visual }: { visual: VisualData | null }) {
  if (!visual) return null
  const rendered = visual.kind === 'ten_frame' ? tenFrame(visual) : numberLine(visual)

  return (
    <div className="flex flex-col items-center gap-3">
      <div role="img" aria-label={rendered.words}>
        {rendered.node}
      </div>
      <p className="max-w-[36rem] text-center font-body text-hint font-bold text-muted">
        {rendered.words}
      </p>
    </div>
  )
}
