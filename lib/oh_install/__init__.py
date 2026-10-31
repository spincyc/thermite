"""Install openHAB on the host that runs bin/thermite install, as the
owner's user, from wherever the owner's clone is.

The install is a list of steps (steps.py). Each step first reads the host
(system.py) to find out whether it is done; if it is not, it lists the
commands that do it (model.py). The runner (runner.py) shows every command,
asks before each one that runs under sudo, runs it, and checks the step
again, so a rerun skips what is done and resumes where the last run
stopped. Nothing here needs PyYAML at import: the install starts on a host
that may not have it yet.
"""
