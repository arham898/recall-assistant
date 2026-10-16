"""Tests for the golden evaluation set.

Each test uses the eval_seed_data fixture which inserts into recalls,
recall_vehicles and recall_components, then rolls back on teardown.
"""

from __future__ import annotations

import random

import pytest
from scripts.make_generated_questions import (
    MIN_GOLD_FOR_TEMPLATE,
    _build_question,
    _fetch_all_recalls_candidates,
    _fetch_group_candidates,
    allocate_quotas,
    display_name,
    select_candidates,
)
from scripts.validate_golden import (
    check_db_not_empty,
    check_generated_file,
    check_gold_ids_exist,
    check_gold_vehicle_link,
    check_no_shared_gold,
    check_unanswerable,
    check_unique_ids,
    check_unique_questions,
)

from recall_assistant.evals.golden import GoldenRow
from recall_assistant.evals.leakage import check_leakage
from recall_assistant.evals.mappings import COMPONENT_GROUPS

# Mark all tests as integration (need Postgres)
pytestmark = pytest.mark.integration


# ===================================================================
# 1. Brakes correctness
# ===================================================================


class TestBrakesCorrectness:
    def test_brakes_ford_ranger_2021(self, db_conn, eval_seed_data):
        """brakes for FORD RANGER 2021 == {24V123000, 24V124000}."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "brakes")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)
        ranger = [
            c
            for c in cands
            if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
        ]
        assert len(ranger) == 1
        assert set(ranger[0].gold_ids) == {"24V123000", "24V124000"}


# ===================================================================
# 2. all_recalls for shared recall
# ===================================================================


class TestAllRecallsShared:
    def test_lincoln_corsair_2021(self, db_conn, eval_seed_data):
        """all_recalls for LINCOLN CORSAIR 2021 == {24V125000}."""
        cands = _fetch_all_recalls_candidates(db_conn)
        corsair = [
            c
            for c in cands
            if c.make == "LINCOLN" and c.model == "CORSAIR" and c.year == 2021
        ]
        assert len(corsair) == 1
        assert set(corsair[0].gold_ids) == {"24V125000"}


# ===================================================================
# 3. Multi-group recall
# ===================================================================


class TestMultiGroup:
    def test_recall_in_two_groups(self, db_conn, eval_seed_data):
        """24V126000 is gold for both airbags and steering."""
        airbags_grp = next(g for g in COMPONENT_GROUPS if g.key == "airbags")
        steering_grp = next(g for g in COMPONENT_GROUPS if g.key == "steering")

        airbag_cands = _fetch_group_candidates(
            db_conn, airbags_grp.key, airbags_grp.categories
        )
        steering_cands = _fetch_group_candidates(
            db_conn, steering_grp.key, steering_grp.categories
        )

        ranger_airbag = [
            c
            for c in airbag_cands
            if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
        ]
        ranger_steering = [
            c
            for c in steering_cands
            if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
        ]

        assert any("24V126000" in c.gold_ids for c in ranger_airbag)
        assert any("24V126000" in c.gold_ids for c in ranger_steering)


# ===================================================================
# 4. EQUIPMENT-only recall
# ===================================================================


class TestEquipmentOnly:
    def test_equipment_in_all_recalls_not_in_groups(self, db_conn, eval_seed_data):
        """24V127000 is in all_recalls for FORD RANGER 2021 but no group."""
        all_cands = _fetch_all_recalls_candidates(db_conn)
        ranger = [
            c
            for c in all_cands
            if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
        ]
        assert len(ranger) == 1
        assert "24V127000" in ranger[0].gold_ids

        # Check it's not in any component group
        for grp in COMPONENT_GROUPS:
            grp_cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)
            ranger_grp = [
                c
                for c in grp_cands
                if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
            ]
            for c in ranger_grp:
                assert "24V127000" not in c.gold_ids, (
                    f"24V127000 found in group {grp.key}"
                )


# ===================================================================
# 5. 10-gold limit
# ===================================================================


class TestTenGoldLimit:
    def test_eleven_on_one_combo_excluded(self, db_conn, eval_seed_data):
        """11 campaigns on ACME WIDGET 2021 -> excluded by 10-gold limit.

        Ask for more vehicles than eligible (without the >10 combo),
        so removing the limit would include the excluded vehicle.
        """
        grp = next(g for g in COMPONENT_GROUPS if g.key == "brakes")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)

        acme = [c for c in cands if c.make == "ACME" and c.model == "WIDGET"]
        assert len(acme) == 1
        assert len(acme[0].gold_ids) == 11

        # Ask for many — ACME should not appear
        rng = random.Random(42)
        selected = select_candidates(cands, 100, rng)
        selected_vehicles = {(c.make, c.model, c.year) for c in selected}
        assert ("ACME", "WIDGET", 2021) not in selected_vehicles


# ===================================================================
# 6. Year exclusions
# ===================================================================


class TestYearExclusions:
    def test_null_9999_2017_never_sampled(self, db_conn, eval_seed_data):
        """Candidate SQL excludes NULL, 9999 and < 2018 years.

        Positive control: FORD RANGER 2021 must be present.
        Negative: NULL, 9999 and 2017 rows must be absent.
        """
        grp = next(g for g in COMPONENT_GROUPS if g.key == "brakes")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)
        keys = {(c.make, c.model, c.year) for c in cands}

        assert ("FORD", "RANGER", 2021) in keys
        assert ("FORD", "RANGER", None) not in keys
        assert ("FORD", "RANGER", 9999) not in keys
        assert ("FORD", "RANGER", 2017) not in keys


# ===================================================================
# 7. Per-make cap
# ===================================================================


class TestPerMakeCap:
    def test_cap_at_5(self, db_conn, eval_seed_data):
        """TESTMAKE1 has 6 eligible vehicles, cap at 5.

        Ask for 7 tires vehicles; with cap on, at most 5 TESTMAKE1.
        """
        grp = next(g for g in COMPONENT_GROUPS if g.key == "tires")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)

        rng = random.Random(42)
        selected = select_candidates(cands, 7, rng, max_per_make=5)

        testmake1_selected = [c for c in selected if c.make == "TESTMAKE1"]
        assert len(testmake1_selected) <= 5


# ===================================================================
# 8. EXCLUDED_MAKES
# ===================================================================


class TestExcludedMakes:
    def test_jaguar_land_rover_never_sampled(self, db_conn, eval_seed_data):
        """JAGUAR and LAND ROVER vehicles never appear in candidates.

        The SQL WHERE excludes them. Ask for more than eligible so
        removing the rule would include them.
        """
        grp = next(g for g in COMPONENT_GROUPS if g.key == "tires")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)

        makes = {c.make for c in cands}
        assert "JAGUAR" not in makes
        assert "LAND ROVER" not in makes


# ===================================================================
# 9. Plural logic
# ===================================================================


class TestPluralLogic:
    def test_remedy_singular_plural(self, db_conn, eval_seed_data):
        """Remedy says 'recall' for 1 gold, 'recalls' for 2."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "steering")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)

        # LINCOLN CORSAIR 2021 has 1 steering recall
        corsair = [
            c
            for c in cands
            if c.make == "LINCOLN" and c.model == "CORSAIR" and c.year == 2021
        ]
        assert len(corsair) == 1
        assert len(corsair[0].gold_ids) == 1
        row1 = _build_question("remedy", corsair[0], "steering", db_conn)
        assert " recall " in row1.question
        assert " recalls " not in row1.question

        # FORD RANGER 2021 has 2 steering recalls (24V125000, 24V126000)
        ranger = [
            c
            for c in cands
            if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
        ]
        assert len(ranger) == 1
        assert len(ranger[0].gold_ids) == 2
        row2 = _build_question("remedy", ranger[0], "steering", db_conn)
        assert " recalls " in row2.question


