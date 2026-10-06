from __future__ import annotations

from datetime import date

import pytest

from engine.normalize import (
    DATE_RANGE_RE,
    YearMonth,
    date_warnings,
    job_interval,
    load_ontology,
    location_matches,
    merge_intervals,
    months_inclusive,
    normalize_candidate,
    parse_date,
    title_to_seniority,
    total_experience_months,
)
from engine.schemas import CandidateExtraction, JobItem, SeniorityLevel, SkillItem

TODAY = date(2026, 10, 5)


def ym(y: int, m: int) -> YearMonth:
    return YearMonth(y, m)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2021-01", ym(2021, 1)),
        ("2021/3", ym(2021, 3)),
        ("Jan 2021", ym(2021, 1)),
        ("January 2021", ym(2021, 1)),
        ("jan. 2021", ym(2021, 1)),
        ("Sept 2019", ym(2019, 9)),
        ("Sep, 2019", ym(2019, 9)),
        ("01/2021", ym(2021, 1)),
        ("1/2021", ym(2021, 1)),
        ("Mar '19", ym(2019, 3)),
        ("2020", ym(2020, 1)),
    ],
)
def test_parse_date_formats(text: str, expected: YearMonth) -> None:
    assert parse_date(text, today=TODAY) == expected


@pytest.mark.parametrize("word", ["present", "Present", "current", "Currently", "now", "till date", "ongoing"])
def test_present_resolves_to_reference_month(word: str) -> None:
    assert parse_date(word, today=TODAY) == ym(2026, 10)


def test_year_only_end_is_december() -> None:
    assert parse_date("2020", is_end=True) == ym(2020, 12)


@pytest.mark.parametrize("bad", ["13/2020", "2021-13", "foo", "", "Jan", "1890"])
def test_unparseable_dates(bad: str) -> None:
    assert parse_date(bad, today=TODAY) is None


@pytest.mark.parametrize(
    ("line", "start", "end"),
    [
        ("Senior ML Engineer | Acme | Jan 2021 - Present", "Jan 2021", "Present"),
        ("Data Analyst 2018-2020", "2018", "2020"),
        ("2019-03 to 2021-06", "2019-03", "2021-06"),
        ("Intern 06/2017 – 08/2017", "06/2017", "08/2017"),
        ("Engineer, May 2015 until Current", "May 2015", "Current"),
    ],
)
def test_date_range_regex(line: str, start: str, end: str) -> None:
    m = DATE_RANGE_RE.search(line)
    assert m and (m["start"], m["end"]) == (start, end)


def test_job_interval_inclusive_months() -> None:
    iv = job_interval("Jan 2021", "Jan 2021", TODAY)
    assert iv and months_inclusive(iv) == 1
    iv = job_interval("2018", "2020", TODAY)
    assert iv and months_inclusive(iv) == 36


def test_job_interval_present_and_future_clamp() -> None:
    assert job_interval("Jan 2026", "Present", TODAY) == (ym(2026, 1), ym(2026, 10))
    assert job_interval("Jan 2026", "Dec 2027", TODAY) == (ym(2026, 1), ym(2026, 10))


@pytest.mark.parametrize(("start", "end"), [("Jun 2021", "Jan 2020"), ("Jan 2021", None), (None, "2020"), ("x", "y")])
def test_job_interval_invalid(start: str | None, end: str | None) -> None:
    assert job_interval(start, end, TODAY) is None


# ---------------------------------------------------------------------------
# Experience merging
# ---------------------------------------------------------------------------


def test_overlapping_jobs_are_counted_once() -> None:
    a = (ym(2018, 1), ym(2020, 12))  # 36 months
    b = (ym(2020, 1), ym(2021, 12))  # overlaps a by 12 months
    assert total_experience_months([a, b]) == 48  # not 60


def test_nested_job_adds_nothing() -> None:
    outer = (ym(2015, 1), ym(2020, 12))
    inner = (ym(2017, 6), ym(2018, 6))
    assert total_experience_months([outer, inner]) == 72


