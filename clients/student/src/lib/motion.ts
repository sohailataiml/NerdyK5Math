import type { Transition, Variants } from 'framer-motion'

/**
 * The spec's animation suggestions, in one place.
 *
 * Centralised because the same spring appears on the fruit, the trail, and the
 * mascot's jump — three components each tuning their own would read as three
 * different products sharing a palette.
 */

export const spring: Transition = { type: 'spring', stiffness: 380, damping: 22 }
export const softSpring: Transition = { type: 'spring', stiffness: 220, damping: 26 }

/** Fruit reveal: scale [0.8, 1.1, 1] with rotate [-5, 5, 0]. */
export const fruitReveal: Variants = {
  locked: { scale: 1, rotate: 0 },
  opening: {
    scale: [0.8, 1.1, 1],
    rotate: [-5, 5, 0],
    transition: { duration: 0.55, ease: 'easeOut' },
  },
}

/** Question entrance: opacity [0,1] + y [20, 0]. */
export const questionEntrance: Variants = {
  hidden: { opacity: 0, y: 20 },
  visible: { opacity: 1, y: 0, transition: softSpring },
  exit: { opacity: 0, y: -12, transition: { duration: 0.18 } },
}

/** Mascot idle: subtle y [0, -4, 0] loop. */
export const mascotIdle = {
  y: [0, -4, 0],
  transition: { duration: 2.6, repeat: Infinity, ease: 'easeInOut' as const },
}

/** Mascot celebrating: the bigger jump on a correct answer. */
export const mascotJump = {
  y: [0, -28, 0, -12, 0],
  rotate: [0, -6, 6, -3, 0],
  transition: { duration: 1.1, ease: 'easeOut' as const },
}

export const staggerChildren = (stagger = 0.08): Transition => ({
  staggerChildren: stagger,
})
