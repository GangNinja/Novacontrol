"""Local from-scratch reasoning engine for NovaControl chat.

Answers common questions directly — math, time, knowledge, conversions,
recommendations — without requiring an external LLM.
"""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Safe math evaluator
# ---------------------------------------------------------------------------

_OPS: dict[str, Any] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
    "%": operator.mod,
    "**": operator.pow,
}

def _ncr(n: float, r: float) -> float:
    """Combinations: n choose r (exam shorthand 10C3 and worded "8 choose 2")."""
    return float(math.comb(int(n), int(r)))


def _npr(n: float, r: float) -> float:
    """Permutations: n P r."""
    return float(math.perm(int(n), int(r)))


def _sind(degrees: float) -> float:
    return math.sin(math.radians(degrees))


def _cosd(degrees: float) -> float:
    return math.cos(math.radians(degrees))


def _tand(degrees: float) -> float:
    return math.tan(math.radians(degrees))


_MATH_FUNCS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    # Degree variants: JEE/NCERT phrasing is angle-in-degrees ("sin 30 degrees").
    "sind": _sind,
    "cosd": _cosd,
    "tand": _tand,
    "log": math.log,  # log(x) natural, log(x, base) arbitrary base
    "log10": math.log10,
    "log2": math.log2,
    "floor": math.floor,
    "ceil": math.ceil,
    # Combinatorics + factorials for JEE counting problems.
    "ncr": _ncr,
    "npr": _npr,
    "factorial": math.factorial,
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _safe_eval_math(expr: str) -> float | None:
    """Evaluate a simple math expression safely. Returns None on failure."""
    cleaned = expr.strip().replace(" ", "")
    # Allow only digits, operators, parens, dots, commas, function names
    if not re.match(r'^[\d+\-*/().,%a-zA-Z_]+$', cleaned):
        return None
    try:
        result = eval(cleaned, {"__builtins__": {}}, _MATH_FUNCS)  # noqa: S307
        return float(result)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------

_LENGTH_CONVERSIONS: dict[str, float] = {
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "km": 1000.0,
    "kilometer": 1000.0,
    "kilometers": 1000.0,
    "cm": 0.01,
    "centimeter": 0.01,
    "centimeters": 0.01,
    "mm": 0.001,
    "millimeter": 0.001,
    "millimeters": 0.001,
    "mi": 1609.344,
    "mile": 1609.344,
    "miles": 1609.344,
    "yd": 0.9144,
    "yard": 0.9144,
    "yards": 0.9144,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
}

_MASS_CONVERSIONS: dict[str, float] = {
    "kg": 1.0,
    "kilogram": 1.0,
    "kilograms": 1.0,
    "g": 0.001,
    "gram": 0.001,
    "grams": 0.001,
    "mg": 0.000001,
    "milligram": 0.000001,
    "lb": 0.453592,
    "lbs": 0.453592,
    "pound": 0.453592,
    "pounds": 0.453592,
    "oz": 0.0283495,
    "ounce": 0.0283495,
    "ounces": 0.0283495,
}

_TEMP_UNITS = {"c", "f", "k", "celsius", "fahrenheit", "kelvin"}

_VOLUME_CONVERSIONS: dict[str, float] = {
    "l": 1.0,
    "liter": 1.0,
    "liters": 1.0,
    "ml": 0.001,
    "milliliter": 0.001,
    "milliliters": 0.001,
    "gal": 3.78541,
    "gallon": 3.78541,
    "gallons": 3.78541,
    "cup": 0.236588,
    "cups": 0.236588,
}

_TIME_UNITS = {"s": "seconds", "sec": "seconds", "seconds": "seconds",
               "min": "minutes", "minute": "minutes", "minutes": "minutes",
               "h": "hours", "hr": "hours", "hour": "hours", "hours": "hours",
               "d": "days", "day": "days", "days": "days",
               "w": "weeks", "week": "weeks", "weeks": "weeks"}

_SPEED_CONVERSIONS: dict[str, float] = {
    "km/h": 1.0, "kmph": 1.0, "kph": 1.0,
    "mph": 1.60934,
    "m/s": 3.6, "mps": 3.6,
    "knots": 1.852, "kn": 1.852,
}

_CONVERSION_GROUPS = [
    (_LENGTH_CONVERSIONS, "length"),
    (_MASS_CONVERSIONS, "mass"),
    (_VOLUME_CONVERSIONS, "volume"),
    (_SPEED_CONVERSIONS, "speed"),
]

_UNIT_ALIASES = {
    "celsius": "c", "fahrenheit": "f", "kelvin": "k",
    "meter": "m", "meters": "m", "kilometer": "km", "kilometers": "km",
    "centimeter": "cm", "centimeters": "cm", "millimeter": "mm",
    "mile": "mi", "miles": "mi", "yard": "yd", "yards": "yd",
    "foot": "ft", "feet": "ft", "inch": "in", "inches": "in",
    "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g",
    "pound": "lb", "pounds": "lb", "ounce": "oz", "ounces": "oz",
    "liter": "l", "liters": "l", "milliliter": "ml", "milliliters": "ml",
    "gallon": "gal", "gallons": "gal", "cup": "cup", "cups": "cup",
}


def _convert_temperature(value: float, from_unit: str, to_unit: str) -> float | None:
    fu = _UNIT_ALIASES.get(from_unit, from_unit)[0]
    tu = _UNIT_ALIASES.get(to_unit, to_unit)[0]
    if fu == tu:
        return value
    # to Celsius first
    if fu == "f":
        c = (value - 32) * 5 / 9
    elif fu == "k":
        c = value - 273.15
    else:
        c = value
    # from Celsius to target
    if tu == "f":
        return c * 9 / 5 + 32
    if tu == "k":
        return c + 273.15
    return c


def _try_convert(value: float, from_unit: str, to_unit: str) -> float | None:
    """Try unit conversion. Returns converted value or None."""
    fu = _UNIT_ALIASES.get(from_unit, from_unit)
    tu = _UNIT_ALIASES.get(to_unit, to_unit)

    # Temperature
    if fu in _TEMP_UNITS and tu in _TEMP_UNITS:
        return _convert_temperature(value, fu, tu)

    # Grouped conversions
    for group, _name in _CONVERSION_GROUPS:
        if fu in group and tu in group:
            return value * group[fu] / group[tu]

    return None


# ---------------------------------------------------------------------------
# Local knowledge base — common facts
# ---------------------------------------------------------------------------