def test_gaps_are_not_counted_and_adjacent_jobs_merge() -> None:
    a = (ym(2015, 1), ym(2015, 12))
    b = (ym(2017, 1), ym(2017, 12))  # one-year gap
    c = (ym(2018, 1), ym(2018, 6))  # starts the month after b ends
    assert merge_intervals([c, a, b]) == [a, (ym(2017, 1), ym(2018, 6))]
    assert total_experience_months([a, b, c]) == 12 + 18


def test_normalize_candidate_merges_overlap_and_orders_jobs() -> None:
    extraction = CandidateExtraction(
        skills=[
            SkillItem(name="k8s", evidence="k8s"),
            SkillItem(name="Python", evidence="Python"),
            SkillItem(name="Prolog", evidence="Prolog"),
        ],
        jobs=[
            JobItem(title="Data Analyst", company="A", start="2018-01", end="2020-02", description="SQL", evidence="e"),
            JobItem(
                title="Senior ML Engineer",
                company="B",
                start="03/2020",
                end="present",
                description="Python and Kubernetes",
                evidence="e",
            ),
            JobItem(
                title="ML Consultant", company="C", start="Jan 2021", end="Dec 2022", description=None, evidence="e"
            ),
            JobItem(title="Tutor", company=None, start=None, end=None, description="Prolog", evidence="e"),
        ],
        projects=[],
        education=[],
        certifications=[],
        location=None,
        notice_period_days=None,
        summary=None,
    )
    n = normalize_candidate(extraction, today=TODAY)
    # 2018-01 .. 2026-10 continuous (consultant overlaps, analyst->engineer adjacent)
    assert n.total_experience_months == 8 * 12 + 10
    assert n.undated_jobs == 1
    assert [j.title for j in n.jobs] == ["Senior ML Engineer", "ML Consultant", "Data Analyst", "Tutor"]
    assert n.jobs[0].is_current and n.jobs[0].end == "2026-10"
    assert n.seniority == SeniorityLevel.SENIOR
    skills = {s.canonical: s for s in n.skills}
    assert skills["Kubernetes"].raw == "k8s" and skills["Kubernetes"].in_ontology
    assert skills["Kubernetes"].last_used == "2026-10"
    assert skills["Python"].last_used == "2026-10"
    assert not skills["Prolog"].in_ontology and skills["Prolog"].last_used is None  # only in an undated job


# ---------------------------------------------------------------------------
# Skill ontology
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("k8s", "Kubernetes"),
        ("PySpark", "Apache Spark"),
        ("golang", "Go"),
        ("sklearn", "scikit-learn"),
        ("Postgres", "PostgreSQL"),
        ("Amazon Web Services", "AWS"),
        ("  Natural   Language  Processing ", "NLP"),
        ("go", "Go"),  # explicitly stated skill: ambiguous alias allowed
        ("Cobol", None),
    ],
)
def test_alias_canonicalization(alias: str, canonical: str | None) -> None:
    assert load_ontology().canonicalize(alias) == canonical


def test_ontology_size_and_no_conflicting_aliases() -> None:
    ont = load_ontology()
    assert len(ont.categories) >= 80
    assert not set(ont.aliases) & set(ont.ambiguous)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Willing to go the extra mile", set()),
        ("CV available on request", set()),
        ("R&D lab and an r-squared of 0.9", set()),
        ("JavaScript and TypeScript", {"JavaScript", "TypeScript"}),  # no Java inside JavaScript
        ("Java services", {"Java"}),
        ("C++ and C# developer", {"C++", "C#"}),
        ("PostgreSQL and NoSQL stores", {"PostgreSQL"}),  # no SQL inside PostgreSQL / NoSQL
        ("Used GitHub Actions", {"GitHub Actions"}),  # no Git inside GitHub
        ("PyTorch/TensorFlow models", {"PyTorch", "TensorFlow"}),
        ("CI/CD pipelines", {"CI/CD"}),
    ],
)
def test_free_text_matching(text: str, expected: set[str]) -> None:
    assert {m.canonical for m in load_ontology().find_mentions(text)} == expected


