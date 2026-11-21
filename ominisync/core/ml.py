import math
import re
from collections import Counter

from .models import MlTrainingExample

LABELS = ("high", "medium", "low")
TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had",
    "was", "were", "are", "your", "you", "our", "their", "them", "they", "was",
    "will", "would", "could", "should", "can", "not", "but", "all", "any", "out",
    "get", "got", "let", "one", "two", "its", "into", "about", "over", "after",
    "before", "just", "now", "how", "what", "when", "where", "who", "why", "please",
}

SEED_EXAMPLES = (
    ("high", "urgent deadline blocked asap action required approve invoice today"),
    ("high", "production server is down blocked release critical incident"),
    ("high", "client asked to sign the contract by end of day deadline"),
    ("high", "payment failed invoice overdue immediate attention required"),
    ("high", "security incident requires immediate review and approval"),
    ("high", "final slides due 4pm submit to John right away"),
    ("high", "please confirm the deadline moved to tomorrow urgent reply"),
    ("high", "customer escalation needs a response before the meeting"),
    ("high", "asap blocked approval needed to continue the deployment"),
    ("high", "legal review required for the contract today"),
    ("medium", "please review the proposal when you have time"),
    ("medium", "follow up on project timeline next week"),
    ("medium", "reminder sync about roadmap tomorrow"),
    ("medium", "could you check the draft and share feedback"),
    ("medium", "meeting schedule for Thursday attached"),
    ("medium", "team lunch on Friday confirm attendance"),
    ("medium", "share the weekly update after standup"),
    ("medium", "coffee chat tomorrow at 4:30 align milestones"),
    ("medium", "check schedule and confirm the time"),
    ("medium", "requested review of the budget proposal"),
    ("low", "thanks for the update"),
    ("low", "looks good to me"),
    ("low", "weekly newsletter with industry news"),
    ("low", "lunch menu for the office"),
    ("low", "social post likes and comments update"),
    ("low", "your package has been delivered"),
    ("low", "reminder to drink water"),
    ("low", "congratulations on the new role"),
    ("low", "here is the photo from the weekend"),
    ("low", "friends shared a funny video"),
)


def _stem(token):
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text):
    tokens = TOKEN_RE.findall(str(text or "").lower())
    return [
        _stem(token)
        for token in tokens
        if len(token) > 2 and token not in STOP_WORDS
    ]


class NaiveBayesClassifier:
    def __init__(self):
        self.class_counts = Counter()
        self.word_counts = {label: Counter() for label in LABELS}
        self.total_examples = 0
        self.vocabulary = set()

    def fit(self, examples):
        for label, text in examples:
            if label not in LABELS:
                continue
            tokens = tokenize(text)
            if not tokens:
                continue
            self.class_counts[label] += 1
            self.total_examples += 1
            for token in tokens:
                self.word_counts[label][token] += 1
                self.vocabulary.add(token)
        return self

    def predict_proba(self, text):
        tokens = tokenize(text)
        if not self.total_examples:
            return {label: 1 / len(LABELS) for label in LABELS}

        log_scores = {}
        vocabulary_size = max(len(self.vocabulary), 1)
        for label in LABELS:
            prior = (self.class_counts[label] + 1) / (self.total_examples + len(LABELS))
            word_total = sum(self.word_counts[label].values())
            score = math.log(prior)
            for token in tokens:
                likelihood = (self.word_counts[label][token] + 1) / (word_total + vocabulary_size)
                score += math.log(likelihood)
            log_scores[label] = score

        highest = max(log_scores.values())
        exponentials = {label: math.exp(value - highest) for label, value in log_scores.items()}
        total = sum(exponentials.values()) or 1
        return {label: exponentials[label] / total for label in LABELS}

    def predict(self, text):
        probabilities = self.predict_proba(text)
        return max(LABELS, key=lambda label: probabilities[label])


BASE_CLASSIFIER = NaiveBayesClassifier().fit(SEED_EXAMPLES)


def classifier_for(profile):
    examples = list(SEED_EXAMPLES)
    if profile is not None and getattr(profile, "pk", None):
        examples.extend(
            (row.label, row.text)
            for row in profile.ml_training_examples.all()
        )
    return NaiveBayesClassifier().fit(examples)


def predict_importance(profile, text):
    classifier = classifier_for(profile)
    probabilities = classifier.predict_proba(text)
    label = max(LABELS, key=lambda item: probabilities[item])
    return {
        "label": label,
        "confidence": round(probabilities[label], 4),
        "scores": {item: round(probabilities[item], 4) for item in LABELS},
        "model": "multinomial_naive_bayes",
    }


def record_feedback(profile, text, label, source="feedback"):
    if label not in LABELS:
        raise ValueError("Label must be high, medium, or low")
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("Text is required")
    MlTrainingExample.objects.create(profile=profile, text=cleaned, label=label, source=source)
    return predict_importance(profile, cleaned)


def rank_activity(profile, items):
    weight = {"high": 3, "medium": 2, "low": 1}
    ranked = []
    for index, item in enumerate(items):
        text = " ".join(str(item.get(key) or "") for key in ("title", "description", "body"))
        prediction = predict_importance(profile, text)
        ranked.append({
            "item": item,
            "prediction": prediction,
            "score": weight[prediction["label"]] + prediction["confidence"] / 10,
            "index": index,
        })
    ranked.sort(key=lambda row: (-row["score"], row["index"]))
    return [{"item": row["item"], "prediction": row["prediction"]} for row in ranked]
