import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Alert,
    AlertRule,
    BriefingSchedule,
    GeneratedBriefing,
    MlTrainingExample,
    OtpCode,
    UserProfile,
    WaitlistEntry,
)
from core.services import McpError, connect_simulated_gmail, get_or_create_profile, gmail_mcp


@override_settings(
    GEMINI_API_KEY="",
    GOOGLE_CLIENT_ID="",
    GOOGLE_CLIENT_SECRET="",
    SENT_DM_API_KEY="",
    PHONE_AUTH_RETURN_OTP=True,
    MOBILE_API_KEY="test-mobile-key",
    APP_URL="http://testserver",
)
class OmniSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="test@example.com",
            email="test@example.com",
            password="StrongPass123!",
            first_name="Test",
        )
        self.profile = get_or_create_profile(self.user)
        self.client.force_login(self.user)
        self.enable_simulated_gmail()

    def enable_simulated_gmail(self):
        response = self.post_json("/api/gmail-connect", {
            "userId": str(self.profile.id), "simulate": True
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.profile.refresh_from_db()

    def post_json(self, path, data):
        return self.client.post(path, data=json.dumps(data), content_type="application/json")

    def delete_json(self, path, data=None):
        return self.client.delete(path, data=json.dumps(data or {}), content_type="application/json")

    def mcp_result(self, response):
        return json.loads(response.json()["result"]["content"][0]["text"])

    def test_seeded_demo_data_and_integrations(self):
        self.assertEqual(self.profile.gmail_messages.count(), 4)
        self.assertEqual(self.profile.whatsapp_messages.count(), 11)
        self.assertEqual(self.profile.whatsapp_contacts.count(), 4)
        self.assertEqual(set(self.profile.connected_apps()), {"gmail", "whatsapp"})

    def test_gmail_starts_disconnected_and_opt_in_is_explicit(self):
        other_user = User.objects.create_user(username="fresh@example.com", password="StrongPass123!")
        fresh_profile = get_or_create_profile(other_user)
        self.assertNotIn("gmail", fresh_profile.connected_apps())
        self.assertIn("whatsapp", fresh_profile.connected_apps())
        with self.assertRaises(McpError) as error:
            gmail_mcp(fresh_profile, "gmail_list_messages", {})
        self.assertIn("not connected", error.exception.message)
        with patch("core.services.publish_event"):
            connect_simulated_gmail(fresh_profile)
        fresh_profile.refresh_from_db()
        self.assertTrue(fresh_profile.integration("gmail")["isSimulated"])
        self.assertEqual(len(gmail_mcp(fresh_profile, "gmail_list_messages", {})["messages"]), 4)

    def test_public_pages(self):
        for url in ("/", "/sign-in", "/sign-up", "/pair", "/offline"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)

    def test_root_static_assets(self):
        for url in ("/001-gmail.png", "/002-whatsapp.png", "/manifest.webmanifest", "/sw.js", "/icons/icon-192.png", "/favicon.ico"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)

    def test_anonymous_dashboard_redirects(self):
        self.client.logout()
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/sign-in", response["Location"])

    def test_all_dashboard_tabs_render(self):
        tabs = ["dashboard", "ai-agent", "briefing", "inbox", "follow-ups", "integrations", "alerts", "settings", "pricing"]
        for tab in tabs:
            response = self.client.get("/dashboard?tab=" + tab)
            self.assertEqual(response.status_code, 200, tab)
            self.assertContains(response, "OmniSync")

    def test_signup_creates_user_and_profile(self):
        self.client.logout()
        response = self.client.post("/sign-up", {
            "name": "Nina", "email": "nina@example.com", "password": "NewStrongPass123!"
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="nina@example.com").exists())
        profile = UserProfile.objects.get(user__username="nina@example.com")
        self.assertEqual(profile.name, "Nina")
        self.assertNotIn("gmail", profile.connected_apps())
        self.assertEqual(profile.gmail_messages.count(), 4)

    def test_signin_valid_and_invalid(self):
        self.client.logout()
        response = self.client.post("/sign-in", {"email": "test@example.com", "password": "StrongPass123!"})
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/sign-in", {"email": "test@example.com", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Failed to sign in. Please verify your credentials.")

    def test_google_sign_in_requires_configuration_message(self):
        self.client.logout()
        response = self.client.get("/auth/google", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Google sign-in needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")

    def test_google_sign_in_oauth_flow_logs_into_dashboard(self):
        self.client.logout()
        with override_settings(
            GOOGLE_CLIENT_ID="client-id",
            GOOGLE_CLIENT_SECRET="secret",
            GOOGLE_SIGNIN_REDIRECT_URI="http://testserver/auth/callback?provider=google",
        ):
            start = self.client.get("/auth/google")
            self.assertEqual(start.status_code, 302)
            self.assertIn("accounts.google.com/o/oauth2/v2/auth", start["Location"])
            self.assertIn("client_id=client-id", start["Location"])
            state = self.client.session["google_oauth_state"]
            with patch("core.views.http_json", side_effect=[
                (200, {"access_token": "ya29.signin"}),
                (200, {
                    "email": "google.user@gmail.com", "email_verified": True,
                    "name": "Google User", "given_name": "Google", "family_name": "User",
                }),
            ]) as mocked:
                callback = self.client.get(
                    "/auth/callback?provider=google&code=google-code&state=" + state
                )
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(callback["Location"], "/dashboard")
        user = User.objects.get(username="google.user@gmail.com")
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        user.refresh_from_db()
        profile = user.profile
        self.assertEqual(profile.auth_provider, "google")

    def test_phone_otp_flow(self):
        self.client.logout()
        sent = self.post_json("/api/phone-auth/send-otp", {"phoneNumber": "+15551234567", "method": "sms"})
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(len(sent.json()["otp"]), 6)
        self.assertIn("Local mode", sent.json()["message"])
        verified = self.post_json("/api/phone-auth/verify-otp", {
            "phoneNumber": "+15551234567", "method": "sms", "otp": sent.json()["otp"], "name": "Phone User"
        })
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.json()["success"])
        self.assertEqual(verified.json()["user"]["name"], "Phone User")
        self.assertFalse(OtpCode.objects.filter(phone="+15551234567", used=False).exists())
        replay = self.post_json("/api/phone-auth/verify-otp", {
            "phoneNumber": "+15551234567", "method": "sms", "otp": sent.json()["otp"]
        })
        self.assertEqual(replay.status_code, 401)

    def test_mobile_connect_key(self):
        self.client.logout()
        response = self.client.get("/api/mobile/connect?key=test-mobile-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["app"], "omniscyn")
        response = self.client.get("/api/mobile/connect?key=bad")
        self.assertEqual(response.status_code, 401)

    def test_waitlist(self):
        response = self.post_json("/api/waitlist", {"email": "leader@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["waitlistCount"], 1429)
        self.assertTrue(WaitlistEntry.objects.filter(email="leader@example.com").exists())

    def test_dashboard_brief_cache_and_force(self):
        first = self.post_json("/api/dashboard-brief", {"userId": str(self.profile.id)})
        self.assertEqual(first.status_code, 200)
        self.assertIn("stats", first.json())
        self.assertEqual(first.json()["model"], "multinomial_naive_bayes")
        self.assertTrue(first.json()["brief"])
        self.assertIn("mlPrediction", first.json()["brief"][0])
        self.assertFalse(first.json()["isFromCache"])
        cached = self.post_json("/api/dashboard-brief", {"userId": str(self.profile.id)})
        self.assertTrue(cached.json()["isFromCache"])
        forced = self.post_json("/api/dashboard-brief", {"userId": str(self.profile.id), "forceRegenerate": True})
        self.assertFalse(forced.json()["isFromCache"])

    def test_ai_chat_persistence_and_clear(self):
        response = self.post_json("/api/ai-chat", {"userId": str(self.profile.id), "prompt": "What is important?", "history": []})
        self.assertEqual(response.status_code, 200)
        self.assertIn("response", response.json())
        self.assertEqual(len(response.json()["suggestions"]), 3)
        self.assertEqual(self.profile.chat_messages.count(), 2)
        history = self.client.get("/api/chat/history")
        self.assertEqual(len(history.json()), 2)
        cleared = self.post_json("/api/chat/clear", {"userId": str(self.profile.id)})
        self.assertEqual(cleared.json()["messages"], [])
        self.assertEqual(self.profile.chat_messages.count(), 0)

    def test_ml_prediction_and_feedback(self):
        user_id = str(self.profile.id)
        urgent = self.post_json("/api/ml/predict", {
            "userId": user_id, "text": "Urgent deadline blocked please respond today"
        })
        self.assertEqual(urgent.status_code, 200)
        self.assertEqual(urgent.json()["label"], "high")
        self.assertEqual(urgent.json()["model"], "multinomial_naive_bayes")
        low = self.post_json("/api/ml/predict", {
            "userId": user_id, "text": "thanks for the update"
        })
        self.assertEqual(low.json()["label"], "low")
        feedback = self.post_json("/api/ml/feedback", {
            "userId": user_id, "text": "Renew the server certificate urgently", "label": "high",
            "source": "test"
        })
        self.assertTrue(feedback.json()["success"])
        self.assertTrue(MlTrainingExample.objects.filter(profile=self.profile, label="high").exists())
        invalid = self.post_json("/api/ml/feedback", {
            "userId": user_id, "text": "Something happened", "label": "critical"
        })
        self.assertEqual(invalid.status_code, 400)

    def test_gmail_mcp_methods(self):
        base = {"userId": str(self.profile.id)}
        listed = self.post_json("/api/gmail-mcp", {**base, "method": "gmail_list_messages", "params": {"q": "label:inbox", "maxResults": 10}})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(self.mcp_result(listed)["messages"]), 4)
        searched = self.post_json("/api/gmail-mcp", {**base, "method": "gmail_search_messages", "params": {"q": "NDA"}})
        self.assertEqual(len(self.mcp_result(searched)["messages"]), 1)
        detail = self.post_json("/api/gmail-mcp", {**base, "method": "gmail_get_message", "params": {"id": "msg_12345"}})
        self.assertEqual(self.mcp_result(detail)["id"], "msg_12345")
        missing = self.post_json("/api/gmail-mcp", {**base, "method": "gmail_get_message", "params": {"id": "missing"}})
        self.assertEqual(missing.json()["error"]["code"], -32602)
        sent = self.post_json("/api/gmail-mcp", {**base, "method": "gmail_send_message", "params": {"to": "x@example.com", "subject": "Hi", "body": "Hello"}})
        self.assertTrue(self.mcp_result(sent)["success"])
        draft = self.post_json("/api/gmail-mcp", {**base, "method": "gmail_create_draft", "params": {"to": "x@example.com", "subject": "Draft", "body": "Hello"}})
        self.assertTrue(self.mcp_result(draft)["success"])
        thread = self.post_json("/api/gmail-mcp", {**base, "method": "gmail_get_thread", "params": {"id": "thread_1"}})
        self.assertEqual(len(self.mcp_result(thread)["messages"]), 1)
        invalid = self.post_json("/api/gmail-mcp", {**base, "method": "not_a_method"})
        self.assertEqual(invalid.json()["error"]["code"], -32601)
        no_method = self.post_json("/api/gmail-mcp", base)
        self.assertEqual(no_method.status_code, 400)

    def test_gmail_connect_get_and_missing_credentials(self):
        response = self.client.get("/api/gmail-connect")
        body = response.json()
        self.assertFalse(body["configured"])
        self.assertEqual(body["clientId"], "")
        self.assertEqual(body["authUrl"], "")
        invalid_state = self.post_json("/api/gmail-connect", {
            "userId": str(self.profile.id), "code": "abc", "state": "wrong"
        })
        self.assertEqual(invalid_state.status_code, 400)
        session = self.client.session
        session["gmail_oauth_state"] = "state-123"
        session.save()
        response = self.post_json("/api/gmail-connect", {
            "userId": str(self.profile.id), "code": "abc", "state": "state-123"
        })
        self.assertEqual(response.status_code, 500)
        self.assertIn("GOOGLE_CLIENT_ID", response.json()["error"])

    def test_gmail_connect_builds_oauth_url_with_state(self):
        with override_settings(
            GOOGLE_CLIENT_ID="client-id",
            GOOGLE_CLIENT_SECRET="secret",
            GOOGLE_REDIRECT_URI="http://testserver/auth/gmail-callback",
        ):
            response = self.client.get("/api/gmail-connect")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["configured"])
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", body["authUrl"])
        self.assertIn("client_id=client-id", body["authUrl"])
        self.assertIn("gmail.modify", body["authUrl"])
        self.assertIn("state=", body["authUrl"])
        self.assertIn("gmail_oauth_state", self.client.session)

    def test_gmail_connect_exchanges_real_code_and_uses_gmail_api(self):
        session = self.client.session
        session["gmail_oauth_state"] = "state-456"
        session.save()
        with override_settings(
            GOOGLE_CLIENT_ID="client-id",
            GOOGLE_CLIENT_SECRET="secret",
            GOOGLE_REDIRECT_URI="http://testserver/auth/gmail-callback",
        ):
            with patch("core.api.http_json", side_effect=[
                (200, {
                    "access_token": "ya29.real", "refresh_token": "refresh-token",
                    "expires_in": 3600, "token_type": "Bearer",
                }),
                (200, {"emailAddress": "real.user@gmail.com"}),
            ]) as mocked:
                response = self.post_json("/api/gmail-connect", {
                    "userId": str(self.profile.id), "code": "real-code", "state": "state-456"
                })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked.call_count, 2)
        self.profile.refresh_from_db()
        gmail = self.profile.integration("gmail")
        self.assertFalse(gmail["isSimulated"])
        self.assertEqual(gmail["email"], "real.user@gmail.com")
        self.assertEqual(gmail["accessToken"], "ya29.real")
        with patch("core.services._gmail_real", return_value={"messages": []}) as real_call:
            gmail_mcp(self.profile, "gmail_list_messages", {})
        self.assertEqual(real_call.call_count, 1)

    def test_whatsapp_connect_and_mcp_methods(self):
        base = {"userId": str(self.profile.id)}
        status = self.post_json("/api/whatsapp-connect", {**base, "action": "status"})
        self.assertEqual(status.json()["status"], "connected")
        recent = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_get_recent_messages"})
        self.assertGreaterEqual(len(self.mcp_result(recent)["messages"]), 4)
        history = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_get_chat_history", "params": {"chatId": "123456789@s.whatsapp.net", "limit": 3}})
        self.assertEqual(len(self.mcp_result(history)["messages"]), 3)
        sent = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_send_message", "params": {"to": "+123456789", "body": "OK"}})
        self.assertTrue(self.mcp_result(sent)["success"])
        search = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_search_chats", "params": {"query": "Alex"}})
        self.assertGreaterEqual(len(self.mcp_result(search)["chats"]), 1)
        summary = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_summarize_conversations"})
        self.assertIn("OmniSync Daily WhatsApp Brief", self.mcp_result(summary)["summary"])
        contact = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_get_contact_details", "params": {"jidOrPhone": "123456789@s.whatsapp.net"}})
        self.assertEqual(self.mcp_result(contact)["name"], "Sarah Jenkins")
        groups = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_list_groups"})
        self.assertEqual(len(self.mcp_result(groups)["groups"]), 1)
        group_messages = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_get_group_messages", "params": {"groupId": "120363024555555555@g.us"}})
        self.assertEqual(len(self.mcp_result(group_messages)["messages"]), 3)
        group_send = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_send_group_messages", "params": {"groupId": "120363024555555555@g.us", "body": "Update"}})
        self.assertTrue(self.mcp_result(group_send)["success"])
        invalid = self.post_json("/api/whatsapp-mcp", {**base, "method": "nope"})
        self.assertEqual(invalid.json()["error"]["code"], -32601)

    def test_whatsapp_connect_validation_disconnect_and_reconnect(self):
        base = {"userId": str(self.profile.id)}
        invalid = self.post_json("/api/whatsapp-connect", {**base, "action": "connect", "phoneNumber": "123"})
        self.assertEqual(invalid.status_code, 400)
        missing = self.post_json("/api/whatsapp-connect", {**base, "action": "connect"})
        self.assertEqual(missing.status_code, 400)
        disconnected = self.post_json("/api/whatsapp-connect", {**base, "action": "disconnect"})
        self.assertEqual(disconnected.json()["status"], "disconnected")
        blocked = self.post_json("/api/whatsapp-mcp", {**base, "method": "whatsapp_get_recent_messages"})
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.json()["error"]["code"], -32001)
        simulated = self.post_json("/api/whatsapp-connect", {**base, "action": "connect-simulated"})
        self.assertTrue(simulated.json()["isSimulated"])
        with patch("core.services._bridge_request", return_value={
            "success": True, "status": "connecting", "isSimulated": False,
            "pairingCode": "ABCD2345", "pairingCodeExpiresAt": 4102444800000,
        }):
            paired = self.post_json("/api/whatsapp-connect", {**base, "action": "connect", "phoneNumber": "+919876543210"})
        self.assertEqual(len(paired.json()["pairingCode"]), 8)
        self.assertFalse(paired.json()["isSimulated"])

    def test_briefings_and_schedules(self):
        user_id = str(self.profile.id)
        empty = self.client.get("/api/briefings?userId=" + user_id)
        self.assertEqual(empty.json(), [])
        created = self.post_json("/api/briefings", {"userId": user_id})
        self.assertEqual(created.status_code, 200)
        briefing_id = created.json()["id"]
        self.assertTrue(GeneratedBriefing.objects.filter(pk=briefing_id).exists())
        fetched = self.client.get("/api/briefings?userId=" + user_id + "&id=" + briefing_id)
        self.assertEqual(fetched.json()["id"], briefing_id)
        missing = self.client.get("/api/briefings?userId=" + user_id + "&id=00000000-0000-0000-0000-000000000000")
        self.assertEqual(missing.status_code, 404)

        schedule = self.post_json("/api/briefings/schedules", {
            "userId": user_id, "name": "Morning Digest", "description": "Daily",
            "apps": ["gmail", "whatsapp"], "categories": ["email"],
            "scheduledTime": "09:00", "frequency": "daily", "priorityLevel": "medium"
        })
        self.assertEqual(schedule.status_code, 200)
        schedule_id = schedule.json()["id"]
        schedules = self.client.get("/api/briefings/schedules?userId=" + user_id)
        self.assertEqual(len(schedules.json()), 1)
        deleted = self.delete_json("/api/briefings/schedules", {"userId": user_id, "id": schedule_id})
        self.assertEqual(deleted.json(), {"success": True})
        self.assertFalse(BriefingSchedule.objects.filter(pk=schedule_id).exists())
        invalid = self.post_json("/api/briefings/schedules", {"userId": user_id})
        self.assertEqual(invalid.status_code, 400)

    def test_briefing_ai_draft_errors(self):
        response = self.post_json("/api/briefings/ai-draft", {"body": "Hello"})
        self.assertEqual(response.status_code, 503)
        missing = self.post_json("/api/briefings/ai-draft", {})
        self.assertEqual(missing.status_code, 400)

    def test_briefing_detail_pages(self):
        briefing = GeneratedBriefing.objects.create(
            profile=self.profile, title="Daily Brief", summary="Summary",
            stats={"email": 1, "messages": 1}, data={"email": [], "messages": []},
        )
        response = self.client.get("/dashboard/briefing/" + str(briefing.id) + "?category=messages")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Daily Brief")
        response = self.client.get("/dashboard/briefing/00000000-0000-0000-0000-000000000000")
        self.assertEqual(response.status_code, 404)

    def test_alerts_flow(self):
        user_id = str(self.profile.id)
        empty = self.client.get("/api/alerts?userId=" + user_id)
        self.assertEqual(empty.json(), [])
        generated = self.post_json("/api/alerts/generate", {"userId": user_id})
        self.assertEqual(generated.status_code, 200)
        self.assertGreater(generated.json()["created"], 0)
        rows = self.client.get("/api/alerts?userId=" + user_id)
        self.assertGreaterEqual(len(rows.json()), 1)
        self.assertTrue(any("ML priority model" in row["condition"] for row in rows.json()))
        alert_id = rows.json()[0]["id"]
        suggestions = self.client.get("/api/alerts/suggestions?userId=" + user_id)
        self.assertGreaterEqual(len(suggestions.json()), 1)

        rule = self.post_json("/api/alerts/rules", {
            "userId": user_id, "name": "Urgent", "description": "Watch urgent mail",
            "apps": ["gmail"], "condition": "urgent or deadline", "priority": "high",
            "notificationMethod": "in_app", "frequency": "real_time", "action": "notify"
        })
        self.assertEqual(rule.status_code, 200)
        self.assertTrue(AlertRule.objects.filter(pk=rule.json()["id"]).exists())

        snoozed = self.post_json("/api/alerts/actions", {"userId": user_id, "alertId": alert_id, "action": "snoozed"})
        self.assertEqual(snoozed.json()["status"], "snoozed")
        resolved = self.post_json("/api/alerts/actions", {"userId": user_id, "alertId": alert_id, "action": "resolved"})
        self.assertEqual(resolved.json()["status"], "resolved")

        summary = self.post_json("/api/alerts/ai", {"alertId": alert_id, "feature": "summary"})
        self.assertIn("result", summary.json())
        reply = self.post_json("/api/alerts/ai", {"alertId": alert_id, "feature": "reply"})
        self.assertIn("result", reply.json())
        invalid = self.post_json("/api/alerts/ai", {"alertId": alert_id, "feature": "bad"})
        self.assertEqual(invalid.status_code, 400)
        missing_fields = self.post_json("/api/alerts/actions", {"userId": user_id})
        self.assertEqual(missing_fields.status_code, 400)

    def test_assistant_settings_and_realtime_events(self):
        response = self.post_json("/api/assistant-settings", {"userId": str(self.profile.id), "settings": {"responseTone": "direct"}})
        self.assertEqual(response.json()["responseTone"], "direct")
        fetched = self.client.get("/api/assistant-settings")
        self.assertEqual(fetched.json()["responseTone"], "direct")
        self.post_json("/api/alerts/rules", {
            "userId": str(self.profile.id), "name": "Rule", "condition": "urgent", "apps": ["gmail"]
        })
        events = self.client.get("/api/realtime-events")
        self.assertEqual(events.status_code, 200)
        self.assertTrue(any(item["event"] == "alert_rule_created" for item in events.json()))

    def test_integration_disconnect(self):
        response = self.post_json("/api/integrations/disconnect", {"userId": str(self.profile.id), "app": "gmail"})
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertNotIn("gmail", self.profile.connected_apps())
        blocked = self.post_json("/api/briefings", {"userId": str(self.profile.id)})
        self.assertEqual(blocked.status_code, 200)

    def test_run_due_jobs_command(self):
        schedule = BriefingSchedule.objects.create(
            profile=self.profile, name="Due", frequency="daily",
            next_run_at=timezone.now() - timedelta(minutes=1),
        )
        call_command("run_due_jobs", "--profile-id", str(self.profile.id))
        self.assertTrue(GeneratedBriefing.objects.filter(schedule=schedule).exists())
        schedule.refresh_from_db()
        self.assertGreater(schedule.next_run_at, timezone.now())

    def test_user_id_compatibility_without_session(self):
        self.client.logout()
        anonymous = Client()
        response = anonymous.post(
            "/api/dashboard-brief",
            data=json.dumps({"userId": str(self.profile.id)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
