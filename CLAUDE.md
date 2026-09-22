# CLAUDE.md

## Commit messages: Ukrainian subject line

Write commit subject lines for this repo in Ukrainian, not the usual English
convention. The history here is already entirely Ukrainian, and this repo *is*
the portfolio dashboard, whose UI is Ukrainian end to end — an English subject
in `git log` reads as language-mixing inside the one product that is itself
written in Ukrainian.

The rule started out with a stronger reason: the dashboard's "Зараз" panel
rendered each automation's last commit subject verbatim, so this repo's own
card showed its own commit messages to users. That panel and the fields behind
it are gone, and nothing displays a commit subject any more — the convention is
kept now for a consistent log, not for a reader.

This is specific to this repo, not a rule for automations registered *in* the
portfolio — their repos keep normal English commit conventions.
