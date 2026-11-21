"""Page views for the Django port of OmniSync."""
from __future__ import annotations

import secrets
import urllib.parse
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render

from .api import _connect_gmail_code
from .services import (
    alert_out,
    briefing_out,
    dashboard_brief,
    get_or_create_profile,
    http_json,
    schedule_out,
    serialize_chat,
)


TAB_TITLES = {
    "dashboard": "Dashboard",
    "ai-agent": "AI Agent",
    "briefing": "Briefing",
    "inbox": "Inbox & Synced Chats",
    "follow-ups": "Follow-Ups Tracker",
    "integrations": "Integrations",
    "alerts": "Alerts",
    "settings": "Settings",
    "pricing": "Pricing Settings",
}

DEFAULT_SETTINGS = {
    "roleContext": "Personal productivity, communication triage, and executive follow-ups",
    "detailLevel": "standard",
    "responseTone": "friendly",
    "autoDraftReplies": True,
    "proactiveSuggestions": True,
    "briefingCadence": "morning",
    "briefingChannels": ["in_app", "email"],
    "syncFrequency": "real_time",
    "alertPriority": "medium_high",
    "alertMethods": ["in_app"],
    "dataRetention": "90_days",
    "saveAiMemory": True,
    "shareUsageAnalytics": False,
}


def _static_root() -> str:
    return str(settings.STATICFILES_DIRS[0])


def root_asset(request, path: str):
    root = Path(_static_root()).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise Http404("Asset not found")
    return FileResponse(target.open("rb"))


def root_icon(request, path: str):
    return root_asset(request, f"icons/{path}")


def favicon(request):
    return root_asset(request, "icons/icon-192.png")


def landing(request):
    return render(request, "landing.html", {"page": "landing"})


def sign_up(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""
        if not email or not password:
            messages.error(request, "Email and password are required.")
        elif User.objects.filter(username=email).exists():
            messages.error(request, "An account with this email already exists.")
        else:
            candidate = User(username=email, email=email, first_name=name)
            try:
                validate_password(password, user=candidate)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                candidate.set_password(password)
                candidate.save()
                profile = get_or_create_profile(candidate)
                profile.name = name or profile.name
                profile.auth_provider = "email"
                profile.save(update_fields=["name", "auth_provider"])
                login(request, candidate, backend="django.contrib.auth.backends.ModelBackend")
                return redirect("dashboard")
    return render(request, "sign_up.html", {"page": "sign-up"})


def sign_in(request):
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=email, password=password)
        if user is None:
            user = authenticate(request, username=email.lower(), password=password)
        if user is not None:
            login(request, user)
            get_or_create_profile(user)
            return redirect(request.GET.get("next") or "dashboard")
        messages.error(request, "Failed to sign in. Please verify your credentials.")
    return render(request, "sign_in.html", {"page": "sign-in"})


def sign_out(request):
    logout(request)
    return redirect("landing")


def google_sign_in(request):
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        messages.error(
            request,
            "Google sign-in is not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to the project .env file, "
            "authorize " + settings.GOOGLE_SIGNIN_REDIRECT_URI + " in Google Cloud Console, and restart the server.",
        )
        return redirect("sign-in")
    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state
    query = urllib.parse.urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_SIGNIN_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "include_granted_scopes": "true",
        "state": state,
    })
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")


