"""The website's cache revalidation call (``core.storefront``): ``POST /api/revalidate`` on the Next.js
storefront with the shared secret. ``ports`` (the contract), ``live`` (the HTTP adapter), ``fake`` (an
in-memory recorder: tests, and any deployment without the URL and the secret), ``registry`` (which one runs)
and ``tasks`` (the ``storefront.revalidate`` outbox handler)."""