_KNOWLEDGE: dict[str, str] = {
    # Planets
    "how many planets": "There are 8 planets in our solar system: Mercury, Venus, Earth, Mars, Jupiter, Saturn, Uranus, and Neptune. Pluto was reclassified as a dwarf planet in 2006.",
    "speed of light": "The speed of light in a vacuum is approximately 299,792,458 meters per second (about 186,282 miles per second).",
    "speed of sound": "The speed of sound in air at 20°C is approximately 343 meters per second (about 1,125 feet per second).",
    "boiling point of water": "Water boils at 100°C (212°F) at standard atmospheric pressure (1 atm). At higher altitudes, the boiling point decreases.",
    "freezing point of water": "Water freezes at 0°C (32°F) at standard atmospheric pressure.",
    "distance to the moon": "The average distance from Earth to the Moon is about 384,400 kilometers (238,855 miles).",
    "distance to the sun": "The average distance from Earth to the Sun is about 149.6 million kilometers (93 million miles), known as 1 Astronomical Unit (AU).",
    "age of the earth": "Earth is approximately 4.54 billion years old, based on radiometric dating of meteorite material.",
    "age of the universe": "The universe is approximately 13.8 billion years old, based on observations of the cosmic microwave background.",
    "diameter of earth": "Earth's equatorial diameter is approximately 12,742 kilometers (7,918 miles).",
    "gravity on earth": "Standard gravity on Earth is 9.80665 m/s² (approximately 32.174 ft/s²).",
    "avogadro's number": "Avogadro's number is approximately 6.022 × 10²³ per mole.",
    "speed of internet": "Fiber optic internet can reach speeds up to 1 Gbps (gigabit per second) or higher. Typical home broadband ranges from 25-500 Mbps.",

    # Inventions
    "who invented the lightbulb": "Thomas Edison is commonly credited with inventing the practical incandescent light bulb in 1879, though many inventors contributed to its development, including Humphry Davy and Joseph Swan.",
    "who invented the telephone": "Alexander Graham Bell is credited with inventing the telephone in 1876, though Antonio Meucci also developed an early version.",
    "who invented the internet": "The internet evolved from ARPANET, developed by Vint Cerf and Bob Kahn in the 1970s. Tim Berners-Lee invented the World Wide Web in 1989.",
    "who invented the airplane": "The Wright Brothers (Orville and Wilbur Wright) made the first powered airplane flight on December 17, 1903, at Kitty Hawk, North Carolina.",

    # Geography
    "capital of france": "The capital of France is Paris.",
    "capital of japan": "The capital of Japan is Tokyo.",
    "capital of india": "The capital of India is New Delhi.",
    "capital of china": "The capital of China is Beijing.",
    "capital of germany": "The capital of Germany is Berlin.",
    "capital of united states": "The capital of the United States is Washington, D.C.",
    "capital of usa": "The capital of the United States is Washington, D.C.",
    "capital of uk": "The capital of the United Kingdom is London.",
    "capital of england": "The capital of England is London.",
    "capital of brazil": "The capital of Brazil is Brasília.",
    "capital of australia": "The capital of Australia is Canberra.",
    "capital of canada": "The capital of Canada is Ottawa.",
    "capital of russia": "The capital of Russia is Moscow.",
    "largest country": "Russia is the largest country by area at approximately 17.1 million square kilometers.",
    "most populated country": "India is the most populated country with approximately 1.44 billion people, surpassing China in 2023.",
    "longest river": "The Nile River in Africa is traditionally considered the longest river at approximately 6,650 km (4,130 miles), though the Amazon River may be longer.",
    "tallest mountain": "Mount Everest is the tallest mountain above sea level at 8,849 meters (29,032 feet).",

    # Science
    "what is dna": "DNA (deoxyribonucleic acid) is a molecule that carries genetic instructions for the development, functioning, growth, and reproduction of all known living organisms.",
    "what is evolution": "Evolution is the process by which species change over time through variations in heritable characteristics. Natural selection, proposed by Charles Darwin, is a key mechanism.",
    "what is gravity": "Gravity is a fundamental force that attracts objects with mass toward each other. On Earth's surface, it causes objects to accelerate at 9.8 m/s².",
    "what is photosynthesis": "Photosynthesis is the process by which green plants convert sunlight, carbon dioxide, and water into glucose and oxygen. It occurs primarily in the chloroplasts of leaf cells.",
    "what is climate change": "Climate change refers to long-term shifts in global temperatures and weather patterns, primarily driven by human activities like burning fossil fuels since the Industrial Revolution.",

    # Technology
    "what is ai": "Artificial Intelligence (AI) is the simulation of human intelligence by machines, including learning, reasoning, problem-solving, perception, and language understanding.",
    "what is machine learning": "Machine Learning is a subset of AI where systems learn patterns from data to make predictions or decisions without being explicitly programmed.",
    "what is quantum computing": "Quantum computing uses quantum mechanical phenomena (superposition and entanglement) to process information in fundamentally different ways than classical computers.",
    "what is blockchain": "Blockchain is a distributed, immutable ledger technology that records transactions across many computers, ensuring that records cannot be altered retroactively.",
    "what is html": "HTML (HyperText Markup Language) is the standard markup language for creating web pages and web applications. It structures content on the web.",

    # Math concepts
    "what is pi": "Pi (π) is a mathematical constant approximately equal to 3.14159265. It represents the ratio of a circle's circumference to its diameter.",
    "what is euler's number": "Euler's number (e) is approximately 2.71828. It is the base of the natural logarithm and appears in many areas of mathematics.",
    "what is the pythagorean theorem": "The Pythagorean theorem states that in a right triangle, the square of the hypotenuse equals the sum of the squares of the other two sides: a² + b² = c².",
    "what is a prime number": "A prime number is a natural number greater than 1 that has no positive divisors other than 1 and itself. Examples: 2, 3, 5, 7, 11, 13.",
}


# ---------------------------------------------------------------------------
# Recommendation databases
# ---------------------------------------------------------------------------

_MOVIE_RECOMMENDATIONS = [
    ("Interstellar", "Sci-fi epic about space travel and black holes"),
    ("The Shawshank Redemption", "Drama about hope and resilience in prison"),
    ("Inception", "Mind-bending thriller about dreams within dreams"),
    ("The Dark Knight", "Batman faces the Joker in this crime epic"),
    ("Parasite", "Korean thriller about class inequality"),
    ("Dune", "Epic sci-fi about politics, religion, and sandworms"),
    ("The Matrix", "Hacker discovers reality is a simulation"),
    ("Oppenheimer", "Biopic of the atomic bomb's creation"),
]

_BOOK_RECOMMENDATIONS = [
    ("Sapiens", "Yuval Noah Harari — history of humankind"),
    ("Atomic Habits", "James Clear — building good habits and breaking bad ones"),
    ("The Art of War", "Sun Tzu — ancient strategy treatise"),
    ("1984", "George Orwell — dystopian classic about surveillance"),
    ("Deep Work", "Cal Newport — focused work in a distracted world"),
    ("Thinking, Fast and Slow", "Daniel Kahneman — cognitive biases and decision-making"),
    ("The Pragmatic Programmer", "Andrew Hunt & David Thomas — software engineering wisdom"),
    ("Dune", "Frank Herbert — epic science fiction novel"),
]

_FOOD_RECOMMENDATIONS = [
    ("Pizza", "Quick, customizable, and always satisfying"),
    ("Pasta", "Endless varieties — from simple aglio e olio to rich lasagna"),
    ("Stir-fry", "Fast, healthy, and uses whatever ingredients you have"),
    ("Tacos", "Fun, customizable, and ready in minutes"),
    ("Risotto", "Creamy comfort food that's easier than it looks"),
    ("Salad bowl", "Fresh, healthy, and endlessly adaptable"),
    ("Soup", "Warming, nutritious, and great for meal prep"),
]


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

_TIME_PATTERNS = re.compile(
    r'(what time|current time|current date|what day|what date|what month|'
    r'what year|today|what\'?s the date|time now|date now)',
    re.IGNORECASE,
)

# A bare expression of digits joined by an operator symbol ("2+3*4", "2^8").
_MATH_EXPR = re.compile(r'[\d]\s*[+\-*/^]\s*[\d]')
# The "add 5 and 7" form. Kept apart from _MATH_WORD_RULES because its answer
# builder slices down to the two operands (trailing phrasing like "... and show
# steps" must never reach the evaluator). Groups capture both operands.
_ADD_FORM = re.compile(r'\badd\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)', re.IGNORECASE)
# Bare function calls the evaluator supports ("sqrt(144)", "log(1000)",
# "ncr(10, 3)") — including the JEE/exam functions.
_FUNC_CALL_MATH = re.compile(
    r'\b(?:sqrt|sind|cosd|tand|sin|cos|tan|log|log10|log2|abs|floor|ceil|round|ncr|npr|factorial)\s*\(',
    re.IGNORECASE,
)

# Exam-math shapes beyond plain expression evaluation: quadratics to solve and
# arithmetic-progression term/sum questions. Detected by _math_detect and
# answered by _quadratic_answer / _ap_answer before the generic evaluator.
_EXAM_MATH = re.compile(
    r"(?:solve|roots?\s+of|factorise|factorize)[^\n]*x\s*\^?\s*2"
    r"|\d+\s*(?:st|nd|rd|th)\s+term"
    r"|sum\s+of\s+(?:the\s+)?first\s+\d+\s+terms",
    re.IGNORECASE,
)

# Declarative worded-arithmetic intent registry: one (phrase pattern, symbolic
# rewrite) row per natural-language way of saying arithmetic. Routing
# (is_arithmetic_query) and the answer builder (_math_answer) both consume this
# table, so recognizing a worded phrase as math and translating it into an
# evaluable expression can never diverge — changing a worded operator is a
# one-row edit, never a parallel regex change in two places. A rewrite is a
# plain backreference string for digit-only forms, or a callable when an
# operand may be a spelled-out number word ("fifteen times three", "2 to the
# power of 8") — the callable converts each captured operand to digits, so the
# evaluator only ever sees symbols.

