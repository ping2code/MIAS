"""Setup Evaluation (Phase 11): a deterministic, descriptive, prospective record of what each frozen Phase 10
candidate's quote showed at fixed later session horizons.

One SetupEvaluation per (TradeSetupAssessment, horizon), containing every original candidate in canonical order.
Inputs are sealed files only: the assessment, a later OptionsSnapshot, a sealed SessionSchedule, an evaluation
protocol and, optionally, InvalidationChecks. No ranking, labels, selection, tuning, sizing, targets or stops.
The pure core imports only the standard library and setup_evaluation; ``runner`` is the only I/O module.
See docs/phase11-setup-evaluation.md.
"""
