"""The tests `thermite test` runs on a test openHAB (lib/oh_cli/test.py):
boot, routing and controls. Each module has SUMMARY, DESCRIPTION, EXAMPLES
and NOTES for its action's help, add_arguments(parser) for its switches,
and run(args) -> int, the exit status. They share the instance on
127.0.0.1:18081, so they run one at a time.
"""
