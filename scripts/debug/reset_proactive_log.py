"""Reset all three proactive cadence pool logs. Run after a cap-burning bug
fix or to clear state during manual testing."""
from storage import db


def main():
    for key in (
        "proactive_log_v1",
        "proactive_ceremony_log_v1",
        "proactive_user_anchored_log_v1",
    ):
        db.runtime_set(key, "[]")
        print(f"{key} cleared.")


if __name__ == "__main__":
    main()
