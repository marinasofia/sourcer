"""Onboarding: turn a candidate's own sources into approved evidence.

``sourcer init`` records the sources (resume, GitHub username, local project
folders, links, a LinkedIn data export), the collectors here read them into
short source items, ``extract`` asks the model for candidate facts that quote
those items, and ``sourcer facts review`` lets the candidate approve each fact.
Approved facts are written into the evidence file the scorer already reads.
Everything is stored in local files; see docs/privacy.md.
"""