# ===================================================================
# 10. Count reference_answer
# ===================================================================


class TestCountReferenceAnswer:
    def test_count_brakes_ford_ranger(self, db_conn, eval_seed_data):
        """Count row for brakes on FORD RANGER 2021 has ref_answer '2'."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "brakes")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)
        ranger = [
            c
            for c in cands
            if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
        ]
        assert len(ranger) == 1
        row = _build_question("count", ranger[0], "brake", db_conn)
        assert row.reference_answer == "2"


# ===================================================================
# 11. Remedy reference_facts
# ===================================================================


class TestRemedyReferenceFacts:
    def test_remedy_brakes_ford_ranger(self, db_conn, eval_seed_data):
        """Remedy for brakes on FORD RANGER 2021 lists both corrective actions."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "brakes")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)
        ranger = [
            c
            for c in cands
            if c.make == "FORD" and c.model == "RANGER" and c.year == 2021
        ]
        row = _build_question("remedy", ranger[0], "brake", db_conn)
        assert len(row.reference_facts) == 2

        fact_ids = {f.campaign_id for f in row.reference_facts}
        assert fact_ids == {"24V123000", "24V124000"}

        for fact in row.reference_facts:
            assert fact.field == "corrective_action"
            assert len(fact.text) > 0


# ===================================================================
# 12. Display names
# ===================================================================


