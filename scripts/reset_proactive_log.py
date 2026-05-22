"""Reset runtime_state.proactive_log_v1 to '[]'. One-shot use after the
cap-burning bug fix. Delete this file after running once."""
from storage import db


def main():
    db.runtime_set("proactive_log_v1", "[]")
    print("proactive_log_v1 cleared.")


if __name__ == "__main__":
    main()
