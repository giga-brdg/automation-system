# ROI — How This Is Actually Calculated

The detailed companion to `dashboard/ROI.md` — the real walkthrough behind its terse,
dashboard-parsed fields. This describes the mechanism the dashboard app applies to
every automation's ROI card, including this repo's own — it isn't a portfolio-wide
standard other automations' repos read; each repo keeps its own `docs/roi_explained.md`
and its own `dashboard/ROI.md` generated from it. Most of what's below (the
`github_sync.py`/`app.py` traces in Caveats and Confidence) documents this repo's
own dashboard codebase specifically, since that's what lives here — another
automation's `docs/roi_explained.md` won't have an equivalent implementation trace
to write, just its own hypothesis and methodology for its own ROI number.

Оновлено 30.10.2026. Розділи про цінність, метрики й відкриті питання написані
українською, бо саме вони йдуть у картку дашборда; трасування коду нижче лишилось
англійською, як було, і не переписувалось у цьому проході.

## Value Hypothesis

**Статус: гіпотеза до заміру.** Усі цілі нижче поставлені 30.10.2026 за запитом Іллі.
Це не раніше погоджені обіцянки, не зобов'язання перед керівництвом і не спостережений
результат. Підтвердженого ефекту на сьогодні немає взагалі.

**Що саме змінюється.** Дашборд має замикати ланцюжок: метрика → відхилення → рішення
→ відповідальний і строк → повторний замір. Користь дає не існування картки, а рішення,
яке після неї ухвалили: зупинити малокорисну доробку, зняти організаційне очікування
запуску, відновити процес, що впав, відключити зайву підписку, знайти готове рішення
замість другої розробки того самого. Контроль «бот працює» таких рішень не дає.

**Грошова ціль, нижній сценарій: $220/міс, у картку взято округлене донизу $200/міс.**
Модель знизу вверх на конкретному механізмі:

- **Checker_FB: виявлення розривів у ланцюжку.** Дашборд показує, що менеджери губять ліди
  після нагадування бота, хоча сам бот працює справно. Тімлід змінює дежурства, розподіл
  лідів або правила обробки нагадувань. Повторний замір підтверджує ефект.
  5 додаткових лідів з номером на місяць (припущення) × $84 (12% угод × $700 прибутку) =
  **$420/міс доходу компанії**.
- **Бюджет контролю: $200/міс.** Базовий $80/міс (хостинг $20 + 4 год поточної підтримки
  × $15) + 4 год управлінських розборів × $15 + 4 год корективних дій × $15. Розбори і
  корективні дії це управлінська робота поверх вже закладеної підтримки кожного проєкту,
  без подвійного рахунку.

$420 − $200 = **$220/міс**. Ціль у метриці: $200/міс.

**Що навмисно НЕ додано.** 16 год/міс перенаправленого ресурсу (16 × $8 × 0,3 = $38,40/міс
умовної вартості розвантаження) до суми не входить: ті самі години, найімовірніше, уже
працюють всередині гіпотези про прискорений запуск, і рахувати їх двічі не можна. Для
незалежного додавання потрібні різні рішення й окремий облік часу.

**Чому цієї суми немає у «## Цінність» і чому `money` у метриках порожній.** Прискорення
управлінськими рішеннями вже сидить усередині вікна користі самої прискореної
автоматизації. Якщо за ті самі два тижні ефект зарахували тій автоматизації, дашборду
його вдруге не приписуємо. Тому статей цінності в цьому репозиторії немає свідомо, а
гроші лишаються лише як ціль головної метрики й розрахунок у цьому файлі. Це гарантує,
що $105,56 не потрапить ні в поточні гроші портфеля, ні в його потенціал.

**Чому немає блоку «повний масштаб».** Дашборд не є каналом продажів, який масштабується
обсягом, тому цифри «на повному масштабі» для нього не рахуємо.

**Що лишається поза сумою.** Збереження конверсії, якості й швидкості у відділах
перевіряється на конкретних випадках; до появи бази не додаємо довільний відсоток від
доходу компанії. Разова розробка дашборда й час керівників на впровадження циклу в
бюджет не закладені.

**Підтримувальна немонетарна ціль:** 16 год/міс перенаправленого ресурсу, якщо є
підтвердження скасованої роботи й нового виконаного результату.

## Methodology

Цінність рахуємо за рішеннями, а не за фактом синхронізації. Гроші тут це економічний
еквівалент управлінських рішень: раніший вихід корисної автоматизації на користь плюс
скасовані зайві платежі, мінус бюджет експлуатації. Власного постійного доходу дашборд
не створює, і сума неадитивна щодо решти портфеля.

