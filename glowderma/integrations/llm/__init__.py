"""Content-generation LLMs (AI product content assistant): the port, the fake, the Gemini adapter, the
registry.

Business logic (``glowderma.assistant``) talks only to :class:`.ports.ContentModel`; another provider
(OpenAI, Anthropic) is one more adapter module and one registry entry.
"""