class TestDisplayNames:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            # Original cases (unchanged expectations)
            ("FORD", "Ford"),
            ("F-150", "F-150"),
            ("MERCEDES-BENZ", "Mercedes-Benz"),
            ("RAV4", "RAV4"),
            ("BMW", "BMW"),
            ("4RUNNER", "4Runner"),
            # Vowel rule: 3-letter parts with vowels title-case
            ("BUS", "Bus"),
            ("PRO", "Pro"),
            ("AIR", "Air"),
            ("NEW FLYER", "New Flyer"),
            ("BAY STAR", "Bay Star"),
            ("PLEASURE WAY", "Pleasure Way"),
            ("IC BUS", "IC Bus"),
            ("FOX FACTORY", "Fox Factory"),
            # Vowel rule: vowelless parts stay upper
            ("FCCC", "FCCC"),
            ("LSV", "LSV"),
            ("DRV", "DRV"),
            # Vowel rule: 3-letter brand names title-case
            ("KIA", "Kia"),
            # Part overrides: brand names / model codes
            ("RAM", "RAM"),
            ("ETI", "ETI"),
            ("FLHTCUTG", "FLHTCUTG"),
            ("MAC TRAILERS", "MAC Trailers"),
            ("ETC 37 IH", "ETC 37 IH"),
            ("330I", "330i"),
            # Vehicle-type acronyms
            ("TRE BEV", "Tre BEV"),
            ("EDV", "EDV"),
            # Compound with hyphen
            ("PACIFICA PLUG-IN HYBRID", "Pacifica Plug-in Hybrid"),
            # Full-string override
            ("E-ONE", "E-ONE"),
        ],
    )
    def test_display_name(self, raw, expected):
        assert display_name(raw) == expected