Структура для цих чисел у програмі вже є, і це не план на майбутнє: репозиторій
автоматизації віддає `dashboard/metrics.json` за контрактом `docs/metrics-contract.md`
(v1.1), `src/metrics_contract.py` його валідує, `src/metrics_sync.py` зберігає заміри як
append-only і рахує статус метрики, `src/value_dashboard.py` показує це на сторінці
цінності. Попереднє твердження цього файлу, що структурованої схеми під числа ще немає,
а є лише вільний текст, застаріло й тут не повторюється.

Вільним текстом лишаються саме текстові поля картки (`Гіпотеза`, `Як вимірюємо`) — це й
далі так. Окремого поля вартості токенів на `ROIEntry` досі немає: витрати на токени
живуть у власній панелі поруч, а не всередині ROI-картки.

Що міряємо по кожному рішенню, щоб замір узагалі можна було зарахувати: дата й джерело
сигналу; значення метрики до; рішення й відповідальний; плановий строк і критерій;
фактична дія; повторний замір; гроші, час або витрати після; інші одночасні зміни. Якщо
сигнал першим дала людина або інший інструмент, дашборду його не приписуємо.

### What else is on the card
Five `dashboard/ROI.md` sections feed `ROIEntry`. One of them — Actual Results —
supplies the headline number itself (see Methodology above); the other four add
context around it:
- **Hypothesis** → `hypothesis`, the free-text baseline this same Methodology
  section above is built around (the registration form's `Гіпотеза` field, e.g.
  "this used to take 3h/week manually"). Rendered as the `roi-baseline` line under
  the divider.
- **How We'll Measure It** → `metric_description`, the registration form's `Як
  вимірюємо` field. Rendered as the `roi-caption` line directly under the headline
  number.