# Cardinal/scale words recognized inside spelled-out operands.
_NUM_WORD_VALUES: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALE_WORDS: dict[str, int] = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}


def _words_to_int(words: str) -> int | None:
    """Parse a spelled-out cardinal ("one hundred and five") into an int."""
    total = 0
    current = 0
    for word in re.split(r"[\s-]+", words.strip().lower()):
        if word == "and":
            continue
        if word in _SCALE_WORDS:
            scale = _SCALE_WORDS[word]
            if scale == 100:
                current = current * 100 if current else 100
            else:
                total += (current or 1) * scale
                current = 0
        elif word in _NUM_WORD_VALUES:
            current += _NUM_WORD_VALUES[word]
        else:
            return None
    return total + current


def _fmt_number(value: float | int) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def _operand_value(text: str) -> float | int | None:
    """Resolve a captured operand to a number: digits pass through, words parse."""
    token = text.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    return _words_to_int(token)


def _binary_rewrite(match: re.Match[str], op: str) -> str:
    """Rewrite a digit-or-spelled operand pair into a symbolic expression."""
    left, right = _operand_value(match.group(1)), _operand_value(match.group(2))
    if left is None or right is None:
        return match.group(0)
    return f"{_fmt_number(left)}{op}{_fmt_number(right)}"


def _unary_rewrite(match: re.Match[str], func: str) -> str:
    value = _operand_value(match.group(1))
    if value is None:
        return match.group(0)
    return f"{func}({_fmt_number(value)})"


def _power_rewrite(match: re.Match[str], exp: int) -> str:
    value = _operand_value(match.group(1))
    if value is None:
        return match.group(0)
    return f"{_fmt_number(value)}**{exp}"


def _percent_rewrite(match: re.Match[str]) -> str:
    """""XX percent of Y" -> (X/100)*Y."""
    percent = _operand_value(match.group(1))
    base = _operand_value(match.group(2))
    if percent is None or base is None:
        return match.group(0)
    return f"({_fmt_number(percent)}/100)*{_fmt_number(base)}"


def _log_base_rewrite(match: re.Match[str]) -> str:
    """Rewrite 'log base B of V' -> log(V, B) — the exam phrasing for log_B(V)."""
    base = _operand_value(match.group(1))
    value = _operand_value(match.group(2))
    if base is None or value is None:
        return match.group(0)
    return f"log({_fmt_number(value)}, {_fmt_number(base)})"


def _log_of_base_rewrite(match: re.Match[str]) -> str:
    """Rewrite 'log of V base B' -> log(V, B)."""
    value = _operand_value(match.group(1))
    base = _operand_value(match.group(2))
    if base is None or value is None:
        return match.group(0)
    return f"log({_fmt_number(value)}, {_fmt_number(base)})"


def _binary_func_word_rule(operator: str, func: str) -> tuple[re.Pattern[str], Callable[[re.Match[str]], str]]:
    """Worded binary function form ('8 choose 2' -> ncr(8, 2))."""
    pattern = re.compile(
        r"\b(" + _OPERAND_SEQ + r")\s+" + operator + r"\s+(" + _OPERAND_SEQ + r")\b",
        re.IGNORECASE,
    )

    def rewrite(match: re.Match[str]) -> str:
        left = _operand_value(match.group(1))
        right = _operand_value(match.group(2))
        if left is None or right is None:
            return match.group(0)
        return f"{func}({_fmt_number(left)}, {_fmt_number(right)})"

    return pattern, rewrite


_TRIG_FUNC = {"sin": "sind", "cos": "cosd", "tan": "tand"}
_TRIG_RADIAN_FUNC = {"sin": "sin", "cos": "cos", "tan": "tan"}


def _trig_degrees_rewrite(match: re.Match[str]) -> str:
    """Rewrite 'sin 30 degrees' -> degree-based trig (explicit exam convention)."""
    func = _TRIG_FUNC[match.group(1).lower()]
    value = _operand_value(match.group(2))
    if value is None:
        return match.group(0)
    return f"{func}({_fmt_number(value)})"


def _trig_radians_rewrite(match: re.Match[str]) -> str:
    """Rewrite bare 'sin 30' -> sin(30) — radians, the historical engine default."""
    func = _TRIG_RADIAN_FUNC[match.group(1).lower()]
    value = _operand_value(match.group(2))
    if value is None:
        return match.group(0)
    return f"{func}({_fmt_number(value)})"


def _factorial_rewrite(match: re.Match[str]) -> str:
    """Rewrite 'factorial of 5' / '5 factorial' -> factorial(5)."""
    value = _operand_value(match.group(1))
    if value is None:
        return match.group(0)
    return f"factorial({_fmt_number(value)})"


def _nc_symbolic_rewrite(match: re.Match[str]) -> str:
    """Rewrite the glued exam shorthand 10C3 -> ncr(10, 3)."""
    return f"ncr({match.group(1)}, {match.group(2)})"


def _np_symbolic_rewrite(match: re.Match[str]) -> str:
    """Rewrite the glued exam shorthand 10P3 -> npr(10, 3)."""
    return f"npr({match.group(1)}, {match.group(2)})"


# An operand: a decimal literal or one or more number words ("fifteen", "two
# hundred and five"), never an arbitrary letter run — "how many times a year"
# cannot match because "how"/"a" are not number words.
_NUM_TOKEN = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand|million"
)
# Greedy on purpose: a multi-word cardinal ("two hundred and five") must be
# consumed whole; expansion stops at the first word that is not a number word,
# so an operator keyword (or ordinary prose) always ends the operand.
_OPERAND_SEQ = r"(?:\d+(?:\.\d+)?|" + _NUM_TOKEN + r")(?:[\s-]+(?:and[\s-]+)?(?:\d+(?:\.\d+)?|" + _NUM_TOKEN + r"))*"


def _binary_word_rule(operator: str, op: str) -> tuple[re.Pattern[str], Callable[[re.Match[str]], str]]:
    # (?<![*)]) — the left operand must start clean: a number glued onto an
    # already-rewritten power ("5**2 times 2") or function call belongs to the
    # compound rows, never to a simple binary row (which would rewrite the bare
    # exponent "2 times 2" and steal the compound row's match).
    pattern = re.compile(
        r"(?<![*)])\b(" + _OPERAND_SEQ + r")\s+" + operator + r"\s+(" + _OPERAND_SEQ + r")\b",
        re.IGNORECASE,
    )
    return pattern, lambda m: _binary_rewrite(m, op)


Rewrite = str | Callable[[re.Match[str]], str]

# An operand of a COMPOUND worded expression: everything _OPERAND_SEQ covers
# (digits, spelled-out cardinals) PLUS forms produced by earlier rewrites in
# the fixed-point loop — a power ("2**3" from "2 cubed") or a function call
# ("sqrt(9)" from "square root of 9"). Without these, a binary operator whose
# neighbor was already translated can never match ("2 cubed plus the square
# root of 9" -> "2**3 plus sqrt(9)" would strand "plus" untranslated).
_COMPOUND_OPERAND = r"(?:" + _OPERAND_SEQ + r"|\d+(?:\.\d+)?(?:\*\*\d+)?|[a-z]+\(\d+(?:\.\d+)?\))"


def _resolve_side(text: str) -> str:
    """Normalize one captured operand side: words -> digits, symbolic passes through."""
    value = _operand_value(text)
    return _fmt_number(value) if value is not None else text.strip()


