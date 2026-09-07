from app.integrations.health_api import count_terminal_failures


def test_terminal_failures_sum_across_delivery_and_inbox_queues() -> None:
    assert count_terminal_failures(
        {"FAILED_TERMINAL": 2, "DELIVERED": 5},
        {"FAILED_TERMINAL": 3, "PROCESSED": 8},
    ) == 5


def test_terminal_failure_classification_is_case_insensitive() -> None:
    assert count_terminal_failures(
        {"failed": 1, "dead_letter": 2},
        {"Permanent_Failure": 3, "retry": 9},
    ) == 6