- **Qualitative Value** → `qualitative_notes`, shown as a bullet list under the ROI
  number. The parser only picks up lines starting with `-` or `*`
  (`github_sync.py`'s `_bullet_items`/`_BULLET_RE`) — plain prose written under that
  heading without a bullet prefix is silently dropped (no error, nothing renders).
  So despite there being no format *requirement*, there is a format the section must
  follow to survive sync: bullet points.
- **Actual Results** → `measured_value`. Two separate things are true about it,
  on two separate sides of the sync, and they're easy to conflate:
  - **Render side (unconditional):** once a value is stored, the template
    shows it as the big number regardless of confidence — it doesn't check
    confidence before rendering (see Caveats: the "Виміряно"/"Оцінка" badge and
    the number below it are independent renders, not a gate).
  - **Write side (gated):** `roi_fields_from_sections` only reads Actual
    Results into `measured_value` *at all* when Confidence's own text starts
    with "measured" (`github_sync.py`, `confidence == "measured"`). If
    Confidence stays "Estimated" — the repo's own current state — anything
    written under Actual Results is ignored in full on that sync, silently,
    with no warning. This is a materially different trap than "it gets
    truncated": a stranger who fills in Actual Results before remembering to
    flip Confidence's wording will see nothing appear post-sync and have no
    on-screen indication why. When the gate does pass, the extraction keeps
    only the first non-blank line of Actual Results and stops — any additional
    lines are silently dropped, the same kind of gotcha as Qualitative Value's
    bullet-only parsing above.
- **Presentation** → `presentation_url`, an optional link to a slide-deck Artifact,
  rendered as a button when present. This repo's own `dashboard/ROI.md` does have a
  `## Presentation` section today, with a link to an earlier ROI deck. **Той дек
  застарів:** він показує методику цінності до 30.10.2026 і не містить ні грошової
  цілі $100/міс, ні трьох процесних цілей. Посилання збережене навмисно, щоб не
  втратити артефакт, але новий дек у цьому проході не публікувався — переробляти його
  можна лише окремим кроком з публікацією нового артефакту.

All of this depends on `dashboard/ROI.md`'s section headers matching the expected
text exactly. `parse_markdown_sections` (`github_sync.py`) keys sections by the
literal `## Heading` text with no fuzzy matching, normalization, or error on a
near-miss — `## Actual results` or `## Result` instead of `## Actual Results`
silently yields nothing for that field on sync, with no warning that the heading
wasn't recognized. This is likely the single easiest mistake to make when hand-
editing `ROI.md`, and it isn't specific to Actual Results — it applies to all six
headings the same way.

Four of these five sync one-directional and additive-only through the
GitHub-sync path — reached via the browser's `/automations/<slug>/resync`
("Оновити з GitHub") button or the initial `/automations/import-github` import,
both of which call `sync_automation_from_github` — whose ROI block applies
`fields[x] or automation.roi.X` for `hypothesis`, `metric_description`,
`measured_value`, `presentation_url`, and `qualitative_notes`, so an
empty/removed section in `ROI.md` never clears one of *these* fields on the next
sync. **Confidence is the exception**: `app.py` sets
`automation.roi.confidence = fields["confidence"]` unconditionally, with no `or`
fallback. `roi_fields_from_sections` defaults confidence to `"estimated"`
whenever the Confidence section is missing or doesn't start with "measured", so
GitHub sync *can* and does reset a previously "measured" card back to
"estimated" — the one ROI column this additive-only guarantee does not cover.

There's a second, separate write path with different behavior: `/api/automations/
<slug>/sync`, the API-key-authenticated route stage-1-supplax's portfolio-sync step
calls after a build finishes. Its `roi` block uses `roi.get(key,
automation.roi.key)` — a missing-*key* fallback, not falsy-or — so a caller that
sends an explicit empty string for `hypothesis`, `metric_description`,
`confidence`, `measured_value`, or `presentation_url` in that endpoint's JSON
payload *does* clear the field. `qualitative_notes` isn't in that route's `roi`
block at all, so it can't be cleared through it either.

Net effect on retraction: `qualitative_notes` currently has no UI or API path that
clears it — not the registration form (no field for it), not the GitHub-sync path
(additive-only), not the API route (not handled there) — so editing the database is
the only way to blank a wrong qualitative note today. Three fields (`hypothesis`,
`metric_description`, `presentation_url`) can be retracted through the
registration form, since it overwrites them on every save including with a blank
value; `measured_value` has no form field to retract it through, but all four
(plus `confidence`, see below) can be cleared via the API route's
explicit-empty-string path. Rewriting `ROI.md` back to blank and re-syncing
through the GitHub-sync path will not clear any of these four fields — that path
is additive-only for them.

`confidence` doesn't fit the pattern above and shouldn't be lumped in with the
other four's "blank value" retraction: the registration form renders it as a
two-option `<select>` (Оцінка / Виміряно, `automation_form.html`) with no blank
option, so a user can never submit an empty confidence through that form —
"retracting" it there means flipping the dropdown back to Estimated, not
blanking a field. `apply_manual_form` also defaults it via
`form.get("confidence", "estimated")`, so a malformed POST that omits the field
entirely silently downgrades a "measured" badge to "estimated" — a distinct risk
from the blank-value retraction the other three free-text fields have. And
unlike those four, confidence is *not* additive-only through GitHub sync (see
"What else is on the card" above): re-syncing a `ROI.md` whose Confidence section
was removed or reworded resets the stored value to "estimated" outright.

## Метрики

Дата запуску: невідома. Програма працює (картка має статус live, і це не змінювалось),
але точної дати запуску в прод ніхто не зафіксував, тому `launched_at` лишається `null`
і заведено відкрите питання. Порожнє значення тут означає «дату не знаємо», а не «ще не
запущено».

Пілот управлінського циклу: листопад 2026, після прийняття бізнес-власником. Перший
розбір процесних цілей — 30.11.2026. Baseline усіх чотирьох метрик невідомий і
фіксується в перший тиждень пілота до втручання; відсутність даних не є нулем і не
означає успіху. Грошовий сценарій прив'язаний до кварталу, тому перший його розбір —
31.01.2027: у листопадовій перевірці квартального результату вимагати не можна.

Коли придатних випадків за період немає, пишемо «немає випадків», а не автоматичні 100%.

### Ефект управлінських рішень
- id: dashboard-economic-goal
- Головна: так
- Тип: інше
- Одиниця: $/міс
- Що краще: більше
- До: немає, див. відкриті питання
- Ціль: 100. Чому саме така: нова ціль, поставлена 30.10.2026 за запитом Іллі, округлена
  донизу з нижнього сценарію $105,56/міс ($155,56 середньомісячного ефекту від одного
  прискореного на 14 днів запуску за квартал плюс $30 скасованої підписки мінус $80
  бюджету експлуатації). Це не раніше погоджена обіцянка і не виміряний результат
- Як перевіряємо: щокварталу звіряємо журнал рішень і рахуємо лише ті випадки, де є
  зафіксований початковий графік, рішення з причиною прискорення, фактичний результат і
  підтвердження, що цей самий ефект не зарахований самій автоматизації
- Як часто міряємо: раз на 90 днів

| Дата | Період | Значення | Джерело | Замір чи оцінка | Примітка |
|---|---|---|---|---|---|

Гроші: не рахуємо, сума живе в цілі метрики і в «## Value Hypothesis»

### Активні автоматизації зі свіжим заміром
- id: live-automations-with-fresh-metric
- Головна: ні
- Тип: якість
- Одиниця: %
- Що краще: більше
- До: немає, див. відкриті питання
- Ціль: 90. Чому саме така: поріг пілота, обраний Іллею 30.10.2026; це не спостережений
  рівень і не раніше погоджена ціль
- Як перевіряємо: раз на місяць рахуємо частку активних автоматизацій, у яких є ключовий
  показник, ціль, відповідальний і замір не старіший за погоджену для них частоту
- Як часто міряємо: раз на 30 днів

| Дата | Період | Значення | Джерело | Замір чи оцінка | Примітка |
|---|---|---|---|---|---|

Гроші: не рахуємо

### Рішення за відхиленнями за два дні
- id: deviation-decisions-within-2-days
- Головна: ні
- Тип: якість
- Одиниця: %
- Що краще: більше
- До: немає, див. відкриті питання
- Ціль: 90. Чому саме така: поріг пілота, обраний Іллею 30.10.2026; це не спостережений
  рівень і не раніше погоджена ціль
- Як перевіряємо: раз на місяць рахуємо частку підтверджених суттєвих відхилень, за
  якими протягом двох робочих днів після виявлення записані рішення, відповідальний і
  строк; нестача або прострочення заміру теж вважається відхиленням
- Як часто міряємо: раз на 30 днів

| Дата | Період | Значення | Джерело | Замір чи оцінка | Примітка |
|---|---|---|---|---|---|

Гроші: не рахуємо

### Повторний замір після виправлення
- id: due-actions-with-repeat-measurement
- Головна: ні
- Тип: якість
- Одиниця: %
- Що краще: більше
- До: немає, див. відкриті питання
- Ціль: 80. Чому саме така: поріг пілота, обраний Іллею 30.10.2026; це не спостережений
  рівень і не раніше погоджена ціль
- Як перевіряємо: раз на місяць рахуємо частку дій, строк оцінки яких настав, за якими
  зроблено повторний замір; успішним закриттям вважаємо лише досягнення заздалегідь
  погодженого критерію, а не закриту задачу, і горизонт беремо з бізнес-процесу
- Як часто міряємо: раз на 30 днів

| Дата | Період | Значення | Джерело | Замір чи оцінка | Примітка |
|---|---|---|---|---|---|

Гроші: не рахуємо

## Відкриті питання

| Метрика (id) | Стаття цінності (id) | Поле | Питання | Кого питати | З якої дати |
|---|---|---|---|---|---|
| немає | немає | launched_at | Яка фактична дата запуску дашборда в роботу? Картка каже live, але точної дати ніхто не зафіксував | Ілля | 2026-10-30 |
| dashboard-economic-goal | немає | baseline | Який управлінський ефект маємо зараз, до пілота? Потрібен зафіксований журнал рішень за попередній квартал | manual_measurement | 2026-10-30 |
| live-automations-with-fresh-metric | немає | baseline | Яка зараз частка активних автоматизацій зі свіжим заміром, власником і ціллю? | manual_measurement | 2026-10-30 |
| deviation-decisions-within-2-days | немає | baseline | Яка зараз частка відхилень, за якими рішення приймають протягом двох робочих днів? | manual_measurement | 2026-10-30 |
| due-actions-with-repeat-measurement | немає | baseline | Яка зараз частка виправлень, які після настання строку отримують повторний замір? | manual_measurement | 2026-10-30 |

Крім baseline, відкритими лишаються: вартість разової розробки й часу керівників на
впровадження циклу (у бюджет $80/міс не входить), конкретна зайва підписка на $30/міс
(ще не виявлена), і правило розподілу спільного ефекту між дашбордом і прискореною
автоматизацією (без нього суму не можна додавати до портфеля).

## Assumptions

- $1 000/міс додаткового ефекту проєкту, 14 днів прискорення й один такий випадок за
  квартал — нові допущення 30.10.2026, а не статистика портфеля.
- Одна зайва підписка на $30/міс існує і її можна припинити без рівноцінної заміни.
- $20/міс хостингу і 4 год/міс підтримки за ставкою $15 з методики — припущення, не
  рахунки.
- Рішення за метриками справді ухвалюють і виконують; без прийняття бізнес-власником
  пілот не стартує.
- The owner's self-reported "time it used to take manually" is accurate enough to use
  as a baseline — it isn't independently verified against, say, old timesheets.
- An automation's ROI stays roughly constant once registered; nothing here re-checks
  whether the manual-process baseline is still realistic months later.

## Caveats
Most automations' ROI is a stated hypothesis, not an independently measured result,
and that's expected to stay the norm for a while — nothing here verifies a
self-reported baseline. But "no real numbers exist anywhere" isn't quite true today:
`ROIEntry` already supports `confidence="measured"` with a real value attached (the
`seed-demo` CLI command ships the Stage 0 sales-forecast automation with
`confidence="measured"`, `measured_value="9.8% MAPE"`, and its card renders a
"Виміряно" badge, not "Оцінка"). The registration form lets any owner flip the
Confidence dropdown to "Виміряно" directly — but the form has no `measured_value`
field at all (`automation_form.html` doesn't render one, and `apply_manual_form` in
`app.py` never reads or sets it), so an owner can only claim the badge through the UI,
not attach a number to it. The number itself can currently arrive via GitHub sync
(`github_sync.py`'s `roi_fields_from_sections`, reached through the browser's
`/automations/<slug>/resync` "Оновити з GitHub" button or the initial
`/automations/import-github` import), the API-key-authenticated
`/api/automations/<slug>/sync` route that stage-1-supplax's portfolio-sync step
calls (see "What else is on the card" above — its overwrite semantics differ from
GitHub sync's additive-only ones), or the `seed-demo` CLI. Nothing enforces the
badge/number pairing either way, though: `github_sync.py` flips confidence to "measured" purely by checking
whether the Confidence line's text starts with "measured" — it never checks that
Actual Results has any content. So a "Виміряно" badge means "someone wrote that
word," not "this number was independently checked." That's the real, ongoing bias
risk: an owner motivated to show their automation's value has no independent check
on the number they enter, whether the card says Estimated or Measured.

That "measured" check is also an English-keyword requirement, which sits at odds
with this repo's own convention for the file it's checking: `dashboard/ROI.md`'s
template comment tells authors to write the file "in Ukrainian on purpose", but
`github_sync.py`'s confidence check only recognizes a Confidence section whose
text literally starts with the English word "measured" (case-insensitive). A
fully Ukrainian phrasing like "Виміряно" would never flip confidence — the
current file only works because its own Confidence line happens to start with
the English word "Estimated" rather than a Ukrainian equivalent. This is a real
trap for exactly the Ukrainian-authoring workflow the template asks for, not
just the abstract self-report bias noted above.

The same unenforced coupling also breaks in the opposite direction on re-sync: the
GitHub-sync handler always takes the new `confidence` from `ROI.md` outright, but
falls back to the *old* stored `measured_value` whenever the new sync's value is
empty (see "What else is on the card" above). So an owner who edits `ROI.md` to move
Confidence back from Measured to Estimated — retracting a claim — gets a card whose
badge correctly flips to "Оцінка," while the big number keeps showing the stale
"measured" value from the previous sync, because the number display itself
(`automation_detail.html`'s `roi-number`) doesn't check confidence at all.

Окремо до цієї гіпотези: прискорення запуску перетинається з вікном користі самої
прискореної автоматизації, тому сума неадитивна щодо портфеля, доки не погоджено
правило розподілу спільного ефекту. Падіння сканера безпеки 29–30.10 через 401 —
сигнал для роботи, а не підстава стверджувати, що раніше на це витрачали якусь
конкретну кількість годин або що вже уникнули інциденту.

## Confidence — Why

Estimated. Жодного заміру ще не зроблено: цілі поставлені 30.10.2026, пілот стартує в
листопаді після прийняття бізнес-власником, baseline усіх метрик невідомий. Measured
стане можливим лише після першого заміру з точним періодом після дати запуску, яку ще
належить з'ясувати.

Навіть коли Estimated зміниться на Measured, це не прибирає ризик самозвіту з Caveats:
число, яке вписав власник автоматизації, лишається неперевіреним. Підтвердження ставить
адміністратор у дашборді, а не цей файл.

## Loose ends
- **`measured_at`** — `ROIEntry` has a `measured_at` timestamp column
  (`models.py`), but nothing in the codebase ever sets or reads it (no sync path,
  form field, or template references it). It's schema, not a live mechanism; don't
  assume a measurement date is tracked anywhere today. (Перевірено 30.10.2026: поле
  досі ніде не читається й не пишеться. Заміри метрик живуть окремо, у
  `MetricMeasurement`.)
