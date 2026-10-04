# Moving from NVIDIA-dev/literate-ai

Literate AI is now published from
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai). The former
`NVIDIA-dev/literate-ai` repository is archived and read-only. This page covers what
existing users need to do; [ADR 0049](../decisions/0049-repository-succession.md)
records the design.

## Installed CLI

Check how `litai` was installed with `litai --version` and `litai doctor`.

- **Prefix install (`make install`) on 1.0.x:** no action is needed once 1.1.0 is
  published. Releases 1.0.x check only the old repository, so 1.1.0 is also attached
  there once as a bridge. Your next self-update installs 1.1.0, and from then on updates
  come from the public repository. To move immediately, clone the public repository and
  run `make install` again with the same `PREFIX`.
- **Pip or editable install:** download the wheel from the
  [latest release](https://github.com/jordanhubbard/literate-ai/releases/latest) and
  install it with `python -m pip install path/to/WHEEL.whl` in the same environment.
- **1.1.0 or later:** self-update follows the declared move automatically, even when the
  wheel was built from an old clone.

## Projects initialized by an earlier release

Nothing in the project needs editing. `.literate/initialization-origin.json` keeps
naming the repository that initialized it; that is provenance. `litai update` from a
public-repository build recognizes the move and plans normally. A project initialized
from any other repository is still refused with `project.update_origin_changed`.

As with every upgrade, a changed installed framework no longer matches the project's
Standard pin. Plan, review and apply the rebind:

```sh
litai project lifecycle rebind-standard --project . --output rebind.json
litai project lifecycle rebind-standard rebind.json --project . --apply --authorize-rebind
```

See [Configuration and CLI reference](configuration-and-cli.md) for rebind details.

## Clones of the literate-ai repository

The public repository has no shared history with the old one. Do not push old branches
or tags to it.

```sh
git remote rename origin nvidia-dev          # keep the archived remote for reference
git remote add origin https://github.com/jordanhubbard/literate-ai.git
git fetch origin --tags --force              # replace old local release tags
git switch -C main origin/main
```

`--force` replaces local `v*` tags with the public historical markers. Each marker's
README names the original release commit in the archived repository. Port unfinished
work by committing its final tree onto `origin/main`, then run
`make public-export-check` before pushing; see
[Public repository export](public-export.md).

## Links in older documents

Issue, pull request, commit and CI-run links created before 2026-09-30 point at the
archived repository and keep their original numbers. Issues that were still open moved
to the public repository with new numbers; the work queue links to them.
