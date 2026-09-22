# Changelog
All notable changes to this project will be documented in this file.

## [Unreleased]
### Added
- Initial scaffold.
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
