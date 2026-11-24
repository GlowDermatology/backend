"""The OpenAPI declarations of the customer API's ``customers`` routes, one module per views module: each
``extend_schema`` is a named constant (``<VIEW>_<METHOD>``) the view decorates its handler with; ``common``
holds the shared header and path parameters, ``delete_body`` the schema class of the ``DELETE`` routes that
carry a body."""
