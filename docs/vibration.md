# Controller vibration — implementation guide

Read this before adding or changing weapon feedback. Vibration goes through
`InputSource.rumble(low, high, ms)` in [`game/inputs.py`](../game/inputs.py);
keyboard sources ignore it, so callers can always invoke it.

## Current feedback

| Event | low | high | ms |
|---|---:|---:|---:|
| Pairing: claim a car | 0.5 | 0.5 | 200 |
| Countdown beat | 0.35 | 0.35 | 140 |
| Countdown GO | 0.8 | 0.9 | 400 |
| Stop command | 0.8 | 0.8 | 150 |
| Shooter fires | 0.2 | 0.7 | 120 |
| Victim is hit | 0.9 | 0.4 | 250 |
| Victim is destroyed | 1.0 | 0.8 | 900 |
| Race finish (loser) | 0.7 | 0.4 | 1200 |

`HIT_RUMBLE` and `DEATH_RUMBLE` live next to the battle constants in
[`game/game.py`](../game/game.py). A fatal hit uses only the death pulse, not
both patterns.

## Rules for future weapons

- **Every new weapon needs vibration feedback on both sides** where it makes
  sense: the user and the victim. Firing should feel different from being hit.
- **Ask the user before choosing a new vibration pattern.** Intensity and length
  are a feel decision, not only a code decision. Do not silently reuse the hit
  pattern for a new effect or introduce a pattern the user has not approved.
- Reuse `hit` and `death` for ordinary damage, but give a genuinely new weapon
  (for example a tractor beam or charged shot) its own distinct pattern once the
  user has agreed on it.
- Keep pulses short unless the event is meant to interrupt play. A long pulse
  blocks any other vibration on that controller for its full duration.
- Put shared values in `game/game.py` as named constants and document them in
  the table above.