class TestMakeSuppression:
    """Make is suppressed only when model starts with it as whole words."""

    def test_polestar_suppressed(self, db_conn, eval_seed_data):
        """POLESTAR / POLESTAR 2 -> 'Polestar 2', not 'Polestar Polestar 2'."""
        from scripts.make_generated_questions import Candidate

        cand = Candidate("POLESTAR", "POLESTAR 2", 2021, ("X",))
        row = _build_question("existence", cand, "backup camera", db_conn)
        assert "Polestar Polestar" not in row.question
        assert "Polestar 2" in row.question

    def test_prefix_not_suppressed(self, db_conn, eval_seed_data):
        """ACME / ACMEVAN keeps its make (prefix, not whole word)."""
        from scripts.make_generated_questions import Candidate

        cand = Candidate("ACME", "ACMEVAN", 2021, ("X",))
        row = _build_question("existence", cand, "brake", db_conn)
        assert "Acme Acmevan" in row.question

    def test_multi_word_make_suppressed(self, db_conn, eval_seed_data):
        """BLUE BIRD / BLUE BIRD VISION -> 'Blue Bird Vision'."""
        from scripts.make_generated_questions import Candidate

        cand = Candidate("BLUE BIRD", "BLUE BIRD VISION", 2021, ("X",))
        row = _build_question("existence", cand, "brake", db_conn)
        assert "Blue Bird Blue" not in row.question
        assert "Blue Bird Vision" in row.question


# ===================================================================
# 13. Quota allocation
# ===================================================================


class TestQuotaAllocation:
    def test_exact_quotas(self):
        """Allocation produces 20/template, 5/group, 80 total."""
        rng = random.Random(42)
        quotas = allocate_quotas(rng)

        # 5 per group
        for grp in COMPONENT_GROUPS:
            total = sum(quotas[grp.key].values())
            assert total == 5, f"{grp.key} has {total}, expected 5"

        # 20 per template
        for t in ("remedy", "consequence", "existence", "count"):
            total = sum(quotas[g.key][t] for g in COMPONENT_GROUPS)
            assert total == 20, f"{t} has {total}, expected 20"

        # 80 total
        grand = sum(sum(q.values()) for q in quotas.values())
        assert grand == 80


# ===================================================================
# 14. Ordering determinism
# ===================================================================


class TestOrderingDeterminism:
    def test_same_result_regardless_of_input_order(self, db_conn, eval_seed_data):
        """select_candidates with same data in two shuffled orders
        produces identical results.  Test never sorts.

        Uses tires group which has 6 TESTMAKE1 candidates, so
        sampling (n=3) exercises the RNG path.
        """
        grp = next(g for g in COMPONENT_GROUPS if g.key == "tires")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)
        assert len(cands) >= 4, f"Need enough candidates to sample; got {len(cands)}"

        # Shuffle twice with different seeds
        order1 = cands[:]
        random.Random(111).shuffle(order1)
        order2 = cands[:]
        random.Random(222).shuffle(order2)
        assert order1 != order2, "Shuffled inputs must differ"

        result1 = select_candidates(order1, 3, random.Random(42))
        result2 = select_candidates(order2, 3, random.Random(42))

        # Results should be identical (same candidates, same order)
        assert result1 == result2


# ===================================================================
# 15. Leakage detection
# ===================================================================


