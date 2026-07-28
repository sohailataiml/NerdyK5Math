import { motion } from 'framer-motion'
import { mascotIdle, mascotJump } from '../lib/motion'

export type MascotMood = 'encouraging' | 'pointing' | 'celebrating' | 'thinking' | 'warm'

const LINE: Record<MascotMood, string> = {
  encouraging: "Let's have a go. Type what you think.",
  // Never "that's wrong". The child already knows the answer was not accepted;
  // repeating it adds nothing and costs something.
  pointing: 'Have a look at the fruit — it might help.',
  celebrating: 'You worked that out!',
  // §11.1's latency beat: the wait for generation reads as the mascot thinking,
  // never as a spinner. A spinner says the machine is busy; this says someone is
  // considering what you wrote.
  thinking: 'Let me have a think…',
  warm: 'You stuck with it. That is the hard part.',
}

/**
 * Kip, a toucan.
 *
 * Drawn in SVG rather than shipped as an image: it is a handful of primitives,
 * it stays crisp at any size, and it means a child on a slow school connection
 * has nothing to download before the tutor can start.
 */
export function Mascot({ mood }: { mood: MascotMood }) {
  const celebrating = mood === 'celebrating'

  return (
    <div className="flex w-[260px] shrink-0 flex-col items-center gap-4">
      <motion.div
        animate={celebrating ? mascotJump : mascotIdle}
        className="relative"
        aria-hidden="true"
      >
        <svg width="180" height="180" viewBox="0 0 180 180" fill="none">
          {/* branch */}
          <rect x="20" y="140" width="140" height="12" rx="6" fill="var(--color-bark-mid)" />
          <rect x="20" y="140" width="140" height="5" rx="3" fill="var(--color-bark-dark)" />

          {/* body */}
          <ellipse cx="90" cy="98" rx="42" ry="46" fill="var(--color-jungle-mid)" />
          <ellipse cx="90" cy="110" rx="27" ry="30" fill="var(--color-jungle-light)" />

          {/* wing */}
          <motion.ellipse
            cx="52"
            cy="98"
            rx="14"
            ry="26"
            fill="var(--color-jungle-dark)"
            opacity="0.55"
            animate={celebrating ? { rotate: [0, -22, 0] } : { rotate: 0 }}
            transition={{ duration: 0.5, repeat: celebrating ? 2 : 0 }}
            style={{ originX: '52px', originY: '86px' }}
          />

          {/* head */}
          <circle cx="96" cy="56" r="30" fill="var(--color-jungle-mid)" />

          {/* the beak — a toucan is mostly beak */}
          <path
            d="M120 48 C 152 40, 166 56, 150 68 C 140 76, 126 70, 120 64 Z"
            fill="var(--color-fruit-orange)"
          />
          <path d="M120 58 C 140 56, 150 60, 150 66 C 138 68, 126 66, 120 62 Z" fill="var(--color-fruit-amber)" />

          {/* eye */}
          <circle cx="104" cy="48" r="9" fill="#ffffff" />
          <motion.circle
            cx="106"
            cy="49"
            r="4.5"
            fill="var(--color-bark-dark)"
            animate={celebrating ? { scale: [1, 1.25, 1] } : {}}
            transition={{ duration: 0.4, repeat: celebrating ? 2 : 0 }}
          />
          <circle cx="104.5" cy="46.5" r="1.6" fill="#ffffff" />

          {/* feet */}
          <rect x="78" y="136" width="7" height="12" rx="3" fill="var(--color-fruit-amber)" />
          <rect x="96" y="136" width="7" height="12" rx="3" fill="var(--color-fruit-amber)" />
        </svg>
      </motion.div>

      <motion.p
        key={mood}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        className="rounded-card border border-jungle-mid/50 bg-jungle-mid/15 px-4 py-3 text-center font-body text-body font-bold text-sky-blue"
      >
        {LINE[mood]}
      </motion.p>
    </div>
  )
}
