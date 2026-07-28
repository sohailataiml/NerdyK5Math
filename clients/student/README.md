# Jungle Math — student client (P0.11)

React + Vite + TypeScript + Tailwind v4 + Framer Motion, built to the Jungle
Math Tutor design spec. It talks to the FastAPI backend in `services/api`.

```bash
python -m scripts.serve          # backend on :8080 (from the repo root)
npm install                      # once
npm run dev                      # client on :5173
```

Then open `http://localhost:5173/?as=<student-principal-id>`. Get an id from
`python -m scripts.seed_pilot`.

`npm run build` type-checks and bundles; `npm run typecheck` is the check alone.

## How it reaches the API

Vite proxies `/student` to `127.0.0.1:8080` rather than calling it cross-origin.
The usual alternative is permissive CORS on the backend, added "just for local"
— on a service that serves children's schoolwork, that is a real widening of the
security posture for the convenience of a dev server, and it is the kind of thing
that ships. The proxy keeps the browser seeing one origin and leaves the backend
untouched.

Identity is `?as=<principal-id>` sent as `x-principal-id`, matching
`services/api/auth.py`. That is pilot-grade and the backend says so itself:
there is no password, session, or token signature. This client does not pretend
otherwise.

## Where this deviates from the spec, and why

**`<StatsGroup />` has no score and no streak.** Both were specified. Neither is
built, and it is not a shortcut:

- The API returns neither. `AnswerResponse` is `correct`, `hint`, `hint_level`,
  `attempt`, `done`, `going_to_teacher`, `message` — building the spec's version
  would have meant inventing the numbers client-side, so the child would be shown
  a scoreboard the audit record cannot corroborate.
- Architecture.md §11.5 rules out score, streak, and timer on this surface. A
  child racing a counter stops asking for hints, and hint-seeking is the single
  behaviour this system exists to reward.

What remains is `hints left`, which does come from the API (`max_hint_level`) and
is phrased as help remaining rather than help spent.

**There is a fifth app state the spec did not name.** The spec lists `question`,
`hint-revealed`, and `celebration`. The backend also ends sessions with
`going_to_teacher`, when no hint could be cleared or the hint levels ran out, and
that needed its own screen rather than being folded into `celebration` as its sad
twin. It is deliberately warm — same bark and amber as everywhere else, no red,
no cross, no "wrong" — because §11.5 is explicit that escalation must never read
as a loss state.

## Rules this client is built to keep

- **No response field carries the correct answer**, and there is no type here to
  receive one. `SafeProblem` mirrors the server's `_SafeProblem`. A child with
  the network tab open is still a child in the pilot.
- **Grading is never client-side.** The client sends what was typed and is told
  whether it was right; it has nothing to compare against.
- **The visual never draws the total.** Ported from the server-rendered page so
  the two surfaces cannot disagree: the ten-frame shows what the child starts
  with and what they are adding, and the number line marks the start but not the
  finish. A filled answer square is an answer leak wearing a diagram, and it
  would walk straight past the leak checker, which inspects hint *text*.
- **The wait is a beat, not a spinner.** §11.1: generation takes seconds, and the
  mascot thinks for that time. A spinner says the machine is busy; a thinking
  toucan says someone is considering what you wrote.
- **`prefers-reduced-motion` is honoured.** Every animation here is decorative —
  nothing communicates state through motion alone — so §11.5's low-stimulation
  path costs the child nothing.

## Layout

```
<App>                        1440×900, vertical stack
├── <Header>                 h:84, px:32, py:16
│   ├── <ProfileBadge />
│   ├── <ProgressTrail />    stepping stones
│   └── <StatsGroup />       hints remaining (see above)
├── <HangingVines>           3 fruit hint slots
│   └── <FruitHint />        locked | open
└── <main>                   px:80, py:40, gap:48
    ├── <Mascot />           idle bob; jumps on correct
    └── <QuestionCard /> | <Celebration /> | <GoingToTeacher />
```

Design tokens live in `src/index.css` under Tailwind v4's `@theme`, so every
spec colour and type role is also a utility (`bg-jungle-dark`, `text-question`).
No component hardcodes a hex.
