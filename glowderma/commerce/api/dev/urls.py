"""Development routes (customer contract §2.15), mounted at ``api/v1/dev/`` only under the local/test settings
with fake payments.
"""

from django.urls import URLPattern, URLResolver

urlpatterns: list[URLPattern | URLResolver] = []
