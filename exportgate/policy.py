"""Release rules and approval rules. Plain code: no model can change these.

Release matrix (jurisdiction x recipient):

                     US person        foreign, licensed      foreign, no license   sanctioned   unknown
  ITAR               RELEASE*         HOLD (EO checks)       BLOCK                 BLOCK        HOLD
  EAR                RELEASE*         HOLD                   HOLD (licence check)  BLOCK        HOLD
  EAR99 / clear      RELEASE          RELEASE                RELEASE               BLOCK        RELEASE
  uncertain          HOLD             HOLD                   HOLD                  BLOCK        HOLD

  * a US-person recipient still needs the requester to be cleared for that program,
    and any MARKING_MISSING / ALTERED_PART_NUMBER flag forces HOLD for a human look.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .fusion import Assessment
from .registry import Person, Recipient, registry

RELEASE, HOLD, BLOCK = "RELEASE", "HOLD", "BLOCK"
APPROVER_ROLE = "EMPOWERED_OFFICIAL"


@dataclass
class Decision:
    action: str            # RELEASE | HOLD | BLOCK
    codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    needs_approval: bool = False


def decide(a: Assessment, requester: Person | None, recipient: Recipient | None, recipient_email: str) -> Decision:
    reg = registry()
    d = Decision(RELEASE)

    def hold(code, why):
        if d.action != BLOCK:
            d.action = HOLD
        d.codes.append(code)
        d.reasons.append(why)

    def block(code, why):
        d.action = BLOCK
        d.codes.append(code)
        d.reasons.append(why)

    if requester is None:
        hold("HOLD_UNKNOWN_REQUESTER", "the person sending this is not in the staff list")

    country = (recipient.country if recipient else "").upper()
    if recipient and country in reg.sanctioned:
        block("BLOCK_SANCTIONED_DESTINATION", f"{recipient.company} is in {country}, a sanctioned/proscribed destination")

    if "INJECTION_ATTEMPT" in a.flags:
        hold("HOLD_INJECTION_ATTEMPT", "the file contains text trying to instruct the AI; ignored, and flagged for review")
    if "UNREAD" in a.flags:
        hold("HOLD_UNREADABLE", "no text could be read from this file and it matches no known drawing")
    if "MARKINGS_UNREAD" in a.flags:
        hold("HOLD_MARKINGS_UNREAD", "looks like a copy of a controlled drawing, and its markings could not be read")

    if a.band == "clear":
        if not d.codes:
            d.codes.append("RELEASE_NOT_CONTROLLED")
            d.reasons.append(f"no export-control evidence (p = {a.p:.3f})")
        d.needs_approval = d.action == HOLD
        return d

    if a.band == "uncertain":
        hold("HOLD_UNCERTAIN", f"possibly controlled (p = {a.p:.2f}); a human must look")

    if recipient is None:
        hold("HOLD_UNKNOWN_RECIPIENT", f"{recipient_email} is not a known, screened recipient")
    elif not recipient.us_person:
        licensed = a.program and a.program in recipient.licenses.values()
        if a.jurisdiction == "ITAR":
            if licensed:
                lic = next(k for k, v in recipient.licenses.items() if v == a.program)
                hold("HOLD_LICENSE_CHECK", f"ITAR data to a foreign person; license {lic} covers {a.program}: "
                                           "Empowered Official must confirm scope")
            else:
                block("BLOCK_ITAR_NO_LICENSE", f"ITAR technical data to {recipient.company} ({country}) "
                                               "with no export license on file")
        elif a.jurisdiction == "EAR":
            hold("HOLD_EAR_LICENSE_CHECK", f"EAR-controlled ({a.classification}) to {recipient.company} ({country}): "
                                          "check license or license exception")

    if requester and a.program and a.jurisdiction in ("ITAR", "EAR") and a.program not in requester.cleared_programs \
            and requester.role != APPROVER_ROLE:
        hold("HOLD_REQUESTER_NOT_CLEARED", f"{requester.name} is not cleared for program {a.program}")

    for f in a.flags:
        if f == "MARKING_MISSING":
            hold("HOLD_MARKING_MISSING", "this copy has had its export marking removed or cropped")
        if f == "ALTERED_PART_NUMBER":
            hold("HOLD_ALTERED_PART_NUMBER", "the part number differs slightly from a controlled register entry")

    if not d.codes:
        d.codes.append("RELEASE_AUTHORIZED_RECIPIENT")
        d.reasons.append(f"{a.jurisdiction} data to a US person ({recipient.company}); requester cleared")
    d.needs_approval = d.action == HOLD
    return d


@dataclass
class ApprovalCheck:
    allowed: bool
    code: str
    reason: str


def check_approval(request_status: str, requester_email: str, approver: Person | None) -> ApprovalCheck:
    """Two-person rule: the requester can never approve, and only an Empowered Official can."""
    if approver is None:
        return ApprovalCheck(False, "REFUSE_UNKNOWN_APPROVER", "approver is not in the staff list")
    if request_status == BLOCK:
        return ApprovalCheck(False, "REFUSE_BLOCKED", "blocked by rule; no one can approve this, it needs a license first")
    if request_status != HOLD:
        return ApprovalCheck(False, "REFUSE_NOT_PENDING", f"request is {request_status}, nothing to approve")
    if approver.email.lower() == (requester_email or "").lower():
        return ApprovalCheck(False, "REFUSE_SELF_APPROVAL", "you can't approve your own release")
    if approver.role != APPROVER_ROLE:
        return ApprovalCheck(False, "REFUSE_NOT_EMPOWERED_OFFICIAL",
                             f"{approver.name} is {approver.role}; only an Empowered Official can approve")
    return ApprovalCheck(True, "OK", "")
