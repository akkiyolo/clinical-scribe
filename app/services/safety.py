"""Deterministic safety checks for AI-drafted prescriptions.

This is plain code, not an LLM: it compares the drafted prescription with the source (the
consult transcript and the approved SOAP plan) and the patient's allergies, and returns flags.
Flags are evidence for the doctor. They never change, remove or add prescription content.

Matching is intentionally conservative. A value is "in the source" only when it can be found
near the drug's mention; numbers must match together with their unit ("5 days" is not satisfied
by "7 days" appearing elsewhere), and an unrecognised value is flagged rather than trusted.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from rapidfuzz import fuzz

HIGH, MEDIUM, LOW = "high", "medium", "low"

FLAG_TYPES = (
    "medication_not_in_source",
    "dose_not_in_source",
    "allergy_conflict",
    "missing_field",
    "no_diagnosis",
    "no_follow_up",
)

# Drug classes used for allergy cross-checks. Deliberately small and conservative: a real
# deployment needs a clinically validated drug database (listed under "Production path").
ALLERGY_DRUG_MAP: dict[str, tuple[str, ...]] = {
    "penicillin": (
        "amoxicillin",
        "ampicillin",
        "penicillin",
        "piperacillin",
        "nafcillin",
        "cloxacillin",
        "flucloxacillin",
        "dicloxacillin",
        "augmentin",
        "amoxiclav",
    ),
    "sulfa": ("sulfamethoxazole", "sulfasalazine", "sulfadiazine", "cotrimoxazole", "bactrim"),
    "nsaid": (
        "ibuprofen",
        "naproxen",
        "diclofenac",
        "indomethacin",
        "celecoxib",
        "aspirin",
        "ketorolac",
        "mefenamic",
        "piroxicam",
        "etoricoxib",
        "nimesulide",
    ),
    "aspirin": ("aspirin", "acetylsalicylic"),
    "codeine": ("codeine", "dihydrocodeine"),
    "opioid": ("morphine", "codeine", "tramadol", "oxycodone", "fentanyl", "hydrocodone"),
    "cephalosporin": (
        "cephalexin",
        "cefazolin",
        "ceftriaxone",
        "cefuroxime",
        "cefixime",
        "cefpodoxime",
    ),
    "tetracycline": ("tetracycline", "doxycycline", "minocycline"),
    "macrolide": ("azithromycin", "erythromycin", "clarithromycin"),
    "fluoroquinolone": (
        "ciprofloxacin",
        "levofloxacin",
        "ofloxacin",
        "moxifloxacin",
        "norfloxacin",
    ),
}
# Aliases people write for a class in an allergy list.
ALLERGY_CLASS_ALIASES = {
    "nsaids": "nsaid",
    "penicillins": "penicillin",
    "sulpha": "sulfa",
    "sulfonamide": "sulfa",
    "sulfonamides": "sulfa",
    "cephalosporins": "cephalosporin",
    "macrolides": "macrolide",
    "opioids": "opioid",
    "quinolone": "fluoroquinolone",
    "quinolones": "fluoroquinolone",
}

NO_ALLERGY_VALUES = {
    "",
    "none",
    "nil",
    "no",
    "n/a",
    "na",
    "nka",
    "nkda",
    "not documented",
    "none known",
    "no known allergies",
    "no known drug allergies",
    "no known allergy",
    "no allergies",
}

NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "fourteen": "14",
    "fifteen": "15",
    "twenty": "20",
    "thirty": "30",
    "half": "0.5",
}

_GENERIC_DRUG_WORDS = {
    "tablet",
    "tablets",
    "capsule",
    "capsules",
    "syrup",
    "cream",
    "ointment",
    "gel",
    "drops",
    "injection",
    "suspension",
    "solution",
    "spray",
    "inhaler",
    "oral",
    "topical",
    "sustained",
    "release",
    "extended",
    "forte",
    "plus",
    "and",
    "with",
}

FREQUENCY_PATTERNS: dict[str, re.Pattern[str]] = {
    "once daily": re.compile(
        r"\b(once (a|per|every|each) day|once daily|once a day|1 time (a|per) day|1x ?(a )?day|"
        r"every day|daily|od|qd|in the morning|every morning|each morning)\b"
    ),
    "twice daily": re.compile(
        r"\b(twice (a|per|every|each) day|twice daily|2 times (a|per) day|2 times daily|2x ?(a )?day|"
        r"bd|bid|b\.i\.d|every 12 hours|12 hourly)\b"
    ),
    "thrice daily": re.compile(
        r"\b(three times (a|per|every|each) day|three times daily|3 times (a|per) day|3 times daily|"
        r"thrice|tds|tid|t\.i\.d|every 8 hours|8 hourly)\b"
    ),
    "four times daily": re.compile(
        r"\b(four times (a|per|every|each) day|four times daily|4 times (a|per) day|4 times daily|"
        r"qid|qds|every 6 hours|6 hourly)\b"
    ),
    "at bedtime": re.compile(
        r"\b(at bedtime|bedtime|at night|nightly|every night|before bed|hs|qhs)\b"
    ),
    "as needed": re.compile(r"\b(as needed|when needed|if needed|when required|prn|sos)\b"),
    "after meals": re.compile(
        r"\b(after (meals?|food|eating)|post prandial|with meals?|with food)\b"
    ),
    "before meals": re.compile(r"\b(before (meals?|food)|empty stomach|ac)\b"),
}

_AMOUNT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|ug|µg|g|gm|ml|iu|units?|tablets?|tabs?|capsules?|caps?|drops?|puffs?|"
    r"sachets?|teaspoons?|tsp|tbsp|sprays?|patch(?:es)?)\b"
)
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|weeks?|months?|wks?)\b")
_UNIT_CANON = {
    "tab": "tablet",
    "tabs": "tablet",
    "tablets": "tablet",
    "cap": "capsule",
    "caps": "capsule",
    "capsules": "capsule",
    "gm": "g",
    "ug": "mcg",
    "µg": "mcg",
    "units": "unit",
    "drops": "drop",
    "puffs": "puff",
    "sachets": "sachet",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "sprays": "spray",
    "patches": "patch",
}
_DAYS_PER = {"day": 1, "week": 7, "wk": 7, "month": 30}


def normalize_text(text: str | None) -> str:
    """Lower-case, spell out nothing: number words become digits, spacing is collapsed."""
    out = (text or "").lower().replace("µg", "mcg")
    out = re.sub(r"[^\w\s./µ%-]", " ", out)
    out = re.sub(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", lambda m: NUMBER_WORDS[m.group(1)], out)
    out = re.sub(r"(\d)([a-z])", r"\1 \2", out)  # 400mg -> 400 mg
    return " ".join(out.split())


def _canon_unit(unit: str) -> str:
    return _UNIT_CANON.get(unit, unit)


def amounts_in(text: str) -> set[tuple[float, str]]:
    """(number, unit) pairs such as (400, 'mg') or (1, 'tablet'); 1 g is also recorded as 1000 mg."""
    found: set[tuple[float, str]] = set()
    for number, unit in _AMOUNT.findall(normalize_text(text)):
        value, unit = float(number), _canon_unit(unit)
        found.add((value, unit))
        if unit == "g":
            found.add((value * 1000, "mg"))
        if unit == "mg" and value >= 1000 and value % 1000 == 0:
            found.add((value / 1000, "g"))
    return found


def durations_in(text: str) -> set[float]:
    """Durations converted to days (1 week == 7 days; 1 month is treated as 30 days)."""
    days: set[float] = set()
    for number, unit in _DURATION.findall(normalize_text(text)):
        base = unit.rstrip("s")
        days.add(float(number) * _DAYS_PER.get(base, 1))
    return days


def frequency_codes(text: str) -> set[str]:
    normalized = normalize_text(text)
    return {code for code, pattern in FREQUENCY_PATTERNS.items() if pattern.search(normalized)}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]{3,}", text)


def significant_drug_tokens(drug_name: str) -> list[str]:
    return [
        t
        for t in re.findall(r"[a-z]{4,}", (drug_name or "").lower())
        if t not in _GENERIC_DRUG_WORDS
    ]


def _token_in(token: str, source_words: list[str]) -> bool:
    """Exact word match, or a near spelling (cyclobenzaprin / cyclobenzaprine)."""
    for word in source_words:
        if word == token:
            return True
        if len(token) >= 6 and abs(len(word) - len(token)) <= 2 and fuzz.ratio(token, word) >= 88:
            return True
    return False


def drug_mentioned(drug_name: str, source: str) -> bool:
    tokens = significant_drug_tokens(drug_name)
    if not tokens:
        return (drug_name or "").strip().lower() in source
    words = _words(source)
    return any(_token_in(token, words) for token in tokens)


def mention_spans(drug_name: str, source: str) -> list[tuple[int, int]]:
    """(start, end) character spans where the drug is mentioned in the source text."""
    tokens = significant_drug_tokens(drug_name)
    return [
        (m.start(), m.end())
        for m in re.finditer(r"[a-z]{3,}", source)
        if any(_token_in(t, [m.group(0)]) for t in tokens)
    ]


# Word endings that identify a drug name even when it is not in the draft (e.g. a drug the
# doctor discussed but the model left out). Used only to stop one drug's window at the next drug.
_DRUG_SUFFIXES = (
    "cillin",
    "mycin",
    "micin",
    "oxacin",
    "azole",
    "profen",
    "fenac",
    "coxib",
    "prazole",
    "tidine",
    "statin",
    "sartan",
    "pril",
    "olol",
    "dipine",
    "formin",
    "gliptin",
    "zepam",
    "zolam",
    "triptan",
    "setron",
    "terol",
    "thiazide",
    "semide",
    "parin",
    "navir",
    "prine",
    "cycline",
    "floxacin",
)
_KNOWN_DRUG_WORDS = {w for members in ALLERGY_DRUG_MAP.values() for w in members} | {
    "paracetamol",
    "acetaminophen",
    "metformin",
    "insulin",
    "prednisolone",
    "prednisone",
    "cetirizine",
    "loratadine",
    "salbutamol",
    "warfarin",
    "heparin",
    "aspirin",
    "codeine",
}


def other_drug_spans(
    source: str, own_tokens: list[str], extra_names: list[str] | None = None
) -> list[tuple[int, int]]:
    """Spans of drug-looking words in the source that are NOT the drug being checked."""
    extra_tokens = {t for name in extra_names or [] for t in significant_drug_tokens(name)}
    spans = []
    for match in re.finditer(r"[a-z]{4,}", source):
        word = match.group(0)
        if any(_token_in(t, [word]) for t in own_tokens):
            continue
        drug_like = (
            word in _KNOWN_DRUG_WORDS
            or word in extra_tokens
            or (len(word) >= 7 and word.endswith(_DRUG_SUFFIXES))
        )
        if drug_like:
            spans.append((match.start(), match.end()))
    return spans


_SENTENCE_BREAK = re.compile(r"[.;:?!]\s")


def mention_window(own: list[tuple[int, int]], others: list[tuple[int, int]], source: str) -> str:
    """Source text around each mention of a drug: from the start of its sentence (so "400 mg
    ibuprofen" counts) to just before the next different drug. Doses are matched against this
    window, so one drug's "7 days" or "one tablet" never supports another drug's value."""
    if not own:
        return source
    pieces = []
    for start, end in own:
        breaks = [m.end() for m in _SENTENCE_BREAK.finditer(source, 0, start)]
        sentence_start = breaks[-1] if breaks else 0
        stop = min([s for s, _ in others if s > end] + [end + 300])
        pieces.append(source[max(0, start - 60, sentence_start) : stop])
    return " ".join(pieces)


def value_supported(field: str, value: str, window: str) -> bool:
    """Is `value` for `field` (strength/dose/frequency/duration) supported by the window text?"""
    norm_value = normalize_text(value)
    if not norm_value:
        return True
    norm_window = normalize_text(window)

    if field in ("strength", "dose"):
        wanted = amounts_in(value)
        if wanted:
            have = amounts_in(window)
            return all(item in have for item in wanted)
    elif field == "duration":
        wanted_days = durations_in(value)
        if wanted_days:
            return wanted_days <= durations_in(window)
    elif field == "frequency":
        wanted_codes = frequency_codes(value)
        if wanted_codes:
            return wanted_codes <= frequency_codes(window)
    return norm_value in norm_window


def _flag(flag_type: str, severity: str, field_ref: str, message: str) -> dict[str, Any]:
    digest = hashlib.sha1(f"{flag_type}|{field_ref}|{message}".encode()).hexdigest()[:10]
    return {
        "id": f"flag-{digest}",
        "type": flag_type,
        "severity": severity,
        "field_ref": field_ref,
        "message": message,
    }


def parse_allergies(*sources: str | list[str] | None) -> list[str]:
    """Split free-text allergy lists into lower-cased items, dropping 'none known' style entries."""
    items: list[str] = []
    for source in sources:
        chunks = [source] if isinstance(source, str) else list(source or [])
        for chunk in chunks:
            for part in re.split(r"[,;\n/]|\band\b", (chunk or "").lower()):
                cleaned = " ".join(part.split()).strip(" .")
                if cleaned and cleaned not in NO_ALLERGY_VALUES:
                    items.append(cleaned)
    return items


def drug_classes(text: str) -> set[str]:
    """Classes a drug (or allergy) text belongs to, e.g. 'ibuprofen' -> {'nsaid'}."""
    lowered = (text or "").lower()
    words = set(re.findall(r"[a-z]{3,}", lowered))
    classes: set[str] = set()
    for key, members in ALLERGY_DRUG_MAP.items():
        if key in lowered or any(m in words or m in lowered for m in members):
            classes.add(key)
    for alias, key in ALLERGY_CLASS_ALIASES.items():
        if alias in words:
            classes.add(key)
    return classes


def allergy_conflicts(drug_name: str, allergies: list[str]) -> list[str]:
    """Allergies the drug conflicts with: same drug, same word, or the same drug class."""
    drug = (drug_name or "").lower()
    drug_tokens = set(significant_drug_tokens(drug)) | set(re.findall(r"[a-z]{4,}", drug))
    drug_cls = drug_classes(drug)
    hits: list[str] = []
    for allergy in allergies:
        allergy_tokens = set(re.findall(r"[a-z]{4,}", allergy))
        direct = allergy in drug or drug.strip() in allergy or bool(allergy_tokens & drug_tokens)
        same_class = bool(drug_cls & drug_classes(allergy))
        if direct or same_class:
            hits.append(allergy)
    return hits


def run_safety_checks(
    draft: dict[str, Any],
    transcript: str | None,
    soap_plan: str | None,
    patient_allergies: str | None,
    mentioned_allergies: list[str] | None = None,
    mentioned_drugs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the safety flags for a drafted prescription. Pure and deterministic.

    `mentioned_drugs` are drug names the entity extraction found in the consult; they only help
    keep one drug's dose window from running into the next drug's instructions.
    """
    source = normalize_text(f"{transcript or ''}\n{soap_plan or ''}")
    allergies = parse_allergies(patient_allergies, mentioned_allergies)
    flags: list[dict[str, Any]] = []

    medications = draft.get("medications") or []
    spans = [
        mention_spans((m.get("drug_name") or ""), source) if m.get("drug_name") else []
        for m in medications
    ]

    for index, med in enumerate(medications):
        ref = f"medications[{index}]"
        name = (med.get("drug_name") or "").strip()
        label = name or f"medication {index + 1}"

        if name and not drug_mentioned(name, source):
            flags.append(
                _flag(
                    "medication_not_in_source",
                    HIGH,
                    f"{ref}.drug_name",
                    f"'{name}' was not found in the transcript or SOAP plan. "
                    "Confirm you actually decided to prescribe it.",
                )
            )

        others = [span for i, group in enumerate(spans) if i != index for span in group]
        if name:
            others += other_drug_spans(source, significant_drug_tokens(name), mentioned_drugs)
        window = mention_window(spans[index], others, source) if name else source
        for field in ("strength", "dose", "frequency", "duration"):
            value = (med.get(field) or "").strip()
            if value and not value_supported(field, value, window):
                flags.append(
                    _flag(
                        "dose_not_in_source",
                        MEDIUM,
                        f"{ref}.{field}",
                        f"{field.capitalize()} '{value}' for {label} was not found in the "
                        "source near this drug.",
                    )
                )

        for allergy in allergy_conflicts(name, allergies) if name else []:
            flags.append(
                _flag(
                    "allergy_conflict",
                    HIGH,
                    f"{ref}.drug_name",
                    f"'{name}' may conflict with the patient's allergy to '{allergy}'.",
                )
            )

        for field in ("drug_name", "dose", "frequency", "duration", "route"):
            if not (med.get(field) or "").strip():
                flags.append(
                    _flag(
                        "missing_field",
                        MEDIUM,
                        f"{ref}.{field}",
                        f"Missing {field.replace('_', ' ')} for {label}.",
                    )
                )

    if not draft.get("diagnosis"):
        flags.append(
            _flag("no_diagnosis", LOW, "diagnosis", "No diagnosis is included in the prescription.")
        )
    if not (draft.get("follow_up") or "").strip():
        flags.append(_flag("no_follow_up", LOW, "follow_up", "No follow-up is specified."))

    # Stable, de-duplicated ids so an acknowledged flag stays acknowledged across re-checks.
    seen: set[str] = set()
    unique = []
    for flag in flags:
        if flag["id"] not in seen:
            seen.add(flag["id"])
            unique.append(flag)
    return unique


def high_flag_ids(flags: list[dict[str, Any]] | None) -> set[str]:
    return {f["id"] for f in flags or [] if f.get("severity") == HIGH}