class TestLeakageDetection:
    def _make_row(self, question: str, gold_ids: list[str]) -> GoldenRow:
        return GoldenRow(
            id="leak_test",
            question=question,
            source="generated",
            gold_campaign_ids=gold_ids,
            reference_answer="",
            pass_rule="cite_all_gold",
        )

    def test_leakage_defect(self):
        """5-gram overlap with defect_summary triggers leakage."""
        texts = {
            "C001": {
                "defect_summary": (
                    "the alpha bravo charlie delta echo system may fail"
                ),
            }
        }
        row = self._make_row(
            "What about the alpha bravo charlie delta echo issue?", ["C001"]
        )
        flagged = check_leakage([row], texts)
        assert len(flagged) >= 1

    def test_leakage_consequence(self):
        """5-gram overlap with consequence_summary triggers leakage."""
        texts = {
            "C002": {
                "consequence_summary": (
                    "foxtrot golf hotel india juliet may cause injury"
                ),
            }
        }
        row = self._make_row(
            "What about foxtrot golf hotel india juliet risk?", ["C002"]
        )
        flagged = check_leakage([row], texts)
        assert len(flagged) >= 1

    def test_leakage_remedy(self):
        """5-gram overlap with corrective_action triggers leakage."""
        texts = {
            "C003": {
                "corrective_action": (
                    "dealer will kilo lima mike november oscar replace"
                ),
            }
        }
        row = self._make_row("Will kilo lima mike november oscar fix it?", ["C003"])
        flagged = check_leakage([row], texts)
        assert len(flagged) >= 1

    def test_leakage_component(self):
        """5-gram overlap with component text triggers leakage."""
        texts = {
            "C004": {
                "component": ("papa quebec romeo sierra tango bravo unit assembly"),
            }
        }
        row = self._make_row("What about papa quebec romeo sierra tango?", ["C004"])
        flagged = check_leakage([row], texts)
        assert len(flagged) >= 1

    def test_no_leakage_short_overlap(self):
        """4-gram overlap does NOT trigger (need 5)."""
        texts = {
            "C005": {
                "defect_summary": "alpha bravo charlie delta echo",
            }
        }
        row = self._make_row("What about alpha bravo charlie delta?", ["C005"])
        flagged = check_leakage([row], texts)
        assert len(flagged) == 0

    def test_vehicle_name_masked(self):
        """A 5-word vehicle name in the question is not flagged.

        The defect text contains the same vehicle name, but the masking
        removes it from the question's 5-grams.
        """
        texts = {
            "C006": {
                "defect_summary": (
                    "Recalling certain 2024 Jeep Grand Cherokee L vehicles"
                ),
            }
        }
        row = GoldenRow(
            id="mask_test",
            question=(
                "Are there any brake recalls for the 2024 Jeep Grand Cherokee L?"
            ),
            source="generated",
            gold_campaign_ids=["C006"],
            reference_answer="",
            make="JEEP",
            model="GRAND CHEROKEE L",
            year=2024,
            pass_rule="cite_all_gold",
        )
        flagged = check_leakage([row], texts)
        assert len(flagged) == 0


# ===================================================================
# 16. Validator checks — broken rows
# ===================================================================


