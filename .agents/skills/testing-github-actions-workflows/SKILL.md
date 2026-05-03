---
name: "testing-github-actions-workflows"
description: "Test GitHub Actions workflow YAML changes end-to-end with static validation, local shell simulation, and PR CI checks. Use when verifying CI/workflow changes such as secret handling in run blocks."
---

# Testing GitHub Actions Workflow Changes

Use this skill when a PR changes files under `.github/workflows/`, especially changes to shell `run:` blocks, secret handling, environment scope, or command arguments.

## Devin Secrets Needed

- `GH_TOKEN` or `GH_TOKEN_2`: needed only for authenticated PR/CI inspection through Devin git tooling.
- No application runtime secrets are needed for static workflow validation or local shell-block simulation.
- `TOKEN` and `PROFILE` are needed only for live Control D app sync runs, not workflow YAML testing.
- Do not use or print real GitHub secret values. Use sentinel values such as `SENTINEL_TOKEN_123` for local simulations.

## Procedure

1. Read the exact changed workflow lines and identify the changed job/step.
2. Write a short test plan before execution with pass/fail criteria for:
   - YAML parseability.
   - Expected step name and location.
   - Expected `env:` scope.
   - Expected command-line arguments in `run:`.
   - Absence of unsafe direct secret interpolation inside shell scripts.
   - Use of least-privilege `permissions` for `GITHUB_TOKEN`.
   - Absence of untrusted context variables directly interpolated into shell scripts.
   - Correctness of `if:` conditions and `concurrency` logic if modified.
3. Parse the workflow with Python/YAML rather than relying on text search alone.
4. For changed shell `run:` blocks, execute the extracted script locally with:
   - The specified shell and working directory, if defined in the YAML.
   - Safe sentinel environment variables.
   - Any required GitHub metadata variables such as `GITHUB_REPOSITORY_OWNER` and `GITHUB_REPOSITORY`.
   - Stub executables placed first on `PATH` for external commands like `github_changelog_generator`.
5. Have the stub record command-line arguments to a temporary file and assert the command received the expected arguments.
6. Re-check PR CI with Devin git tooling after local validation.
7. Re-check PR comments for actionable review/regression feedback.
8. Post one compact PR comment with:
   - Runtime assertions.
   - CI status.
   - Limitations, such as not triggering a live release workflow.
   - Devin session link.

## Token-handling assertions

For secret-hardening changes, validate all of the following:

- The secret expression, e.g. `${{ secrets.GITHUB_TOKEN }}`, appears only in an `env:` mapping at the intended scope.
- The shell `run:` block uses the environment variable, e.g. `$GITHUB_TOKEN`.
- The shell `run:` block does not contain the literal secret expression.
- The token env var is scoped as narrowly as possible, preferably to the single step that needs it.
- The workflow grants only the minimum token permissions needed for the changed steps.
- Untrusted contexts such as issue titles, PR titles, or PR bodies are not directly interpolated into shell scripts.
- A local shell simulation proves the command receives the sentinel token from the environment.

## Reporting

For shell-only workflow testing, do not create a screen recording. Include command output in `test-report.md` and attach it to the final user message.