def _compound_binary_word_rule(operator: str, op: str) -> tuple[re.Pattern[str], Callable[[re.Match[str]], str]]:
    """Binary rule whose operands may be worded OR already-symbolic forms."""
    # Trailing lookahead instead of \b: when the right operand is a function
    # call ("sqrt(9)") the match ends in ")", a non-word char, and a trailing
    # \b would demand a word char on the other side — always false at a space
    # or end of string. (?![a-z*]) blocks only a glued-on letter/power op.
    pattern = re.compile(
        r"\b(" + _COMPOUND_OPERAND + r")\s+" + operator + r"\s+(" + _COMPOUND_OPERAND + r")(?![a-z*])",
        re.IGNORECASE,
    )

    def rewrite(match: re.Match[str]) -> str:
        left = _resolve_side(match.group(1))
        right = _resolve_side(match.group(2))
        return f"{left}{op}{right}"

    return pattern, rewrite

_MATH_WORD_RULES: tuple[tuple[re.Pattern[str], Rewrite], ...] = (
    # "to the power of" leads: claiming the power pair first keeps
    # "square root of 8 to the power of 3" -> "sqrt(8)**3" instead of letting a
    # later row mis-slice the expression.
    _binary_word_rule("to the power of", "**"),
    # Worded math functions/powers NEXT: a unary row must claim its operand
    # before a simple binary row can steal it as a bare digit. Binary-first
    # ordering mangled "the square root of 81 minus 5 squared" — the simple
    # minus row ate "81 minus 5" before "square root of"/"squared" could claim
    # their operands, stranding an unevaluable "81-5".
    # Worded math functions -> supported calls ("square root of 256", "5 squared").
    (re.compile(r"\b(?:the\s+)?square\s+root\s+of\s+(" + _OPERAND_SEQ + r")\b", re.IGNORECASE),
     lambda m: _unary_rewrite(m, "sqrt")),
    (re.compile(r"\bsqrt\s+of\s+(" + _OPERAND_SEQ + r")\b", re.IGNORECASE),
     lambda m: _unary_rewrite(m, "sqrt")),
    (re.compile(r"\b(" + _OPERAND_SEQ + r")\s+squared\b", re.IGNORECASE),
     lambda m: _power_rewrite(m, 2)),
    (re.compile(r"\b(" + _OPERAND_SEQ + r")\s+cubed\b", re.IGNORECASE),
     lambda m: _power_rewrite(m, 3)),
    # Spelled-out binary operators -> symbols ("15 times 3", "fifteen times three").
    _binary_word_rule("plus", "+"),
    _binary_word_rule("minus", "-"),
    _binary_word_rule("times", "*"),
    _binary_word_rule("over", "/"),
    _binary_word_rule("multiplied by", "*"),
    _binary_word_rule("divided by", "/"),
    # Compound forms: binary operators whose neighbors may already have been
    # rewritten by the rows above ("2 cubed plus the square root of 9" ->
    # "2**3+sqrt(9)"). Registered AFTER the simple rows so simple phrases keep
    # their original single-rule translation; the fixed-point loop lets these
    # fire on a later pass once neighbors are symbolic.
    _compound_binary_word_rule("plus", "+"),
    _compound_binary_word_rule("minus", "-"),
    _compound_binary_word_rule("times", "*"),
    _compound_binary_word_rule("multiplied by", "*"),
    _compound_binary_word_rule("divided by", "/"),
    _compound_binary_word_rule("over", "/"),
    # Percentage of: "15 percent of 200" -> (15/100)*200 (words work: "ten percent of 300").
    (re.compile(r"\b(" + _OPERAND_SEQ + r")\s+percent\s+of\s+(" + _OPERAND_SEQ + r")\b", re.IGNORECASE),
     lambda m: _percent_rewrite(m)),
    # ── JEE / exam forms ─────────────────────────────────────────────
    # log base B of V ("log base 2 of 8") — registered BEFORE the log-of row so
    # both orderings of base/value stay unambiguous.
    (re.compile(r"\blog\s+base\s+(" + _OPERAND_SEQ + r")\s+of\s+(" + _OPERAND_SEQ + r")\b", re.IGNORECASE),
     _log_base_rewrite),
    (re.compile(r"\blog\s+(?:of\s+)?(" + _OPERAND_SEQ + r")\s+base\s+(" + _OPERAND_SEQ + r")\b", re.IGNORECASE),
     _log_of_base_rewrite),
    # Degree trig: "sin 30 degrees", "cos of 60" — exam convention is degrees.
    (re.compile(r"\b(sin|cos|tan)\s+(?:of\s+)?(" + _OPERAND_SEQ + r")\s*(?:degrees?|°)\b", re.IGNORECASE),
     _trig_degrees_rewrite),
    (re.compile(r"\b(sin|cos|tan)\s+(?:of\s+)?(" + _OPERAND_SEQ + r")\b(?!.{0,20}\bdegrees?\b)", re.IGNORECASE),
     _trig_radians_rewrite),
    # Combinatorics: worded and glued exam shorthand.
    _binary_func_word_rule("choose", "ncr"),
    _binary_func_word_rule("p", "npr"),
    (re.compile(r"\b(\d+)\s*[cC]\s*(\d+)\b(?!at)"), _nc_symbolic_rewrite),
    (re.compile(r"\b(\d+)\s*[pP]\s*(\d+)\b(?![a-z])"), _np_symbolic_rewrite),
    # Factorials: "factorial of 5", "6 factorial".
    (re.compile(r"\bfactorial\s+of\s+(" + _OPERAND_SEQ + r")\b", re.IGNORECASE),
     _factorial_rewrite),
    (re.compile(r"\b(" + _OPERAND_SEQ + r")\s+factorial\b", re.IGNORECASE),
     _factorial_rewrite),
)


def rewrite_worded_math(expr: str, *, max_passes: int = 8) -> str:
    """Apply the rule registry to a fixed point: worded math -> symbolic.

    ONE canonical walk, consumed by the answer builder and importable by tests
    (the drift guard uses it to bring a raw phrase to the state where a given
    compound row actually fires — compound rows only match after simpler rows
    have rewritten their neighbors).
    """
    for _ in range(max_passes):
        changed = False
        for pattern, replacement in _MATH_WORD_RULES:
            updated = pattern.sub(replacement, expr)
            if updated != expr:
                expr, changed = updated, True
        if not changed:
            break
    return expr


def is_arithmetic_query(lower: str) -> bool:
    """Symbolic or worded arithmetic the scratch evaluator can compute locally.

    The single routing predicate for "route math to the local brain": bare symbols
    ("2+2"), the "add 5 and 7" form, function calls ("sqrt(144)"), and every
    worded phrase declared in the _MATH_WORD_RULES registry (spelled-out
    operators like "15 times 3" and worded functions like "square root of 256"
    or "5 squared") all count.
    """
    return bool(
        _MATH_EXPR.search(lower)
        or _ADD_FORM.search(lower)
        or _FUNC_CALL_MATH.search(lower)
        or _EXAM_MATH.search(lower)
        or any(pattern.search(lower) for pattern, _ in _MATH_WORD_RULES)
    )


# Complement matcher for _classify's math branch: shapes is_arithmetic_query
# does not already match — a bare chain of symbol/space characters ("5 +") or a
# calculate/compute/what-is prefix over digits ("calculate 2+"). Worded
# operators, worded functions, function calls, and the "add X and Y" form belong
# to the registry / dedicated matchers above, never duplicated here.
_MATH_PATTERNS = re.compile(
    r'^[\d\s+\-*/().,%^]+$|'
    r'(calculate|compute|add|what is|what\'?s)\s+[\d\s+\-*/().%^]+',
    re.IGNORECASE,
)

# Quadratic standard form: ax^2 + bx + c (spaces optional around terms, the
# leading coefficient may be implicit — "x^2 - 5x + 6" — c may be missing, x
# may be written x^2 or x**2).
_QUADRATIC = re.compile(
    r"(-?\d+(?:\.\d+)?)?\s*\*?\s*x\s*\^?\s*2\s*"
    r"(?:([+-])\s*(\d+(?:\.\d+)?)\s*\*?\s*x\s*)?"
    r"(?:([+-])\s*(\d+(?:\.\d+)?)\s*)?",
    re.IGNORECASE,
)

