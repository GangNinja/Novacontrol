"""Local from-scratch reasoning engine for NovaControl chat.

Answers common questions directly — math, time, knowledge, conversions,
recommendations — without requiring an external LLM.
"""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Mapping
from datetime import datetime, timezone
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

_MATH_FUNCS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "floor": math.floor,
    "ceil": math.ceil,
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
# Digits joined by a spelled-out operator ("15 times 3", "2 divided by 4").
_WORDED_MATH = re.compile(
    r'\d+(?:\.\d+)?\s+(?:plus|minus|times|over|multiplied by|divided by)\s+\d+(?:\.\d+)?',
    re.IGNORECASE,
)
# The "add 5 and 7" form.
_ADD_FORM = re.compile(r'\badd\s+\d+(?:\.\d+)?\s+and\s+\d+(?:\.\d+)?', re.IGNORECASE)
# Worded math functions the evaluator can compute ("square root of 256", "5 squared").
_MATH_FUNC_WORD = (
    r'\b(?:the\s+)?square root of\s+\d+(?:\.\d+)?'
    r'|\bsqrt of\s+\d+(?:\.\d+)?'
    r'|\b\d+(?:\.\d+)?\s+(?:squared|cubed)\b'
)
_WORDED_FUNC_MATH = re.compile(_MATH_FUNC_WORD, re.IGNORECASE)
# Bare function calls the evaluator supports ("sqrt(144)", "log(1000)").
_FUNC_CALL_MATH = re.compile(r'\b(?:sqrt|sin|cos|tan|log|log10|log2|abs|floor|ceil|round)\s*\(', re.IGNORECASE)


def is_arithmetic_query(lower: str) -> bool:
    """Symbolic or worded arithmetic the scratch evaluator can compute locally.

    The single routing predicate for "route math to the local brain": bare symbols
    ("2+2"), spelled-out operators ("15 times 3", "add 5 and 7"), worded
    functions ("square root of 256", "5 squared"), and function calls
    ("sqrt(144)") all count.
    """
    return bool(
        _MATH_EXPR.search(lower)
        or _WORDED_MATH.search(lower)
        or _ADD_FORM.search(lower)
        or _WORDED_FUNC_MATH.search(lower)
        or _FUNC_CALL_MATH.search(lower)
    )


