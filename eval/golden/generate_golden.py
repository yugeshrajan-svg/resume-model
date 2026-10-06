"""Generate the synthetic golden set (jobs, resumes, labels, annotations).

Everything is fictional; contact details use example.com. Each resume is defined once as structured
data, and its text, its skill/date annotations and its relevance labels are all derived from that
definition, so annotations are ground truth by construction (not produced by the engine).

Coverage: date layouts below / above / same-line / two-column (PDF) / year-only / start-only / mixed,
TXT / PDF / DOCX, resumes with and without projects, a resume with no jobs, hyphen-wrapped bullets.

Run:  python eval/golden/generate_golden.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import docx
import pymupdf

HERE = Path(__file__).parent
REFERENCE_DATE = "2026-10-05"  # "present" in annotations resolves against this date
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

JOBS = {
    "ml_engineer": (HERE.parents[1] / "data" / "sample" / "jd.txt").read_text(encoding="utf-8"),
    "data_engineer": """Job Title: Senior Data Engineer
Location: Bengaluru, India / Remote

Responsibilities
- Build batch and streaming data pipelines
- Own the data warehouse and data modeling

Requirements
- 4+ years of experience in data engineering roles
- Strong Python and SQL
- Apache Spark and Airflow
- Kafka for streaming
- Experience with AWS or GCP

Nice to have
- dbt, Snowflake
- Terraform

Notice period: we can wait up to 60 days.
""",
    "data_analyst": """Job Title: Junior Data Analyst
Location: Hyderabad, India

Responsibilities
- Build dashboards and reports for business teams

Requirements
- 1+ years of experience in data analysis
- Strong SQL and Python
- Tableau or Power BI
- Statistics

Nice to have
- A/B testing, Pandas

