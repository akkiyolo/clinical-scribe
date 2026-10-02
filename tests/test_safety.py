"""Unit tests for the deterministic safety checks (no database, no LLM)."""

from __future__ import annotations

import pytest

from app.services.safety import (
    allergy_conflicts,
    amounts_in,
    drug_mentioned,
    durations_in,
    frequency_codes,
    high_flag_ids,
    normalize_text,
    parse_allergies,
    run_safety_checks,
    value_supported,
)

TRANSCRIPT = (
    "Doctor: I'll prescribe Ibuprofen 400mg, take one tablet twice daily after meals for 5 days. "
    "Also Cyclobenzaprine 5mg at bedtime for 7 days. Follow up in two weeks."
)


def med(name="Ibuprofen", **kw):
    base = {
        "drug_name": name,
        "strength": "400 mg",
        "dose": "1 tablet",
        "route": "oral",
        "frequency": "twice daily after meals",
        "duration": "5 days",
    }
    base.update(kw)
    return base


def checks(meds, transcript=TRANSCRIPT, plan="", allergies=None, mentioned=None, **draft):
    body = {"diagnosis": ["x"], "follow_up": "2 weeks", "medications": meds}
    body.update(draft)
    return run_safety_checks(body, transcript, plan, allergies, mentioned)


def kinds(flags):
    return sorted((f["type"], f["severity"], f["field_ref"]) for f in flags)


class TestMedicationInSource:
    def test_clean_prescription_has_no_flags(self):
        assert checks([med()]) == []

    def test_unknown_drug_is_high(self):
        flags = checks([med("Warfarin")])
        assert ("medication_not_in_source", "high", "medications[0].drug_name") in kinds(flags)

    def test_case_and_spelling_variants_are_found(self):
        assert drug_mentioned("IBUPROFEN", normalize_text("take ibuprofen"))
        assert drug_mentioned(
            "Cyclobenzaprine", normalize_text("cyclobenzaprin 5mg")
        )  # one letter off
        assert not drug_mentioned("Amoxicillin", normalize_text("take amoxapine daily"))

    def test_brand_plus_generic_needs_only_one_name_to_match(self):
        assert drug_mentioned("Brufen (Ibuprofen)", normalize_text("ibuprofen twice daily"))

    def test_a_drug_in_the_soap_plan_counts_as_sourced(self):
        assert checks(
            [med("Paracetamol", strength=None, dose=None, frequency=None, duration=None)],
            transcript="Doctor: rest well",
            plan="1. Paracetamol as needed",
        )  # no medication_not_in_source
        flags = checks(
            [med("Paracetamol")], transcript="Doctor: rest well", plan="1. Paracetamol 650 mg"
        )
        assert "medication_not_in_source" not in {f["type"] for f in flags}

    def test_content_is_never_modified(self):
        draft = {"diagnosis": [], "medications": [med("Warfarin")]}
        before = repr(draft)
        run_safety_checks(draft, TRANSCRIPT, "", None)
        assert repr(draft) == before


class TestValuesInSource:
    def test_numbers_must_match_with_their_unit(self):
        flags = checks([med(duration="7 days")])  # transcript says 5 days for ibuprofen
        assert ("dose_not_in_source", "medium", "medications[0].duration") in kinds(flags)

    def test_another_drugs_numbers_do_not_support_this_drug(self):
        # 7 days belongs to Cyclobenzaprine in the transcript, not to Ibuprofen
        assert [
            f
            for f in checks(
                [
                    med(duration="7 days"),
                    med(
                        "Cyclobenzaprine",
                        strength="5 mg",
                        dose=None,
                        frequency="at bedtime",
                        duration="7 days",
                    ),
                ]
            )
            if f["field_ref"] == "medications[0].duration"
        ]
        ok = checks(
            [
                med(),
                med(
                    "Cyclobenzaprine",
                    strength="5 mg",
                    dose=None,
                    frequency="at bedtime",
                    duration="7 days",
                ),
            ]
        )
        assert not [
            f
            for f in ok
            if f["field_ref"].startswith("medications[1]") and f["type"] != "missing_field"
        ]

    def test_wrong_strength_and_wrong_dose(self):
        flags = checks([med(strength="800 mg", dose="2 tablets")])
        refs = {f["field_ref"] for f in flags if f["type"] == "dose_not_in_source"}
        assert refs == {"medications[0].strength", "medications[0].dose"}

    def test_number_words_and_unit_spacing_are_normalised(self):
        assert amounts_in("one tablet and 400mg and 0.5 g") >= {
            (1.0, "tablet"),
            (400.0, "mg"),
            (0.5, "g"),
            (500.0, "mg"),
        }
        assert durations_in("for two weeks") == {14.0} and durations_in("10 days or 1 month") == {
            10.0,
            30.0,
        }

    def test_weeks_and_days_are_equivalent(self):
        assert value_supported("duration", "7 days", "continue for 1 week")
        assert not value_supported("duration", "8 days", "continue for 1 week")

    def test_frequency_synonyms(self):
        assert frequency_codes("BD after food") == {"twice daily", "after meals"}
        assert frequency_codes("three times a day") == {"thrice daily"}
        assert value_supported("frequency", "twice a day", "take it twice daily after meals")
        assert not value_supported("frequency", "three times daily", "take it twice daily")
        assert value_supported("frequency", "at bedtime", "Cyclobenzaprine 5mg every night")

    def test_unrecognised_values_fall_back_to_text_matching(self):
        assert value_supported("dose", "as directed", "take as directed by the doctor")
        assert not value_supported("dose", "as directed", "take it daily")


