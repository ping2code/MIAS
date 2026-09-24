"""MIAS scheduler/orchestration (Phase 3): runs existing collectors as isolated child processes.

The orchestrator never imports collector modules or ``shared.config`` (so it never loads
``.env``); it only knows each collector's name, command, cadence, timeout and policy.
"""
