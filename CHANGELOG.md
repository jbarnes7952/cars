# Changelog

One entry per tagged release. Versions match `.claude-plugin/plugin.json`
and the `vX.Y.Z` git tag. Releases before 1.18.0 are untagged; see the git
log for them.

## 1.18.4

- Presence checks a declared pid with `os.kill(pid, 0)` instead of looking
  for `/proc/<pid>`. macOS has no procfs, so every pid there read as dead:
  pid-only peers were never `live`, and running sessions' `<pid>.sock` rows
  could be evicted. Linux behaviour is unchanged. Windows is still not
  supported, since signal 0 there sends Ctrl-C. (#26)

## 1.18.3

- The serve process's registry poll holds one read connection open instead
  of opening and closing one every cycle. SQLite no longer creates and
  deletes the WAL side files on each poll, which was a directory write every
  second or two across a fleet. (#18)

## 1.18.2

- Hooks never take a row carrying another session's `session_id` for their
  own, and SessionEnd never deletes on a shared `cwd`. An unregistered
  session exiting next to a registered peer used to delete the peer's row.
  (#25, fixes #23)

## 1.18.1

- Plugin author and marketplace owner are "The Ark Project". README installs
  via the ark-project catalog. A test rejects home-directory paths in any
  tracked file. (#24)

## 1.18.0

- SessionStart titles an untitled session `<project>-<sid[:4]>` and labels
  the tmux window once, behind `LEDGER_SESSION_TITLE` and
  `LEDGER_TMUX_RELABEL`. The register nudge passes that title as
  `session_name`. The project-driven tmux relabel hook is gone. (#20)