def test_ambiguous_aliases_match_only_as_skill_list_items() -> None:
    ont = load_ontology()
    section = "Languages: Python, Go, R | CV"
    assert {m.canonical for m in ont.find_mentions(section, section)} == {"Python", "Go", "R", "Computer Vision"}


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Data Science Intern", 1),
        ("Junior Developer", 2),
        ("Associate Data Scientist", 2),
        ("Machine Learning Engineer", 3),
        ("Internal Tools Engineer", 3),
        ("Senior ML Engineer", 4),
        ("Sr. Data Analyst", 4),
        ("Staff Engineer", 5),
        ("Lead Data Scientist", 5),
        ("Senior Engineering Manager", 5),
        ("Principal Scientist", 6),
        ("Head of AI", 6),
    ],
)
def test_title_to_seniority(title: str, level: int) -> None:
    assert title_to_seniority(title) == level


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "allowed", "ok"),
    [
        ("Bengaluru, India", ["Bengaluru, India"], True),
        ("Bangalore, Karnataka, India", ["Bengaluru, India"], True),  # city alias
        ("Pune, India", ["Bengaluru, India"], False),
        ("Pune, India", ["India"], True),  # country-level allowance
        ("Lyon, France", ["Bengaluru, India", "Remote"], True),  # remote role accepts anywhere
        ("Remote", ["Bengaluru, India"], False),
        ("Remote", ["Bengaluru, India (Hybrid)", "Remote"], True),
        ("Mumbai", [], True),
    ],
)
def test_location_matches(candidate: str, allowed: list[str], ok: bool) -> None:
    assert location_matches(candidate, allowed) is ok


# ---------------------------------------------------------------------------
# Incomplete dates
# ---------------------------------------------------------------------------


def _jobs(*jobs: tuple[str, str | None, str | None]) -> CandidateExtraction:
    return CandidateExtraction(
        skills=[],
        jobs=[JobItem(title=t, company=None, start=s, end=e, description=None, evidence=t) for t, s, e in jobs],
        projects=[], education=[], certifications=[], location=None, notice_period_days=None, summary=None,
    )  # fmt: skip


def test_year_only_past_year_counts_twelve_months() -> None:
    [job] = normalize_candidate(_jobs(("Analyst", "2022", None)), today=TODAY).jobs
    assert (job.start, job.end, job.months, job.date_confidence) == ("2022-01", "2022-12", 12, "low")
    assert "year only" in (job.date_note or "")


def test_year_only_current_year_counts_up_to_this_month() -> None:
    [job] = normalize_candidate(_jobs(("Analyst", "2026", "2026")), today=TODAY).jobs
    assert (job.start, job.end, job.months) == ("2026-01", "2026-10", 10)


def test_start_only_most_recent_job_is_ongoing() -> None:
    n = normalize_candidate(_jobs(("Engineer", "Jan 2022", None), ("Analyst", "Mar 2019", "Dec 2021")), today=TODAY)
    engineer = n.jobs[0]
    assert (engineer.start, engineer.end, engineer.date_confidence, engineer.is_current) == (
        "2022-01", "2026-10", "low", True,
    )  # fmt: skip
    assert n.jobs[1].date_confidence == "high"


def test_start_only_older_job_ends_when_next_job_starts() -> None:
    n = normalize_candidate(_jobs(("Engineer", "Jan 2022", "Present"), ("Analyst", "Mar 2019", None)), today=TODAY)
    analyst = n.jobs[1]
    assert (analyst.start, analyst.end, analyst.date_confidence) == ("2019-03", "2022-01", "low")
    assert "next role began (2022-01)" in (analyst.date_note or "")
    assert n.total_experience_months == (2026 * 12 + 10) - (2019 * 12 + 3) + 1  # overlap month counted once


def test_year_only_bound_on_full_range_is_low_confidence() -> None:
    [job] = normalize_candidate(_jobs(("Analyst", "2018", "2020")), today=TODAY).jobs
    assert job.months == 36 and job.date_confidence == "low"


def test_full_range_is_high_confidence_and_undated_is_none() -> None:
    n = normalize_candidate(_jobs(("A", "Jan 2020", "Dec 2020"), ("B", None, None)), today=TODAY)
    assert [j.date_confidence for j in n.jobs] == ["high", None]


