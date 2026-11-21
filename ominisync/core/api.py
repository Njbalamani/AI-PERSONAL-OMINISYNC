"""JSON API views for the Django port of OmniSync."""
from __future__ import annotations

import json
import secrets
import urllib.parse

from django.conf import settings
from django.contrib.auth import login
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Alert, UserProfile, WaitlistEntry
from .services import (
    McpError,
    ai_chat_response,
    ai_draft,
    alert_ai,
    alert_out,
    apply_alert_action,
    briefing_out,
    clear_chat,
    connect_simulated_gmail,
    create_alert_rule,
    create_briefing,
    create_schedule,
    dashboard_brief,
    generate_alerts,
    generate_suggestions,
    get_or_create_profile,
    gmail_mcp,
    http_json,
    join_waitlist,
    now_iso,
    request_profile,
    rule_out,
    save_chat,
    schedule_out,
    send_otp,
    serialize_chat,
    verify_otp,
    whatsapp_connect,
    whatsapp_mcp,
)
from . import ml as ml_service


def _json_body(request) -> dict:
    cached = getattr(request, "json_payload", None)
    if isinstance(cached, dict):
        return cached
    payload: dict = {}
    if request.body:
        try:
            parsed = json.loads(request.body.decode("utf-8"))
            if isinstance(parsed, dict):
                payload = parsed
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
    request.json_payload = payload
    return payload


def _response(payload, status: int = 200) -> JsonResponse:
    return JsonResponse(payload, status=status, safe=not isinstance(payload, list))


def _error(message: str, status: int = 400) -> JsonResponse:
    return _response({"error": message}, status)


def _profile(request):
    return request_profile(request)


def _gmail_redirect_uri() -> str:
    return settings.GOOGLE_REDIRECT_URI or f"{settings.APP_URL.rstrip('/')}/auth/gmail-callback"


def _gmail_auth_url(request) -> tuple[str, str]:
    redirect_uri = _gmail_redirect_uri()
    state = secrets.token_urlsafe(32)
    request.session["gmail_oauth_state"] = state
    query = urllib.parse.urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.modify",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}", redirect_uri


