"""Fitzpatrick skin phototype questionnaire and burn thresholds.

The questionnaire and score bands follow ARPANSA's "Fitzpatrick skin phototype"
sheet (arpansa.gov.au, 2022): ten questions scored 0-4 in three sections, with
totals 0-6 = Type I, 7-13 = II, 14-20 = III, 21-27 = IV, 28-34 = V, 35+ = VI.

Burn thresholds are the minimal erythemal dose (MED) in SED. Published values
vary widely between people and studies (e.g. Type I 1.5-3 SED, Types V-VI
6-20 SED). The defaults here are the commonly used reference set
200/250/300/450/600/1000 J/m^2, at or near the low end of each range, and the
GUI lets the user override them. ARPANSA notes "a very fair-skinned person can
receive sunburn and skin damage from only two SEDs".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Question:
    key: str
    section: str
    text: str
    options: tuple[str, str, str, str, str]  # index == score


SECTION_GENETIC = "Genetic (physical traits)"
SECTION_SENSITIVITY = "Sensitivity (reaction to sun exposure)"
SECTION_TANNING = "Intentional exposure (tanning habits)"
SECTIONS = (SECTION_GENETIC, SECTION_SENSITIVITY, SECTION_TANNING)

QUESTIONS: tuple[Question, ...] = (
    Question("eyes", SECTION_GENETIC, "What colour are your eyes?",
             ("Light blue or green, grey", "Blue, green, grey",
              "Dark blue or green, light brown (hazel)", "Dark brown", "Brownish black")),
    Question("hair", SECTION_GENETIC, "What is the colour of your hair (naturally and before ageing)?",
             ("Red", "Blonde", "Chestnut or dark blonde", "Dark brown", "Black")),
    Question("skin", SECTION_GENETIC, "What is the colour of your skin (unexposed areas)?",
             ("Pink", "Very pale", "Light brown or olive", "Brown", "Dark brown")),
    Question("freckles", SECTION_GENETIC, "Do you have freckles on unexposed areas?",
             ("Many", "Several", "Few", "Rare", "None")),
    Question("burn", SECTION_SENSITIVITY, "What happens to your skin if you stay in the sun for an extended period?",
             ("Severe burns, blistering, peeling", "Moderate burns, blistering, peeling",
              "Burns sometimes followed by peeling", "Rare burns", "No burns")),
    Question("turn_brown", SECTION_SENSITIVITY, "Do you turn brown after sun exposure?",
             ("Never", "Rarely", "Sometimes", "Often", "Always")),
    Question("how_brown", SECTION_SENSITIVITY, "How brown do you get?",
             ("Hardly or not at all", "Light tan", "Medium tan", "Dark tan", "Very dark tan")),
    Question("face", SECTION_SENSITIVITY, "Is your face sensitive to the sun?",
             ("Very sensitive", "Sensitive", "Mildly sensitive", "Resistant", "Very resistant")),
    Question("tan_often", SECTION_TANNING, "How often do you tan?",
             ("Never", "Rarely", "Sometimes", "Often", "Always")),
    Question("last_exposure", SECTION_TANNING,
             "When did you last expose your skin to the sun or artificial tanning sources (tanning beds)?",
             ("More than three months ago", "In the last 2-3 months", "In the last 1-2 months",
              "In the last week", "In the last day")),
)

MAX_SCORE = 4 * len(QUESTIONS)


@dataclass(frozen=True)
class SkinType:
    number: int
    roman: str
    score_range: str
    title: str
    description: str
    example: str
    med_sed: float  # default burn threshold (minimal erythemal dose), SED
    med_range: str  # published range, for context
    color: str  # band colour from ARPANSA's phototype sheet

    @property
    def label(self) -> str:
        return f"Type {self.roman}: {self.title}"


SKIN_TYPES: tuple[SkinType, ...] = (
    SkinType(1, "I", "0-6", "Pale white skin", "Extremely sensitive skin, always burns, never tans.",
             "red hair with freckles", 2.0, "about 1.5-3 SED", "#efd862"),
    SkinType(2, "II", "7-13", "White skin", "Very sensitive skin, burns easily, tans minimally.",
             "fair-skinned, fair-haired Caucasians, northern Asians", 2.5, "about 2.5-4 SED", "#e5b660"),
    SkinType(3, "III", "14-20", "Light brown skin", "Sensitive skin, sometimes burns, slowly tans to light brown.",
             "darker Caucasians, some Asians", 3.0, "about 3-5 SED", "#b0683a"),
    SkinType(4, "IV", "21-27", "Moderate brown skin",
             "Mildly sensitive, burns minimally, always tans to moderate brown.",
             "Mediterranean and Middle Eastern Caucasians, southern Asians", 4.5, "about 4-6 SED", "#8a4f2c"),
    SkinType(5, "V", "28-34", "Dark brown skin", "Resistant skin, rarely burns, tans well.",
             "some Hispanics, some Africans", 6.0, "about 6-20 SED", "#76330c"),
    SkinType(6, "VI", "35+", "Deeply pigmented dark brown to black skin",
             "Very resistant skin, never burns, deeply pigmented.",
             "darker Africans, Indigenous Australians", 10.0, "about 6-20 SED", "#2e1a1d"),
)


def skin_type(number: int) -> SkinType:
    if not 1 <= number <= 6:
        raise ValueError(f"Fitzpatrick skin type must be 1-6, got {number}")
    return SKIN_TYPES[number - 1]


def score_answers(answers: Mapping[str, int]) -> int:
    """Total score for a complete set of answers (``question key -> option index 0-4``)."""
    missing = [q.key for q in QUESTIONS if q.key not in answers]
    if missing:
        raise ValueError(f"Unanswered questions: {', '.join(missing)}")
    total = 0
    for q in QUESTIONS:
        value = answers[q.key]
        if not 0 <= value <= 4:
            raise ValueError(f"Answer for {q.key!r} must be 0-4, got {value}")
        total += value
    return total


def skin_type_from_score(score: int) -> SkinType:
    if score < 0:
        raise ValueError("Score cannot be negative")
    for upper, number in ((6, 1), (13, 2), (20, 3), (27, 4), (34, 5)):
        if score <= upper:
            return skin_type(number)
    return skin_type(6)
