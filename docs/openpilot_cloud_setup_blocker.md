# Openpilot cloud development setup: repository blocker report

## Result

The workspace supplied for this task is not an openpilot checkout.  It is the
`kaira` project: a Python package for Indian index-options data ingestion.  No
openpilot source, `tools/setup_dependencies.sh`, `uv.lock`, GitHub Actions
workflow, replay tool, or `AGENTS.md` file exists in this repository.  The
only Git repository found below `/workspace` is this one.

Because the requested setup must use the existing openpilot repository and its
supported scripts and pinned dependencies, do **not** apply openpilot setup
steps to this checkout.  Doing so would invent configuration for a different
project and would not be reproducible.

## Cloud environment audit

| Item | Observed value |
| --- | --- |
| Branch created for this assessment | `codex/cloud-dev-setup` |
| OS | Ubuntu 24.04.4 LTS (Noble), x86_64 |
| System Python | 3.14.4 |
| Docker CLI/daemon | Docker CLI is not installed |
| Free workspace disk at start | approximately 30 GB of 32 GB |
| Project Python requirement | `>=3.11` (from this repository's `pyproject.toml`) |
| Intended openpilot OS/Python | not determinable: the openpilot repository is absent |

The current repository's README documents a Windows PowerShell virtualenv
quickstart; it contains no Linux-distribution requirement or openpilot
instructions.  It has unconstrained lower-bound Python dependencies and no
lockfile, so there are no pinned openpilot dependencies to preserve here.

## Commands run and outcomes

The following commands were run from the repository root.  Installation output
was captured under `/tmp` and deliberately not added to Git.

```bash
git status --short --branch
rg --files -g 'AGENTS.md' -g 'README*' -g 'pyproject.toml' -g 'uv.lock' \
  -g 'setup_dependencies.sh' -g '*.yml' -g '*.yaml'
find /workspace -maxdepth 3 -type d -name .git -print
find . -maxdepth 4 -type f \( -name 'setup_dependencies.sh' -o -name 'uv.lock' \
  -o -name 'AGENTS.md' -o -path '*/.github/workflows/*' \) -print | sort
uname -a
cat /etc/os-release
df -h .
python3 --version
command -v uv
command -v docker
uv venv --clear --seed --python 3.11 .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q src
```

Creating a seeded CPython 3.11.13 virtual environment and installing this
repository's package succeeded.  The first unseeded `uv venv` attempt failed
only because that environment intentionally omitted `pip`; recreating it with
`--seed` allowed the README's `python -m pip install -e .` command to run.
This is a local tool behavior, not an openpilot dependency failure.

`pytest` and `compileall` did **not** complete successfully for the checked-out
`kaira` source: `src/kaira/providers/nse_bhavcopy.py` has a pre-existing syntax
error near line 303.  This assessment does not change that unrelated source
file.  No recorded data, model weights, or vehicle-facing workflows were
downloaded or run.

## Smallest next step

Provide or mount the intended openpilot Git checkout, including its Git
history, README, lockfiles, `tools/setup_dependencies.sh`, and CI workflows.
Then repeat the repository audit before any dependency change.  In particular:

1. Read the checkout's setup documentation and inspect its OS/Python pins,
   platform markers, and CI matrix.
2. Check disk availability before fetching any logs, route segments, model
   artifacts, or replay data.
3. Run the repository's documented Linux setup command and retain complete
   installation logs if it fails.
4. Inspect Docker availability before making Docker part of the workflow; it
   is unavailable in this Cloud runtime.
5. Build and run only the repository's non-hardware unit tests and a
   recorded-route/replay smoke test.  Do not connect vehicle CAN, steering,
   braking, or actuator hardware.

## Reproduction boundary and Intel Mac guidance

No openpilot workflow is validated by this branch, so there is no supported
openpilot command to reproduce yet.  Once the correct checkout is available,
the validated Linux commands, exact Python version, lockfile use, build result,
and replay command should be recorded verbatim in this document or the
repository's own setup documentation.

For the Intel macOS 13.7.8 machine, use it for editing, Git operations, code
review, and connecting to the Linux Cloud environment.  Do not claim native
dependency installation, compilation, or replay support until the actual
openpilot project's platform markers and CI-supported macOS targets have been
inspected.  The reported `comma-deps-acados` wheel issue remains unverified in
this workspace because no dependency declaration or lockfile referencing it is
present.
