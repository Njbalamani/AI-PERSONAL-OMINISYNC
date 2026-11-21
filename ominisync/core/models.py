"""Database models for the OmniSync Django port.

The original project stored most state in InsForge/PostgREST tables.  Every one
of those tables is represented here as a first-class Django model so the app is
fully persistent without any external backend.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class UserProfile(models.Model):
    """One profile per Django auth user; mirrors the original `users` table."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    phone = models.CharField(max_length=32, blank=True, db_index=True)
    name = models.CharField(max_length=200, blank=True)
    auth_provider = models.CharField(max_length=32, default="email")
    verification_method = models.CharField(max_length=16, blank=True)
    integrations = models.JSONField(default=dict, blank=True)
    dashboard_brief = models.JSONField(default=dict, blank=True)
    assistant_settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.name or getattr(self.user, "first_name", "") or self.user.email or self.user.username

    @property
    def app_user_id(self):
        return str(self.id)

    def connected_apps(self, monitorable_only=False):
        allowed = {"gmail", "whatsapp"} if monitorable_only else None
        result = []
        for key, value in (self.integrations or {}).items():
            if isinstance(value, dict) and value.get("connected"):
                if allowed is None or key in allowed:
                    result.append(key)
        return result

    def integration(self, app):
        value = (self.integrations or {}).get(app)
        return value if isinstance(value, dict) else {}

    def set_integration(self, app, data):
        integrations = dict(self.integrations or {})
        integrations[app] = data
        self.integrations = integrations
        self.save(update_fields=["integrations"])

    def clear_integration(self, app):
        integrations = dict(self.integrations or {})
        integrations.pop(app, None)
        self.integrations = integrations
        self.save(update_fields=["integrations"])

    def get_setting(self, key, default=None):
        return (self.assistant_settings or {}).get(key, default)


class OtpCode(models.Model):
    """Single-use, short-lived phone verification code."""

    phone = models.CharField(max_length=32, db_index=True)
    method = models.CharField(max_length=16)
    code = models.CharField(max_length=6)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class WaitlistEntry(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ChatMessage(models.Model):
    """Persistent AI-agent conversation history."""

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="chat_messages")
    sender = models.CharField(max_length=16)
    text = models.TextField()
    suggestions = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class GmailMessage(models.Model):
    """A simulated or cached Gmail message/draft/sent item."""

    INBOX = "inbox"
    SENT = "sent"
    DRAFT = "draft"
    KIND_CHOICES = [(INBOX, "Inbox"), (SENT, "Sent"), (DRAFT, "Draft")]

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="gmail_messages")
    message_id = models.CharField(max_length=128)
    thread_id = models.CharField(max_length=128, blank=True)
    sender = models.CharField(max_length=255, blank=True)
    recipient = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=300, blank=True)
    date = models.CharField(max_length=120, blank=True)
    snippet = models.TextField(blank=True)
    body = models.TextField(blank=True)
    labels = models.JSONField(default=list, blank=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=INBOX)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["profile", "kind"])]


class WhatsAppContact(models.Model):
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="whatsapp_contacts")
    jid = models.CharField(max_length=160)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=40, blank=True)
    verified_name = models.CharField(max_length=200, blank=True)
    is_group = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]


class WhatsAppMessage(models.Model):
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="whatsapp_messages")
    message_id = models.CharField(max_length=128)
    chat_id = models.CharField(max_length=160)
    sender = models.CharField(max_length=200)
    body = models.TextField()
    from_me = models.BooleanField(default=False)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["timestamp"]
        constraints = [models.UniqueConstraint(fields=["profile", "message_id"], name="unique_wa_message_per_profile")]


class WhatsAppSession(models.Model):
    profile = models.OneToOneField(UserProfile, on_delete=models.CASCADE, related_name="whatsapp_session")
    phone_number = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=24, default="disconnected")
    pairing_code = models.CharField(max_length=16, blank=True)
    pairing_expires_at = models.DateTimeField(null=True, blank=True)
    is_simulated = models.BooleanField(default=False)
    connected_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    def __str__(self):
        return f"{self.profile} — {self.status}"


class AlertRule(models.Model):
    """Mirrors `public.alert_rules` from db/alerts.sql."""

    PRIORITIES = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
    STATUSES = [("active", "Active"), ("paused", "Paused"), ("archived", "Archived")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="alert_rules")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    apps = models.JSONField(default=list, blank=True)
    condition = models.TextField()
    priority = models.CharField(max_length=8, choices=PRIORITIES, default="medium")
    notification_method = models.CharField(max_length=32, default="in_app")
    frequency = models.CharField(max_length=32, default="real_time")
    action = models.CharField(max_length=32, default="notify")
    status = models.CharField(max_length=16, choices=STATUSES, default="active")
    next_check_at = models.DateTimeField(default=timezone.now)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class Alert(models.Model):
    """Mirrors `public.alerts` from db/alerts.sql."""

    PRIORITIES = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
    STATUSES = [
        ("active", "Active"),
        ("triggered", "Triggered"),
        ("resolved", "Resolved"),
        ("snoozed", "Snoozed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="alerts")
    rule = models.ForeignKey(AlertRule, on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts")
    dedupe_key = models.CharField(max_length=300, blank=True, null=True, unique=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    full_details = models.TextField(blank=True, default="")
    source_app = models.CharField(max_length=32)
    app_logo = models.CharField(max_length=120, blank=True)
    priority = models.CharField(max_length=8, choices=PRIORITIES, default="medium")
    status = models.CharField(max_length=16, choices=STATUSES, default="triggered")
    condition = models.TextField(blank=True, default="")
    requires_response = models.BooleanField(default=False)
    suggested_action = models.TextField(blank=True, default="")
    last_action = models.CharField(max_length=32, blank=True, null=True)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    triggered_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["profile", "-created_at"])]


class BriefingSchedule(models.Model):
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="briefing_schedules")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    apps = models.JSONField(default=list, blank=True)
    categories = models.JSONField(default=list, blank=True)
    scheduled_time = models.CharField(max_length=8, default="09:00")
    frequency = models.CharField(max_length=16, default="daily")
    priority_level = models.CharField(max_length=16, default="medium")
    next_run_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class GeneratedBriefing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="generated_briefings")
    schedule = models.ForeignKey(BriefingSchedule, on_delete=models.SET_NULL, null=True, blank=True)
    title = models.CharField(max_length=255)
    summary = models.TextField(blank=True, default="")
    stats = models.JSONField(default=dict, blank=True)
    data = models.JSONField(default=dict, blank=True)
    ai_generated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class MlTrainingExample(models.Model):
    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="ml_training_examples")
    text = models.TextField()
    label = models.CharField(max_length=16)
    source = models.CharField(max_length=32, default="feedback")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class RealtimeEvent(models.Model):
    """Audit trail for the realtime events the original app published."""

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="realtime_events")
    event = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
