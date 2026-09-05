"""Quantum-inspired match probabilities (mathematical analogy, not QC hardware).

Open/innovative idea used in some sports-modelling experiments:
  - Encode home/draw/away as complex amplitudes
  - Interference from form / market / pi-rating "phase" shifts
  - Collapse via Born rule: P_i = |A_i|^2 / sum |A_j|^2

This is transparent classical linear algebra inspired by quantum measurement —
documented as experimental, educational, and blended at modest weight.
"""
from __future__ import annotations

import cmath
import math
from typing import Any, Dict, Optional, Tuple


def _amp(mag: float, phase: float) -> complex:
    return cmath.rect(max(1e-6, mag), phase)


def quantum_inspired_probs(
    ph: float,
    pd: float,
    pa: float,
    *,
    elo_edge: float = 0.0,
    pi_gd: float = 0.0,
    form_edge: float = 0.0,
    market_ph: Optional[float] = None,
    market_pd: Optional[float] = None,
    market_pa: Optional[float] = None,
    league_high_scoring: bool = False,
) -> Dict[str, Any]:
    """Build amplitudes from classical probs + phase interference, then Born-normalize."""
    # Magnitudes from sqrt of classical probs (so Born recovers them if phase=0)
    m_h = math.sqrt(max(1e-6, ph))
    m_d = math.sqrt(max(1e-6, pd))
    m_a = math.sqrt(max(1e-6, pa))

    # Phases: relative "coherence" from strength / form / market disagreement
    # Positive elo/pi edge rotates toward home win amplitude
    phase_h = 0.35 * math.tanh(elo_edge / 120.0) + 0.45 * math.tanh(pi_gd / 1.5) + 0.25 * math.tanh(form_edge)
    phase_a = -phase_h
    # Draw gets phase from parity / stalemate signals
    phase_d = 0.20 * math.tanh(-abs(elo_edge) / 150.0) + (0.15 if league_high_scoring else 0.0)

    if market_ph is not None and market_pa is not None:
        # Market vs model disagreement → extra interference
        phase_h += 0.30 * (market_ph - ph)
        phase_a += 0.30 * (market_pa - pa)
        if market_pd is not None:
            phase_d += 0.25 * (market_pd - pd)

    Ah = _amp(m_h, phase_h)
    Ad = _amp(m_d, phase_d)
    Aa = _amp(m_a, phase_a)

    # Optional weak entanglement-style coupling: small cross term toward draw when sides close
    if abs(ph - pa) < 0.08:
        Ad = Ad + 0.08 * (Ah + Aa)

    w_h, w_d, w_a = abs(Ah) ** 2, abs(Ad) ** 2, abs(Aa) ** 2
    s = w_h + w_d + w_a
    return {
        "p_home": w_h / s,
        "p_draw": w_d / s,
        "p_away": w_a / s,
        "amplitudes": {
            "home": {"re": Ah.real, "im": Ah.imag, "phase": cmath.phase(Ah)},
            "draw": {"re": Ad.real, "im": Ad.imag, "phase": cmath.phase(Ad)},
            "away": {"re": Aa.real, "im": Aa.imag, "phase": cmath.phase(Aa)},
        },
        "method": "born_rule_interference_v1",
        "note": "Quantum-inspired classical amplitudes; not quantum hardware.",
    }


def blend_with_quantum(
    classical: Tuple[float, float, float],
    quantum: Dict[str, float],
    weight_q: float = 0.22,
) -> Tuple[float, float, float]:
    w = max(0.0, min(0.45, weight_q))
    ph = (1 - w) * classical[0] + w * float(quantum["p_home"])
    pd = (1 - w) * classical[1] + w * float(quantum["p_draw"])
    pa = (1 - w) * classical[2] + w * float(quantum["p_away"])
    s = ph + pd + pa
    return ph / s, pd / s, pa / s