def _gmail_account_email(access_token: str) -> str:
    status, data = http_json(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        "GET",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if status == 200 and isinstance(data, dict):
        return str(data.get("emailAddress") or "")
    return ""


def _revoke_gmail_token(profile: UserProfile) -> None:
    gmail = profile.integration("gmail")
    if gmail.get("isSimulated"):
        return
    token = gmail.get("refreshToken") or gmail.get("accessToken")
    if not token:
        return
    try:
        http_json(
            "https://oauth2.googleapis.com/revoke",
            "POST",
            data=urllib.parse.urlencode({"token": token}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except Exception:
        pass


def _connect_gmail_code(profile: UserProfile, code: str) -> dict:
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise RuntimeError(
            "Google OAuth credentials (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET) are not configured in the server environment."
        )
    status, data = http_json(
        "https://oauth2.googleapis.com/token",
        "POST",
        data=urllib.parse.urlencode({
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _gmail_redirect_uri(),
            "grant_type": "authorization_code",
        }).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200 or not isinstance(data, dict):
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error_description") or data.get("error") or "")
        raise RuntimeError(f"Google OAuth code exchange failed: {detail}".rstrip(": "))
    access_token = str(data.get("access_token") or "")
    if not access_token:
        raise RuntimeError("Google OAuth response did not include an access token.")
    gmail = profile.integration("gmail")
    try:
        email = _gmail_account_email(access_token)
    except Exception:
        email = ""
    profile.set_integration("gmail", {
        "connected": True,
        "accessToken": access_token,
        "refreshToken": data.get("refresh_token") or gmail.get("refreshToken", ""),
        "expiresAt": timezone.now().timestamp() * 1000 + int(data.get("expires_in") or 3600) * 1000,
        "tokenType": str(data.get("token_type") or "Bearer"),
        "scope": str(data.get("scope") or "https://www.googleapis.com/auth/gmail.modify"),
        "isSimulated": False,
        "email": email or "Connected Gmail account",
        "connectedAt": now_iso(),
    })
    return {"success": True, "email": email or "Connected Gmail account"}


def _mcp_envelope(method, params, request_id, result=None, error=None) -> dict:
    response_frame = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response_frame["error"] = error
    else:
        response_frame["result"] = result
    mcp_result = None
    if result is not None:
        mcp_result = {"content": [{"type": "text", "text": json.dumps(result)}]}
    return {
        "responseFrame": response_frame,
        "result": mcp_result,
        "error": error,
    }


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_gmail_connect(request):
    if request.method == "GET":
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            return _response({
                "clientId": "", "authUrl": "", "redirectUri": _gmail_redirect_uri(),
                "configured": False,
            })
        auth_url, redirect_uri = _gmail_auth_url(request)
        return _response({
            "clientId": settings.GOOGLE_CLIENT_ID,
            "authUrl": auth_url,
            "redirectUri": redirect_uri,
            "configured": True,
        })

    payload = _json_body(request)
    profile = _profile(request)
    if not profile:
        return _error("User ID is required.")
    if payload.get("simulate") is True:
        return _response(connect_simulated_gmail(profile))

    expected_state = request.session.pop("gmail_oauth_state", None)
    state = payload.get("state")
    if not expected_state or not state or state != expected_state:
        return _error("Invalid or expired Gmail OAuth state.", 400)
    code = str(payload.get("code") or "").strip()
    if not code:
        return _error("Authorization code is required.", 400)
    try:
        result = _connect_gmail_code(profile, code)
    except RuntimeError as exc:
        return _error(str(exc), 500)
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)
    return _response(result)


@csrf_exempt
@require_http_methods(["POST"])
def api_gmail_mcp(request):
    payload = _json_body(request)
    method = payload.get("method")
    request_id = payload.get("id", 1)
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    authenticated = bool(getattr(request, "user", None) and request.user.is_authenticated)
    if not method or not (payload.get("userId") or authenticated):
        return _response({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request: method and userId are required"},
            "id": request_id,
        }, status=400)
    profile = _profile(request)
    if not profile:
        return _response({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request: method and userId are required"},
            "id": request_id,
        }, status=400)
    request_frame = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
    try:
        result = gmail_mcp(profile, method, params)
        envelope = _mcp_envelope(method, params, request_id, result=result)
    except McpError as exc:
        envelope = _mcp_envelope(method, params, request_id, error={"code": exc.code, "message": exc.message})
    except Exception as exc:
        envelope = _mcp_envelope(
            method, params, request_id,
            error={"code": -32603, "message": str(exc) or "Internal Server Error"},
        )
    return _response({"requestFrame": request_frame, **envelope})


@csrf_exempt
@require_http_methods(["POST"])
def api_whatsapp_mcp(request):
    payload = _json_body(request)
    method = payload.get("method")
    request_id = payload.get("id", 1)
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    authenticated = bool(getattr(request, "user", None) and request.user.is_authenticated)
    if not method or not (payload.get("userId") or authenticated):
        return _response({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request: method and userId are required"},
            "id": request_id,
        }, status=400)
    profile = _profile(request)
    if not profile:
        return _response({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request: method and userId are required"},
            "id": request_id,
        }, status=400)
    try:
        result = whatsapp_mcp(profile, method, params)
        envelope = _mcp_envelope(method, params, request_id, result=result)
    except McpError as exc:
        if exc.code in (-32001, -32002):
            return _response({
                "jsonrpc": "2.0",
                "error": {"code": exc.code, "message": exc.message},
                "id": request_id,
            }, status=400 if exc.code == -32001 else 503)
        envelope = _mcp_envelope(method, params, request_id, error={"code": exc.code, "message": exc.message})
    except Exception as exc:
        envelope = _mcp_envelope(
            method, params, request_id,
            error={"code": -32603, "message": str(exc) or "Internal Server Error"},
        )
    return _response(envelope)


@csrf_exempt
@require_http_methods(["POST"])
def api_whatsapp_connect(request):
    payload = _json_body(request)
    if not payload.get("userId"):
        return _error("User ID is required.")
    profile = _profile(request)
    if not profile:
        return _error("User ID is required.")
    action = payload.get("action")
    phone_number = payload.get("phoneNumber") or ""
    if action == "connect" and not phone_number:
        return _error("Phone number is required for connection.")
    try:
        return _response(whatsapp_connect(profile, action or "status", phone_number))
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["POST"])
def api_ai_chat(request):
    payload = _json_body(request)
    if not payload.get("userId") or not payload.get("prompt"):
        return _error("User ID is required" if not payload.get("userId") else "Prompt is required")
    profile = _profile(request)
    if not profile:
        return _error("User not found", 404)
    prompt = str(payload.get("prompt")).strip()
    if not prompt:
        return _error("Prompt is required")
    history = payload.get("history")
    if not isinstance(history, list):
        history = serialize_chat(profile)
    try:
        result = ai_chat_response(profile, prompt, history[-10:])
    except RuntimeError:
        return _error("Failed to generate response", 500)
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)
    save_chat(profile, "user", prompt)
    suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
    save_chat(profile, "agent", result.get("response", ""), suggestions)
    return _response({
        "response": result.get("response", ""),
        "suggestions": suggestions,
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_dashboard_brief(request):
    payload = _json_body(request)
    if not payload.get("userId"):
        return _error("User ID is required")
    profile = _profile(request)
    if not profile:
        return _error("User not found or database error", 404)
    try:
        brief = dict(dashboard_brief(profile, force=bool(payload.get("forceRegenerate"))))
        brief.setdefault("isFromCache", False)
        return _response(brief)
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_briefings(request):
    profile = _profile(request)
    if request.method == "GET":
        user_id = request.GET.get("userId")
        if not user_id:
            return _error("User ID is required")
        if not profile:
            return _error("User not found or database error", 404)
        briefing_id = request.GET.get("id")
        if briefing_id:
            try:
                briefing = profile.generated_briefings.filter(pk=briefing_id).first()
            except Exception:
                briefing = None
            if not briefing:
                return _error("Briefing not found", 404)
            return _response(briefing_out(briefing))
        rows = [briefing_out(item) for item in profile.generated_briefings.all()]
        return _response(rows)
    payload = _json_body(request)
    if not payload.get("userId"):
        return _error("User ID is required")
    if not profile:
        return _error("User not found or database error", 404)
    if not profile.connected_apps(monitorable_only=True):
        return _error("No connected apps. Please connect Gmail or WhatsApp from Integrations.")
    try:
        briefing = create_briefing(profile, payload.get("scheduleId"))
        return _response(briefing_out(briefing))
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
def api_briefing_schedules(request):
    payload = _json_body(request)
    user_id = payload.get("userId") or request.GET.get("userId")
    if not user_id:
        return _error("User ID and Schedule ID are required" if request.method == "DELETE" else "User ID is required")
    profile = _profile(request)
    if not profile:
        return _error("User not found or database error", 404)
    if request.method == "GET":
        return _response([schedule_out(item) for item in profile.briefing_schedules.all()])
    if request.method == "DELETE":
        schedule_id = payload.get("id") or request.GET.get("id")
        if not schedule_id:
            return _error("User ID and Schedule ID are required")
        deleted, _ = profile.briefing_schedules.filter(pk=schedule_id).delete()
        if not deleted:
            return _error("Schedule not found", 404)
        return _response({"success": True})
    required = ("name", "scheduledTime", "frequency", "priorityLevel")
    if any(not payload.get(field) for field in required):
        return _error("Required fields: userId, name, scheduledTime, frequency, priorityLevel")
    try:
        schedule = create_schedule(profile, payload)
        return _response(schedule_out(schedule))
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["POST"])
def api_briefing_ai_draft(request):
    payload = _json_body(request)
    if not (payload.get("context") or payload.get("body")):
        return _error("Context or body is required")
    try:
        result = ai_draft(payload)
        return _response(result)
    except RuntimeError as exc:
        message = str(exc)
        status = 503 if message == "AI service not configured" else 500
        return _error(message, status)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["GET"])
def api_alerts(request):
    user_id = request.GET.get("userId")
    if not user_id:
        return _error("User ID is required")
    profile = _profile(request)
    if not profile:
        return _error("User not found or database error", 404)
    rows = [alert_out(item) for item in profile.alerts.all()[:50]]
    return _response(rows)


@csrf_exempt
@require_http_methods(["GET"])
def api_alert_suggestions(request):
    user_id = request.GET.get("userId")
    if not user_id:
        return _error("User ID is required")
    profile = _profile(request)
    if not profile:
        return _error("User not found or database error", 404)
    try:
        return _response(generate_suggestions(profile))
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["POST"])
def api_alerts_generate(request):
    payload = _json_body(request)
    if not payload.get("userId"):
        return _error("User ID is required")
    profile = _profile(request)
    if not profile:
        return _error("User not found or database error", 404)
    try:
        result = generate_alerts(profile)
        return _response({"success": True, **result})
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["POST"])
def api_alert_rules(request):
    payload = _json_body(request)
    if not payload.get("userId"):
        return _error("Required fields: userId, name, apps, condition")
    profile = _profile(request)
    if not profile:
        return _error("User not found or database error", 404)
    apps = payload.get("apps")
    if not payload.get("name") or not payload.get("condition") or not isinstance(apps, list) or not apps:
        return _error("Required fields: userId, name, apps, condition")
    try:
        rule = create_alert_rule(profile, payload)
        return _response(rule_out(rule))
    except Exception as exc:
        return _error(str(exc) or "Internal Server Error", 500)


