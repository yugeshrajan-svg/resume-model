# Talent-pool retrieval on labelled resumes

480 resumes in 24 groups; 479 candidates indexed with `ollama:nomic-embed-text:v1.5`. Query = the group name. Random ranking ≈ 4% precision.

| Group | Query | Resumes | P@10 | P@20 | Most confused with (top 10) | Resumes with recognised skills |
|---|---|---|---|---|---|---|
| ACCOUNTANT | Accountant | 20 | 80% | 75% | AUTOMOBILE (1), FINANCE (1) | 100% |
| ADVOCATE | Advocate | 20 | 30% | 35% | CONSULTANT (2), HEALTHCARE (2), ARTS (1) | 100% |
| AGRICULTURE | Agriculture | 20 | 50% | 30% | PUBLIC-RELATIONS (1), ENGINEERING (1), DESIGNER (1) | 100% |
| APPAREL | Apparel | 20 | 20% | 20% | DIGITAL-MEDIA (2), BUSINESS-DEVELOPMENT (2), SALES (1) | 100% |
| ARTS | Arts | 20 | 40% | 25% | TEACHER (2), DIGITAL-MEDIA (1), DESIGNER (1) | 100% |
| AUTOMOBILE | Automobile | 20 | 30% | 20% | AVIATION (3), DIGITAL-MEDIA (2), HEALTHCARE (1) | 100% |
| AVIATION | Aviation | 20 | 90% | 60% | AUTOMOBILE (1) | 100% |
| BANKING | Banking | 20 | 60% | 45% | FINANCE (2), ACCOUNTANT (2) | 100% |
| BPO | BPO | 20 | 60% | 35% | CONSTRUCTION (1), CONSULTANT (1), DESIGNER (1) | 100% |
| BUSINESS-DEVELOPMENT | Business development | 20 | 40% | 35% | AUTOMOBILE (2), BANKING (1), HEALTHCARE (1) | 95% |
| CHEF | Chef | 20 | 100% | 90% | — | 100% |
| CONSTRUCTION | Construction | 20 | 90% | 75% | CONSULTANT (1) | 100% |
| CONSULTANT | Consultant | 20 | 50% | 50% | AUTOMOBILE (1), FINANCE (1), BPO (1) | 100% |
| DESIGNER | Designer | 20 | 90% | 55% | ARTS (1) | 100% |
| DIGITAL-MEDIA | Digital media | 20 | 70% | 65% | ENGINEERING (2), PUBLIC-RELATIONS (1) | 100% |
| ENGINEERING | Engineering | 20 | 60% | 45% | CONSTRUCTION (2), DESIGNER (2) | 100% |
| FINANCE | Finance | 20 | 60% | 45% | BANKING (3), ADVOCATE (1) | 100% |
| FITNESS | Fitness | 20 | 90% | 55% | ADVOCATE (1) | 100% |
| HEALTHCARE | Healthcare | 20 | 60% | 60% | CONSULTANT (1), FITNESS (1), BPO (1) | 100% |
| HR | HR professional | 20 | 70% | 70% | ARTS (2), BPO (1) | 100% |
| INFORMATION-TECHNOLOGY | Information technology | 20 | 40% | 35% | CONSULTANT (2), BANKING (1), DIGITAL-MEDIA (1) | 100% |
| PUBLIC-RELATIONS | Public relations | 20 | 80% | 60% | DIGITAL-MEDIA (1), ARTS (1) | 100% |
| SALES | Sales | 20 | 30% | 15% | BUSINESS-DEVELOPMENT (6), ARTS (1) | 100% |
| TEACHER | Teacher | 20 | 70% | 60% | ARTS (2), AGRICULTURE (1) | 100% |

Macro average: P@10 61%, P@20 48%. Median search latency 0.63s.
Rule-based extraction recognises at least one skill in 100% of resumes (group average); the skill dictionary determines which skills the fast mode can match against requirements.
