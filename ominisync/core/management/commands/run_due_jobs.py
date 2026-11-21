from django.core.management.base import BaseCommand

from core.models import UserProfile
from core.services import run_due_alert_rules, run_due_schedules


class Command(BaseCommand):
    help = "Generate due briefings and evaluate due alert rules."

    def add_arguments(self, parser):
        parser.add_argument("--profile-id", dest="profile_id", default="")

    def handle(self, *args, **options):
        profiles = UserProfile.objects.all()
        if options["profile_id"]:
            profiles = profiles.filter(pk=options["profile_id"])
        briefings = 0
        alerts = 0
        for profile in profiles:
            briefings += run_due_schedules(profile)
            alerts += run_due_alert_rules(profile)
        self.stdout.write(self.style.SUCCESS(
            f"Generated {briefings} briefing(s) and {alerts} alert(s)."
        ))