def test_date_warnings_list_low_confidence_jobs() -> None:
    n = normalize_candidate(_jobs(("Engineer", "Jan 2022", None), ("Analyst", "2019", None)), today=TODAY)
    warnings = date_warnings(n)
    assert len(warnings) == 2 and all(w.startswith("Low-confidence dates for") for w in warnings)


def test_candidate_without_jobs_is_entry_level() -> None:
    assert normalize_candidate(_jobs(), today=TODAY).seniority == 1


# ---------------------------------------------------------------------------
# Inflected aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fine-tuned BERT models", {"Fine-tuning"}),
        ("fine-tunes LLMs", {"Fine-tuning", "LLMs"}),
        ("Containerized services", set()),  # not an alias form
        ("Built microservices", {"Microservices"}),
        ("Wrote Python scripts", {"Python"}),
        ("Benchmarked LightGBM models", {"LightGBM"}),
    ],
)
def test_inflected_alias_forms_match(text: str, expected: set[str]) -> None:
    assert {m.canonical for m in load_ontology().find_mentions(text)} == expected


@pytest.mark.parametrize(
    "text",
    [
        "reacted quickly to incidents",
        "sparked interest in data",
        "forecasts revenue weekly",
        "snowflakes fell",
        "rusted",
    ],
)
def test_guarded_aliases_do_not_match_ordinary_english(text: str) -> None:
    assert load_ontology().find_mentions(text) == []


def test_inflected_forms_helper() -> None:
    from engine.normalize import inflected_forms

    assert set(inflected_forms("fine-tuning")) >= {"fine-tune", "fine-tuned", "fine-tunes", "fine-tuning"}
    assert set(inflected_forms("pipeline")) >= {"pipelines", "pipelined", "pipelining"}
    assert set(inflected_forms("dashboard")) >= {"dashboards", "dashboarded"}


def test_short_acronyms_are_not_inflected() -> None:
    assert load_ontology().find_mentions("SQLed the data") == []


# ---------------------------------------------------------------------------
# Profession skills (ontology 2.0): specific phrases match anywhere; everyday words only in skills lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Managed the general ledger and accounts payable in QuickBooks",
            {"General Ledger", "Accounts Payable", "QuickBooks"},
        ),
        ("Menu planning, food cost control; ServSafe certified", {"Menu Planning", "Cost Control", "Food Safety"}),
        ("Wrote lesson plans and improved classroom management", {"Lesson Planning", "Classroom Management"}),
        ("Recruited new staff as a registered nurse", {"Recruiting", "Nursing"}),
        ("Drafted plans in AutoCAD and read blueprints", {"AutoCAD", "Blueprint Reading"}),
        ("Configured Active Directory and VPN access", {"Active Directory", "VPN"}),
        ("Advanced Excel and Microsoft Word", {"Microsoft Excel", "Microsoft Word"}),
    ],
)
def test_profession_skills_in_free_text(text: str, expected: set[str]) -> None:
    assert {m.canonical for m in load_ontology().find_mentions(text)} == expected


@pytest.mark.parametrize(
    "text",
    [
        "I excel at building teams and the outlook is strong",  # verbs and nouns, not Microsoft products
        "Senior Data Engineer | Larkspur Logistics",  # employer names
        "Cashier | Sunrise Nursing Home",  # no Nursing from the employer
        "Helped customers with purchases and key accounts at the bank",  # guarded stems
        "Advertised the role and billed hours to a brand new client",
        "French cuisine and Spanish tapas",
        "Ran app onboarding experiments",
        "Sales tax on budget hotels",
    ],
)
def test_everyday_words_are_not_profession_skills(text: str) -> None:
    assert load_ontology().find_mentions(text) == []


def test_everyday_words_count_when_listed_as_skills() -> None:
    section = "Skills: Excel, Word, Access, Sales, Inventory, Budgets, Logistics, Spanish"
    assert {m.canonical for m in load_ontology().find_mentions(section, section)} == {
        "Microsoft Excel",
        "Microsoft Word",
        "Microsoft Access",
        "Sales",
        "Inventory Management",
        "Budgeting",
        "Logistics",
        "Spanish",
    }
