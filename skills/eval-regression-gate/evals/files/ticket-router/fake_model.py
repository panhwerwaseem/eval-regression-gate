#!/usr/bin/env python3
"""A stand-in for a model call. Used only by the eval fixtures and the tests.

Reads a support ticket on stdin and prints one line of JSON with a category,
a priority and a reply, the way a ticket-routing prompt would. It is a keyword
router, not a model, so the fixtures can be regenerated offline and always
come out the same. Each variant plays one version of the prompt:

  baseline      misses outage tickets (they are routed to "other")
  regression    the refund rule is gone, so refund tickets land in "billing"
  improvement   baseline plus a working outage rule
  noise         the same rules as baseline; only the unstable tickets differ

Tickets that mention both an invoice and a password are unstable in every
variant: the category is "login" about two times in three. The choice is a
hash of the variant's seed, EVAL_CASE_ID and EVAL_TRIAL, which run_cases.py
sets, so a given trial always gives the same answer.

Usage: fake_model.py --variant baseline < ticket.txt
"""
import argparse
import hashlib
import json
import os
import sys

REFUND = ("refund", "money back", "charged twice")
OUTAGE = ("outage", "is down", "502", "not loading", "unavailable")
LOGIN = ("password", "log in", "sign in", "locked out")
SHIPPING = ("delivery", "tracking", "parcel", "courier")
BILLING = ("invoice", "receipt", "billing")
REPLIES = {
    "refund": "We have opened a refund request and will confirm by email.",
    "billing": "Your invoice is available under Account, Billing.",
    "outage": "We are investigating. Follow the status page for updates.",
    "login": "We have sent a password reset link to your email address.",
    "shipping": "Your tracking number is in the dispatch email.",
    "other": "Thanks for getting in touch. Could you tell us a bit more?",
}
SEEDS = {"baseline": "seed-b", "regression": "seed-d", "improvement": "seed-k", "noise": "seed-h"}


def mentions(text, words):
    return any(word in text for word in words)


def coin(variant):
    """True about two times in three, fixed for a given variant, case and trial."""
    key = "%s|%s|%s" % (SEEDS[variant], os.environ.get("EVAL_CASE_ID", ""),
                        os.environ.get("EVAL_TRIAL", "1"))
    return hashlib.sha256(key.encode("utf-8")).digest()[0] % 3 != 0


def route(text, variant):
    text = text.lower()
    if mentions(text, BILLING) and mentions(text, LOGIN):
        return "login" if coin(variant) else "billing"
    if mentions(text, REFUND):
        return "billing" if variant == "regression" else "refund"
    if mentions(text, OUTAGE):
        return "outage" if variant == "improvement" else "other"
    if mentions(text, LOGIN):
        return "login"
    if mentions(text, SHIPPING):
        return "shipping"
    if mentions(text, BILLING):
        return "billing"
    return "other"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", choices=sorted(SEEDS), default="baseline")
    args = parser.parse_args()
    category = route(sys.stdin.read(), args.variant)
    answer = {"category": category,
              "priority": "high" if category in ("outage", "refund") else "normal",
              "reply": REPLIES[category]}
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
