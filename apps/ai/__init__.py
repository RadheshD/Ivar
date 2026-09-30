"""Intent layer: turns a WhatsApp message into a validated, structured intent.

Pipeline: reply ID -> rules/aliases -> difflib fuzzy matching -> LLM fallback -> validation.
This package never talks to the Backend Port; it returns raw entity text that the
conversation engine resolves.
"""
