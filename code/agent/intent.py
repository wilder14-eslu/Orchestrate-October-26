"""Deterministic, multilingual, negation-aware classification of request_text.

Rules fire in a fixed order; the first match decides and is recorded for the trace.
Non-deletion rules run first so that a negated or scoped deletion word ("do not
delete", "remove me from marketing lists only") never authorizes a deletion.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

DELETE_TERMS = (r"delet\w*|eras\w*|remov\w*|close\w*|clos\w*|wipe\w*|purg\w*|"
                r"elimin\w*|borr\w*|suprim\w*|exclu\w*|apag\w*|supprim\w*|suppression|effac\w*|"
                r"삭제|탈퇴|削除|消去|退会")
NEGATED_DELETION = re.compile(
    r"\b(?:do\s+not|don['’]t|dont|never|without|not\s+(?:asking\s+for|a\s+request\s+to|requesting)|no\s+(?:quiero|deseo)|"
    r"nao\s+quero|ne\s+(?:pas|veux\s+pas))\b[\w\s,'’]{0,30}?\b(?:" + DELETE_TERMS + r")", re.I)
NEGATED_DELETION_TAIL = re.compile(r"\b(?:" + DELETE_TERMS + r")\w*\s+(?:is\s+)?not\s+(?:what|requested|needed)", re.I)
KEEP_ACCOUNT = re.compile(
    r"\b(?:keep\s+(?:the|my)\s+(?:account|profile)|leave\s+(?:the|my)\s+(?:account|profile)[\w\s]{0,40}?"
    r"(?:active|in\s+place|as\s+it\s+is)|continue\s+using|still\s+(?:use|need)\s+(?:it|the\s+account|my\s+account)|"
    r"account\s+as\s+it\s+is)\b", re.I)
MARKETING_ONLY = re.compile(r"\b(?:marketing|promotional|newsletter|unsubscribe|mailing\s+list)\w*\b", re.I)
OBJECTS = (r"account|profile|data|personal|record|result|entry|copy|copies|information|details|traces|export|archive|"
           r"document|attachment|compte|cuenta|conta|계정|アカウント")
ACCOUNT_DELETION = re.compile(
    r"(?:" + DELETE_TERMS + r")[\w\s,'’-]{0,60}?(?:" + OBJECTS + r")|(?:" + OBJECTS + r")[\w\s,'’-]{0,60}?(?:" + DELETE_TERMS + r")"
    r"|(?:account|profile)[\w\s]{0,20}?\b(?:is|was|has\s+been)\s+(?:gone|deleted|removed|closed|erased)", re.I)
OTHER_REQUEST = re.compile(r"\b(?:copy|download|export|portable|access|what\s+personal\s+information|correct|change|update|"
                           r"misspell\w*|reset|password|login|invoice\w*)\b", re.I)


@dataclass(frozen=True)
class Intent:
    is_deletion: bool
    rule: str
    evidence: str


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", stripped)  # recompose Hangul/Kana after removing Latin accents


def classify(request_text: str) -> Intent:
    text = _norm(request_text or "")
    for rule, pattern, verdict in (("negated_deletion", NEGATED_DELETION, False),
                                   ("negated_deletion", NEGATED_DELETION_TAIL, False),
                                   ("keeps_account", KEEP_ACCOUNT, False)):
        m = pattern.search(text)
        if m:
            return Intent(verdict, rule, m.group(0))
    m = MARKETING_ONLY.search(text)
    if m and not re.search(r"\b(?:account|profile)\b[\w\s,'’-]{0,40}\b(?:delet|eras|clos)", text, re.I):
        return Intent(False, "marketing_opt_out", m.group(0))
    m = ACCOUNT_DELETION.search(text)
    if m:
        return Intent(True, "account_deletion", m.group(0))
    m = OTHER_REQUEST.search(text)
    if m:
        return Intent(False, "other_request_type", m.group(0))
    return Intent(False, "no_deletion_signal", "")
