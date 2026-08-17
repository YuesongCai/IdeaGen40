"""Strategy plugins. Every module here is imported by the registry at load time.

Adding a strategy is adding a file — no edit to the pipeline, no import list to
maintain. Removing one is deleting a file, and any book it filled stays traceable
because every stored verdict carries the strategy name and version.
"""
