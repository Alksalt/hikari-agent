"""User-facing acknowledgement controls for structured email actions."""

from tools.mail_actions.decide import mail_action_decide
from tools.mail_actions.update import (
    mail_action_acknowledge,
    mail_action_resolve,
    mail_action_snooze,
)

ALL_TOOLS = [
    mail_action_acknowledge, mail_action_resolve, mail_action_snooze,
    mail_action_decide,
]