class TestValidatorBrokenRows:
    def test_duplicate_id(self):
        row = GoldenRow(
            id="dup1",
            question="Q1",
            source="generated",
            gold_campaign_ids=["C1"],
            reference_answer="A",
            pass_rule="cite_all_gold",
        )
        errors = check_unique_ids([row, row])
        assert errors

    def test_duplicate_question(self):
        r1 = GoldenRow(
            id="r1",
            question="What is the fix?",
            source="generated",
            gold_campaign_ids=["C1"],
            reference_answer="A",
            pass_rule="cite_all_gold",
        )
        r2 = GoldenRow(
            id="r2",
            question="  what  is  the  fix?  ",
            source="generated",
            gold_campaign_ids=["C1"],
            reference_answer="A",
            pass_rule="cite_all_gold",
        )
        errors = check_unique_questions([r1, r2])
        assert errors

    def test_gold_id_missing(self, db_conn, eval_seed_data):
        row = GoldenRow(
            id="missing_gold",
            question="Q",
            source="generated",
            gold_campaign_ids=["NONEXISTENT_999"],
            reference_answer="A",
            pass_rule="cite_all_gold",
        )
        errors = check_gold_ids_exist(db_conn, [row])
        assert errors

    def test_unanswerable_with_gold(self):
        row = GoldenRow(
            id="bad_unanswerable",
            question="Q",
            source="unanswerable",
            gold_campaign_ids=["C1"],
            reference_answer="",
            pass_rule="decline",
        )
        errors = check_unanswerable([row])
        assert errors

    def test_unanswerable_wrong_pass_rule(self):
        row = GoldenRow(
            id="bad_rule",
            question="Q",
            source="unanswerable",
            gold_campaign_ids=[],
            reference_answer="",
            pass_rule="cite_all_gold",
        )
        errors = check_unanswerable([row])
        assert errors

    def test_shared_gold_across_generated_rows(self):
        """Two generated rows sharing a gold campaign ID is an error."""
        r1 = GoldenRow(
            id="g1",
            question="Q1",
            source="generated",
            gold_campaign_ids=["C1", "C2"],
            reference_answer="A",
            pass_rule="cite_all_gold",
        )
        r2 = GoldenRow(
            id="g2",
            question="Q2",
            source="generated",
            gold_campaign_ids=["C2", "C3"],
            reference_answer="A",
            pass_rule="cite_all_gold",
        )
        errors = check_no_shared_gold([r1, r2])
        assert errors
        assert "C2" in errors[0]

    def test_generated_file_missing(self, tmp_path):
        """Missing generated.jsonl is an error."""
        errors = check_generated_file(tmp_path / "nonexistent.jsonl")
        assert errors
        assert "not found" in errors[0]

    def test_generated_file_empty(self, tmp_path):
        """Empty generated.jsonl is an error."""
        empty = tmp_path / "generated.jsonl"
        empty.write_text("")
        errors = check_generated_file(empty)
        assert errors
        assert "empty" in errors[0]

    def test_gold_not_on_vehicle(self, db_conn, eval_seed_data):
        """Gold campaign that exists but isn't linked to the stated vehicle."""
        row = GoldenRow(
            id="bad_link",
            question="Q",
            source="handwritten",
            gold_campaign_ids=["24V123000"],  # exists, but on FORD RANGER 2021
            reference_answer="A",
            make="LINCOLN",
            model="CORSAIR",
            year=2021,
            pass_rule="cite_any_gold",
        )
        errors = check_gold_vehicle_link(db_conn, [row])
        assert errors
        assert "24V123000" in errors[0]
        assert "LINCOLN" in errors[0]

    def test_unicode_round_trip(self, tmp_path):
        """A row with curly apostrophe and accented letter survives write/read."""
        from scripts.validate_golden import _load_jsonl

        row = GoldenRow(
            id="unicode_test",
            question="What\u2019s the fix for the Citro\u00ebn recall?",
            source="generated",
            gold_campaign_ids=["C1"],
            reference_answer="Dealer\u2019s Citro\u00ebn fix",
            pass_rule="cite_all_gold",
        )
        path = tmp_path / "generated.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(row.model_dump_json() + "\n")
        loaded = _load_jsonl(path)
        assert len(loaded) == 1
        assert loaded[0].question == row.question
        assert loaded[0].reference_answer == row.reference_answer


# ===================================================================
# 17. test_empty_db_fails
# ===================================================================


class TestEmptyDbFails:
    def test_empty_db(self, db_conn):
        """No fixture loaded => tables are empty => check fails."""
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM recalls")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM recall_vehicles")
            assert cur.fetchone()[0] == 0
        errors = check_db_not_empty(db_conn)
        assert errors


# ===================================================================
# 18. Exact-match rule, fixture checks, positive controls
# ===================================================================


class TestExactMatchRule:
    """Group matching uses exact membership, not prefix."""

    def test_service_brakes_prefix_finds_nothing(self, db_conn, eval_seed_data):
        """With categories=("SERVICE BRAKES",) FORD RANGER 2021 has no
        candidate, because its components are "SERVICE BRAKES, HYDRAULIC"
        and "PARKING BRAKE".  A prefix match would find it."""
        cands = _fetch_group_candidates(db_conn, "brakes", ("SERVICE BRAKES",))
        keys = {(c.make, c.model, c.year) for c in cands}
        assert ("FORD", "RANGER", 2021) not in keys