class TestWindowing:
    SOURCE = (
        "Ibuprofen 400mg, one tablet twice daily after meals for 5 days. "
        "Cyclobenzaprine 5mg at bedtime for 7 days."
    )

    def test_a_previous_drugs_dose_does_not_support_the_next_drug(self):
        cyclo = {
            "drug_name": "Cyclobenzaprine",
            "strength": "5 mg",
            "dose": "1 tablet",
            "route": "oral",
            "frequency": "at bedtime",
            "duration": "7 days",
        }
        flags = checks([med(), cyclo], transcript=self.SOURCE)
        assert [(f["type"], f["field_ref"]) for f in flags] == [
            ("dose_not_in_source", "medications[1].dose")
        ]

    def test_a_dose_written_before_the_drug_name_in_the_same_sentence_counts(self):
        flags = checks(
            [med(frequency="twice daily", duration="5 days")],
            transcript="Take 400mg of ibuprofen, one tablet twice daily for 5 days.",
        )
        assert flags == []

    def test_each_sentence_gets_its_own_values(self):
        flags = checks(
            [med(duration="7 days")], transcript=self.SOURCE
        )  # 7 days belongs to the other drug
        assert ("dose_not_in_source", "medium", "medications[0].duration") in kinds(flags)


class TestAllergies:
    def test_direct_match(self):
        flags = checks([med()], allergies="Ibuprofen")
        assert ("allergy_conflict", "high", "medications[0].drug_name") in kinds(flags)

    @pytest.mark.parametrize(
        "allergy,drug,transcript",
        [
            ("Penicillin", "Amoxicillin", "amoxicillin 500 mg"),
            ("penicillins", "Ampicillin", "ampicillin 500 mg"),
            ("Sulfa drugs", "Sulfamethoxazole", "sulfamethoxazole 400 mg"),
            ("NSAIDs", "Diclofenac", "diclofenac 50 mg"),
            (
                "aspirin",
                "Ibuprofen",
                "ibuprofen 400 mg",
            ),  # cross-sensitivity within the NSAID class
            ("Amoxicillin", "Piperacillin", "piperacillin 4 g"),  # same class, different drug
            ("macrolide antibiotics", "Azithromycin", "azithromycin 500 mg"),
        ],
    )
    def test_class_conflicts(self, allergy, drug, transcript):
        flags = checks([{"drug_name": drug}], transcript=transcript, allergies=allergy)
        assert any(f["type"] == "allergy_conflict" and f["severity"] == "high" for f in flags), (
            allergy,
            drug,
        )

    def test_unrelated_allergy_is_not_flagged(self):
        assert not [
            f
            for f in checks([med()], allergies="Peanuts, latex")
            if f["type"] == "allergy_conflict"
        ]

    @pytest.mark.parametrize(
        "value", [None, "", "None", "none known", "No known allergies", "NKDA", "nil", " , "]
    )
    def test_no_allergy_values_never_flag(self, value):
        assert parse_allergies(value) == []
        assert not [f for f in checks([med()], allergies=value) if f["type"] == "allergy_conflict"]

    def test_trailing_commas_do_not_flag_everything(self):
        assert not [
            f for f in checks([med()], allergies="Peanuts,, ,") if f["type"] == "allergy_conflict"
        ]

    def test_allergies_mentioned_in_the_consult_count_too(self):
        flags = checks([med()], allergies=None, mentioned=["ibuprofen"])
        assert any(f["type"] == "allergy_conflict" for f in flags)

    def test_helper_lists_the_matching_allergy(self):
        assert allergy_conflicts("Amoxicillin", ["peanuts", "penicillin"]) == ["penicillin"]


class TestMissingFieldsAndOthers:
    def test_missing_fields_are_medium_flags_per_field(self):
        flags = checks([{"drug_name": "Ibuprofen"}])
        assert {f["field_ref"] for f in flags if f["type"] == "missing_field"} == {
            "medications[0].dose",
            "medications[0].frequency",
            "medications[0].duration",
            "medications[0].route",
        }
        assert all(f["severity"] == "medium" for f in flags if f["type"] == "missing_field")

    def test_blank_strings_count_as_missing(self):
        flags = checks([med(dose="  ", route="")])
        assert {"medications[0].dose", "medications[0].route"} <= {
            f["field_ref"] for f in flags if f["type"] == "missing_field"
        }

    def test_no_diagnosis_and_no_follow_up_are_low(self):
        flags = run_safety_checks(
            {"diagnosis": [], "follow_up": None, "medications": []}, "", "", None
        )
        assert kinds(flags) == [
            ("no_diagnosis", "low", "diagnosis"),
            ("no_follow_up", "low", "follow_up"),
        ]

    def test_flag_shape_matches_the_spec(self):
        flag = checks([med("Warfarin")])[0]
        assert set(flag) == {"id", "type", "severity", "field_ref", "message"}
        assert flag["type"] in (
            "medication_not_in_source",
            "dose_not_in_source",
            "allergy_conflict",
            "missing_field",
            "no_diagnosis",
            "no_follow_up",
        )

    def test_flag_ids_are_stable_across_reruns_and_unique(self):
        first, second = checks([med("Warfarin")]), checks([med("Warfarin")])
        assert [f["id"] for f in first] == [f["id"] for f in second]
        assert len({f["id"] for f in first}) == len(first)

    def test_only_high_flags_need_acknowledgment(self):
        flags = checks([med("Warfarin")])
        assert (
            high_flag_ids(flags) == {f["id"] for f in flags if f["severity"] == "high"}
            and high_flag_ids([]) == set()
        )