# Arithmetic progression: "a = 2 d = 5", "first term 3 common difference 4",
# "ap with a 2 and d 5".
_AP_SPEC = re.compile(
    r"(?:a(?:\s*=|\s+is)?\s*(-?\d+(?:\.\d+)?))\s*(?:and\s*)?(?:"
    r"d(?:\s*=|\s+is)?\s*(-?\d+(?:\.\d+)?)"
    r"|common\s+difference\s*(?:of|is|=)?\s*(-?\d+(?:\.\d+)?)"
    r"|first\s+term\s*(?:of|is|=)?\s*(-?\d+(?:\.\d+)?)"
    r"|\bwith\b)",
    re.IGNORECASE,
)

_UNIT_PATTERN = re.compile(
    r'([\d.]+)\s*([a-zA-Z/°]+)\s+(?:in|to|into|=)\s+([a-zA-Z/°]+)',
    re.IGNORECASE,
)

_TEMP_PATTERN = re.compile(
    r'([\d.]+)\s*°?\s*(celsius|fahrenheit|kelvin|°?c|°?f|°?k)\s+'
    r'(?:in|to|into|=)\s*(celsius|fahrenheit|kelvin|°?c|°?f|°?k)',
    re.IGNORECASE,
)


# The two classifiers that used to live here (_classify, scratchable_intent) and
# the scattered matchers they each carried (_GREETINGS, time/conversion regexes,
# knowledge/recommendation word lists) now live in ONE intent registry defined
# below, after the answer builders it wires up. _classify and scratchable_intent
# are read surfaces over that registry.


# ---------------------------------------------------------------------------
# Answer builders
# ---------------------------------------------------------------------------

def _quadratic_answer(text: str) -> dict[str, Any] | None:
    """Solve ax^2 + bx + c = 0 via the discriminant (JEE algebra staple).

    Returns None when the text is not a recognizable standard-form quadratic.
    """
    cleaned = text.lower().replace("**", "^")
    cleaned = re.sub(r"\b(equation|solve|the|roots?|of)\b", " ", cleaned)
    cleaned = cleaned.replace("= 0", "").replace("=0", "")
    match = _QUADRATIC.search(cleaned)
    if not match:
        return None
    a = float(match.group(1).replace(" ", "")) if match.group(1) else 1.0
    if a == 0:
        return None
    b = 0.0
    if match.group(2):
        b = float(match.group(3)) * (-1 if match.group(2) == "-" else 1)
    c = 0.0
    if match.group(4):
        c = float(match.group(5)) * (-1 if match.group(4) == "-" else 1)

    discriminant = b * b - 4 * a * c
    two_a = 2 * a
    if discriminant > 0:
        root1 = (-b + math.sqrt(discriminant)) / two_a
        root2 = (-b - math.sqrt(discriminant)) / two_a
        roots = f"x = {_fmt_number(root1)} and x = {_fmt_number(root2)}"
        kind = "two distinct real roots"
    elif discriminant == 0:
        root = -b / two_a
        roots = f"x = {_fmt_number(root)} (repeated)"
        kind = "one repeated real root"
    else:
        real = -b / two_a
        imag = math.sqrt(-discriminant) / two_a
        sign = "+" if imag >= 0 else "-"
        roots = f"x = {_fmt_number(real)} {sign} {_fmt_number(abs(imag))}i"
        kind = "complex conjugate roots"
    return {
        "message": f"Solving `{text.strip()}`: {roots} ({kind}).",
        "sections": [
            {"title": "Quadratic", "items": [
                f"Equation: {_fmt_number(a)}x^2 + {_fmt_number(b)}x + {_fmt_number(c)} = 0",
                f"Discriminant: {_fmt_number(discriminant)}",
                f"Roots: {roots}",
            ]},
        ],
    }


def _ap_answer(text: str) -> dict[str, Any] | None:
    """Arithmetic-progression term/sum from an a-and-d spec (JEE sequences).

    Understands "a = 2 d = 5 find the 10th term" and "sum of the first 10 terms
    of an ap with a 2 and d 5". Returns None when no a/d pair is present.
    """
    lower = text.lower()
    spec = _AP_SPEC.search(lower)
    if not spec:
        return None
    a = float(spec.group(1))
    d: float | None = None
    for group in spec.groups()[1:]:
        if group is not None:
            d = float(group)
            break
    if d is None:
        # "ap with a 2 and d 5" style: second number after "d"
        d_match = re.search(r"\bd\s*(?:=|is)?\s*(-?\d+(?:\.\d+)?)", lower)
        if not d_match:
            return None
        d = float(d_match.group(1))

    term_match = re.search(r"(\d+)\s*(?:st|nd|rd|th)\s+term", lower)
    sum_match = re.search(r"sum\s+of\s+(?:the\s+)?first\s+(\d+)\s+terms", lower)
    items: list[str] = [f"First term a = {_fmt_number(a)}", f"Common difference d = {_fmt_number(d)}"]
    if term_match:
        n = int(term_match.group(1))
        nth = a + (n - 1) * d
        items.append(f"{n}th term = a + (n-1)d = {_fmt_number(nth)}")
        message = f"The {n}th term of the AP is **{_fmt_number(nth)}**."
    elif sum_match:
        n = int(sum_match.group(1))
        total = n / 2 * (2 * a + (n - 1) * d)
        items.append(f"Sum of first {n} terms = n/2·(2a + (n-1)d) = {_fmt_number(total)}")
        message = f"The sum of the first {n} terms is **{_fmt_number(total)}**."
    else:
        return None
    return {"message": message, "sections": [{"title": "Arithmetic progression", "items": items}]}


def _math_answer(text: str) -> dict[str, Any]:
    """Evaluate a math expression and explain the result.

    Exam shapes (quadratics, AP term/sum questions) are answered by their
    dedicated solvers before the general expression evaluator.
    """
    quadratic = _quadratic_answer(text)
    if quadratic is not None:
        return quadratic
    ap = _ap_answer(text)
    if ap is not None:
        return ap
    expr = text.lower()
    add_form = expr.startswith("add ")
    # Keep only the two operands for "add X and Y ..." so trailing phrasing
    # ("... and show steps") never reaches the evaluator.
    if add_form:
        add_match = _ADD_FORM.match(expr)
        if add_match:
            expr = f"{add_match.group(1)} and {add_match.group(2)}"
    for prefix in ("calculate ", "compute ", "what is ", "what's ", "solve ", "add "):
        if expr.startswith(prefix):
            expr = expr[len(prefix):]
    # What the user typed (minus leading question words) — shown back in the answer.
    display = re.sub(r'\bwhat is\b', '', expr).strip().rstrip('?.!,;:')
    # Apply the declarative worded-arithmetic registry (spelled-out operators ->
    # symbols, worded functions -> calls) to a fixed point: replacing one
    # spelled-out operator can expose a neighboring operand to a later rule
    # ("5 squared times 2" -> "5**2 times 2" -> "5**2*2"). Same table that
    # routing uses, so translation can never drift from recognition.
    expr = rewrite_worded_math(expr)
    if add_form:
        expr = re.sub(r'\band\b', '+', expr)
    expr = re.sub(r'\bwhat is\b', '', expr).strip().rstrip('?.!,;:')

    result = _safe_eval_math(expr)
    if result is not None:
        # Format nicely
        if result == int(result) and abs(result) < 1e15:
            formatted = str(int(result))
        else:
            formatted = f"{result:,.6g}"
        return {
            # Backticks keep the raw expression literal when it contains * (e.g. 15*3).
            "message": f"The result of `{display}` is **{formatted}**.",
            "sections": [
                {"title": "Calculation", "items": [
                    f"Expression: {display}",
                    f"Result: {formatted}",
                ]},
            ],
        }
    return {
        "message": f"I couldn't evaluate that expression. Try something like `2 + 3 * 4` or `sqrt(144)`.",
        "sections": [],
    }


