// Time-to-full estimate - port of estimator.py.
//   CC phase: fit % vs time (last 10 min) to predict when CV starts, add the expected CV tail.
//   CV phase: fit ln(current) vs time (last 4 min) and solve for the cut-off current.
// The CV tail length and cut-off fraction are learned per pack and kept in NVS.
#pragma once
#include <Preferences.h>
#include <math.h>
#include "charger.h"

class Estimator {
 public:
  static const int CC_WINDOW_S = 600, CV_WINDOW_S = 240, MIN_FIT_SPAN_S = 45;
  static constexpr float CV_CURRENT_DROP = 0.85f, DEFAULT_END_FRACTION = 0.10f, SMOOTHING = 0.2f, LEARN_WEIGHT = 0.5f;
  static const int CV_CONFIRM_SAMPLES = 3, CV_START_PERCENT = 99, DEFAULT_CV_TAIL_S = 600;

  void add(const RealTime& rt) {
    if (lastStatus_ == 1 && rt.status == 2) learn();
    lastStatus_ = rt.status;
    if (rt.status != 1) {
      reset();
      return;
    }
    if (n_ && rt.elapsedS < at(n_ - 1).t) reset();  // new charge

    chem_ = rt.chemistry;
    cells_ = rt.cells;
    push({rt.elapsedS, rt.percent, (uint16_t)max<int>(rt.currentMa, 1)});
    detectCv(rt);

    float rem;
    if (!rawRemaining(rt.elapsedS, rem)) return;
    float eta = millis() / 1000.0f + rem;
    etaS_ = etaS_ < 0 ? eta : etaS_ + SMOOTHING * (eta - etaS_);
  }

  // Seconds left, or -1 while still estimating.
  float remainingS() const { return etaS_ < 0 ? -1 : max(0.0f, etaS_ - millis() / 1000.0f); }
  const char* phase() const { return !n_ ? "" : cvStart_ >= 0 ? "Topping off" : "Constant current"; }

 private:
  struct Sample { uint32_t t; uint8_t pct; uint16_t ma; };
  static const int CAP = 720;
  Sample buf_[CAP];
  int head_ = 0, n_ = 0;

  const Sample& at(int i) const { return buf_[(head_ + i) % CAP]; }
  void push(const Sample& s) {
    if (n_ == CAP) { head_ = (head_ + 1) % CAP; n_--; }
    buf_[(head_ + n_) % CAP] = s;
    n_++;
    while (n_ > 2 && at(0).t + CC_WINDOW_S < s.t) { head_ = (head_ + 1) % CAP; n_--; }
  }

  void reset() {
    head_ = n_ = 0;
    peakMa_ = 0;
    below_ = 0;
    cvStart_ = -1;
    etaS_ = -1;
  }

  void detectCv(const RealTime& rt) {
    if (cvStart_ >= 0) return;
    peakMa_ = max<int>(peakMa_, rt.currentMa);
    below_ = (peakMa_ && rt.currentMa < CV_CURRENT_DROP * peakMa_) ? below_ + 1 : 0;
    if (below_ >= CV_CONFIRM_SAMPLES || (rt.percent >= CV_START_PERCENT && n_ > 1)) cvStart_ = rt.elapsedS;
  }

  // Least-squares fit over samples with t >= from; y = ln(mA) if logCurrent else %.
  bool fit(uint32_t from, bool logCurrent, float& slope, float& intercept, float& span) const {
    double sx = 0, sy = 0, sxx = 0, sxy = 0;
    int m = 0;
    uint32_t t0 = 0, t1 = 0;
    for (int i = 0; i < n_; i++) {
      const Sample& s = at(i);
      if (s.t < from) continue;
      if (!m) t0 = s.t;
      t1 = s.t;
      double x = s.t - (double)from, y = logCurrent ? log((double)s.ma) : s.pct;
      sx += x; sy += y; sxx += x * x; sxy += x * y; m++;
    }
    span = t1 - t0;
    double d = m * sxx - sx * sx;
    if (m < 2 || d == 0) return false;
    slope = (m * sxy - sx * sy) / d;
    intercept = (sy - slope * sx) / m - slope * from;  // back to absolute t
    return true;
  }

  bool rawRemaining(uint32_t t, float& rem) {
    float endMa = learned("f", DEFAULT_END_FRACTION) * max(peakMa_, 1);
    float tail = learned("t", DEFAULT_CV_TAIL_S);
    float slope, icpt, span;

    if (cvStart_ >= 0) {
      uint32_t from = max<int32_t>(cvStart_, (int32_t)t - CV_WINDOW_S);
      if (fit(from, true, slope, icpt, span) && span >= MIN_FIT_SPAN_S && slope < 0) {
        float iNow = expf(slope * t + icpt);
        rem = iNow > endMa ? logf(iNow / endMa) / -slope : 0;
        return true;
      }
      rem = max(60.0f, tail - (t - cvStart_));
      return true;
    }

    bool varied = false;
    for (int i = 1; i < n_ && !varied; i++) varied = at(i).pct != at(0).pct;
    if (!varied || !fit(0, false, slope, icpt, span) || span < MIN_FIT_SPAN_S || slope <= 0) return false;
    float pctNow = slope * t + icpt;
    rem = max(0.0f, (CV_START_PERCENT - pctNow) / slope) + tail;
    return true;
  }

  String key(const char* field) const { return String(field) + chem_ + "_" + cells_; }

  float learned(const char* field, float def) {
    Preferences p;
    p.begin("estimator", true);
    float v = p.getFloat(key(field).c_str(), def);
    p.end();
    return v;
  }

  void learn() {
    if (cvStart_ < 0 || n_ < 3) return;
    const Sample& last = at(n_ - 1);
    float tail = last.t - cvStart_;
    float iEnd = last.ma, slope, icpt, span;
    // single readings are noisy near the cut-off; use the fitted decay instead
    if (fit(max<int32_t>(cvStart_, (int32_t)last.t - CV_WINDOW_S), true, slope, icpt, span) && slope < 0)
      iEnd = expf(slope * last.t + icpt);
    float frac = peakMa_ ? iEnd / peakMa_ : DEFAULT_END_FRACTION;
    if (tail < 30 || frac <= 0 || frac >= 1) return;

    Preferences p;
    p.begin("estimator", false);
    bool seen = p.isKey(key("t").c_str());
    float oldT = p.getFloat(key("t").c_str(), tail), oldF = p.getFloat(key("f").c_str(), frac);
    p.putFloat(key("t").c_str(), seen ? oldT + LEARN_WEIGHT * (tail - oldT) : tail);
    p.putFloat(key("f").c_str(), seen ? oldF + LEARN_WEIGHT * (frac - oldF) : frac);
    p.end();
  }

  int peakMa_ = 0, below_ = 0, lastStatus_ = -1;
  int32_t cvStart_ = -1;
  float etaS_ = -1;
  uint8_t chem_ = 0, cells_ = 0;
};
