/**
 * The hint fruit, drawn rather than photographed.
 *
 * Deliberately SVG and not an image file. A photograph would mean a licensed
 * asset, a network fetch, and a child on a slow school connection watching three
 * empty slots while they load — and the one thing this surface must not do is
 * make a child wait before they can ask for help. Drawn fruit is a couple of
 * kilobytes, stays crisp at any size, and recolours from the design tokens so it
 * cannot drift from the palette.
 *
 * Each fruit is built the same way, which is what makes three species look like
 * one set: a body lit from the upper left by a radial gradient, a shaded flank,
 * a specular highlight, and a stem and leaves sized to actually read at 88px
 * rather than to be correct at 400px and invisible here.
 *
 * The three are deliberately different *shapes*, not three spheres in different
 * colours. At this size hue alone is not enough to tell them apart, and a child
 * picking between hints should be able to see which fruit is which.
 */

export type FruitKind = 'orange' | 'mango' | 'passionfruit'

const LEAF = 'var(--color-jungle-light)'
const LEAF_DARK = 'var(--color-jungle-mid)'
const STEM = 'var(--color-bark-mid)'

/** Stem and a two-leaf sprig, sized to be legible at 88px. */
function Sprig({ tilt = 0 }: { tilt?: number }) {
  return (
    <g transform={`rotate(${tilt} 50 20)`}>
      <path
        d="M50 30 C 48 22, 48 14, 51 4"
        stroke={STEM}
        strokeWidth="5"
        strokeLinecap="round"
        fill="none"
      />
      {/* big leaf, right */}
      <path d="M52 16 C 68 4, 88 8, 92 20 C 76 30, 58 26, 52 16 Z" fill={LEAF} />
      <path
        d="M52 16 C 68 10, 82 14, 92 20 C 78 19, 62 17, 52 16 Z"
        fill={LEAF_DARK}
        opacity="0.6"
      />
      {/* smaller leaf, left */}
      <path d="M49 20 C 38 14, 24 18, 22 27 C 34 33, 45 28, 49 20 Z" fill={LEAF_DARK} />
      <path d="M49 20 C 39 18, 30 22, 22 27 C 34 25, 43 21, 49 20 Z" fill={LEAF} opacity="0.45" />
    </g>
  )
}

function Shadow({ cy = 100, rx = 21 }: { cy?: number; rx?: number }) {
  return <ellipse cx="50" cy={cy} rx={rx} ry="4" fill="#000000" opacity="0.2" />
}

function Orange() {
  return (
    <>
      <defs>
        <radialGradient id="f-orange" cx="35%" cy="28%" r="80%">
          <stop offset="0%" stopColor="#ffc46b" />
          <stop offset="42%" stopColor="var(--color-fruit-orange)" />
          <stop offset="100%" stopColor="#a8380a" />
        </radialGradient>
      </defs>
      <Shadow />
      <circle cx="50" cy="66" r="32" fill="url(#f-orange)" />
      {/* pitted rind — without it a citrus reads as a rubber ball */}
      <g fill="#00000020">
        <circle cx="38" cy="58" r="1.9" />
        <circle cx="47" cy="51" r="1.7" />
        <circle cx="59" cy="55" r="1.8" />
        <circle cx="67" cy="66" r="1.9" />
        <circle cx="43" cy="72" r="1.8" />
        <circle cx="55" cy="79" r="1.7" />
        <circle cx="33" cy="69" r="1.6" />
        <circle cx="64" cy="78" r="1.6" />
        <circle cx="50" cy="63" r="1.5" />
      </g>
      <ellipse
        cx="39"
        cy="52"
        rx="11"
        ry="7"
        fill="#ffffff"
        opacity="0.42"
        transform="rotate(-30 39 52)"
      />
      <Sprig tilt={-5} />
    </>
  )
}

function Mango() {
  return (
    <>
      <defs>
        <linearGradient id="f-mango" x1="20%" y1="10%" x2="85%" y2="95%">
          <stop offset="0%" stopColor="#fde047" />
          <stop offset="32%" stopColor="var(--color-fruit-amber)" />
          <stop offset="70%" stopColor="#d62828" />
          <stop offset="100%" stopColor="#7f1d1d" />
        </linearGradient>
      </defs>
      <Shadow cy={102} rx={23} />
      {/* A real mango: fat low belly, tapered shoulder, tilted as it would hang.
          The tilt is what stops it reading as an egg. */}
      <g transform="rotate(-14 50 66)">
        <path
          d="M50 30
             C 72 32, 88 50, 86 70
             C 84 89, 66 100, 48 99
             C 28 98, 14 84, 15 65
             C 16 46, 31 30, 50 30 Z"
          fill="url(#f-mango)"
        />
        {/* the red blush mangoes carry on the sunward cheek */}
        <path
          d="M64 34 C 80 44, 88 62, 82 80 C 90 62, 84 42, 64 34 Z"
          fill="#991b1b"
          opacity="0.45"
        />
        <ellipse
          cx="38"
          cy="49"
          rx="12"
          ry="7"
          fill="#ffffff"
          opacity="0.45"
          transform="rotate(-34 38 49)"
        />
      </g>
      <Sprig tilt={6} />
    </>
  )
}

function Passionfruit() {
  return (
    <>
      <defs>
        <radialGradient id="f-passion" cx="34%" cy="26%" r="80%">
          <stop offset="0%" stopColor="#ddd6fe" />
          <stop offset="34%" stopColor="var(--color-fruit-purple)" />
          <stop offset="82%" stopColor="#5b21b6" />
          <stop offset="100%" stopColor="#3b0764" />
        </radialGradient>
      </defs>
      <Shadow cy={101} rx={20} />
      {/* Slightly taller than wide, which is what distinguishes it from the
          orange once colour is stripped out by the unripe filter. */}
      <ellipse cx="50" cy="67" rx="29" ry="33" fill="url(#f-passion)" />
      <ellipse
        cx="40"
        cy="50"
        rx="13"
        ry="8"
        fill="#ffffff"
        opacity="0.5"
        transform="rotate(-30 40 50)"
      />
      {/* waxy vertical sheen down the shaded flank */}
      <ellipse
        cx="65"
        cy="76"
        rx="8"
        ry="15"
        fill="#2e1065"
        opacity="0.35"
        transform="rotate(16 65 76)"
      />
      <ellipse cx="62" cy="58" rx="3" ry="9" fill="#ffffff" opacity="0.18" transform="rotate(20 62 58)" />
      <Sprig tilt={9} />
    </>
  )
}

const BODY: Record<FruitKind, () => React.ReactElement> = {
  orange: Orange,
  mango: Mango,
  passionfruit: Passionfruit,
}

export function Fruit({ kind, ripe }: { kind: FruitKind; ripe: boolean }) {
  const Body = BODY[kind]
  return (
    <svg
      viewBox="0 0 100 108"
      width="88"
      height="95"
      aria-hidden="true"
      /* Unripe fruit is in shadow — still recognisably fruit, still its own
         colour. Desaturating it to a grey stone would say "denied"; a locked
         hint is one the child has not needed yet, not one they have been
         refused, and on this surface that distinction is the whole design. */
      style={{
        filter: ripe ? 'saturate(1.05)' : 'saturate(0.45) brightness(0.72)',
        opacity: ripe ? 1 : 0.62,
        transition: 'filter 400ms ease, opacity 400ms ease',
      }}
    >
      <Body />
    </svg>
  )
}