def _time_answer(text: str) -> dict[str, Any]:
    """Answer time and date questions."""
    now = datetime.now()
    lower = text.lower()
    parts: list[str] = []

    if "time" in lower:
        parts.append(f"Current time: **{now.strftime('%I:%M %p')}**")
    if any(w in lower for w in ("date", "day", "today")):
        parts.append(f"Today: **{now.strftime('%A, %B %d, %Y')}**")
    if "month" in lower:
        parts.append(f"Current month: **{now.strftime('%B %Y')}**")
    if "year" in lower:
        parts.append(f"Current year: **{now.year}**")

    if not parts:
        parts.append(f"Right now it is **{now.strftime('%A, %B %d, %Y at %I:%M %p')}**.")

    message = "\n\n".join(parts)
    return {
        "message": message,
        "sections": [
            {"title": "Date & Time", "items": [
                f"Day: {now.strftime('%A')}",
                f"Date: {now.strftime('%B %d, %Y')}",
                f"Time: {now.strftime('%I:%M:%S %p')}",
                f"ISO: {now.isoformat()}",
            ]},
        ],
    }


def _conversion_answer(text: str) -> dict[str, Any]:
    """Handle unit conversions."""
    lower = text.lower()

    # Temperature
    temp_match = _TEMP_PATTERN.search(lower)
    if temp_match:
        value, from_u, to_u = float(temp_match.group(1)), temp_match.group(2), temp_match.group(3)
        result = _convert_temperature(value, from_u, to_u)
        if result is not None:
            fu = from_u[0].upper() if len(from_u) == 1 else from_u.title()
            tu = to_u[0].upper() if len(to_u) == 1 else to_u.title()
            return {
                "message": f"**{value} {fu}** = **{result:.2f} {tu}**",
                "sections": [
                    {"title": "Conversion", "items": [
                        f"{value} {fu} → {result:.2f} {tu}",
                        f"Formula: {from_u} to {to_u}",
                    ]},
                ],
            }

    # General unit conversion
    unit_match = _UNIT_PATTERN.search(lower)
    if unit_match:
        value, from_u, to_u = float(unit_match.group(1)), unit_match.group(2), unit_match.group(3)
        result = _try_convert(value, from_u, to_u)
        if result is not None:
            if abs(result) >= 1000:
                formatted = f"{result:,.2f}"
            else:
                formatted = f"{result:.4g}"
            return {
                "message": f"**{value} {from_u}** = **{formatted} {to_u}**",
                "sections": [
                    {"title": "Conversion", "items": [
                        f"{value} {from_u} → {formatted} {to_u}",
                    ]},
                ],
            }

    # Currency (approximate rates)
    currency_match = re.search(
        r'([\d.]+)\s*(usd|eur|gbp|inr|jpy|cad|aud|cny|krw)\s+'
        r'(?:in|to|=)\s*(usd|eur|gbp|inr|jpy|cad|aud|cny|krw)',
        lower,
    )
    if currency_match:
        value, from_c, to_c = float(currency_match.group(1)), currency_match.group(2), currency_match.group(3)
        rates = {
            "usd": 1.0, "eur": 0.92, "gbp": 0.79, "inr": 83.5,
            "jpy": 149.5, "cad": 1.36, "aud": 1.53, "cny": 7.24, "krw": 1330.0,
        }
        if from_c in rates and to_c in rates:
            result = value / rates[from_c] * rates[to_c]
            return {
                "message": f"**{value:,.2f} {from_c.upper()}** ≈ **{result:,.2f} {to_c.upper()}** (approximate)",
                "sections": [
                    {"title": "Currency Conversion", "items": [
                        f"{value:,.2f} {from_c.upper()} → {result:,.2f} {to_c.upper()}",
                        "Rates are approximate and may not reflect current market values.",
                    ]},
                ],
            }

    # Time duration conversion
    time_match = re.search(
        r'([\d.]+)\s*(second|minute|hour|day|week|sec|min|hr|h|d|w)s?'
        r'\s+(?:in|to|=)\s*(second|minute|hour|day|week|sec|min|hr|h|d|w)s?',
        lower,
    )
    if time_match:
        value = float(time_match.group(1))
        from_u = time_match.group(2)
        to_u = time_match.group(3)
        from_name = _TIME_UNITS.get(from_u, from_u)
        to_name = _TIME_UNITS.get(to_u, to_u)
        # Convert everything to seconds
        to_seconds = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
        result = value * to_seconds[from_name] / to_seconds[to_name]
        return {
            "message": f"**{value:g} {from_name}** = **{result:g} {to_name}**",
            "sections": [
                {"title": "Time Conversion", "items": [
                    f"{value:g} {from_name} → {result:g} {to_name}",
                ]},
            ],
        }

    return {
        "message": "I couldn't parse that conversion. Try something like `100 km in miles` or `72 F to C`.",
        "sections": [],
    }


def _knowledge_answer(text: str) -> dict[str, Any]:
    """Look up a fact from the local knowledge base."""
    lower = text.lower()

    # Direct match against knowledge keys
    for key, answer in _KNOWLEDGE.items():
        # Check if the key appears in the query or the query matches the key closely
        if key in lower or lower.startswith(key):
            # Build a response that's a bit more conversational
            return {
                "message": answer,
                "sections": [
                    {"title": "Fact", "items": [answer]},
                ],
            }

    # Partial match — try to find the closest key
    best_match = None
    best_score = 0
    query_words = set(lower.split())
    for key in _KNOWLEDGE:
        key_words = set(key.split())
        overlap = len(query_words & key_words)
        if overlap > best_score:
            best_score = overlap
            best_match = key

    if best_match and best_score >= 1:
        return {
            "message": _KNOWLEDGE[best_match],
            "sections": [
                {"title": "Fact", "items": [_KNOWLEDGE[best_match]]},
            ],
        }

    # Not found — suggest Explore
    return {
        "message": f"I don't have a local answer for that. Try **Explore** for a researched answer about this topic.",
        "sections": [
            {"title": "Not in Local Knowledge", "items": [
                "This question requires up-to-date or specialized information.",
                "Use the **Explore** panel for a source-checked answer.",
            ]},
        ],
    }


def _recommendation_answer(text: str) -> dict[str, Any]:
    """Provide recommendations based on the request type."""
    lower = text.lower()

    if any(w in lower for w in ("movie", "film", "watch", "show", "series")):
        import random
        picks = random.sample(_MOVIE_RECOMMENDATIONS, min(3, len(_MOVIE_RECOMMENDATIONS)))
        items = [f"**{title}** — {desc}" for title, desc in picks]
        return {
            "message": f"Here are some movies I'd recommend:\n\n" + "\n".join(f"• {i}" for i in items),
            "sections": [{"title": "Movie Recommendations", "items": items}],
        }

    if any(w in lower for w in ("book", "read", "novel", "author")):
        import random
        picks = random.sample(_BOOK_RECOMMENDATIONS, min(3, len(_BOOK_RECOMMENDATIONS)))
        items = [f"**{title}** — {desc}" for title, desc in picks]
        return {
            "message": f"Here are some books I'd recommend:\n\n" + "\n".join(f"• {i}" for i in items),
            "sections": [{"title": "Book Recommendations", "items": items}],
        }

    if any(w in lower for w in ("food", "eat", "cook", "meal", "dinner", "lunch", "snack", "recipe")):
        import random
        picks = random.sample(_FOOD_RECOMMENDATIONS, min(3, len(_FOOD_RECOMMENDATIONS)))
        items = [f"**{name}** — {desc}" for name, desc in picks]
        return {
            "message": f"Here are some food ideas:\n\n" + "\n".join(f"• {i}" for i in items),
            "sections": [{"title": "Food Suggestions", "items": items}],
        }

    # Generic recommendation
    return {
        "message": "I can suggest movies, books, or food ideas. What kind of recommendation are you looking for?",
        "sections": [
            {"title": "I Can Recommend", "items": [
                "Movies and TV shows",
                "Books and reading material",
                "Food and meal ideas",
            ]},
        ],
    }


# ---------------------------------------------------------------------------
# Intent registry — single source of truth
# ---------------------------------------------------------------------------
# Every scratch capability is ONE row: how the engine detects the intent
# (detect), the narrower gate decide() uses to send it to a canned local answer
# (routing, None = same as detect), the answer builder the engine runs
# (answer), its confidence, and example utterances the routing tests are
# generated from. _classify and scratchable_intent are read surfaces over
# these rows — changing one behavior is a one-row edit, never a parallel edit
# in two functions. Examples must route through BOTH surfaces for their row
# (a routing_safe row's example must also clear the narrow routing gate, so
# broad-only phrases like "what is dark matter" stay out of the table — those
# deliberate two-width edges are pinned by the static INTENTS/narrow tests).

