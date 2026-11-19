"""HashiCorp Vault KV v2 for runtime credentials (the AI assistant's provider keys): port, fake, live,
registry.

Django never holds a Vault token: the live adapter talks to a local Vault Agent ``api_proxy`` listener
(``use_auto_auth_token``), one agent per role — the api's writes new key versions, the worker's reads them
(infra ``feat/ai-vault``).
"""