def auth_callback(request):
    provider = request.GET.get("provider")
    if provider == "google":
        error = request.GET.get("error")
        code = request.GET.get("code")
        state = request.GET.get("state")
        expected_state = request.session.pop("google_oauth_state", "")
        if error or not code or not state or not expected_state or not secrets.compare_digest(str(expected_state), state):
            return render(request, "auth_callback.html", {
                "page": "auth-callback",
                "success": False,
                "error": error or "Invalid or expired Google sign-in state. Please try signing in again.",
            })
        redirect_uri = settings.GOOGLE_SIGNIN_REDIRECT_URI
        status, token_data = http_json("https://oauth2.googleapis.com/token", "POST", data=urllib.parse.urlencode({
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200 or not isinstance(token_data, dict) or not token_data.get("access_token"):
            return render(request, "auth_callback.html", {
                "page": "auth-callback",
                "success": False,
                "error": "Could not retrieve your user session. Please try signing in again.",
            })
        status, info = http_json(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        email = str((info or {}).get("email") or "").lower() if status == 200 and isinstance(info, dict) else ""
        verified = str(info.get("email_verified", "true")).lower() if isinstance(info, dict) else "true"
        if not email or verified in {"false", "0", "no"}:
            return render(request, "auth_callback.html", {
                "page": "auth-callback",
                "success": False,
                "error": "Could not verify your Google account. Please try signing in again.",
            })
        user = User.objects.filter(username=email).first()
        if not user:
            user = User.objects.create_user(
                username=email,
                email=email,
                first_name=str(info.get("given_name") or ""),
                last_name=str(info.get("family_name") or ""),
            )
            user.set_unusable_password()
            user.save()
        profile = get_or_create_profile(user)
        profile.auth_provider = "google"
        profile.name = str(info.get("name") or profile.name)
        profile.save(update_fields=["auth_provider", "name"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("dashboard")
    success = bool(request.user.is_authenticated)
    return render(request, "auth_callback.html", {
        "page": "auth-callback",
        "success": success,
        "error": "" if success else "Could not retrieve your user session. Please try signing in again.",
    })


def gmail_callback(request):
    status = "loading"
    error_message = ""
    code = request.GET.get("code")
    error_param = request.GET.get("error")
    if error_param:
        status = "error"
        error_message = error_param or "Access denied by Google."
    elif not code:
        status = "error"
        error_message = "No authorization code received from Google."
    elif not request.user.is_authenticated:
        status = "error"
        error_message = "Please sign in before connecting Gmail."
    else:
        expected_state = request.session.pop("gmail_oauth_state", None)
        state = request.GET.get("state") or ""
        if not expected_state or not secrets.compare_digest(str(expected_state), state):
            status = "error"
            error_message = "Invalid or expired Gmail OAuth state. Please start the connection again."
        else:
            try:
                profile = get_or_create_profile(request.user)
                _connect_gmail_code(profile, code)
                status = "success"
            except Exception as exc:
                status = "error"
                error_message = str(exc) or "An error occurred during account sync."
    return render(request, "gmail_callback.html", {
        "page": "gmail-callback",
        "status": status,
        "error_message": error_message,
        "redirect_url": "/dashboard?tab=integrations",
    })


def pair(request):
    return render(request, "pair.html", {
        "page": "pair",
        "server_url": request.build_absolute_uri("/").rstrip("/"),
        "api_key": settings.MOBILE_API_KEY,
    })


def offline(request):
    return render(request, "offline.html", {"page": "offline"})


@login_required
def dashboard(request):
    profile = get_or_create_profile(request.user)
    active_tab = request.GET.get("tab") or "dashboard"
    if active_tab not in TAB_TITLES:
        active_tab = "dashboard"
    assistant_settings = dict(DEFAULT_SETTINGS)
    assistant_settings.update(profile.assistant_settings or {})
    context = {
        "page": "dashboard",
        "profile": profile,
        "user_id": str(profile.id),
        "active_tab": active_tab,
        "tab_title": TAB_TITLES[active_tab],
        "connected_apps": profile.connected_apps(),
        "gmail_connected": "gmail" in profile.connected_apps(),
        "gmail_simulated": bool(profile.integration("gmail").get("isSimulated")),
        "whatsapp_connected": "whatsapp" in profile.connected_apps(),
        "whatsapp_simulated": bool(profile.integration("whatsapp").get("isSimulated")),
        "brief": dashboard_brief(profile),
        "chat_messages": serialize_chat(profile),
        "briefings": [briefing_out(item) for item in profile.generated_briefings.all()[:10]],
        "schedules": [schedule_out(item) for item in profile.briefing_schedules.all()[:10]],
        "alerts": [alert_out(item) for item in profile.alerts.all()[:50]],
        "assistant_settings": assistant_settings,
        "gmail_client_id": settings.GOOGLE_CLIENT_ID,
        "categories": ["email", "messages", "mentions", "tasks", "follow_ups"],
    }
    return render(request, "dashboard.html", context)


@login_required
def briefing_detail(request, briefing_id):
    profile = get_or_create_profile(request.user)
    try:
        briefing = profile.generated_briefings.filter(pk=briefing_id).first()
    except Exception:
        briefing = None
    if not briefing:
        return render(request, "briefing_detail.html", {
            "page": "briefing-detail",
            "profile": profile,
            "user_id": str(profile.id),
            "briefing": None,
            "not_found": True,
        }, status=404)
    category = request.GET.get("category") or "email"
    categories = [
        {"key": "email", "label": "Email"},
        {"key": "messages", "label": "Messages"},
        {"key": "mentions", "label": "Mentions"},
        {"key": "tasks", "label": "Tasks"},
        {"key": "follow_ups", "label": "Follow-Ups"},
    ]
    if category not in [item["key"] for item in categories]:
        category = "email"
    return render(request, "briefing_detail.html", {
        "page": "briefing-detail",
        "profile": profile,
        "user_id": str(profile.id),
        "briefing": briefing_out(briefing),
        "not_found": False,
        "category": category,
        "categories": categories,
        "app_logos": {
            "gmail": "/001-gmail.png",
            "whatsapp": "/002-whatsapp.png",
            "telegram": "/004-telegram.png",
            "slack": "/005-slack.png",
        },
    })