Predicate = Callable[[str, str], bool]           # (lower, cleaned) -> matched
Answer = Callable[[str, Mapping[str, Any]], dict[str, Any]]  # (text, context) -> {message, sections}


@dataclass(frozen=True)
class _ScratchIntent:
    kind: str
    routing_safe: bool
    detect: Predicate
    routing: Predicate | None = None
    answer: Answer | None = None
    confidence: str = "medium"
    examples: tuple[str, ...] = ()


_GREETINGS = frozenset({
    "hi", "hello", "hey", "hai",
    "good morning", "good afternoon", "good evening",
})

# Time/date detection is deliberately two-width. The engine answers
# month/year/"current date" questions (detect), but decide() only routes the
# common forms to a local answer — "explain what year the berlin wall fell"
# must stay research, never a local clock answer.
_TIME_ROUTING = re.compile(
    r'what time|current time|what day|what date|today|time now|date now',
    re.IGNORECASE,
)

_CURRENCY_PATTERN = re.compile(
    r'[\d.]+\s*(?:usd|eur|gbp|inr|jpy|cad|aud|cny|krw)\s+(?:in|to|=)\s*'
    r'(?:usd|eur|gbp|inr|jpy|cad|aud|cny|krw)',
    re.IGNORECASE,
)
_DURATION_PATTERN = re.compile(
    r'[\d.]+\s*(?:second|minute|hour|day|week|sec|min|hr|h|d|w)s?\s+(?:in|to|=)\s*'
    r'(?:second|minute|hour|day|week|sec|min|hr|h|d|w)s?',
    re.IGNORECASE,
)


def _greeting_match(lower: str, cleaned: str) -> bool:
    return cleaned in _GREETINGS


def _phone_control_match(lower: str, cleaned: str) -> bool:
    if _contains(lower, "phone", "android", "mobile", "sms"):
        return any(w in lower for w in ("open ", "launch ", "control", "send", "call ", "text ", "message", "screenshot", "dial ", "search", "find ", "look up", "look for"))
    if "whatsapp" in lower:
        return True  # a phone-only app; no device word needed
    # Phone-action verbs that stand alone: text/call/dial/screenshot.
    return (
        lower.startswith(("call ", "dial ", "text ", "sms "))
        or _contains(lower, "send a text", "send text", "take a screenshot", "take screenshot", "screenshot my phone", "screenshot of my phone")
    )


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _desktop_control_match(lower: str, cleaned: str) -> bool:
    return any(p in lower for p in ("open ", "launch ", "close app", "control desktop", "run app"))


def _capabilities_match(lower: str, cleaned: str) -> bool:
    return any(p in lower for p in ("what can you do", "your capabilities", "who are you", "what are you"))


def _time_detect(lower: str, cleaned: str) -> bool:
    return bool(_TIME_PATTERNS.search(lower))


def _time_routing(lower: str, cleaned: str) -> bool:
    return bool(_TIME_ROUTING.search(lower))


def _conversion_match(lower: str, cleaned: str) -> bool:
    """Any conversion shape — temperature, unit, currency, or duration — is one
    intent, and the answer engine's builder handles all four."""
    return bool(
        _TEMP_PATTERN.search(lower)
        or _UNIT_PATTERN.search(lower)
        or _CURRENCY_PATTERN.search(lower)
        or _DURATION_PATTERN.search(lower)
    )


def _math_detect(lower: str, cleaned: str) -> bool:
    """Engine-wide math: symbolic/worded arithmetic, exam shapes, complement shapes."""
    if _EXAM_MATH.search(lower):
        return True
    if not (is_arithmetic_query(lower) or _MATH_PATTERNS.search(lower)):
        return False
    # "what is 2+2" is still math, but prose like "what is the capital..." is not.
    if not any(lower.startswith(q) for q in ("what is", "who", "how many")):
        return True
    if is_arithmetic_query(lower):
        return True
    return "sqrt" in lower or "sin " in lower or "cos " in lower or "log " in lower


def _math_routing(lower: str, cleaned: str) -> bool:
    return (
        is_arithmetic_query(lower)
        or _EXAM_MATH.search(lower) is not None
        or any(
            lower.startswith(p)
            for p in ("calculate ", "compute ", "sqrt", "sin ", "cos ", "log", "solve ")
        )
    )


def _knowledge_detect(lower: str, cleaned: str) -> bool:
    """Broad knowledge look: prefix/factual questions route to the knowledge
    engine, which answers an exact key and otherwise offers Explore."""
    prefixes = ("what is ", "what are ", "who is ", "who was ", "who invented",
                "how many ", "how far ", "how old ", "how tall ", "how deep",
                "how fast ", "how hot ", "how cold ")
    if any(cleaned.startswith(p) for p in prefixes):
        for key in _KNOWLEDGE:
            if key in cleaned or cleaned.startswith(key):
                return True
        # General knowledge pattern — not a math expression
        if not re.search(r'[\d]\s*[+\-*/^]', cleaned):
            return True
    factual_patterns = ("capital of ", "invented ", "discovered ", "president of",
                        "population of", "area of", "currency of")
    return any(cleaned.startswith(p) for p in factual_patterns)


def _knowledge_routing(lower: str, cleaned: str) -> bool:
    """Narrow gate: only an exact knowledge key counts as a canned local answer."""
    return any(key in lower or lower.startswith(key) for key in _KNOWLEDGE)


def _recommendation_detect(lower: str, cleaned: str) -> bool:
    patterns = (
        "recommend", "suggest", "what should i", "what can i",
        "give me a", "give me some", "i want to", "i need a",
        "movie", "book", "food", "eat", "watch", "read",
    )
    return any(p in lower for p in patterns)


def _recommendation_routing(lower: str, cleaned: str) -> bool:
    if any(w in lower for w in ("recommend", "suggest", "what should i", "what can i eat")):
        return True
    return any(lower.startswith(p) for p in ("movie", "book", "food", "i want to watch", "i want to read"))


def _capabilities_of(context: Mapping[str, Any]) -> dict[str, bool]:
    modules = tuple(str(module) for module in context.get("modules", ()))
    return _capabilities(modules, context)


def _kb_answer(builder: Callable[[str], dict[str, Any]]) -> Answer:
    """Adapt a text-only answer builder to the registry's (text, context) shape."""
    def build(text: str, context: Mapping[str, Any]) -> dict[str, Any]:
        return builder(text)
    return build


def _greeting_answer(text: str, context: Mapping[str, Any]) -> dict[str, Any]:
    caps = _capabilities_of(context)
    return {"message": _compose_greeting(caps), "sections": _capability_sections(caps)}


def _phone_answer(text: str, context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message": "Phone control is not active yet. NovaControl needs a paired phone bridge before it can safely operate your device.",
        "sections": [
            {
                "title": "Required Before Phone Control",
                "items": [
                    "Pair the phone through an explicit bridge such as Android Debug Bridge or a dedicated companion app.",
                    "Show every requested action before execution.",
                    "Require approval for sensitive actions like messages, calls, payments, files, and settings.",
                ],
            }
        ],
    }


def _desktop_answer(text: str, context: Mapping[str, Any]) -> dict[str, Any]:
    target = _desktop_target(text.lower())
    return {
        "message": f"I can prepare a desktop action for {target}, but execution must be approval-gated.",
        "sections": [
            {
                "title": "Desktop Plan",
                "items": [
                    f"Create an action to open or control {target}.",
                    "Show the exact action in the UI.",
                    "Run it only after approval.",
                ],
            }
        ],
    }


def _capabilities_answer(text: str, context: Mapping[str, Any]) -> dict[str, Any]:
    caps = _capabilities_of(context)
    return {
        "message": "NovaControl is running as a local scratch-brain control app with chat, research, build, learning, voice, desktop, and browser modules.",
        "sections": _capability_sections(caps),
    }


