"""Business services for the Django port of OmniSync.

Every external integration has a deterministic local implementation so the whole
product works without credentials.  When credentials are available the same
functions call Gemini / the Gmail REST API over plain HTTPS (no extra packages).
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import secrets
import string
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from email.utils import format_datetime
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import (
    Alert,
    AlertRule,
    BriefingSchedule,
    ChatMessage,
    GeneratedBriefing,
    GmailMessage,
    OtpCode,
    RealtimeEvent,
    UserProfile,
    WaitlistEntry,
    WhatsAppContact,
    WhatsAppMessage,
    WhatsAppSession,
)
from . import ml as ml_service

User = get_user_model()

APP_LOGOS = {
    "gmail": "/001-gmail.png",
    "whatsapp": "/002-whatsapp.png",
    "telegram": "/004-telegram.png",
    "slack": "/005-slack.png",
    "discord": "/006-discord.png",
    "linkedin": "/007-linkedin.png",
    "outlook": "/003-email.png",
}
MONITORABLE_APPS = ("gmail", "whatsapp")
IMPORTANT_TERMS = (
    "urgent", "asap", "deadline", "blocked", "approve", "review", "reply",
    "respond", "follow up", "follow-up", "invoice", "confirm",
)
URGENT_RE = re.compile(r"urgent|asap|deadline|blocked", re.I)
RESPONSE_RE = re.compile(r"reply|respond|confirm|approve|review|follow", re.I)


class McpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def http_json(url: str, method: str = "GET", payload: dict | None = None,
              headers: dict | None = None, data: bytes | None = None,
              timeout: int = 30) -> tuple[int, Any]:
    """Tiny HTTP+JSON helper built on the standard library."""
    body = data
    all_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, raw


def now_iso() -> str:
    return timezone.now().isoformat()


def iso_format(dt, default: str = "") -> str:
    return dt.isoformat() if dt else default


# ---------------------------------------------------------------------------
# Profiles and demo data
# ---------------------------------------------------------------------------
def get_or_create_profile(user: User) -> UserProfile:
    profile, created = UserProfile.objects.get_or_create(
        user=user,
        defaults={"name": user.first_name or user.email or user.username},
    )
    if created:
        seed_demo_data(profile)
    return profile


def profile_from_user_id(user_id: str | None) -> UserProfile | None:
    if not user_id:
        return None
    try:
        return UserProfile.objects.get(pk=int(user_id))
    except (ValueError, TypeError):
        try:
            return UserProfile.objects.get(pk=user_id)
        except Exception:
            return None
    except UserProfile.DoesNotExist:
        return None


def request_profile(request) -> UserProfile | None:
    """Resolve the caller from the Django session, with API-token compatibility."""
    if getattr(request, "user", None) and request.user.is_authenticated:
        return get_or_create_profile(request.user)
    user_id = request.GET.get("userId") or (getattr(request, "json_payload", None) or {}).get("userId")
    return profile_from_user_id(user_id)


def random_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def seed_demo_data(profile: UserProfile) -> None:
    """Seed the fictional Gmail/WhatsApp datasets used by the original demo."""
    if profile.gmail_messages.exists():
        return
    base = timezone.now()
    demos = [
        ("msg_12345", "thread_1", "John Doe <john.doe@acme.com>", "Q2 Slides & Marketing Budget Proposal",
         "Sarah, please review the Q2 slides and the proposed 10% marketing budget adjustment before 4:45 PM.",
         "Please review the attached Q2 slides. We are proposing a 10% marketing allocation adjustment. Need your approval by 4:45 PM.",
         ["INBOX", "UNREAD", "URGENT"], 0),
        ("msg_67890", "thread_2", "Sarah Jenkins <sarah.j@finance.corp>", "NDA for Client Review",
         "Can you review the NDA for the client review?",
         "Hi Rahul, attached is the NDA for the client review. Could you reply with approval today?",
         ["INBOX", "UNREAD"], 1),
        ("msg_11223", "thread_3", "Alex Rivera <alex@startup.co>", "Coffee tomorrow at 4:30 PM?",
         "Coffee tomorrow at 4:30 PM near the downtown office?",
         "Want to grab coffee tomorrow at 4:30 PM near the downtown office and align on the tech roadmap?",
         ["INBOX"], 2),
        ("msg_44556", "thread_4", "OpenAI Billing <billing@openai.com>", "Your monthly invoice is ready",
         "Your monthly invoice is ready.",
         "Your monthly invoice is ready. No action is required at this time.",
         ["INBOX"], 24),
    ]
    for mid, tid, sender, subject, snippet, body, labels, hours in demos:
        GmailMessage.objects.create(
            profile=profile, message_id=mid, thread_id=tid, sender=sender,
            recipient="Rahul <rahul@omnisync.ai>", subject=subject,
            date=format_datetime(base - timedelta(hours=hours), usegmt=True),
            snippet=snippet, body=body, labels=labels, kind=GmailMessage.INBOX,
        )
    contacts = [
        ("123456789@s.whatsapp.net", "Sarah Jenkins", "+123456789", "Sarah Jenkins", False),
        ("987654321@s.whatsapp.net", "Alex Rivera", "+987654321", "", False),
        ("111222333@s.whatsapp.net", "John Doe", "+111222333", "", False),
        ("120363024555555555@g.us", "Marketing Launch Team", "", "", True),
    ]
    for jid, name, phone, verified, group in contacts:
        WhatsAppContact.objects.create(
            profile=profile, jid=jid, name=name, phone=phone,
            verified_name=verified, is_group=group,
        )
    messages = [
        ("wmsg_1", contacts[0][0], "Sarah Jenkins", "Can you review the NDA terms before the client call?", False, 1),
        ("wmsg_2", contacts[0][0], "Rahul", "I will review it within 2 days.", True, 0.8),
        ("wmsg_3", contacts[0][0], "Sarah Jenkins", "Lunch tomorrow?", False, 0.5),
        ("wmsg_4", contacts[1][0], "Alex Rivera", "Coffee tomorrow 4:30 PM?", False, 2),
        ("wmsg_5", contacts[1][0], "Rahul", "Downtown Starbucks works.", True, 1.9),
        ("wmsg_6", contacts[1][0], "Alex Rivera", "Let's align on the tech roadmap too.", False, 1.8),
        ("wmsg_7", contacts[2][0], "John Doe", "Please submit Q2 slides by 4:45 PM.", False, 4),
        ("wmsg_8", contacts[2][0], "Rahul", "Final draft is being compiled.", True, 3.9),
        ("wmsg_9", contacts[3][0], "Alex Rivera", "Are the slides ready?", False, 1.5),
        ("wmsg_10", contacts[3][0], "Sarah Jenkins", "Budget reviewed, waiting on Rahul.", False, 1.4),
        ("wmsg_11", contacts[3][0], "John Doe", "Rahul said slides by 4:45 PM.", False, 1.3),
    ]
    for mid, jid, sender, body, from_me, hours in messages:
        WhatsAppMessage.objects.create(
            profile=profile, message_id=mid, chat_id=jid, sender=sender,
            body=body, from_me=from_me, timestamp=base - timedelta(hours=hours),
        )
    session, _ = WhatsAppSession.objects.get_or_create(profile=profile)
    if session.status != "connected":
        session.status = "connected"
        session.is_simulated = True
        session.phone_number = session.phone_number or "+15550199"
        session.connected_at = session.connected_at or timezone.now()
        session.save()
    profile.set_integration("whatsapp", {
        "connected": True,
        "isSimulated": True,
        "phoneNumber": session.phone_number,
        "connectedAt": now_iso(),
    })


def connect_simulated_gmail(profile: UserProfile) -> dict:
    email = "demo@omnisync.ai"
    seed_demo_data(profile)
    profile.set_integration("gmail", {
        "connected": True,
        "isSimulated": True,
        "email": email,
        "connectedAt": now_iso(),
    })
    publish_event(profile, "integration_connected", {"app": "gmail", "simulated": True})
    return {"success": True, "simulated": True, "email": email}


# ---------------------------------------------------------------------------
# AI service (Gemini with deterministic fallbacks)
# ---------------------------------------------------------------------------
def ai_generate(prompt: str, model: str | None = None, json_mode: bool = True) -> Any | None:
    """Return parsed JSON/text from Gemini, or None when not configured."""
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    model_name = model or settings.GEMINI_MODEL
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}"
        f":generateContent?key={urllib.parse.quote(key)}"
    )
    generation_config: dict[str, Any] = {"temperature": 0.1}
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    status, data = http_json(url, "POST", {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    })
    if status != 200 or not isinstance(data, dict):
        return None
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    if json_mode:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return text


def ai_chat_response(profile: UserProfile, prompt: str, history: list[dict] | None = None) -> dict:
    context_parts = []
    try:
        gmail = gmail_mcp(profile, "gmail_list_messages", {"q": "label:inbox", "maxResults": 5})
        for msg in gmail.get("messages", []):
            context_parts.append(
                f"From: {msg.get('from')}\nSubject: {msg.get('subject')}\n"
                f"Snippet: {msg.get('snippet')}\nDate: {msg.get('date')}\nID: {msg.get('id')}"
            )
    except Exception:
        context_parts.append("No Gmail messages synced.")
    try:
        wa = whatsapp_mcp(profile, "whatsapp_get_recent_messages", {})
        for msg in wa.get("messages", []):
            context_parts.append(
                f"Chat: {msg.get('chatName')}\nSender: {msg.get('from')}\n"
                f"Body: {msg.get('body')}\nTimestamp: {msg.get('timestamp')}"
            )
    except Exception:
        context_parts.append("No WhatsApp messages synced.")
    context = "\n---\n".join(context_parts) or "No recent messages found."

    if not settings.GEMINI_API_KEY:
        ranked = ml_service.rank_activity(profile, connected_activity(profile, "alerts"))
        important = [row for row in ranked if row["prediction"]["label"] != "low"][:3]
        if important:
            lines = []
            for row in important:
                item = row["item"]
                prediction = row["prediction"]
                lines.append(
                    f"- {item['title']} ({item['app']}, ML priority {prediction['label']}, "
                    f"{int(prediction['confidence'] * 100)}% confidence)"
                )
            response_text = (
                "Local ML mode is active. I ranked your connected activity with a Naive Bayes "
                "priority model and found these items worth attention:\n\n"
                + "\n".join(lines)
                + "\n\nConfigure GEMINI_API_KEY for full generative answers."
            )
        else:
            response_text = (
                "Local ML mode is active. The priority model found no high or medium items "
                "in your connected activity. Configure GEMINI_API_KEY for full generative answers."
            )
        return {
            "response": response_text,
            "suggestions": ["Summarize the top item", "Draft a reply", "What should I do next?"],
        }

    system_prompt = (
        "You are OmniSync, an advanced AI personal assistant. Answer using the connected "
        "platform data below. Name Gmail and WhatsApp explicitly when relevant. Be helpful, "
        "direct, and format with markdown. Return strictly valid JSON with exactly these keys: "
        '{"response": "detailed markdown response", "suggestions": ["...", "...", "..."]}'
        f"\n\nConnected data:\n{context}\n\nConversation history:\n{json.dumps(history or [])}"
    )
    contents = [
        {"role": "user", "parts": [{"text": system_prompt}]},
    ]
    for item in (history or [])[-10:]:
        role = "user" if item.get("sender") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": str(item.get("text", ""))}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.GEMINI_CHAT_MODEL}:generateContent?key={urllib.parse.quote(settings.GEMINI_API_KEY)}"
    )
    status, data = http_json(url, "POST", {
        "contents": contents,
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    })
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError("Failed to generate response")
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Failed to generate response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "response": text,
            "suggestions": ["Can you summarize that?", "What should I do next?", "Draft a reply email"],
        }


# ---------------------------------------------------------------------------
# Gmail MCP dispatcher
# ---------------------------------------------------------------------------
def _gmail_message_out(msg: GmailMessage, full: bool = False) -> dict:
    out = {
        "id": msg.message_id,
        "threadId": msg.thread_id,
        "from": msg.sender or "Unknown Sender",
        "subject": msg.subject or "No Subject",
        "date": msg.date,
        "snippet": msg.snippet,
        "labels": list(msg.labels or []),
    }
    if full:
        out.update({"to": msg.recipient, "body": msg.body or msg.snippet})
    return out


def _google_token(profile: UserProfile) -> str | None:
    gmail = profile.integration("gmail")
    if not gmail.get("connected") or gmail.get("isSimulated"):
        return None
    expires = gmail.get("expiresAt") or 0
    token = gmail.get("accessToken")
    if token and expires > timezone.now().timestamp() * 1000 + 60000:
        return token
    refresh = gmail.get("refreshToken")
    if not refresh or not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        return None
    status, data = http_json("https://oauth2.googleapis.com/token", "POST", data=urllib.parse.urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status == 200 and isinstance(data, dict) and data.get("access_token"):
        gmail["accessToken"] = data["access_token"]
        gmail["expiresAt"] = timezone.now().timestamp() * 1000 + int(data.get("expires_in", 3600)) * 1000
        if data.get("refresh_token"):
            gmail["refreshToken"] = data["refresh_token"]
        profile.set_integration("gmail", gmail)
        return gmail["accessToken"]
    return None


def gmail_mcp(profile: UserProfile, method: str, params: dict | None = None) -> dict:
    params = params or {}
    gmail = profile.integration("gmail")
    if not gmail.get("connected"):
        raise McpError(-32001, "Gmail is not connected. Connect with Google OAuth or enable simulated mode.")
    if gmail.get("isSimulated"):
        return _gmail_simulated(profile, method, params)
    token = _google_token(profile)
    if not token:
        raise McpError(-32002, "Gmail session expired. Reconnect your Google account.")
    return _gmail_real(profile, method, params, token)


def _gmail_simulated(profile: UserProfile, method: str, params: dict) -> dict:
    if method in ("gmail_list_messages", "gmail_search_messages"):
        q = (params.get("q") or ("label:inbox" if method == "gmail_list_messages" else "")).lower()
        rows = list(profile.gmail_messages.filter(kind=GmailMessage.INBOX))
        if method == "gmail_search_messages" and q:
            rows = [m for m in rows if q in (m.subject + m.sender + m.body).lower()]
        elif "label:inbox" in q:
            rows = [m for m in rows if "INBOX" in (m.labels or [])]
        limit = int(params.get("maxResults") or 10) if method == "gmail_list_messages" else None
        if limit:
            rows = rows[:limit]
        return {"messages": [_gmail_message_out(m) for m in rows]}
    if method == "gmail_get_message":
        mid = params.get("id")
        msg = profile.gmail_messages.filter(message_id=mid).first()
        if not msg:
            raise McpError(-32602, f"Message with ID {mid} not found")
        return _gmail_message_out(msg, full=True)
    if method == "gmail_send_message":
        body = params.get("body") or ""
        msg = profile.gmail_messages.create(
            message_id=random_id("msg"), thread_id=random_id("thread"),
            sender="Rahul <rahul@omnisync.ai>", recipient=params.get("to", ""),
            subject=params.get("subject", ""), date=format_datetime(timezone.now(), usegmt=True),
            snippet=(body[:100] + "...") if len(body) > 100 else body,
            body=body, labels=["SENT", "INBOX"], kind=GmailMessage.SENT,
        )
        return {
            "success": True, "message": "Email sent successfully (Simulated)",
            "messageId": msg.message_id, "threadId": msg.thread_id,
        }
    if method == "gmail_create_draft":
        did = random_id("draft")
        msg = profile.gmail_messages.create(
            message_id=did, thread_id=params.get("threadId") or random_id("thread"),
            sender="Rahul <rahul@omnisync.ai>", recipient=params.get("to", ""),
            subject=params.get("subject", ""), date=format_datetime(timezone.now(), usegmt=True),
            snippet=params.get("body", "")[:100], body=params.get("body", ""),
            labels=["DRAFT"], kind=GmailMessage.DRAFT,
        )
        return {
            "success": True, "message": "Draft created successfully (Simulated)",
            "draft": {"id": did, "message": {"id": did, "threadId": msg.thread_id}},
        }
    if method == "gmail_get_thread":
        tid = params.get("id")
        rows = profile.gmail_messages.filter(thread_id=tid)
        if not rows.exists():
            raise McpError(-32602, f"Thread with ID {tid} not found")
        return {"id": tid, "messages": [_gmail_message_out(m, full=True) for m in rows]}
    raise McpError(-32601, f"Method not found: {method}")


def _gmail_real(profile: UserProfile, method: str, params: dict, token: str) -> dict:
    base = "https://gmail.googleapis.com/gmail/v1/users/me/"
    headers = {"Authorization": f"Bearer {token}"}

    def req(path: str, req_method: str = "GET", payload: dict | None = None):
        status, data = http_json(base + path, req_method, payload, headers)
        if status != 200:
            raise McpError(-32603, (data or {}).get("error", {}).get("message", "Google API request failed."))
        return data

    def detailed(messages: list[dict]) -> list[dict]:
        out = []
        for item in messages:
            mid = item.get("id")
            detail = req(f"messages/{mid}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date")
            headers_map = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            out.append({
                "id": mid, "threadId": item.get("threadId"),
                "from": headers_map.get("From", "Unknown Sender"),
                "subject": headers_map.get("Subject", "No Subject"),
                "date": headers_map.get("Date", detail.get("internalDate", "")),
                "snippet": detail.get("snippet", ""), "labels": detail.get("labelIds", []),
            })
        return out

    if method in ("gmail_list_messages", "gmail_search_messages"):
        q = params.get("q") or ("label:inbox" if method == "gmail_list_messages" else "")
        max_results = int(params.get("maxResults") or 10)
        listing = req(f"messages?q={urllib.parse.quote(q)}&maxResults={max_results}")
        return {"messages": detailed(listing.get("messages", []))}
    if method == "gmail_get_message":
        detail = req(f"messages/{params.get('id')}?format=full")
        headers_map = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}

        def extract(part):
            data = (part.get("body") or {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            for child in part.get("parts", []):
                found = extract(child)
                if found:
                    return found
            return ""

        raw = extract(detail.get("payload", {}))
        body = re.sub(r"<[^>]*>", "", raw) if "<" in raw else raw
        return {
            "id": detail.get("id"), "threadId": detail.get("threadId"),
            "from": headers_map.get("From", ""), "to": headers_map.get("To", ""),
            "subject": headers_map.get("Subject", ""), "date": headers_map.get("Date", ""),
            "snippet": detail.get("snippet", ""), "body": body or detail.get("snippet", ""),
            "labels": detail.get("labelIds", []),
        }
    if method in ("gmail_send_message", "gmail_create_draft"):
        raw_message = (
            f"To: {params.get('to', '')}\r\nSubject: {params.get('subject', '')}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\nMIME-Version: 1.0\r\n\r\n"
            f"{params.get('body', '')}"
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw_message).decode()
        if method == "gmail_send_message":
            sent = req("messages/send", "POST", {"raw": encoded})
            return {
                "success": True, "message": "Email sent successfully via Gmail API",
                "messageId": sent.get("id"), "threadId": sent.get("threadId"),
            }
        draft = req("drafts", "POST", {"message": {"raw": encoded}})
        message = draft.get("message") or {}
        return {
            "success": True, "message": "Draft created successfully via Gmail API",
            "draft": {"id": draft.get("id"), "message": {
                "id": message.get("id"), "threadId": message.get("threadId"),
            }},
        }
    if method == "gmail_get_thread":
        thread = req(f"threads/{params.get('id')}")
        return {"id": thread.get("id"), "messages": detailed(thread.get("messages", []))}
    raise McpError(-32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# WhatsApp MCP dispatcher
# ---------------------------------------------------------------------------
def normalize_whatsapp_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("00"):
        digits = digits[2:]
    digits = digits.lstrip("0")
    if len(digits) == 14 and digits.startswith("9191"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits
    if not re.fullmatch(r"[1-9]\d{7,14}", digits):
        raise ValueError(
            "Enter a valid phone number, with country code if outside India. "
            "Indian 10-digit numbers are accepted."
        )
    return digits


WHATSAPP_BRIDGE_URL = "http://127.0.0.1:3001"
WHATSAPP_BRIDGE_DIR = Path(settings.BASE_DIR) / "whatsapp_bridge"


def _bridge_alive() -> bool:
    try:
        with urllib.request.urlopen(WHATSAPP_BRIDGE_URL + "/health", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def _start_bridge() -> None:
    if _bridge_alive():
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["node", "index.js"],
        cwd=str(WHATSAPP_BRIDGE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    for _ in range(20):
        if _bridge_alive():
            return
        import time
        time.sleep(0.25)
    raise ValueError("WhatsApp bridge failed to start. Install Node.js and run npm install inside whatsapp_bridge.")


def _bridge_request(path: str, payload: dict, timeout: int = 30) -> dict:
    _start_bridge()
    request = urllib.request.Request(
        WHATSAPP_BRIDGE_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise ValueError(str(exc)) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"WhatsApp bridge is unreachable: {exc.reason}") from exc
    if "error" in result:
        error = result["error"]
        raise McpError(int(error.get("code") or -32603), str(error.get("message") or "WhatsApp bridge error"))
    return result


def _bridge_datetime(ms: int | None):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc)


def whatsapp_connect(profile: UserProfile, action: str, phone_number: str = "") -> dict:
    session, _ = WhatsAppSession.objects.get_or_create(profile=profile)
    if action == "status":
        result = {
            "success": True,
            "status": session.status,
            "isSimulated": session.is_simulated,
        }
        if session.pairing_code and session.pairing_expires_at and session.pairing_expires_at > timezone.now():
            result["pairingCode"] = session.pairing_code
            result["pairingCodeExpiresAt"] = int(session.pairing_expires_at.timestamp() * 1000)
        if session.error:
            result["error"] = session.error
        if not session.is_simulated and session.status != "disconnected":
            bridge = _bridge_request("/status", {"sessionId": str(profile.id)}, timeout=5)
            session.status = bridge.get("status", session.status)
            session.pairing_code = bridge.get("pairingCode") or ""
            session.pairing_expires_at = _bridge_datetime(bridge.get("pairingCodeExpiresAt"))
            session.error = bridge.get("error") or ""
            session.save(update_fields=["status", "pairing_code", "pairing_expires_at", "error"])
            result.update(bridge)
            if bridge.get("connected"):
                profile.set_integration("whatsapp", {
                    "connected": True,
                    "phoneNumber": bridge.get("phoneNumber") or session.phone_number,
                    "isSimulated": False,
                    "connectedAt": iso_format(timezone.now()),
                })
        return result
    if action == "disconnect":
        if not session.is_simulated and session.status != "disconnected":
            try:
                _bridge_request("/disconnect", {"sessionId": str(profile.id)}, timeout=10)
            except Exception:
                pass
        session.status = "disconnected"
        session.pairing_code = ""
        session.pairing_expires_at = None
        session.is_simulated = False
        session.connected_at = None
        session.error = ""
        session.save()
        profile.clear_integration("whatsapp")
        return {"success": True, "status": "disconnected"}
    if action == "connect-simulated":
        session.status = "connected"
        session.is_simulated = True
        session.phone_number = phone_number or "+15550199"
        session.connected_at = timezone.now()
        session.pairing_code = ""
        session.pairing_expires_at = None
        session.error = ""
        session.save()
        profile.set_integration("whatsapp", {
            "connected": True, "phoneNumber": session.phone_number,
            "isSimulated": True, "connectedAt": iso_format(session.connected_at),
        })
        return {"success": True, "status": "connected", "isSimulated": True}
    if action == "connect":
        if not phone_number:
            raise ValueError("Phone number is required for connection.")
        digits = normalize_whatsapp_phone(phone_number)
        bridge = _bridge_request("/connect", {"sessionId": str(profile.id), "phoneNumber": digits}, timeout=30)
        session.phone_number = digits
        session.status = bridge.get("status", "connecting")
        session.pairing_code = bridge.get("pairingCode") or ""
        session.pairing_expires_at = _bridge_datetime(bridge.get("pairingCodeExpiresAt"))
        session.is_simulated = False
        session.error = bridge.get("error") or ""
        if bridge.get("connected"):
            session.connected_at = timezone.now()
            profile.set_integration("whatsapp", {
                "connected": True, "phoneNumber": digits, "isSimulated": False,
                "connectedAt": iso_format(session.connected_at),
            })
        session.save()
        bridge.setdefault("success", True)
        bridge.setdefault("notice", "Enter this exact code in WhatsApp → Linked Devices → Link with phone number instead.")
        return bridge
    raise ValueError("Invalid action.")


def _wa_message_out(msg: WhatsAppMessage, include_from_me: bool = True) -> dict:
    out = {
        "messageId": msg.message_id,
        "from": msg.sender,
        "body": msg.body,
        "timestamp": iso_format(msg.timestamp),
    }
    if include_from_me:
        out["fromMe"] = msg.from_me
    return out


def whatsapp_mcp(profile: UserProfile, method: str, params: dict | None = None) -> dict:
    params = params or {}
    integration = profile.integration("whatsapp")
    if not integration.get("connected"):
        raise McpError(-32001, "WhatsApp integration is not connected.")
    session = WhatsAppSession.objects.filter(profile=profile).first()
    if session and session.status not in ("connected",):
        raise McpError(-32002, f"WhatsApp connection is currently {session.status}. Please check details.")
    if session and not session.is_simulated:
        return _bridge_request("/mcp", {
            "sessionId": str(profile.id),
            "method": method,
            "params": params,
        }, timeout=15)

    contacts = list(profile.whatsapp_contacts.all())
    by_jid = {c.jid: c for c in contacts}
    messages = list(profile.whatsapp_messages.all())

    if method == "whatsapp_get_recent_messages":
        latest = []
        seen = set()
        for msg in reversed(messages):
            if msg.chat_id in seen:
                continue
            seen.add(msg.chat_id)
            contact = by_jid.get(msg.chat_id)
            latest.append({
                "chatId": msg.chat_id,
                "chatName": contact.name if contact else msg.chat_id,
                "from": msg.sender, "body": msg.body,
                "timestamp": iso_format(msg.timestamp), "fromMe": msg.from_me,
            })
        latest.sort(key=lambda item: item["timestamp"], reverse=True)
        return {"messages": latest}

    if method == "whatsapp_get_chat_history":
        chat_id = params.get("chatId")
        if not chat_id:
            raise McpError(-32602, "Argument 'chatId' is required")
        rows = [m for m in messages if m.chat_id == chat_id][-int(params.get("limit") or 20):]
        return {"messages": [_wa_message_out(m) for m in rows]}

    if method == "whatsapp_send_message":
        to, body = params.get("to"), params.get("body")
        if not to or not body:
            raise McpError(-32602, "Arguments 'to' and 'body' are required")
        jid = to if "@" in str(to) else f"{re.sub(chr(92) + 'D', '', str(to))}@s.whatsapp.net"
        mid = random_id("wmsg_sim")
        contact = by_jid.get(jid)
        if not contact:
            contact = WhatsAppContact.objects.create(profile=profile, jid=jid, name=jid, phone=jid)
        WhatsAppMessage.objects.create(
            profile=profile, message_id=mid, chat_id=jid, sender="Rahul", body=body,
            from_me=True, timestamp=timezone.now(),
        )
        return {
            "success": True, "message": "WhatsApp message sent successfully (Simulated)",
            "messageId": mid, "recipient": jid,
        }

    if method == "whatsapp_search_chats":
        query = (params.get("query") or "").lower()
        hits = []
        for contact in contacts:
            body_match = any(query in m.body.lower() for m in messages if m.chat_id == contact.jid)
            if query in contact.name.lower() or query in contact.jid.lower() or body_match:
                last = [m for m in messages if m.chat_id == contact.jid]
                hits.append({
                    "chatId": contact.jid, "name": contact.name,
                    "isGroup": contact.is_group,
                    "lastMessage": last[-1].body if last else "",
                })
        return {"chats": hits}

    if method == "whatsapp_summarize_conversations":
        chat_id = params.get("chatId")
        if chat_id:
            rows = [m for m in messages if m.chat_id == chat_id]
            contact = by_jid.get(chat_id)
            name = contact.name if contact else chat_id
            return {"summary": f"A chat session with {name} containing {len(rows)} messages. The conversation includes coordinate updates and general task management."}
        return {"summary": (
            "OmniSync Daily WhatsApp Brief\n"
            "• Sarah Jenkins: NDA review and lunch coordination\n"
            "• Alex Rivera: Coffee at 4:30 PM and roadmap discussion\n"
            "• John Doe: Q2 slides due by 4:45 PM\n"
            "• Marketing Launch Team: budget and slide status updates"
        )}

    if method == "whatsapp_get_contact_details":
        needle = params.get("jidOrPhone") or ""
        contact = next((c for c in contacts if needle in (c.jid, c.phone)), None)
        if contact:
            return {
                "id": contact.jid, "name": contact.name, "phone": contact.phone,
                "verifiedName": contact.verified_name,
            }
        jid = needle if "@" in needle else f"{re.sub(chr(92) + 'D', '', needle)}@s.whatsapp.net"
        return {"id": jid, "name": needle, "phone": needle}

    if method == "whatsapp_list_groups":
        return {"groups": [{"id": c.jid, "name": c.name} for c in contacts if c.is_group]}

    if method == "whatsapp_get_group_messages":
        group_id = params.get("groupId")
        if not group_id:
            raise McpError(-32602, "Argument 'groupId' is required")
        contact = by_jid.get(group_id)
        if not contact or not contact.is_group:
            raise McpError(-32602, f"Group with ID {group_id} not found")
        rows = [m for m in messages if m.chat_id == group_id][-int(params.get("limit") or 20):]
        return {"messages": [_wa_message_out(m, include_from_me=False) for m in rows]}

    if method in ("whatsapp_send_group_messages", "whatsapp_send_group_message"):
        group_id, body = params.get("groupId"), params.get("body")
        if not group_id or not body:
            raise McpError(-32602, "Arguments 'groupId' and 'body' are required")
        contact = by_jid.get(group_id)
        if not contact or not contact.is_group:
            raise McpError(-32602, f"Group with ID {group_id} not found")
        mid = random_id("wmsg_sim")
        WhatsAppMessage.objects.create(
            profile=profile, message_id=mid, chat_id=group_id, sender="Rahul",
            body=body, from_me=True, timestamp=timezone.now(),
        )
        return {
            "success": True, "message": "WhatsApp group message sent successfully (Simulated)",
            "messageId": mid, "recipientGroup": group_id,
        }
    raise McpError(-32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Connected activity (shared by alerts and briefings)
# ---------------------------------------------------------------------------
def connected_activity(profile: UserProfile, purpose: str = "alerts") -> list[dict]:
    items: list[dict] = []
    apps = profile.connected_apps(monitorable_only=True)
    if "gmail" in apps:
        try:
            query = (
                "label:inbox is:unread newer_than:7d" if purpose == "alerts" else "label:inbox is:unread"
            )
            result = gmail_mcp(profile, "gmail_list_messages", {"q": query, "maxResults": 20})
            for msg in result.get("messages", []):
                items.append({
                    "id": str(msg.get("id")), "app": "gmail",
                    "title": msg.get("subject") or "Email",
                    "description": msg.get("snippet") or "",
                    "body": msg.get("snippet") or "", "time": msg.get("date"),
                })
        except Exception:
            pass
    if "whatsapp" in apps:
        try:
            result = whatsapp_mcp(profile, "whatsapp_get_recent_messages", {})
            for msg in result.get("messages", []):
                items.append({
                    "id": str(msg.get("chatId")), "app": "whatsapp",
                    "title": msg.get("chatName") or "WhatsApp chat",
                    "description": msg.get("body") or "",
                    "body": msg.get("body") or "", "time": msg.get("timestamp"),
                })
        except Exception:
            pass
    return items


def publish_event(profile: UserProfile, event: str, payload: dict | None = None) -> None:
    RealtimeEvent.objects.create(profile=profile, event=event, payload=payload or {})


# ---------------------------------------------------------------------------
# Dashboard brief
# ---------------------------------------------------------------------------
MOCK_BRIEF = {
    "stats": {"importantCount": 3, "priorityCount": 2, "followUpCount": 1},
    "brief": [
        {"id": "b1", "app": "gmail", "type": "Gmail Digest", "title": "Q2 Budget Review",
         "summary": "Sarah requested a final review of the Q2 budget proposal with a proposed 10% marketing spend adjustment.",
         "time": "12m ago", "action": "Review proposal"},
        {"id": "b2", "app": "whatsapp", "type": "WhatsApp Digest", "title": "Co-Founder Sync",
         "summary": "Alex wants to meet for coffee tomorrow at 4:30 PM to align on tech roadmap milestones.",
         "time": "45m ago", "action": "Check schedule"},
    ],
    "priorityItems": [
        {"id": "p1", "app": "gmail", "title": "Submit Q2 Slides to John", "time": "Due 4:45 PM today",
         "description": "Send final compiled roadmap and budget presentation slides.", "priority": "High"},
        {"id": "p2", "app": "whatsapp", "title": "Coffee with Alex", "time": "Tomorrow 4:30 PM",
         "description": "Discuss tech milestones and next deployment roadmap.", "priority": "Medium"},
    ],
}


def dashboard_brief(profile: UserProfile, force: bool = False) -> dict:
    cached = profile.dashboard_brief or {}
    generated_at = cached.get("generatedAt")
    if not force and generated_at:
        try:
            age = (timezone.now() - timezone.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))).total_seconds()
            if 0 <= age < 2 * 60 * 60:
                return {**cached, "isFromCache": True}
        except (ValueError, TypeError):
            pass

    activity = connected_activity(profile, "dashboard")
    brief = None
    if settings.GEMINI_API_KEY and activity:
        prompt = (
            "You are OmniSync. Build a concise dashboard intelligence brief from the real "
            "connected-app data below. Do not invent anything.\n\n"
            f"Data:\n{json.dumps(activity[:6])}\n\n"
            "Return JSON: {\"stats\":{\"importantCount\":0,\"priorityCount\":0,\"followUpCount\":0},"
            "\"brief\":[{\"id\",\"app\",\"type\",\"title\",\"summary\",\"time\",\"action\"}],"
            "\"priorityItems\":[{\"id\",\"app\",\"title\",\"time\",\"description\",\"priority\"}]}. "
            "Keep summaries under 12 words."
        )
        result = ai_generate(prompt, settings.GEMINI_MODEL)
        if isinstance(result, dict) and "stats" in result:
            brief = result
    if brief is None:
        connected = profile.connected_apps()
        if connected:
            brief = json.loads(json.dumps(MOCK_BRIEF))
            ranked = ml_service.rank_activity(profile, activity)
            important = [row for row in ranked if row["prediction"]["label"] != "low"]
            selected = (important or ranked)[:3]
            brief_items = []
            priority_items = []
            for row in selected:
                item = row["item"]
                prediction = row["prediction"]
                summary = str(item.get("description") or item.get("body") or "No summary available.")
                if len(summary) > 140:
                    summary = summary[:137] + "..."
                action = (
                    "Reply now" if prediction["label"] == "high"
                    else "Review item" if prediction["label"] == "medium"
                    else "Check update"
                )
                brief_items.append({
                    "id": item.get("id", ""),
                    "app": item.get("app", ""),
                    "type": "ML Priority Brief",
                    "title": str(item.get("title") or "Update")[:80],
                    "summary": summary,
                    "time": item.get("time") or "",
                    "action": action,
                    "mlPrediction": prediction,
                })
                if len(priority_items) < 2 and prediction["label"] in ("high", "medium"):
                    priority_items.append({
                        "id": item.get("id", ""),
                        "app": item.get("app", ""),
                        "title": str(item.get("title") or "Update")[:80],
                        "time": item.get("time") or "",
                        "description": summary,
                        "priority": "High" if prediction["label"] == "high" else "Medium",
                        "mlPrediction": prediction,
                    })
            if not priority_items and brief_items:
                item = brief_items[0]
                priority_items.append({
                    "id": item["id"], "app": item["app"], "title": item["title"],
                    "time": item["time"], "description": item["summary"],
                    "priority": "Low", "mlPrediction": item["mlPrediction"],
                })
            brief["brief"] = brief_items
            brief["priorityItems"] = priority_items
            brief["stats"] = {
                "importantCount": len(brief_items) + len(priority_items),
                "priorityCount": sum(1 for p in priority_items if p["priority"] == "High"),
                "followUpCount": sum(1 for b in brief_items if b["action"] in ("Reply now", "Check update")),
            }
            brief["model"] = "multinomial_naive_bayes"
        else:
            brief = json.loads(json.dumps(MOCK_BRIEF))

    brief["generatedAt"] = now_iso()
    brief["isSimulated"] = not bool(settings.GEMINI_API_KEY and activity)
    if not brief["isSimulated"]:
        brief.pop("warn", None)
    else:
        brief["warn"] = (
            "GEMINI_API_KEY env var missing. Running in simulated fallback mode."
            if not settings.GEMINI_API_KEY else "No connected app activity. Running in simulated fallback mode."
        )
    profile.dashboard_brief = brief
    profile.save(update_fields=["dashboard_brief"])
    return brief


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------
def next_run_time(scheduled_time: str, frequency: str):
    now = timezone.now()
    if frequency == "hourly":
        return now + timedelta(hours=1)
    if frequency == "weekly":
        return now + timedelta(days=7)
    return now + timedelta(days=1)


def create_schedule(profile: UserProfile, payload: dict) -> BriefingSchedule:
    return BriefingSchedule.objects.create(
        profile=profile,
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        apps=payload.get("apps") or [],
        categories=payload.get("categories") or [],
        scheduled_time=payload.get("scheduledTime") or "09:00",
        frequency=payload.get("frequency") or "daily",
        priority_level=payload.get("priorityLevel") or "medium",
        next_run_at=next_run_time(payload.get("scheduledTime") or "09:00", payload.get("frequency") or "daily"),
    )


def create_briefing(profile: UserProfile, schedule_id: str | None = None) -> GeneratedBriefing:
    schedule = profile.briefing_schedules.filter(pk=schedule_id).first() if schedule_id else None
    defaults = {
        "name": f"Daily Briefing — {timezone.now():%b %d, %Y}",
        "description": "Your daily communication digest and action items summary.",
        "apps": ["gmail", "whatsapp"],
        "categories": ["email", "messages", "mentions", "tasks", "follow_ups"],
    }
    name = schedule.name if schedule else defaults["name"]
    description = schedule.description if schedule else defaults["description"]
    apps = (schedule.apps if schedule and schedule.apps else defaults["apps"])
    categories = (schedule.categories if schedule and schedule.categories else defaults["categories"])

    emails: list[dict] = []
    messages: list[dict] = []
    if "gmail" in apps and "gmail" in profile.connected_apps():
        try:
            result = gmail_mcp(profile, "gmail_list_messages", {"q": "label:inbox is:unread", "maxResults": 15})
            for msg in result.get("messages", []):
                emails.append({
                    "id": msg.get("id"), "app": "gmail", "from": msg.get("from"),
                    "subject": msg.get("subject"), "snippet": msg.get("snippet"),
                    "time": msg.get("date"), "body": msg.get("snippet", ""),
                })
        except Exception:
            pass
    if "whatsapp" in apps and "whatsapp" in profile.connected_apps():
        try:
            result = whatsapp_mcp(profile, "whatsapp_get_recent_messages", {})
            for i, msg in enumerate(result.get("messages", [])):
                messages.append({
                    "id": f"wa_{i}", "app": "whatsapp", "from": msg.get("from") or msg.get("chatName"),
                    "snippet": msg.get("body"), "time": msg.get("timestamp"),
                    "body": msg.get("body"),
                })
        except Exception:
            pass

    stats = {
        "email": len(emails), "messages": len(messages),
        "mentions": 0, "tasks": 0, "follow_ups": 0,
    }
    data = {"email": emails, "messages": messages, "mentions": [], "tasks": [], "follow_ups": []}
    title = name if (emails or messages) else f"{name} — No new activity"
    summary = (
        f"{len(emails)} email(s) and {len(messages)} message(s) from your connected apps. "
        "Review the categories below for details."
        if (emails or messages) else
        "No new messages or emails found in your connected apps at this time."
    )
    ai_generated = False

    if settings.GEMINI_API_KEY and (emails or messages):
        prompt = (
            "You are OmniSync. Create a briefing from this real data, without inventing facts.\n"
            f"Schedule: {name} — {description}\nCategories: {categories}\n"
            f"Data: {json.dumps({'email': emails, 'messages': messages})}\n"
            "Return JSON with title, summary, stats (email,messages,mentions,tasks,follow_ups), "
            "and data with keys email,messages,mentions,tasks,follow_ups."
        )
        result = ai_generate(prompt, settings.GEMINI_MODEL)
        if isinstance(result, dict) and "data" in result:
            title = result.get("title") or title
            summary = result.get("summary") or summary
            stats = result.get("stats") or stats
            data = result.get("data") or data
            ai_generated = True

    return GeneratedBriefing.objects.create(
        profile=profile, schedule=schedule, title=title, summary=summary,
        stats=stats, data=data, ai_generated=ai_generated,
    )


def briefing_out(briefing: GeneratedBriefing) -> dict:
    return {
        "id": str(briefing.id), "user_id": str(briefing.profile_id),
        "schedule_id": str(briefing.schedule_id) if briefing.schedule_id else None,
        "title": briefing.title, "summary": briefing.summary,
        "stats": briefing.stats, "data": briefing.data,
        "ai_generated": briefing.ai_generated,
        "created_at": iso_format(briefing.created_at),
    }


def schedule_out(schedule: BriefingSchedule) -> dict:
    return {
        "id": str(schedule.id), "user_id": str(schedule.profile_id),
        "name": schedule.name, "description": schedule.description,
        "apps": schedule.apps, "categories": schedule.categories,
        "scheduledTime": schedule.scheduled_time, "frequency": schedule.frequency,
        "priorityLevel": schedule.priority_level,
        "next_run_at": iso_format(schedule.next_run_at),
        "created_at": iso_format(schedule.created_at),
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def normalize_priority(value: str | None) -> str:
    return value if value in ("high", "low") else "medium"


def alert_out(alert: Alert) -> dict:
    return {
        "id": str(alert.id), "user_id": str(alert.profile_id),
        "rule_id": str(alert.rule_id) if alert.rule_id else None,
        "dedupe_key": alert.dedupe_key, "title": alert.title,
        "description": alert.description, "full_details": alert.full_details,
        "source_app": alert.source_app, "app_logo": alert.app_logo,
        "priority": alert.priority, "status": alert.status,
        "condition": alert.condition, "requires_response": alert.requires_response,
        "suggested_action": alert.suggested_action, "last_action": alert.last_action,
        "snoozed_until": iso_format(alert.snoozed_until),
        "triggered_at": iso_format(alert.triggered_at),
        "created_at": iso_format(alert.created_at), "updated_at": iso_format(alert.updated_at),
    }


def rule_out(rule: AlertRule) -> dict:
    return {
        "id": str(rule.id), "user_id": str(rule.profile_id), "name": rule.name,
        "description": rule.description, "apps": rule.apps, "condition": rule.condition,
        "priority": rule.priority, "notification_method": rule.notification_method,
        "frequency": rule.frequency, "action": rule.action, "status": rule.status,
        "next_check_at": iso_format(rule.next_check_at),
        "last_checked_at": iso_format(rule.last_checked_at),
        "created_at": iso_format(rule.created_at), "updated_at": iso_format(rule.updated_at),
    }


def create_alert_rule(profile: UserProfile, payload: dict) -> AlertRule:
    frequency = payload.get("frequency") or "real_time"
    interval = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "15_minutes": timedelta(minutes=15)}
    rule = AlertRule.objects.create(
        profile=profile,
        name=payload.get("name", ""), description=payload.get("description", ""),
        apps=payload.get("apps") or [], condition=payload.get("condition", ""),
        priority=normalize_priority(payload.get("priority")),
        notification_method=payload.get("notificationMethod") or "in_app",
        frequency=frequency, action=payload.get("action") or "notify",
        status=payload.get("status") or "active",
        next_check_at=timezone.now() + interval.get(frequency, timedelta(minutes=5)),
    )
    ml_service.record_feedback(
        profile,
        f"{rule.name} {rule.description} {rule.condition}",
        rule.priority,
        source="alert_rule",
    )
    publish_event(profile, "alert_rule_created", {"rule": rule_out(rule)})
    return rule


def _insert_alert(profile, dedupe_key, title, description, full_details, source_app,
                  priority, condition, requires_response, suggested_action, rule=None) -> Alert | None:
    if not dedupe_key or Alert.objects.filter(dedupe_key=dedupe_key).exists():
        return None
    alert = Alert.objects.create(
        profile=profile, rule=rule, dedupe_key=dedupe_key, title=title,
        description=description, full_details=full_details,
        source_app=source_app, app_logo=APP_LOGOS.get(source_app, "/003-email.png"),
        priority=normalize_priority(priority), status="triggered", condition=condition,
        requires_response=bool(requires_response), suggested_action=suggested_action,
    )
    publish_event(profile, "alert_created", {"alert": alert_out(alert)})
    return alert


def generate_alerts(profile: UserProfile) -> dict:
    activity = connected_activity(profile, "alerts")
    if not activity:
        return {"created": 0, "scanned": 0}
    candidates = []
    if settings.GEMINI_API_KEY:
        prompt = (
            "You are OmniSync. From this real connected-app activity, list at most 5 items "
            "that genuinely need attention. Do not invent anything.\n"
            f"Activity: {json.dumps(activity)}\n"
            "Return a JSON array of objects with activityId, sourceApp (gmail|whatsapp), title, "
            "description, fullDetails, priority (high|medium|low), requiresResponse (bool), "
            "condition and suggestedAction."
        )
        result = ai_generate(prompt, settings.GEMINI_MODEL)
        if isinstance(result, list):
            valid = {f"{item['app']}:{item['id']}" for item in activity}
            for item in result[:5]:
                key = f"{item.get('sourceApp')}:{item.get('activityId')}"
                if key in valid:
                    candidates.append({
                        "activityId": item.get("activityId"),
                        "sourceApp": "whatsapp" if item.get("sourceApp") == "whatsapp" else "gmail",
                        "title": item.get("title", "Item needs attention"),
                        "description": item.get("description", ""),
                        "fullDetails": item.get("fullDetails", ""),
                        "priority": normalize_priority(item.get("priority")),
                        "requiresResponse": bool(item.get("requiresResponse")),
                        "condition": item.get("condition", "AI auto-monitor detected important connected app activity"),
                        "suggestedAction": item.get("suggestedAction", "Review this item and respond, resolve, snooze, or convert it into a task."),
                    })
    if not candidates:
        for row in ml_service.rank_activity(profile, activity)[:8]:
            item = row["item"]
            prediction = row["prediction"]
            text = f"{item['title']} {item['description']} {item['body']}".lower()
            if prediction["label"] == "low" and not any(term in text for term in IMPORTANT_TERMS):
                continue
            candidates.append({
                "activityId": item["id"], "sourceApp": item["app"],
                "title": (
                    "Urgent item needs attention" if prediction["label"] == "high"
                    else "Email may need attention" if item["app"] == "gmail"
                    else "Message may need follow-up"
                ),
                "description": item["description"], "fullDetails": item["body"],
                "priority": "high" if URGENT_RE.search(text) or prediction["label"] == "high" else "medium",
                "requiresResponse": bool(RESPONSE_RE.search(text) or prediction["label"] == "high"),
                "condition": (
                    f"ML priority model classified this item as {prediction['label']} "
                    f"({int(prediction['confidence'] * 100)}% confidence)"
                ),
                "suggestedAction": "Review this item and respond, resolve, snooze, or convert it into a task.",
            })
        candidates = candidates[:5]

    created = 0
    for item in candidates:
        key = f"auto-ai:{profile.id}:{item['sourceApp']}:{item['activityId']}"
        alert = _insert_alert(
            profile, key, item["title"], item["description"], item["fullDetails"],
            item["sourceApp"], item["priority"], item["condition"],
            item["requiresResponse"], item["suggestedAction"],
        )
        if alert:
            created += 1
    return {"created": created, "scanned": len(activity)}


def generate_suggestions(profile: UserProfile) -> list[dict]:
    apps = profile.connected_apps(monitorable_only=True)
    if not apps:
        return []
    activity = connected_activity(profile, "alerts")
    suggestions: list[dict] = []
    gmail_items = [a for a in activity if a["app"] == "gmail"]
    wa_items = [a for a in activity if a["app"] == "whatsapp"]
    if gmail_items:
        suggestions.append({
            "title": "Unread email needing response",
            "description": f"Monitor {len(gmail_items)} unread Gmail message(s) for reply requests, approvals, deadlines, and client follow-ups.",
            "apps": ["gmail"], "priority": "high",
            "condition": "Unread Gmail contains reply, approve, urgent, deadline, invoice, review, or follow up",
            "action": "draft_reply",
        })
    if wa_items:
        suggestions.append({
            "title": "Message follow-up detector",
            "description": f"Watch recent WhatsApp activity across {len(wa_items)} message(s) for commitments and unanswered requests.",
            "apps": ["whatsapp"], "priority": "medium",
            "condition": "Recent WhatsApp message contains tomorrow, remind, send, confirm, review, or follow up",
            "action": "mark_follow_up",
        })
    if len(apps) > 1 and activity:
        suggestions.append({
            "title": "Cross-app urgent mention",
            "description": "Alert when any connected monitored app contains urgent language or a direct mention that requires action.",
            "apps": apps, "priority": "high",
            "condition": "Connected app activity contains urgent, blocked, asap, deadline, or direct action request",
            "action": "notify",
        })
    suggestions = suggestions[:3]

    if settings.GEMINI_API_KEY and activity:
        prompt = (
            "Suggest up to 3 alert rules using only these connected apps: " + ", ".join(apps) + ".\n"
            f"Real activity: {json.dumps(activity[:10])}\n"
            "Return JSON array with title, description, apps, priority (high|medium|low), "
            "condition, action (notify|draft_reply|create_task|mark_follow_up). Do not invent data."
        )
        result = ai_generate(prompt, settings.GEMINI_MODEL)
        if isinstance(result, list):
            filtered = [
                item for item in result
                if isinstance(item.get("apps"), list) and all(a in apps for a in item["apps"])
            ]
            if filtered:
                suggestions = filtered[:3]
    publish_event(profile, "alert_suggestions_updated", {"count": len(suggestions)})
    return suggestions


def apply_alert_action(profile: UserProfile, alert_id: str, action: str) -> Alert | None:
    alert = profile.alerts.filter(pk=alert_id).first()
    if not alert:
        return None
    statuses = {
        "resolved": "resolved", "snoozed": "snoozed", "task": "active",
        "follow_up": "active", "send_reply": "resolved",
    }
    alert.status = statuses.get(action, "active")
    if action == "snoozed":
        alert.snoozed_until = timezone.now() + timedelta(hours=2)
    if action in ("task", "follow_up", "send_reply"):
        alert.last_action = action
    alert.save()
    publish_event(profile, "alert_updated", {"alertId": str(alert.id), "status": alert.status})
    return alert


def alert_ai(alert: Alert, feature: str, reply_context: str = "") -> dict:
    prompts = {
        "summary": "Generate a concise, clear summary of this alert:\n",
        "next_action": "Suggest the single best next action for this alert:\n",
        "reply": "Draft a short, professional reply for this alert:\n",
    }
    base = prompts.get(feature, prompts["summary"])
    context = (
        f"Title: {alert.title}\nDescription: {alert.description}\n"
        f"Details: {alert.full_details}\nPriority: {alert.priority}\n"
        f"Suggested action: {alert.suggested_action}\nAdditional context: {reply_context}"
    )
    if not settings.GEMINI_API_KEY:
        fallback = {
            "summary": alert.description or alert.title,
            "next_action": alert.suggested_action or "Review this item and decide the next step.",
            "reply": f"Hi,\n\nThank you for the update regarding {alert.title}. I will review it and get back to you shortly.\n\nRegards,\nRahul",
        }
        return {"result": fallback.get(feature, fallback["summary"]), "simulated": True}
    text = ai_generate(
        "You are OmniSync, an advanced AI personal assistant. " + base + context,
        json_mode=False,
    )
    return {"result": text or "", "simulated": False}


def ai_draft(payload: dict) -> dict:
    context = payload.get("context") or payload.get("body") or ""
    if not context:
        raise ValueError("Context or body is required")
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("AI service not configured")
    kind = payload.get("type", "email")
    if kind == "email":
        prompt = (
            "Write a concise, professional email reply to the message below. "
            "Use 2-4 short paragraphs, no headers and no markdown. "
            f"From: {payload.get('from', '')}\nSubject: {payload.get('subject', '')}\n"
            f"Message: {context}\nReply type: {payload.get('replyType', 'reply')}"
        )
    else:
        prompt = (
            "Write a natural, friendly chat reply in 1-3 sentences to this message. "
            f"From: {payload.get('from', '')}\nMessage: {context}"
        )
    text = ai_generate(prompt, json_mode=False)
    if not text:
        raise RuntimeError("AI draft failed. Please try again.")
    return {"draft": text}


# ---------------------------------------------------------------------------
# Phone auth, waitlist, realtime helpers
# ---------------------------------------------------------------------------
def send_otp(phone: str, method: str) -> dict:
    if method not in ("sms", "whatsapp"):
        raise ValueError("Method must be 'sms' or 'whatsapp'")
    code = f"{random.randint(0, 999999):06d}"
    OtpCode.objects.filter(phone=phone, method=method, used=False).update(used=True)
    OtpCode.objects.create(
        phone=phone, method=method, code=code,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    delivered = False
    if settings.SENT_DM_API_KEY:
        status, _ = http_json("https://api.sent.dm/v1/messages", "POST", {
            "to": phone, "templateId": settings.SENT_DM_TEMPLATE_ID,
            "variables": {"otp": code},
        }, headers={"Authorization": f"Bearer {settings.SENT_DM_API_KEY}"})
        delivered = status in (200, 201, 202)
    result = {"success": True, "sent": delivered, "method": method}
    if settings.PHONE_AUTH_RETURN_OTP:
        result["otp"] = code
    return result


def verify_otp(phone: str, otp: str, method: str, name: str = "") -> dict:
    if not phone or not method:
        raise ValueError("Phone number and method are required")
    if method not in ("sms", "whatsapp"):
        raise ValueError("Method must be 'sms' or 'whatsapp'")
    record = OtpCode.objects.filter(
        phone=phone, method=method, code=str(otp), used=False,
        expires_at__gte=timezone.now(),
    ).order_by("-created_at").first()
    if not record:
        raise ValueError("Invalid or expired OTP. Please request a new code.")
    record.used = True
    record.save(update_fields=["used"])
    username = f"phone_{re.sub(chr(92) + 'D', '', phone)}"
    user, created = User.objects.get_or_create(username=username, defaults={"first_name": name or "User"})
    profile, profile_created = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            "phone": phone, "name": name or f"User {phone[-4:]}",
            "auth_provider": "phone", "verification_method": method,
        },
    )
    profile.last_login_at = timezone.now()
    profile.verification_method = method
    profile.save(update_fields=["last_login_at", "verification_method"])
    if created or profile_created:
        seed_demo_data(profile)
    return {
        "id": str(profile.id), "phone": phone, "name": profile.name,
        "auth_provider": "phone", "verification_method": method,
        "isNewUser": created or profile_created,
    }


def join_waitlist(email: str) -> WaitlistEntry:
    entry, _ = WaitlistEntry.objects.get_or_create(email=email.lower().strip())
    return entry


def serialize_chat(profile: UserProfile) -> list[dict]:
    return [
        {"sender": m.sender, "text": m.text, "suggestions": m.suggestions}
        for m in profile.chat_messages.order_by("created_at")
    ]


def save_chat(profile: UserProfile, sender: str, text: str, suggestions: list | None = None) -> None:
    ChatMessage.objects.create(profile=profile, sender=sender, text=text, suggestions=suggestions or [])


def clear_chat(profile: UserProfile) -> None:
    profile.chat_messages.all().delete()


def run_due_schedules(profile: UserProfile) -> int:
    """Generate briefings for every due schedule (used by the cron management command)."""
    due = profile.briefing_schedules.filter(next_run_at__lte=timezone.now())
    count = 0
    for schedule in due:
        create_briefing(profile, str(schedule.id))
        schedule.next_run_at = next_run_time(schedule.scheduled_time, schedule.frequency)
        schedule.save(update_fields=["next_run_at", "updated_at"])
        count += 1
    return count


def run_due_alert_rules(profile: UserProfile) -> int:
    """Evaluate active alert rules against connected activity."""
    created = 0
    for rule in profile.alert_rules.filter(status="active", next_check_at__lte=timezone.now()):
        activity = connected_activity(profile, "alerts")
        tokens = [t for t in re.split(r"[^a-z0-9@._-]+", (rule.condition or rule.name).lower()) if len(t) > 3]
        terms = tokens or ["urgent", "deadline", "asap", "blocked", "follow up", "reply", "approve", "review"]
        for item in activity[:5]:
            text = f"{item['title']} {item['description']} {item['body']}".lower()
            if not any(term in text for term in terms):
                continue
            requires = rule.action == "draft_reply" or bool(re.search(r"reply|respond|email|message", (rule.condition or ""), re.I))
            if rule.action == "create_task":
                suggested = "Convert this alert into a task and assign a due date."
            elif rule.action == "mark_follow_up":
                suggested = "Create a follow-up so this conversation does not go stale."
            else:
                suggested = "Review this item and decide the next step."
            alert = _insert_alert(
                profile, f"{rule.id}:{item['app']}:{item['id']}", item["title"],
                item["description"], item["body"], item["app"], rule.priority,
                rule.condition, requires, suggested, rule,
            )
            if alert:
                created += 1
        rule.last_checked_at = timezone.now()
        interval = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "15_minutes": timedelta(minutes=15)}
        rule.next_check_at = timezone.now() + interval.get(rule.frequency, timedelta(minutes=5))
        rule.save(update_fields=["last_checked_at", "next_check_at", "updated_at"])
    return created


def hash_password(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@transaction.atomic
def reset_demo(profile: UserProfile) -> None:
    """Restore the seeded demo state for the current user."""
    profile.gmail_messages.all().delete()
    profile.whatsapp_messages.all().delete()
    profile.whatsapp_contacts.all().delete()
    profile.alerts.all().delete()
    profile.alert_rules.all().delete()
    profile.generated_briefings.all().delete()
    profile.briefing_schedules.all().delete()
    profile.chat_messages.all().delete()
    profile.dashboard_brief = {}
    profile.integrations = {}
    profile.save(update_fields=["dashboard_brief", "integrations"])
    seed_demo_data(profile)