_MATH_PATTERNS = re.compile(
    r'^[\d\s+\-*/().,%^]+$|'
    r'(calculate|compute|add|what is|what\'?s)\s+[\d\s+\-*/().%^]+|'
    r'sqrt\s*\(|sin\s*\(|cos\s*\(|tan\s*\(|log\s*\(|'
    r'[\d]+\s*[\+\-\*/\^]\s*[\d]+|'
    r'\d+(?:\.\d+)?\s+(?:plus|minus|times|over|multiplied by|divided by)\s+\d+(?:\.\d+)?|'
    r'add\s+\d+(?:\.\d+)?\s+and\s+\d+(?:\.\d+)?',
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


def _classify(lower: str) -> str:
    """Classify a user message into a scratch brain intent."""
    cleaned = lower.strip(" .,!?\"'")

    # Greetings
    if cleaned in _GREETINGS:
        return "greeting"

    # Phone control
    if any(w in lower for w in ("phone", "android", "mobile", "whatsapp", "call ", "sms")):
        if any(w in lower for w in ("open ", "launch ", "control", "send", "call ", "text ", "message")):
            return "phone_control"

    # Desktop control
    if any(phrase in lower for phrase in ("open ", "launch ", "close app", "control desktop", "run app")):
        return "desktop_control"

    # Capabilities
    if any(phrase in lower for phrase in ("what can you do", "your capabilities", "who are you", "what are you")):
        return "capabilities"

    # Time / date
    if _TIME_PATTERNS.search(lower):
        return "time"

    # Temperature conversion (check before general unit conversion)
    if _TEMP_PATTERN.search(lower):
        return "conversion"

    # Unit conversion: "100 km in miles", "5 kg to lbs"
    if _UNIT_PATTERN.search(lower):
        return "conversion"

    # Currency conversion: "100 usd in eur", "50 eur to gbp"
    if re.search(r'[\d.]+\s*(?:usd|eur|gbp|inr|jpy|cad|aud|cny|krw)\s+(?:in|to|=)\s*(?:usd|eur|gbp|inr|jpy|cad|aud|cny|krw)', lower):
        return "conversion"

    # Time duration conversion: "2 hours in minutes"
    if re.search(r'[\d.]+\s*(?:second|minute|hour|day|week|sec|min|hr|h|d|w)s?\s+(?:in|to|=)\s*(?:second|minute|hour|day|week|sec|min|hr|h|d|w)s?', lower):
        return "conversion"

    # Math
    if is_arithmetic_query(lower) or _MATH_PATTERNS.search(lower):
        # Exclude things that look like questions about facts, not math
        if not any(lower.startswith(q) for q in ("what is", "who", "how many")):
            return "math"
        # "what is 2+2" / "what is 15 times 3" should still be math
        if is_arithmetic_query(lower):
            return "math"
        if "sqrt" in lower or "sin " in lower or "cos " in lower or "log " in lower:
            return "math"

    # Knowledge lookup
    if _is_knowledge_query(cleaned):
        return "knowledge"

    # Recommendations
    if _is_recommendation_query(lower):
        return "recommendation"

    return "unknown"


def _is_knowledge_query(lower: str) -> bool:
    """Check if this looks like a factual knowledge question."""
    prefixes = ("what is ", "what are ", "who is ", "who was ", "who invented",
                "how many ", "how far ", "how old ", "how tall ", "how deep",
                "how fast ", "how hot ", "how cold ")
    if any(lower.startswith(p) for p in prefixes):
        # Check if it's a known fact
        for key in _KNOWLEDGE:
            if key in lower or lower.startswith(key):
                return True
        # General knowledge pattern — not a math expression
        if not re.search(r'[\d]\s*[\+\-\*/\^]', lower):
            return True
    # General factual patterns without question words
    factual_patterns = ("capital of ", "invented ", "discovered ", "president of",
                        "population of", "area of", "currency of")
    if any(lower.startswith(p) for p in factual_patterns):
        return True
    return False


def _is_recommendation_query(lower: str) -> bool:
    """Check if this looks like a recommendation request."""
    patterns = (
        "recommend", "suggest", "what should i", "what can i",
        "give me a", "give me some", "i want to", "i need a",
        "movie", "book", "food", "eat", "watch", "read",
    )
    return any(p in lower for p in patterns)


_GREETINGS = frozenset({
    "hi", "hello", "hey", "hai",
    "good morning", "good afternoon", "good evening",
})


def scratchable_intent(lower: str) -> str | None:
    """Narrow routing classifier: which canned local answer does scratch have?

    Single predicate imported by brain.py's ``decide``. Returns the scratch
    intent name ("greeting" | "math" | "time" | "conversion" | "knowledge" |
    "recommendation") when the request has a local answer, else ``None``.

    Intentionally narrower than :func:`_classify`: broad "what is X" questions
    only count when they hit an exact knowledge key — otherwise they deserve a
    real web answer. Do not broaden this with ``_classify``'s catch-all
    branches (``desktop_control`` etc.): those are engine intents, not canned
    local answers, and letting them through would swallow Explore research.
    """
    cleaned = lower.strip(" .,!?\"'\t\r\n")
    if cleaned in _GREETINGS:
        return "greeting"
    # Math expressions — symbolic, spelled-out operators, or worded functions
    if is_arithmetic_query(lower):
        return "math"
    if any(lower.startswith(p) for p in ("calculate ", "compute ", "sqrt", "sin ", "cos ", "log")):
        return "math"
    # Time / date
    if re.search(r'what time|current time|what day|what date|today|time now|date now', lower):
        return "time"
    # Unit conversion
    if re.search(r'[\d.]+\s*[a-zA-Z/°]+\s+(?:in|to|into|=)\s+[a-zA-Z/°]+', lower):
        return "conversion"
    if re.search(r'[\d.]+\s*(?:usd|eur|gbp|inr|jpy)\s+(?:in|to)', lower):
        return "conversion"
    if re.search(r'[\d.]+\s*(?:second|minute|hour|day|week)s?\s+(?:in|to|=)', lower):
        return "conversion"
    # Exact knowledge key match
    for key in _KNOWLEDGE:
        if key in lower or lower.startswith(key):
            return "knowledge"
    # Recommendations
    if any(w in lower for w in ("recommend", "suggest", "what should i", "what can i eat")):
        return "recommendation"
    # Specific movie/book/food requests
    if any(lower.startswith(p) for p in ("movie", "book", "food", "i want to watch", "i want to read")):
        return "recommendation"
    return None


# ---------------------------------------------------------------------------
# Answer builders
# ---------------------------------------------------------------------------

def _math_answer(text: str) -> dict[str, Any]:
    """Evaluate a math expression and explain the result."""
    expr = text.lower()
    add_form = expr.startswith("add ")
    # Keep only the two operands for "add X and Y ..." so trailing phrasing
    # ("... and show steps") never reaches the evaluator.
    if add_form:
        add_match = re.match(r'add\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)', expr)
        if add_match:
            expr = f"{add_match.group(1)} and {add_match.group(2)}"
    for prefix in ("calculate ", "compute ", "what is ", "what's ", "solve ", "add "):
        if expr.startswith(prefix):
            expr = expr[len(prefix):]
    # What the user typed (minus leading question words) — shown back in the answer.
    display = re.sub(r'\bwhat is\b', '', expr).strip().rstrip('?.!,;:')
    # Translate spelled-out operators into symbols so the evaluator can read them.
    expr = re.sub(r'\bmultiplied by\b', '*', expr)
    expr = re.sub(r'\bdivided by\b', '/', expr)
    expr = re.sub(r'\btimes\b', '*', expr)
    expr = re.sub(r'\bover\b', '/', expr)
    expr = re.sub(r'\bplus\b', '+', expr)
    expr = re.sub(r'\bminus\b', '-', expr)
    # Worded functions the evaluator already supports (sqrt via _MATH_FUNCS).
    expr = re.sub(r'\b(?:the\s+)?square root of\s+(\d+(?:\.\d+)?)', r'sqrt(\1)', expr)
    expr = re.sub(r'\bsqrt of\s+(\d+(?:\.\d+)?)', r'sqrt(\1)', expr)
    expr = re.sub(r'\b(\d+(?:\.\d+)?)\s+squared\b', r'\g<1>**2', expr)
    expr = re.sub(r'\b(\d+(?:\.\d+)?)\s+cubed\b', r'\g<1>**3', expr)
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
        modules = tuple(str(module) for module in context.get("modules", ()))
        capabilities = _capabilities(modules, context)
        intent = _classify(lower)
        topic = _topic(request)

        if intent == "greeting":
            message = _compose_greeting(capabilities)
            sections = _capability_sections(capabilities)

        elif intent == "phone_control":
            message = "Phone control is not active yet. NovaControl needs a paired phone bridge before it can safely operate your device."
            sections = [
                {
                    "title": "Required Before Phone Control",
                    "items": [
                        "Pair the phone through an explicit bridge such as Android Debug Bridge or a dedicated companion app.",
                        "Show every requested action before execution.",
                        "Require approval for sensitive actions like messages, calls, payments, files, and settings.",
                    ],
                }
            ]

        elif intent == "desktop_control":
            target = _desktop_target(lower)
            message = f"I can prepare a desktop action for {target}, but execution must be approval-gated."
            sections = [
                {
                    "title": "Desktop Plan",
                    "items": [
                        f"Create an action to open or control {target}.",
                        "Show the exact action in the UI.",
                        "Run it only after approval.",
                    ],
                }
            ]

        elif intent == "capabilities":
            message = "NovaControl is running as a local scratch-brain control app with chat, research, build, learning, voice, desktop, and browser modules."
            sections = _capability_sections(capabilities)

        elif intent == "math":
            result = _math_answer(request)
            message = result["message"]
            sections = result.get("sections", [])

        elif intent == "time":
            result = _time_answer(request)
            message = result["message"]
            sections = result.get("sections", [])

        elif intent == "conversion":
            result = _conversion_answer(request)
            message = result["message"]
            sections = result.get("sections", [])

        elif intent == "knowledge":
            result = _knowledge_answer(request)
            message = result["message"]
            sections = result.get("sections", [])

        elif intent == "recommendation":
            result = _recommendation_answer(request)
            message = result["message"]
            sections = result.get("sections", [])

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

        # Compute confidence
        confidence = "high" if intent in ("greeting", "math", "time", "conversion") else \
                     "medium" if intent != "unknown" else "low"

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
