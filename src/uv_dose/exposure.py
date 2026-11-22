"""Real-world adjustments to ARPANSA's ambient dose: shade, reflective surroundings, sunscreen.

ARPANSA measures UV on a flat, unshaded sensor facing the sky. What reaches your
skin differs, so each outing gets a single multiplier on the ambient dose:

    factor = (shade + reflection) / effective SPF

* **Shade** blocks the direct sun but not the UV scattered from the open sky,
  which is roughly half the total around midday in summer and more at other
  times. Good shade typically cuts UV by about half to three-quarters (Cancer
  Council: "up to 75%"); the factors here sit at the cautious end.
* **Reflection** from the ground adds UV, mostly to faces, necks and the
  underside of arms. The WHO's UV Index guide gives typical reflectances: fresh
  snow up to 80%, sea foam about 25%, dry beach sand about 15%, and most grass,
  soil and water under 10%. Shade overhead doesn't stop light reflected from
  the sunlit ground around you, so reflection is added rather than scaled.
* **Sunscreen** is tested at 2 mg/cm², but most people apply a half to a
  quarter of that. Protection is taken to fall in proportion to the amount
  applied: ``SPF_eff = 1 + (SPF - 1) x fraction``. Some studies find it falls
  faster than that, so treat the result as a best case for that amount. An
  amount of "None" means no sunscreen on any outing, whatever SPF each one has.

These are rough, population-level figures meant to make a plan more realistic.
They are not measurements of any particular spot.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    value: float
    short: str  # compact label for lists


SHADE_CHOICES: tuple[Choice, ...] = (
    Choice("sun", "Open sun", 1.0, "open sun"),
    Choice("patchy", "Patchy shade (scattered trees)", 0.75, "patchy shade"),
    Choice("good", "Good shade (umbrella, dense tree)", 0.5, "good shade"),
)

SURFACE_CHOICES: tuple[Choice, ...] = (
    Choice("ground", "Grass, soil or paving", 0.0, ""),
    Choice("water", "Water (pool, lake or sea)", 0.10, "water"),
    Choice("sand", "Beach sand", 0.15, "sand"),
    Choice("snow", "Fresh snow", 0.80, "snow"),
)

# Fraction of the tested 2 mg/cm² layer that is actually applied.
APPLICATION_CHOICES: tuple[Choice, ...] = (
    Choice("tested", "Thick, as tested (about 1 tsp per limb)", 1.0, "thick"),
    Choice("typical", "Typical (about half that)", 0.5, "typical"),
    Choice("thin", "Thin (about a quarter)", 0.25, "thin"),
    Choice("none", "None (no sunscreen)", 0.0, "none"),  # turns off every outing's SPF
)

DEFAULT_SHADE = "sun"
DEFAULT_SURFACE = "ground"
DEFAULT_APPLICATION = "typical"

# Sunscreen wears off with sweat, swimming and towelling; Cancer Council advises
# reapplying every two hours.
REAPPLY_HOURS = 2


def choice(choices: tuple[Choice, ...], key: str) -> Choice:
    """The choice with ``key``, or the first one if the key is unknown (e.g. an old settings file)."""
    return next((c for c in choices if c.key == key), choices[0])


def effective_spf(spf: float, applied_fraction: float) -> float:
    """Protection actually achieved when only ``applied_fraction`` of the tested amount is used."""
    spf = max(spf, 1.0)
    return 1.0 + (spf - 1.0) * max(0.0, min(applied_fraction, 1.0))


def exposure_factor(shade: float = 1.0, reflection: float = 0.0, spf_eff: float = 1.0) -> float:
    """Fraction of ARPANSA's ambient dose that reaches the skin."""
    return (max(shade, 0.0) + max(reflection, 0.0)) / max(spf_eff, 1.0)