Notice period: we can wait up to 30 days.
""",
}


@dataclass
class Job:
    title: str
    company: str
    start: str  # "YYYY-MM", or "YYYY" for year-only
    end: str | None  # "YYYY-MM", "YYYY", "present", or None (start only)
    bullets: list[str]
    # Annotated interval when the written dates are incomplete (by the documented inference rules).
    approx: tuple[str, str] | None = None


@dataclass
class Resume:
    file: str
    layout: str  # below | above | same_line | two_column | year_only | start_only | mixed | no_jobs
    name: str
    location: str
    notice: str
    summary: str
    jobs: list[Job]
    skills: list[str]  # skills section (canonical names, as written)
    extra_skills: list[str] = field(default_factory=list)  # canonical skills mentioned only in bullets/projects
    projects: list[tuple[str, str]] = field(default_factory=list)
    education: str = "B.Tech in Computer Science, Anna University, 2016"
    labels: dict[str, int] = field(default_factory=dict)  # job_id -> 0..3
    wrap_bullets: bool = False


def _fmt(d: str | None) -> str:
    if d is None:
        return ""
    if d == "present":
        return "Present"
    if len(d) == 4:
        return d
    year, month = d.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


def _dates(job: Job) -> str:
    if job.end is None:
        return f"{_fmt(job.start)} -"
    if job.end == job.start and len(job.start) == 4:
        return job.start
    return f"{_fmt(job.start)} - {_fmt(job.end)}"


def _wrap(bullet: str) -> list[str]:
    """Hard-wrap a bullet mid-word with a hyphen, as copied PDF text often is."""
    cut = len(bullet) // 2
    while cut < len(bullet) and not bullet[cut].isalpha():
        cut += 1
    return [f"- {bullet[:cut]}-", f"  {bullet[cut:]}"]


def render_text(r: Resume) -> list[str]:
    lines = [r.name, f"{r.name.split()[0].lower()}.{r.name.split()[-1].lower()}@example.com"]
    lines += [f"Location: {r.location}", f"Notice period: {r.notice}", "", "Summary", r.summary, ""]
    if r.jobs:
        lines.append("Experience")
    for k, job in enumerate(r.jobs):
        header = f"{job.title} | {job.company}"
        layout = r.layout
        if layout == "mixed":
            layout = "same_line" if k % 2 == 0 else "below"
        if layout in ("same_line", "year_only", "start_only"):
            lines.append(f"{header} | {_dates(job)}")
        elif layout == "above":
            lines += [_dates(job), header]
        else:  # below
            lines += [header, _dates(job)]
        for b in job.bullets:
            lines += _wrap(b) if r.wrap_bullets else [f"- {b}"]
    if r.projects:
        lines += ["", "Projects"] + [f"{name}: {desc}" for name, desc in r.projects]
    lines += ["", "Skills", ", ".join(r.skills), "", "Education", r.education]
    return lines


def write_two_column_pdf(r: Resume, path: Path) -> None:
    """Dates in a left column, titles/bullets in a right column, written column by column."""
    doc = pymupdf.open()
    page = doc.new_page()
    head = [r.name, f"Location: {r.location}", f"Notice period: {r.notice}", "Summary", r.summary, "Experience"]
    y = 50
    for line in head:
        page.insert_text((50, y), line, fontsize=10)
        y += 14
    rows = []
    for job in r.jobs:
        rows.append(y)
        y += 14 * (1 + len(job.bullets)) + 6
    for job, row_y in zip(r.jobs, rows, strict=True):  # left column first
        page.insert_text((50, row_y), _dates(job), fontsize=10)
    for job, row_y in zip(r.jobs, rows, strict=True):  # then the right column
        page.insert_text((200, row_y), f"{job.title} | {job.company}", fontsize=10)
        for i, b in enumerate(job.bullets, start=1):
            page.insert_text((200, row_y + 14 * i), f"- {b}", fontsize=10)
    tail = ["Projects", *[f"{n}: {d}" for n, d in r.projects]] if r.projects else []
    tail += ["Skills", ", ".join(r.skills), "Education", r.education]
    for line in tail:
        page.insert_text((50, y), line, fontsize=10)
        y += 14
    doc.save(path)
    doc.close()


def _wrap_words(text: str, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return [*lines, current] if current else lines


def write_sidebar_pdf(r: Resume, path: Path) -> None:
    """Two-column resume written row by row (as many PDF generators do): a left sidebar (contact, skills,
    education) at a tighter line pitch, and a right main column (summary, jobs with long wrapped bullets).
    In raw content order the sidebar lines interleave with the jobs, pushing each date more than three
    lines away from its title; reading order (left column, then right) keeps each job together."""
    left = ["Contact", f"{r.name.split()[0].lower()}.{r.name.split()[-1].lower()}@example.com",
            f"Location: {r.location}", f"Notice period: {r.notice}", "", "Skills", *r.skills, "",
            "Education", *_wrap_words(r.education, 28)]  # fmt: skip
    right = ["Summary", *_wrap_words(r.summary, 60), "", "Experience"]
    for job in r.jobs:
        right += [job.title, job.company, _dates(job)]
        for bullet in job.bullets:
            wrapped = _wrap_words(bullet, 58)
            right += [f"- {wrapped[0]}", *(f"  {w}" for w in wrapped[1:])]
    rows = [(110 + 9 * i, 50, text) for i, text in enumerate(left) if text]
    rows += [(110 + 14 * i, 230, text) for i, text in enumerate(right) if text]
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), r.name, fontsize=14)
    for y, x, text in sorted(rows):  # row by row across both columns
        page.insert_text((x, y), text, fontsize=9)
    doc.save(path)
    doc.close()


def _annotation(r: Resume) -> dict:
    jobs = []
    for job in r.jobs:
        if job.approx:
            start, end, approximate = job.approx[0], job.approx[1], True
        elif len(job.start) == 4:  # year-only range: Jan of the start year .. Dec of the end year
            start, end, approximate = f"{job.start}-01", f"{job.end}-12", True
        else:
            start, end, approximate = job.start, job.end, False
        jobs.append({"title": job.title, "start": start, "end": end, "approximate": approximate})
    return {
        "candidate_file": r.file,
        "layout": r.layout,
        "has_projects": bool(r.projects),
        "skills": sorted(set(r.skills) | set(r.extra_skills)),
        "jobs": jobs,
    }


RESUMES = [
    Resume(
        "g01_ml_below.txt", "below", "Ishaan Verma", "Bengaluru, India", "30 days",
        "Machine learning engineer focused on NLP and retrieval systems.",
        [
            Job("Senior Machine Learning Engineer", "Northwind AI", "2021-02", "present",
                ["Built RAG pipelines with LangChain on Azure OpenAI", "Deployed PyTorch models on Kubernetes with Docker"]),
            Job("Machine Learning Engineer", "Bluefin Retail", "2018-06", "2021-01",
                ["Trained recommendation models in Python and SQL on AWS", "Tracked experiments in MLflow"]),
        ],
        ["Python", "SQL", "PyTorch", "Docker", "Kubernetes", "Azure", "AWS", "LangChain", "MLflow"],
        extra_skills=["Azure OpenAI", "OpenAI API", "RAG", "Machine Learning", "NLP", "Vector Databases"],
        projects=[("Semantic Job Search", "vector databases and PyTorch embeddings for job matching")],
        labels={"ml_engineer": 3, "data_engineer": 1, "data_analyst": 0},
    ),
    Resume(
        "g02_ml_above.txt", "above", "Meera Pillai", "Remote", "45 days",
        "Engineer building production ML services.",
        [
            Job("Machine Learning Engineer", "Kestrel Health", "2020-09", "present",
                ["Served PyTorch models behind FastAPI on AWS", "Containerised training jobs with Docker and Kubernetes"]),
            Job("Data Scientist", "Orchid Bank", "2019-01", "2020-08",
                ["Built credit models with scikit-learn and SQL"]),
        ],
        ["Python", "SQL", "PyTorch", "Docker", "Kubernetes", "AWS", "FastAPI", "scikit-learn"],
        extra_skills=["Machine Learning"],
        labels={"ml_engineer": 2, "data_engineer": 1, "data_analyst": 1},
    ),
    Resume(
        "g03_ml_same.txt", "same_line", "Nikhil Rao", "Bengaluru, India", "30 days",
        "ML engineer with three years of experience.",
        [
            Job("Machine Learning Engineer", "Pebble Labs", "2023-07", "present",
                ["Fine-tuned PyTorch vision models", "Wrote data loaders in Python and SQL"]),
        ],
        ["Python", "SQL", "PyTorch", "Docker", "Computer Vision"],
        extra_skills=["Fine-tuning", "Machine Learning"],
        projects=[("Defect Detector", "object detection with PyTorch on factory images")],
        labels={"ml_engineer": 1, "data_engineer": 0, "data_analyst": 0},
    ),
    Resume(
        "g04_ml_twocol.pdf", "two_column", "Farah Siddiqui", "Bengaluru, India", "60 days",
        "Lead ML engineer for LLM platforms.",
        [
            Job("Lead Machine Learning Engineer", "Vertex Analytics", "2019-04", "present",
                ["Led PyTorch and Kubernetes LLM platform on Azure"]),
            Job("Machine Learning Engineer", "Quill Data", "2015-08", "2019-03",
                ["Python, SQL and Docker services on AWS"]),
        ],
        ["Python", "SQL", "PyTorch", "Docker", "Kubernetes", "Azure", "AWS", "LangChain", "MLflow", "Apache Spark"],
        extra_skills=["LLMs", "Machine Learning"],
        projects=[("Prompt Router", "LangChain routing across LLMs with MLflow tracking")],
        labels={"ml_engineer": 3, "data_engineer": 1, "data_analyst": 0},
    ),
    Resume(
        "g05_de_below.txt", "below", "Rahul Menon", "Bengaluru, India", "30 days",
        "Data engineer building batch and streaming platforms.",
        [
            Job("Senior Data Engineer", "Larkspur Logistics", "2021-03", "present",
                ["Built Kafka streaming pipelines into Snowflake", "Orchestrated Spark jobs with Airflow on AWS"]),
            Job("Data Engineer", "Cobalt Telecom", "2019-01", "2021-02",
                ["Modelled warehouse tables with dbt and SQL", "Automated infrastructure with Terraform"]),
        ],
        ["Python", "SQL", "Apache Spark", "Airflow", "Kafka", "AWS", "dbt", "Snowflake", "Terraform"],
        extra_skills=[],
        labels={"ml_engineer": 1, "data_engineer": 3, "data_analyst": 1},
    ),
    Resume(
        "g06_de_yearonly.txt", "year_only", "Sana Khan", "Remote", "30 days",
        "Data engineer working on GCP pipelines.",
        [
            Job("Data Engineer", "Harbor Freight Tech", "2024", "2024",
                ["Maintained Airflow DAGs and Spark jobs on GCP"], approx=("2024-01", "2024-12")),
            Job("Junior Data Engineer", "Maple Insights", "2020", "2023",
                ["Wrote Python and SQL ETL jobs"]),
        ],
        ["Python", "SQL", "Apache Spark", "Airflow", "GCP"],
        extra_skills=["ETL"],
        labels={"ml_engineer": 0, "data_engineer": 2, "data_analyst": 1},
    ),
    Resume(
        "g07_da_startonly.txt", "start_only", "Divya Iyer", "Hyderabad, India", "15 days",
        "Analyst building dashboards for sales teams.",
        [
            Job("Data Analyst", "Saffron Retail", "2024-01", None,
                ["Built Tableau dashboards from SQL queries", "Ran statistics tests in Python with Pandas"],
                approx=("2024-01", "present")),
        ],
        ["SQL", "Python", "Tableau", "Statistics", "Pandas"],
        extra_skills=[],
        projects=[("Churn Dashboard", "Tableau dashboard tracking churn with SQL")],
        labels={"ml_engineer": 0, "data_engineer": 0, "data_analyst": 3},
    ),
    Resume(
        "g08_da_intern_above.txt", "above", "Arjun Nair", "Hyderabad, India", "immediate",
        "Recent graduate interested in analytics.",
        [
            Job("Data Analyst Intern", "Lotus Media", "2026-01", "2026-06",
                ["Built Power BI reports with SQL", "Cleaned survey data in Python"]),
        ],
        ["SQL", "Python", "Power BI"],
        extra_skills=[],
        labels={"ml_engineer": 0, "data_engineer": 0, "data_analyst": 2},
    ),
    Resume(
        "g09_webdev_same.txt", "same_line", "Kabir Malhotra", "Pune, India", "30 days",
        "Front-end developer.",
        [
            Job("Web Developer", "Pixel Forge", "2020-05", "present",
                ["Built React apps with Node.js APIs", "Wrote JavaScript and TypeScript components"]),
        ],
        ["JavaScript", "TypeScript", "React", "Node.js"],
        extra_skills=[],
        projects=[("Portfolio Site", "React and Node.js personal site")],
        labels={"ml_engineer": 0, "data_engineer": 0, "data_analyst": 0},
    ),
    Resume(
        "g10_research_nojobs.txt", "no_jobs", "Tanvi Joshi", "Bengaluru, India", "immediate",
        "PhD graduate in NLP.",
        [],
        ["Python", "PyTorch", "NLP", "Hugging Face Transformers"],
        extra_skills=["Deep Learning"],
        projects=[
            ("Multilingual NER", "Hugging Face Transformers NER for Indic languages in PyTorch"),
            ("Summariser", "deep learning summarisation with PyTorch"),
        ],
        education="Ph.D. in Computer Science, Indian Institute of Science, 2026",
        labels={"ml_engineer": 1, "data_engineer": 0, "data_analyst": 0},
    ),
    Resume(
        "g11_de_wrapped.txt", "below", "Varun Bhat", "Bengaluru, India", "30 days",
        "Data engineer with streaming experience.",
        [
            Job("Data Engineer", "Granite Payments", "2022-01", "present",
                ["Developed streaming ingestion with Kafka and Spark on AWS", "Scheduled data quality checks with Airflow"]),
            Job("Software Engineer", "Ivy Systems", "2021-01", "2021-12",
                ["Wrote Python and SQL services"]),
        ],
        ["Python", "SQL", "Apache Spark", "Kafka", "Airflow", "AWS"],
        extra_skills=[],
        wrap_bullets=True,
        labels={"ml_engineer": 0, "data_engineer": 2, "data_analyst": 1},
    ),
    Resume(
        "g12_hybrid_mixed.txt", "mixed", "Anjali Kulkarni", "Remote", "60 days",
        "ML and data platform engineer.",
        [
            Job("Senior ML Platform Engineer", "Helios Energy", "2021-06", "present",
                ["Built feature pipelines with Spark and Airflow", "Deployed PyTorch models with Docker on Kubernetes"]),
            Job("Data Engineer", "Quarry Labs", "2018-02", "2021-05",
                ["Python and SQL ETL on AWS", "Tracked models in MLflow"]),
        ],
        ["Python", "SQL", "Apache Spark", "Airflow", "PyTorch", "Docker", "Kubernetes", "AWS", "MLflow"],
        extra_skills=["ETL"],
        labels={"ml_engineer": 2, "data_engineer": 2, "data_analyst": 1},
    ),
    Resume(
        "g13_backend_below.txt", "below", "Siddharth Jain", "Bengaluru, India", "30 days",
        "Backend engineer on payments systems.",
        [
            Job("Senior Software Engineer", "Ember Fintech", "2019-09", "present",
                ["Built Java Spring Boot microservices with Kafka", "Ran services on Kubernetes and AWS"]),
        ],
        ["Java", "Spring Boot", "Kafka", "Docker", "Kubernetes", "AWS", "SQL"],
        extra_skills=["Microservices"],
        labels={"ml_engineer": 0, "data_engineer": 1, "data_analyst": 0},
    ),
    Resume(
        "g14_ds_below.txt", "below", "Pooja Hegde", "Hyderabad, India", "30 days",
        "Data scientist in insurance pricing.",
        [
            Job("Data Scientist", "Granary Insurance", "2021-04", "present",
                ["Built pricing models in R and Python", "Presented statistics in Tableau"]),
        ],
        ["Python", "R", "SQL", "Statistics", "Tableau", "scikit-learn"],
        extra_skills=["Time Series"],
        projects=[("Claims Forecast", "time series forecasting with scikit-learn")],
        labels={"ml_engineer": 1, "data_engineer": 0, "data_analyst": 2},
    ),
    Resume(
        "g15_da_docx_same.docx", "same_line", "Ritika Sen", "Hyderabad, India", "30 days",
        "Data analyst for growth experiments.",
        [
            Job("Data Analyst", "Marigold Commerce", "2024-08", "present",
                ["Analysed A/B testing results with Python and SQL", "Built Power BI dashboards"]),
        ],
        ["SQL", "Python", "Power BI", "Statistics", "A/B Testing"],
        extra_skills=[],
        labels={"ml_engineer": 0, "data_engineer": 0, "data_analyst": 3},
    ),
    Resume(
        "g16_ml_sidebar.pdf", "two_column_sidebar", "Neha Bansal", "Bengaluru, India", "30 days",
        "Machine learning engineer working on forecasting and recommendation systems for retail.",
        [
            Job("Machine Learning Engineer", "Tamarind Commerce", "2022-03", "present",
                ["Built demand models in PyTorch and served them with Docker on Kubernetes across four regions",
                 "Designed feature pipelines in Python and SQL feeding nightly training and evaluation jobs",
                 "Introduced MLflow experiment tracking and model registry adopted by three product teams",
                 "Mentored two junior engineers and ran the weekly model review"]),
            Job("Data Scientist", "Juniper Mobility", "2019-07", "2022-02",
                ["Developed churn and pricing models with scikit-learn and XGBoost on AWS",
                 "Wrote SQL pipelines and dashboards used by the marketing analytics team",
                 "Ran A/B testing for app onboarding experiments with product managers"]),
        ],
        ["Python", "SQL", "PyTorch", "Docker", "Kubernetes", "AWS", "MLflow", "scikit-learn", "XGBoost"],
        extra_skills=["A/B Testing", "Machine Learning", "Recommender Systems", "Time Series"],
        education="B.Tech in Computer Science, Delhi Technological University, 2019",
        labels={"ml_engineer": 2, "data_engineer": 1, "data_analyst": 1},
    ),
]  # fmt: skip


def main() -> None:
    (HERE / "jobs").mkdir(exist_ok=True)
    (HERE / "resumes").mkdir(exist_ok=True)
    for job_id, text in JOBS.items():
        (HERE / "jobs" / f"{job_id}.txt").write_text(text, encoding="utf-8")
    for r in RESUMES:
        path = HERE / "resumes" / r.file
        if r.layout == "two_column_sidebar":
            write_sidebar_pdf(r, path)
        elif r.file.endswith(".pdf"):
            write_two_column_pdf(r, path)
        elif r.file.endswith(".docx"):
            d = docx.Document()
            for line in render_text(r):
                d.add_paragraph(line)
            d.save(path)
        else:
            path.write_text("\n".join(render_text(r)) + "\n", encoding="utf-8")
    with (HERE / "labels.jsonl").open("w", encoding="utf-8") as f:
        for r in RESUMES:
            for job_id in JOBS:
                f.write(json.dumps({"job_id": job_id, "candidate_file": r.file, "label": r.labels[job_id]}) + "\n")
    with (HERE / "annotations.jsonl").open("w", encoding="utf-8") as f:
        for r in RESUMES:
            f.write(json.dumps(_annotation(r)) + "\n")
    meta = {"reference_date": REFERENCE_DATE, "jobs": list(JOBS), "resumes": len(RESUMES)}
    (HERE / "golden.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(JOBS)} jobs, {len(RESUMES)} resumes, {len(RESUMES) * len(JOBS)} labels to {HERE}")


if __name__ == "__main__":
    main()