# Engine/_classify precedence: phone before desktop ("open whatsapp on my
# phone" is phone control), time before conversion before math, knowledge before
# recommendation — the historical engine branch order, now the row order.
_INTENT_ROWS: tuple[_ScratchIntent, ...] = (
    _ScratchIntent("greeting", True, _greeting_match, answer=_greeting_answer, confidence="high",
                   examples=("hi", "good morning")),
    _ScratchIntent("phone_control", False, _phone_control_match, answer=_phone_answer,
                   examples=("open whatsapp on my phone", "text mom on my phone",
                             "call john", "take a screenshot on my phone",
                             "search cats on youtube on my phone")),
    _ScratchIntent("desktop_control", False, _desktop_control_match, answer=_desktop_answer,
                   examples=("open notepad",)),
    _ScratchIntent("capabilities", False, _capabilities_match, answer=_capabilities_answer,
                   examples=("what can you do",)),
    _ScratchIntent("time", True, _time_detect, routing=_time_routing,
                   answer=_kb_answer(_time_answer), confidence="high",
                   examples=("what time is it", "what day is today")),
    _ScratchIntent("conversion", True, _conversion_match,
                   answer=_kb_answer(_conversion_answer), confidence="high",
                   examples=("100 km in miles", "72 F to C")),
    _ScratchIntent("math", True, _math_detect, routing=_math_routing,
                   answer=_kb_answer(_math_answer), confidence="high",
                   examples=("what is 2+2", "15 times 3")),
    _ScratchIntent("knowledge", True, _knowledge_detect, routing=_knowledge_routing,
                   answer=_kb_answer(_knowledge_answer),
                   examples=("what is the capital of france", "who invented the telephone")),
    _ScratchIntent("recommendation", True, _recommendation_detect, routing=_recommendation_routing,
                   answer=_kb_answer(_recommendation_answer),
                   examples=("recommend a movie", "what should i eat")),
)
_INTENT_BY_KIND = {row.kind: row for row in _INTENT_ROWS}
_ENGINE_ORDER = tuple(row.kind for row in _INTENT_ROWS)

# decide() consults only the routing_safe rows, and checks math BEFORE
# time/conversion — an arithmetic phrase wins over a time keyword.
_SCRATCHABLE_KIND_ORDER = ("greeting", "math", "time", "conversion", "knowledge", "recommendation")


def _first_match(
    order: Sequence[str], lower: str, cleaned: str, *, routing: bool,
) -> _ScratchIntent | None:
    """First row in `order` that matches: detect predicates for the engine,
    routing predicates (routing_safe rows only) for decide()."""
    for kind in order:
        row = _INTENT_BY_KIND[kind]
        if routing and not row.routing_safe:
            continue
        predicate = row.routing if routing and row.routing is not None else row.detect
        if predicate(lower, cleaned):
            return row
    return None


def _classify(lower: str) -> str:
    """Broad engine classification: walk the intent registry's detect predicates.

    Used by the answer engine to pick which canned response branch runs. Covers
    engine-only intents (``phone_control``/``desktop_control``/``capabilities``)
    too — those rows are routing_safe=False, so scratchable_intent never
    surfaces them.
    """
    cleaned = lower.strip(" .,!?\"'")
    row = _first_match(_ENGINE_ORDER, lower, cleaned, routing=False)
    return row.kind if row else "unknown"


def scratchable_intent(lower: str) -> str | None:
    """Narrow routing classifier: which canned local answer does scratch have?

    Single predicate imported by brain.py's ``decide``. Returns the scratch
    intent name ("greeting" | "math" | "time" | "conversion" | "knowledge" |
    "recommendation") when the request has a local answer, else ``None``.

    A read surface over the registry: walks the routing_safe rows in
    _SCRATCHABLE_KIND_ORDER and runs each row's narrow ``routing`` predicate
    (falling back to ``detect`` for rows with a single width, like greeting or
    conversion). Intentionally narrower than :func:`_classify`: broad "what is
    X" questions only count when they hit an exact knowledge key — otherwise
    they deserve a real web answer. routing_safe=False rows are engine
    intents, not canned local answers, and never surface here.
    """
    cleaned = lower.strip(" .,!?\"'\t\r\n")
    row = _first_match(_SCRATCHABLE_KIND_ORDER, lower, cleaned, routing=True)
    return row.kind if row else None


# ---------------------------------------------------------------------------
# Existing answer builders (unchanged)
# ---------------------------------------------------------------------------

def _topic(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9_+#.-]+", text)
    return " ".join(words[:8]) or "your request"


def _capabilities(modules: tuple[str, ...], context: Mapping[str, Any]) -> dict[str, bool]:
    module_set = set(modules)
    phone_bridge = context.get("phone_bridge", {})
    phone_ready = bool(phone_bridge.get("available")) if isinstance(phone_bridge, Mapping) else False
    return {
        "chat": "agents" in module_set or "memory" in module_set,
        "research": "explore" in module_set,
        "build": bool(context.get("self_improvement_available")),
        "desktop": bool(context.get("desktop_available")),
        "browser": bool(context.get("browser_available")),
        "voice": "voice" in module_set,
        "phone": "phone" in module_set and phone_ready,
    }


def _compose_greeting(capabilities: Mapping[str, bool]) -> str:
    active = ", ".join(name for name, enabled in capabilities.items() if enabled and name != "phone")
    return f"I am NovaControl running on my local scratch brain. Active modules: {active}."


def _capability_sections(capabilities: Mapping[str, bool]) -> list[dict[str, object]]:
    ready = [name.title() for name, enabled in capabilities.items() if enabled]
    blocked = [name.title() for name, enabled in capabilities.items() if not enabled]
    return [
        {"title": "Ready Now", "items": ready or ["Core runtime"]},
        {"title": "Not Ready Yet", "items": blocked or ["No blocked capability reported"]},
    ]


def _desktop_target(lower: str) -> str:
    for prefix in ("open ", "launch ", "run app "):
        if prefix in lower:
            return lower.split(prefix, 1)[1].split(" and ", 1)[0].strip() or "the requested app"
    return "the requested app"


def _next_actions(intent: str) -> list[str]:
    if intent == "desktop_control":
        return ["Open the Build or CLI panel to inspect the planned action.", "Approve only the action you want executed."]
    if intent == "phone_control":
        return ["Add a phone bridge phase.", "Pair the phone explicitly.", "Add approval gates for every sensitive phone action."]
    if intent == "unknown":
        return ["Ask in Explore for factual/current answers.", "Ask in Build for code changes.", "Ask for desktop control using a clear command."]
    return ["Ask a question.", "Use Explore for verified research.", "Use Build for approved code changes."]


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class ScratchReasoningEngine:
    """Small symbolic brain that answers common questions without an external LLM."""

    def answer(self, text: str, context: Mapping[str, Any]) -> dict[str, Any]:
        request = " ".join(text.strip().split())
        lower = request.lower()
        cleaned = lower.strip(" .,!?\"'\t\r\n")
        modules = tuple(str(module) for module in context.get("modules", ()))
        capabilities = _capabilities(modules, context)
        topic = _topic(request)
        # Pick the winning intent exactly like _classify does, then run its row's
        # own answer builder — classification and response live on the same row.
        row = _first_match(_ENGINE_ORDER, lower, cleaned, routing=False)
        intent = row.kind if row else "unknown"

        if row is not None and row.answer is not None:
            built = row.answer(request, context)
            message = built["message"]
            sections = built.get("sections", [])
            confidence = row.confidence
        else:
            # Unknown — fall back to generic with Explore suggestion
            if capabilities.get("research"):
                message = f"I can handle {topic} locally at a planning level, or you can use Explore for a source-checked answer."
            else:
                message = f"I understood the topic as {topic}, but source-checked research is not active in this runtime."
            sections = [
                {
                    "title": "How I Reasoned",
                    "items": [
                        f"Detected topic: {topic}.",
                        "Used local intent rules and runtime capability context.",
                        "For factual or current information, use Explore so I can compare online sources.",
                    ],
                }
            ]
            confidence = "low"

        return {
            "message": message,
            "brain_mode": "scratch",
            "model_configured": False,
            "uses_external_llm": False,
            "intent": intent,
            "topic": topic,
            "sections": sections,
            "next_actions": _next_actions(intent),
            "confidence": confidence,
        }