class TestFixtureYearRows:
    """The fixture has FORD RANGER rows with year NULL, 9999 and 2017."""

    def test_bad_year_rows_exist(self, db_conn, eval_seed_data):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT year FROM recall_vehicles "
                "WHERE make = 'FORD' AND model = 'RANGER' AND year IS NULL"
            )
            assert cur.fetchone() is not None, "NULL year row missing"
            cur.execute(
                "SELECT year FROM recall_vehicles "
                "WHERE make = 'FORD' AND model = 'RANGER' AND year = 9999"
            )
            assert cur.fetchone() is not None, "9999 year row missing"
            cur.execute(
                "SELECT year FROM recall_vehicles "
                "WHERE make = 'FORD' AND model = 'RANGER' AND year = 2017"
            )
            assert cur.fetchone() is not None, "2017 year row missing"


class TestFixtureExcludedMakeRows:
    """The fixture has both JAGUAR and LAND ROVER rows."""

    def test_excluded_make_rows_exist(self, db_conn, eval_seed_data):
        with db_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM recall_vehicles WHERE make = 'JAGUAR' LIMIT 1")
            assert cur.fetchone() is not None, "JAGUAR row missing"
            cur.execute(
                "SELECT 1 FROM recall_vehicles WHERE make = 'LAND ROVER' LIMIT 1"
            )
            assert cur.fetchone() is not None, "LAND ROVER row missing"


class TestCapFixtureExceedsCap:
    """Positive control: fixture has more TESTMAKE1 vehicles than the cap."""

    def test_cap_fixture_exceeds_cap(self, db_conn, eval_seed_data):
        """Without cap, all 6 TESTMAKE1 vehicles would be selected."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "tires")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)

        rng = random.Random(42)
        # max_per_make=9999 effectively removes the cap
        selected = select_candidates(cands, 7, rng, max_per_make=9999)

        testmake1 = [c for c in selected if c.make == "TESTMAKE1"]
        assert len(testmake1) > 5, "Without cap, all 6 TESTMAKE1 should be selected"


# ===================================================================
# 19. No-shared-gold rule
# ===================================================================


class TestNoSharedGold:
    """A recall shared by two vehicles is gold in at most one question."""

    def test_shared_recall_gold_in_one_question(self, db_conn, eval_seed_data):
        """24V125000 is on both FORD RANGER 2021 and LINCOLN CORSAIR 2021
        in the steering group.  After selecting one, the other's gold
        overlaps and must be skipped."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "steering")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)

        # Both vehicles should be candidates
        keys = {(c.make, c.model, c.year) for c in cands}
        assert ("FORD", "RANGER", 2021) in keys
        assert ("LINCOLN", "CORSAIR", 2021) in keys

        used_gold: set[str] = set()
        rng = random.Random(42)
        selected = select_candidates(
            cands, 100, rng, max_per_make=9999, used_gold_ids=used_gold
        )

        # At most one of the two should be selected
        selected_keys = {(c.make, c.model, c.year) for c in selected}
        both = ("FORD", "RANGER", 2021) in selected_keys and (
            "LINCOLN",
            "CORSAIR",
            2021,
        ) in selected_keys
        assert not both, "Shared recall 24V125000 should prevent both being selected"


# ===================================================================
# 20. Min-gold floor for count / all_recalls
# ===================================================================