@csrf_exempt
@require_http_methods(["POST"])
def api_alert_actions(request):
    payload = _json_body(request)
    if not all(payload.get(field) for field in ("userId", "alertId", "action")):
        return _error("Required fields: userId, alertId, action")
    profile = _profile(request)
    if not profile:
        return _error("User not found or database error", 404)
    alert = apply_alert_action(profile, str(payload.get("alertId")), str(payload.get("action")))
    if not alert:
        return _error("Alert not found", 404)
    return _response(alert_out(alert))


@csrf_exempt
@require_http_methods(["POST"])
def api_alerts_ai(request):
    payload = _json_body(request)
    if not payload.get("alertId") or not payload.get("feature"):
        return _error("Required fields: alertId, feature")
    feature = str(payload.get("feature"))
    if feature not in ("summary", "next_action", "reply"):
        return _error("Invalid feature requested")
    profile = _profile(request)
    try:
        alert = profile.alerts.filter(pk=payload.get("alertId")).first() if profile else None
        if not alert:
            alert = Alert.objects.filter(pk=payload.get("alertId")).first()
    except Exception:
        alert = None
    if not alert:
        return _error("Alert not found", 404)
    result = alert_ai(alert, feature, str(payload.get("replyContext") or ""))
    return _response(result)


@csrf_exempt
@require_http_methods(["POST"])
def api_phone_send_otp(request):
    payload = _json_body(request)
    phone = str(payload.get("phoneNumber") or "").strip()
    method = str(payload.get("method") or "").strip()
    if not phone or not method:
        return _error("Phone number and method are required")
    try:
        result = send_otp(phone, method)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc) or "Failed to send OTP", 500)
    label = "WhatsApp" if method == "whatsapp" else "SMS"
    result["message"] = (
        f"Local mode: use the displayed OTP to continue via {label}."
        if not result.get("sent")
        else f"OTP sent via {label}"
    )
    result["localMode"] = not result.get("sent")
    return _response(result)


