"""The dashboard's own copy of stage-1-supplax's 9-phase interview
(~/.claude/skills/stage-1-supplax/references/pipeline-phases.md) - lets an
automator answer it once, at registration time, in the dashboard UI instead
of live in a Claude Code session. Phase numbers and question keys are chosen
to match that reference file 1:1, so the skill's own step 0 can pull this via
`GET /api/automations/<slug>/stage1-answers` and skip whatever key it finds
already filled, without a separate mapping table to keep in sync by hand.
"""

PHASES = [
    {
        "num": 1, "title": "Наміри та рамки",
        "questions": [
            {"key": "one_liner", "label": "Одним реченням: що це будує?", "type": "text"},
            {"key": "audience", "label": "Основна аудиторія", "type": "select", "options": [
                ("end_users", "Кінцеві користувачі (публічний продукт)"),
                ("internal_team", "Тільки внутрішня команда"),
                ("other_developers", "Інші розробники (бібліотека/інструмент)"),
                ("other", "Інше"),
            ]},
            {"key": "open_source", "label": "Відкритий код чи приватний?", "type": "select", "options": [
                ("open_source", "Відкритий код"),
                ("private", "Приватний, внутрішній"),
                ("not_decided", "Ще не вирішено"),
            ]},
            {"key": "v1_optimizes_for", "label": "На що оптимізує v1?", "type": "select", "options": [
                ("mvp_fast", "Швидко випустити MVP"),
                ("long_term", "Довгострокова підтримуваність"),
                ("learning", "Навчання / експеримент"),
                ("other", "Інше"),
            ]},
            {"key": "v1_done", "label": "Як виглядає «готово» для v1?", "type": "textarea"},
            {"key": "target_metric_type", "label": "Яка головна метрика цієї автоматизації?",
             "type": "select", "options": [
                ("time_saved", "Час ручної праці"),
                ("conversion", "Конверсія / пропускна здатність"),
                ("quality", "Якість або точність"),
                ("cost", "Витрати"),
                ("other", "Інше"),
            ]},
            {"key": "manual_reduction_target",
             "label": "Наскільки хочемо скоротити ручну дію чи покращити цю метрику? "
                       "(конкретне число — %, год/тиждень, в.п. конверсії тощо; якщо є "
                       "AUTOMATION_REQUEST.md з цифрами поточного процесу — відштовхуйся від них)",
             "type": "textarea"},
            {"key": "success_check",
             "label": "Як зрозуміємо, що спрацювало — яке значення звіримо і коли? "
                       "(можна лишити порожнім — оцінимо самі)",
             "type": "textarea"},
        ],
    },
    {
        "num": 2, "title": "Архітектура та стек",
        "questions": [
            {"key": "stack_confirm",
             "label": "Стек (мова/фреймворк) — підтвердь або впиши, якщо не визначили автоматично",
             "type": "text"},
            {"key": "archetype", "label": "Тип проєкту", "type": "select", "options": [
                ("automation_pipeline", "Автоматизація / пайплайн (розклад, ETL, оркестрація)"),
                ("dashboard_analytics", "Дашборд / аналітика"),
                ("service_api_library", "Сервіс, API або бібліотека"),
                ("not_sure", "Ще не знаю"),
            ]},
            {"key": "architecture_style", "label": "Стиль архітектури", "type": "select", "options": [
                ("monolith", "Монолit"),
                ("modular_monolith", "Модульний монолit"),
                ("microservices", "Мікросервіси"),
                ("not_sure", "Ще не знаю"),
            ]},
            {"key": "data_storage", "label": "Зберігання даних", "type": "select", "options": [
                ("none", "Не потрібне"),
                ("relational_db", "Реляційна БД"),
                ("nosql", "NoSQL"),
                ("file_based", "Файлове зберігання"),
                ("other", "Інше"),
            ]},
            {"key": "expected_scale", "label": "Очікуваний масштаб", "type": "select", "options": [
                ("prototype", "Прототип / низьке навантаження"),
                ("medium", "Середнє навантаження"),
                ("high_scale", "Високе навантаження — проєктувати одразу"),
            ]},
            {"key": "hard_to_reverse_decision",
             "label": "Раннє важкозворотне рішення (фреймворк, БД, протокол), варте запису як ADR? "
                       "(або лиши порожнім)",
             "type": "textarea"},
        ],
    },
    {
        "num": 3, "title": "Процес розробки",
        "questions": [
            {"key": "branching_strategy", "label": "Стратегія гілок", "type": "select", "options": [
                ("trunk_based", "Trunk-based (коміти в main)"),
                ("github_flow", "GitHub flow (feature-гілки + PR)"),
                ("git_flow", "Git flow (develop + release гілки)"),
                ("not_decided", "Не вирішено"),
            ]},
            {"key": "review_process", "label": "Хто ревʼюїть зміни", "type": "select", "options": [
                ("just_me", "Тільки я"),
                ("small_fixed_team", "Невелика фіксована команда"),
                ("open_to_any", "Відкрито для будь-кого"),
                ("not_decided", "Не вирішено"),
            ]},
            {"key": "commit_convention", "label": "Конвенція комітів", "type": "select", "options": [
                ("conventional_commits", "Conventional Commits"),
                ("free_form", "Вільна форма"),
                ("not_decided", "Не вирішено"),
            ]},
            {"key": "outside_contributors", "label": "Очікуються зовнішні контриб'ютори?", "type": "select", "options": [
                ("yes", "Так"),
                ("no", "Ні"),
            ]},
        ],
    },
    {
        "num": 4, "title": "Тестування та QA",
        "questions": [
            {"key": "test_types", "label": "Типи тестів", "type": "multiselect", "options": [
                ("unit", "Unit"),
                ("integration", "Integration"),
                ("e2e", "End-to-end"),
                ("none_yet", "Ще немає"),
            ]},
            {"key": "coverage_expectation", "label": "Очікування щодо покриття", "type": "select", "options": [
                ("high", "Прагнемо високого покриття"),
                ("best_effort", "Best-effort"),
                ("no_target", "Без формальної цілі"),
            ]},
            {"key": "test_framework", "label": "Тестовий фреймворк", "type": "text"},
            {"key": "ci_blocks_merge", "label": "CI має блокувати мердж при провалі тестів?", "type": "select", "options": [
                ("required", "Обов'язково"),
                ("advisory", "Лише порада, не блокує"),
            ]},
        ],
    },
    {
        "num": 5, "title": "Безпека та комплаєнс",
        "questions": [
            {"key": "public_attack_surface",
             "label": "Публічна поверхня атаки (розгорнутий сервіс, опублікований API, обробка кредів)?",
             "type": "select", "options": [
                ("yes", "Так"),
                ("no_internal_only", "Ні, лише внутрішнє"),
            ]},
            {"key": "compliance_requirements", "label": "Вимоги комплаєнсу", "type": "select", "options": [
                ("none", "Немає"),
                ("gdpr", "GDPR"),
                ("soc2", "SOC 2"),
                ("hipaa", "HIPAA"),
                ("other", "Інше"),
            ]},
            {"key": "secrets_handling", "label": "Як зберігаються секрети", "type": "select", "options": [
                ("env_file", ".env файл, у .gitignore"),
                ("secrets_manager", "Окремий secrets manager"),
                ("not_decided", "Ще не вирішено"),
            ]},
        ],
    },
    {
        "num": 6, "title": "Governance та ліцензія",
        "questions": [
            {"key": "license", "label": "Ліцензія", "type": "select", "options": [
                ("mit", "MIT"),
                ("apache2", "Apache-2.0"),
                ("proprietary", "Proprietary, без публічної ліцензії"),
                ("other", "Інше"),
            ]},
            {"key": "copyright_holder", "label": "Власник copyright (ім'я/компанія)", "type": "text"},
        ],
    },
    {
        "num": 7, "title": "CI/CD та релізи",
        "questions": [
            {"key": "deploy_target", "label": "Куди деплоїться", "type": "select", "options": [
                ("not_deployed", "Не деплоїться (бібліотека/CLI)"),
                ("cloud_vm_container", "Cloud VM або контейнер"),
                ("serverless", "Serverless"),
                ("static_site", "Статичний сайт"),
                ("other", "Інше"),
            ]},
            {"key": "environments", "label": "Середовища", "type": "select", "options": [
                ("just_prod", "Тільки prod"),
                ("staging_prod", "Staging + prod"),
                ("dev_staging_prod", "Dev + staging + prod"),
            ]},
            {"key": "versioning_scheme", "label": "Схема версіонування", "type": "select", "options": [
                ("semver", "Semantic Versioning"),
                ("calver", "Calendar Versioning"),
                ("not_decided", "Не вирішено"),
            ]},
            {"key": "ci_now", "label": "Налаштовувати CI зараз?", "type": "select", "options": [
                ("full_pipeline", "Повний пайплайн (lint → test → build → deploy)"),
                ("lint_test_only", "Лише lint + test"),
                ("no_ci_yet", "Ще без CI"),
            ]},
            {"key": "rollout_strategy", "label": "Стратегія розгортання", "type": "select", "options": [
                ("direct_deploy", "Прямий деплой"),
                ("canary", "Canary"),
                ("blue_green", "Blue-green"),
                ("not_decided", "Не вирішено"),
            ]},
        ],
    },
    {
        "num": 8, "title": "Операції та підтримка",
        "questions": [
            {"key": "oncall_needed", "label": "Потрібен on-call / incident response?", "type": "select", "options": [
                ("yes", "Так"),
                ("not_yet", "Ще зарано"),
            ]},
            {"key": "support_channel", "label": "Канал підтримки", "type": "select", "options": [
                ("clickup", "ClickUp"),
                ("github_issues", "GitHub Issues"),
                ("email", "Email"),
                ("community_chat", "Community-чат (Discord/Slack)"),
                ("not_decided", "Не вирішено"),
            ]},
            {"key": "monitoring", "label": "Моніторинг / спостережуваність", "type": "select", "options": [
                ("logs_only", "Тільки логи"),
                ("metrics_alerts", "Метрики + алерти"),
                ("full_observability", "Повний observability-стек"),
                ("not_deciding", "Ще не вирішуємо"),
            ]},
        ],
    },
    {
        "num": 9, "title": "Rollback та перевірка",
        "questions": [
            {"key": "rollback_approach", "label": "Підхід до rollback", "type": "select", "options": [
                ("git_revert_redeploy", "Git revert + редеплой"),
                ("feature_flags", "Feature flags"),
                ("not_applicable", "Не застосовно ще"),
                ("not_decided", "Не вирішено"),
            ]},
            {"key": "post_deploy_verification", "label": "Перевірка після деплою", "type": "select", "options": [
                ("manual_smoke_test", "Ручний smoke test"),
                ("automated_health_checks", "Автоматичні health checks"),
                ("not_decided", "Не вирішено"),
            ]},
        ],
    },
]


def collect_answers(form):
    """Build the {"1": {...}, "2": {...}} dict this feature stores from a
    submitted stage1_form.html POST - only non-empty answers, so the JSON
    stays a lean set of *known* facts rather than a full grid of blanks."""
    answers = {}
    for phase in PHASES:
        phase_answers = {}
        for q in phase["questions"]:
            field_name = f"p{phase['num']}_{q['key']}"
            if q["type"] == "multiselect":
                values = [v for v in form.getlist(field_name) if v]
                if values:
                    phase_answers[q["key"]] = values
            else:
                value = form.get(field_name, "").strip()
                if value:
                    phase_answers[q["key"]] = value
            if q["type"] == "select":
                other_value = form.get(f"{field_name}_other", "").strip()
                if other_value:
                    phase_answers[f"{q['key']}_other"] = other_value
        if phase_answers:
            answers[str(phase["num"])] = phase_answers
    return answers


def answered_phase_count(answers):
    """How many of the 9 phases have at least one answer - shown on the
    automation page as e.g. "4/9 фаз заповнено" rather than a bare yes/no."""
    if not answers:
        return 0
    return len([p for p in answers.values() if p])
