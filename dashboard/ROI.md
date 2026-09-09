# ROI / Value

<!-- Machine-facing contract for the portfolio dashboard's GitHub sync (see
     src/github_sync.py). Not for humans browsing the repo - docs/roi_explained.md
     is the real methodology. Written in Ukrainian on purpose: this text renders
     directly inside the dashboard's Ukrainian-language UI, next to labels like
     "Оцінка"/"Виміряно" - condense docs/roi_explained.md's English methodology
     into short Ukrainian captions here, don't just translate it verbatim, and
     keep Hypothesis/How We'll Measure It short - they render in a 300px sidebar
     panel, not a full-width block. Regenerate via automation-portfolio-sync,
     don't hand-edit this prose. -->

## Hypothesis
У Supplax автоматизації досі губилися між GitHub, ClickUp і усною передачею — за рік
ніхто вже не пам'ятав, навіщо їх зробили. Дашборд збирає статус і ROI кожної
автоматизації в одній картці — і для керівництва, і для власників.

## How We'll Measure It
Час на ручний збір статусу автоматизації по GitHub, ClickUp і чату проти одного кліка
синхронізації — за оцінкою власника.

## Confidence
Estimated — реальних даних ще немає.

## Actual Results
<!-- Empty at bootstrap - fills in as real data comes in. -->

## Qualitative Value
- Прозорість: усі автоматизації видно в одному місці, а не розкидані по GitHub, ClickUp і усній передачі.
- Синхронізація з GitHub одним кліком підтягує дані з 6 файлів репозиторію замість ручного переписування.
- Позначка "Виміряно" проти "Оцінка" не дає видати здогадку за перевірений факт — картка стає "Виміряно" лише коли власник прямо це написав.
- За оцінкою, ручний збір статусу автоматизації по GitHub, ClickUp і чату займав приблизно 10-15 хвилин — картка дає це за секунди.
- Токен-бюджет і сплеск-алерти вже працюють: Telegram-повідомлення при 80%/100% бюджету і при різкому сплеску витрат, а не сюрприз у рахунку наприкінці місяця.
- Швидша реакція на збої автоматизацій — коли запрацюють Telegram-алерти про самі падіння (Next).

## Presentation
https://claude.ai/code/artifact/213b713a-e7df-4e63-9f87-8afd094420e2
