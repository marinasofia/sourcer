"""The pipeline: source -> dedupe -> parse -> score -> output, wired by ``graph``.

``state`` holds the shared state type, ``filters`` the deterministic policy
(exclusions, timing flag, level fit), ``scoring`` the model stage and the
scoring service shared by HTTP and MCP.
"""
