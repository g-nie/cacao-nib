# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-06-14

### Added

- Add cross-file rules via deferred diagnostics. Rules can now condition a finding
  on whether a module is imported elsewhere in the run. A visitor returns an
  `UnimportedDiagnostic` or `ImportedDiagnostic`, carrying a dotted `module` whose
  reachability decides the verdict.
- Add per-module import table and `resolve()` to rules. Give rules import-aware
  checks: `self.imports` is the current module's `{local name -> fully-qualified origin}`
  table, and `self.resolve(node)` qualifies a `Name`/`Attribute` chain through it
  (e.g. `np.array` -> `"numpy.array"`).
