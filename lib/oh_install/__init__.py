"""Install openHAB on the host that runs bin/thermite install, as the
owner's user, from the selected private site or complete source checkout.

The install is a list of steps (steps.py). Each step first reads the host
(system.py) to find out whether it is done; if it is not, it lists the
commands that do it (model.py). The runner (runner.py) reports each stage,
uses the owner's single approval for the run, runs it, and checks the step
again, so a rerun skips what is done and resumes where the last run
stopped. Nested deployment reuses that approval for its ownership handoff.
Verbose mode shows commands and check details. Quiet subprocess capture is
limited to noninteractive commands; password/package prompts retain their
terminal. Noninteractive root helpers authenticate first, then run under
sudo -n with progress active. Progress painting never probes a service.
Nothing here needs PyYAML at import: the install starts on a host
that may not have it yet.
"""
