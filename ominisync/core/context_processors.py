"""Small template context helpers."""
from django.conf import settings


def global_context(request):
    return {
        "app_name": "OmniSync.ai",
        "gemini_configured": bool(settings.GEMINI_API_KEY),
        "google_configured": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET),
        "google_signin_redirect_uri": settings.GOOGLE_SIGNIN_REDIRECT_URI,
    }
