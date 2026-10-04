"""Shared dispatch budget for tests that run real action subprocesses.

Hosted runners can take several minutes to start receivers and observe native
dependency closures. Tests that exercise expiry use explicit short deadlines.
"""

from datetime import timedelta

ACTION_TEST_DEADLINE = timedelta(minutes=30)