class TestMinGoldFloor:
    """Candidates with 1 gold recall are never sampled for count or all_recalls."""

    def test_one_gold_excluded_from_count(self, db_conn, eval_seed_data):
        """Each TESTMAKE1 vehicle has exactly 1 tires recall.  The count
        template floor from MIN_GOLD_FOR_TEMPLATE excludes them."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "tires")
        cands = _fetch_group_candidates(db_conn, grp.key, grp.categories)

        # Verify TESTMAKE1 vehicles each have exactly 1 gold recall
        tm1 = [c for c in cands if c.make == "TESTMAKE1"]
        assert len(tm1) >= 1
        for c in tm1:
            assert len(c.gold_ids) == 1

        rng = random.Random(42)
        selected = select_candidates(
            cands, 100, rng, min_gold=MIN_GOLD_FOR_TEMPLATE["count"], max_per_make=9999
        )

        selected_makes = {c.make for c in selected}
        assert "TESTMAKE1" not in selected_makes

    def test_one_gold_excluded_from_all_recalls(self, db_conn, eval_seed_data):
        """LINCOLN CORSAIR 2021 has 1 recall in total.  The all_recalls
        floor from MIN_GOLD_FOR_TEMPLATE excludes it.

        Asks for more questions than eligible vehicles so without the
        floor Corsair would be picked.
        """
        cands = _fetch_all_recalls_candidates(db_conn)

        corsair = [
            c
            for c in cands
            if c.make == "LINCOLN" and c.model == "CORSAIR" and c.year == 2021
        ]
        assert len(corsair) == 1
        assert len(corsair[0].gold_ids) == 1

        rng = random.Random(42)
        selected = select_candidates(
            cands,
            100,
            rng,
            min_gold=MIN_GOLD_FOR_TEMPLATE["all_recalls"],
            max_per_make=9999,
        )

        selected_keys = {(c.make, c.model, c.year) for c in selected}
        assert ("LINCOLN", "CORSAIR", 2021) not in selected_keys


# ===================================================================
# 21. Backup camera sub-component exclusion
# ===================================================================


class TestBackupCameraExclusion:
    """Non-camera BACK OVER PREVENTION components are excluded."""

    def test_pedestrian_alert_only_not_gold(self, db_conn, eval_seed_data):
        """24V160000 has only WARNINGS:EXTERNAL/PEDESTRIAN ALERT.
        It must not be backup_camera gold for TESTCAM SEDAN 2022."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "backup_camera")
        cands = _fetch_group_candidates(
            db_conn, grp.key, grp.categories, grp.excluded_subs
        )
        sedan_gold = set()
        for c in cands:
            if c.make == "TESTCAM" and c.model == "SEDAN" and c.year == 2022:
                sedan_gold.update(c.gold_ids)
        assert "24V160000" not in sedan_gold

    def test_camera_plus_alert_is_gold(self, db_conn, eval_seed_data):
        """24V161000 has DISPLAY FUNCTION + WARNINGS:EXTERNAL/PEDESTRIAN ALERT.
        It IS backup_camera gold because DISPLAY FUNCTION is not excluded."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "backup_camera")
        cands = _fetch_group_candidates(
            db_conn, grp.key, grp.categories, grp.excluded_subs
        )
        sedan_gold = set()
        for c in cands:
            if c.make == "TESTCAM" and c.model == "SEDAN" and c.year == 2022:
                sedan_gold.update(c.gold_ids)
        assert "24V161000" in sedan_gold

    def test_automatic_braking_space_not_gold(self, db_conn, eval_seed_data):
        """24V162000 has 'BACK OVER PREVENTION: AUTOMATIC SYSTEM BRAKING'
        (space after colon).  TRIM handles the space; it must be excluded."""
        grp = next(g for g in COMPONENT_GROUPS if g.key == "backup_camera")
        cands = _fetch_group_candidates(
            db_conn, grp.key, grp.categories, grp.excluded_subs
        )
        sedan_gold = set()
        for c in cands:
            if c.make == "TESTCAM" and c.model == "SEDAN" and c.year == 2022:
                sedan_gold.update(c.gold_ids)
        assert "24V162000" not in sedan_gold


# ===================================================================
# 22. Scripts importable as subprocesses from repo root
# ===================================================================


class TestScriptsImportable:
    """Both scripts must be runnable with `python scripts/...` from the
    repo root without ModuleNotFoundError or ImportError."""

    def test_generator_imports(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/make_generated_questions.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "ModuleNotFoundError" not in result.stderr, result.stderr
        assert "ImportError" not in result.stderr, result.stderr

    def test_validator_imports(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/validate_golden.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "ModuleNotFoundError" not in result.stderr, result.stderr
        assert "ImportError" not in result.stderr, result.stderr
