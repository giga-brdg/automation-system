# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]
### Added
- Initial scaffold.
- Власник щойно знайденої автоматизації визначається за головним contributor'ом
  репозиторію: адміністратор проставляє людині GitHub-логін на її сторінці, і
  скан віддає репозиторій їй. `AUTOMATION_SYNC_OWNER_EMAIL` лишається запасним
  варіантом. Потребує `flask migrate-github-username`.
- Адміністратор може передати автоматизацію іншому власнику прямо з її сторінки.
### Changed
### Deprecated
### Removed
- Картки «Статус» і «Зараз» зі сторінки автоматизації, разом із полями, що їх
  живили (`last_synced_at`, `last_commit_message`, `last_commit_at`,
  `current_stage_override`), викликом `github_sync.fetch_latest_commit()` і
  розбором секції `## Current Stage` у `dashboard/SUMMARY.md`. Колонки
  лишились у БД: міграції в проєкті тільки додавальні.
### Fixed
### Security