@csrf_exempt
@require_http_methods(["POST"])
def api_phone_verify_otp(request):
    payload = _json_body(request)
    phone = str(payload.get("phoneNumber") or "").strip()
    otp = str(payload.get("otp") or "").strip()
    method = str(payload.get("method") or "").strip()
    if not phone or not otp or not method:
        return _error("Phone number, OTP, and method are required")
    try:
        result = verify_otp(phone, otp, method, str(payload.get("name") or ""))
    except ValueError as exc:
        return _error(str(exc), 401)
    except Exception as exc:
        return _error(str(exc) or "Failed to verify OTP", 500)
    profile = UserProfile.objects.select_related("user").get(pk=result["id"])
    login(request, profile.user, backend="django.contrib.auth.backends.ModelBackend")
    return _response({
        "success": True,
        "user": {
            "id": str(profile.id),
            "name": profile.name,
            "phone": profile.phone,
            "auth_provider": "phone",
            "verification_method": profile.verification_method,
        },
        "isNewUser": bool(result.get("isNewUser")),
    })


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_mobile_connect(request):
    payload = _json_body(request)
    supplied = (
        request.headers.get("X-API-Key")
        or request.GET.get("key")
        or payload.get("key")
        or payload.get("apiKey")
    )
    if not supplied or supplied != settings.MOBILE_API_KEY:
        return _error("Invalid mobile API key.", 401)
    return _response({
        "success": True,
        "app": "omniscyn",
        "url": settings.APP_URL.rstrip("/"),
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_waitlist(request):
    payload = _json_body(request)
    email = str(payload.get("email") or "").strip()
    if not email or "@" not in email:
        return _error("Please enter a valid business email.")
    join_waitlist(email)
    return _response({
        "success": True,
        "message": "You're on the list!",
        "waitlistCount": 1428 + WaitlistEntry.objects.count(),
    })


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_assistant_settings(request):
    payload = _json_body(request)
    profile = request_profile(request)
    if not profile:
        return _error("User ID is required")
    if request.method == "POST":
        current = dict(profile.assistant_settings or {})
        supplied = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        current.update(supplied)
        profile.assistant_settings = current
        profile.save(update_fields=["assistant_settings"])
    return _response(profile.assistant_settings or {})


@csrf_exempt
@require_http_methods(["GET"])
def api_realtime_events(request):
    profile = request_profile(request)
    if not profile:
        return _error("User ID is required")
    rows = [{
        "event": item.event,
        "payload": item.payload,
        "created_at": item.created_at.isoformat(),
    } for item in profile.realtime_events.all()[:20]]
    return _response(rows)


@csrf_exempt
@require_http_methods(["POST"])
def api_integrations_disconnect(request):
    payload = _json_body(request)
    profile = _profile(request)
    app = str(payload.get("app") or "").lower()
    if not profile:
        return _error("User ID is required")
    if not app:
        return _error("App is required")
    if app == "whatsapp":
        whatsapp_connect(profile, "disconnect")
    else:
        if app == "gmail":
            _revoke_gmail_token(profile)
        profile.clear_integration(app)
    return _response({"success": True, "app": app, "connected": False})


@csrf_exempt
@require_http_methods(["POST"])
def api_chat_clear(request):
    _json_body(request)
    profile = _profile(request)
    if not profile:
        return _error("User ID is required")
    clear_chat(profile)
    return _response({"success": True, "messages": []})


@csrf_exempt
@require_http_methods(["GET"])
def api_chat_history(request):
    profile = request_profile(request)
    if not profile:
        return _error("User ID is required")
    return _response(serialize_chat(profile))


@csrf_exempt
@require_http_methods(["POST"])
def api_ml_predict(request):
    payload = _json_body(request)
    profile = request_profile(request)
    if not profile:
        return _error("User ID is required")
    text = str(payload.get("text") or "").strip()
    if not text:
        return _error("Text is required")
    return _response(ml_service.predict_importance(profile, text))


@csrf_exempt
@require_http_methods(["POST"])
def api_ml_feedback(request):
    payload = _json_body(request)
    profile = _profile(request)
    if not profile:
        return _error("User ID is required")
    try:
        result = ml_service.record_feedback(
            profile,
            str(payload.get("text") or ""),
            str(payload.get("label") or "").lower(),
            str(payload.get("source") or "feedback"),
        )
    except ValueError as exc:
        return _error(str(exc))
    return _response({"success": True, **result})
