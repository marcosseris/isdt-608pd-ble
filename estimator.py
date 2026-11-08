"""Time-to-full estimate for a CC/CV lithium charge.

A charge has two phases:
  CC (constant current): current is flat and the pack voltage / % rise steadily.
  CV (constant voltage): the pack is at full voltage and the current decays
      roughly exponentially until it falls below the charger's cut-off.

The ISDT % jumps to ~99 around the CC->CV switch and then sits there for the
whole CV tail, so % alone badly underestimates the end. Instead:
  CC: fit % vs time to predict when CV starts, then add the expected CV tail.
  CV: fit ln(current) vs time and solve for when it reaches the cut-off.
The CV tail length and cut-off current are learned per pack (chemistry + cell
count) after each completed charge and saved to estimator_history.json.
"""

from __future__ import annotations

import json
import math
import os
import time

from isdt import RealTime

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estimator_history.json")

CC_WINDOW_S = 600          # % fit uses the last 10 min
CV_WINDOW_S = 240          # current-decay fit uses the last 4 min
MIN_FIT_SPAN_S = 45        # don't trust a fit over less than this
CV_CURRENT_DROP = 0.85     # CV once current < 85 % of the CC current...
CV_CONFIRM_SAMPLES = 3     # ...for this many samples in a row
CV_START_PERCENT = 99      # the charger shows ~99 % once CV begins
DEFAULT_END_FRACTION = 0.10  # cut-off current as a fraction of CC current
DEFAULT_CV_TAIL_S = 600    # CV duration guess until we've seen a full charge
LEARN_WEIGHT = 0.5         # how fast learned values follow new charges
SMOOTHING = 0.2            # ETA low-pass filter (0..1, higher = more reactive)


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Least-squares slope and intercept, or None if x has no spread."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


class ChargeEstimator:
    def __init__(self, history_file: str = HISTORY_FILE):
        self.history_file = history_file
        try:
            with open(history_file, encoding="utf-8") as f:
                self.history: dict = json.load(f)
        except (OSError, ValueError):
            self.history = {}
        self.last_status: int | None = None
        self._reset()

    # ---- public -------------------------------------------------------
    @property
    def phase(self) -> str | None:
        """'CC', 'CV' or None when not charging."""
        if not self.samples:
            return None
        return "CV" if self.cv_start is not None else "CC"

    def add(self, rt: RealTime, now: float | None = None) -> None:
        now = rt.received if now is None else now
        if self.last_status == 1 and rt.status == 2:
            self._learn()
        self.last_status = rt.status
        if rt.status != 1:
            self._reset()
            return
        if self.samples and rt.elapsed_s < self.samples[-1][0]:
            self._reset()  # charger started a new charge

        self.key = f"{rt.chemistry_name}-{len(rt.cells)}S"
        t = float(rt.elapsed_s)
        self.samples.append((t, rt.percent, max(rt.current_ma, 1)))
        while self.samples and self.samples[0][0] < t - CC_WINDOW_S:
            self.samples.pop(0)
        self._detect_cv(t, rt)

        rem = self._raw_remaining(t, rt)
        if rem is None:
            return
        eta = now + rem
        self._eta = eta if self._eta is None else self._eta + SMOOTHING * (eta - self._eta)
        self._last_now = now

    def remaining_s(self, now: float | None = None) -> float | None:
        if self._eta is None:
            return None
        return max(0.0, self._eta - (time.time() if now is None else now))

    def eta(self) -> float | None:
        return self._eta

    # ---- internals ----------------------------------------------------
    def _reset(self) -> None:
        self.samples: list[tuple[float, int, int]] = []  # (elapsed s, %, mA)
        self.peak_ma = 0
        self.below = 0
        self.cv_start: float | None = None
        self.key: str | None = None
        self._eta: float | None = None
        self._last_now: float | None = None

    def _learned(self, field: str, default: float) -> float:
        return self.history.get(self.key or "", {}).get(field, default)

    def _detect_cv(self, t: float, rt: RealTime) -> None:
        if self.cv_start is not None:
            return
        self.peak_ma = max(self.peak_ma, rt.current_ma)
        if self.peak_ma and rt.current_ma < CV_CURRENT_DROP * self.peak_ma:
            self.below += 1
        else:
            self.below = 0
        if self.below >= CV_CONFIRM_SAMPLES:
            self.cv_start = t
        elif rt.percent >= CV_START_PERCENT and len(self.samples) > 1:
            self.cv_start = t

    def _raw_remaining(self, t: float, rt: RealTime) -> float | None:
        end_ma = self._learned("end_fraction", DEFAULT_END_FRACTION) * max(self.peak_ma, 1)
        cv_tail = self._learned("cv_tail_s", DEFAULT_CV_TAIL_S)

        if self.cv_start is not None:
            pts = [(x, math.log(i)) for x, _, i in self.samples if x >= max(self.cv_start, t - CV_WINDOW_S)]
            if pts and pts[-1][0] - pts[0][0] >= MIN_FIT_SPAN_S:
                fit = _linfit([p[0] for p in pts], [p[1] for p in pts])
                if fit and fit[0] < 0:
                    i_now = math.exp(fit[0] * t + fit[1])  # fitted, so less noisy
                    return max(0.0, math.log(i_now / end_ma) / -fit[0]) if i_now > end_ma else 0.0
            # not enough CV data yet: fall back to the learned tail
            return max(60.0, cv_tail - (t - self.cv_start))

        pts = [(x, p) for x, p, _ in self.samples]
        if pts[-1][0] - pts[0][0] < MIN_FIT_SPAN_S or len({p for _, p in pts}) < 2:
            return None
        fit = _linfit([p[0] for p in pts], [p[1] for p in pts])
        if not fit or fit[0] <= 0:
            return None
        pct_now = fit[0] * t + fit[1]
        return max(0.0, (CV_START_PERCENT - pct_now) / fit[0]) + cv_tail

    def _learn(self) -> None:
        if self.cv_start is None or not self.samples or not self.key:
            return
        t_end, _, i_end = self.samples[-1]
        tail = t_end - self.cv_start
        # Single readings are noisy near the cut-off; use the fitted decay instead.
        pts = [(x, math.log(i)) for x, _, i in self.samples if x >= max(self.cv_start, t_end - CV_WINDOW_S)]
        fit = _linfit([p[0] for p in pts], [p[1] for p in pts]) if len(pts) > 2 else None
        if fit and fit[0] < 0:
            i_end = math.exp(fit[0] * t_end + fit[1])
        frac = i_end / self.peak_ma if self.peak_ma else DEFAULT_END_FRACTION
        if tail < 30 or not 0 < frac < 1:
            return
        old = self.history.get(self.key, {})
        mix = lambda k, v: v if k not in old else old[k] + LEARN_WEIGHT * (v - old[k])
        self.history[self.key] = {
            "cv_tail_s": round(mix("cv_tail_s", tail)),
            "end_fraction": round(mix("end_fraction", frac), 3),
            "charges": old.get("charges", 0) + 1,
        }
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2)
        except OSError:
            pass
