"""Reserved ACCEPT admission fails closed and refuses substituted source receipts."""

import unittest
from dataclasses import replace

from tests.support.fixtures_test_standard_project_lifecycle import (
    ContractEvidenceLifecyclePorts,
    LifecyclePorts,
    _decision,
    _diamond_lock,
    _identity,
    _names,
    _prepared_execution,
    _prepared_nodes,
    _service,
)


class AcceptReservationAdmissionTests(unittest.TestCase):
    def test_invalid_or_failed_reserved_acceptance_releases_slot_and_fails(self):
        def invalid_result():
            return object()

        def failure():
            raise RuntimeError("accept failed")

        for outcome in (invalid_result, failure):
            with self.subTest(outcome=outcome.__name__):
                lock = _diamond_lock()
                execution, requests = _prepared_execution(lock)
                names = _names(lock)
                ports = LifecyclePorts(execution, names)
                released = []

                class Reservation:
                    def run(self, outcome=outcome):
                        return outcome()

                    def release(self, released=released):
                        released.append(True)

                class Acceptor:
                    def accept(self, plan, test, execution):
                        raise AssertionError("scheduler bypassed ACCEPT admission")

                    def try_reserve_accept(self, plan, test, execution):
                        return Reservation()

                service = _service(ports)
                service.acceptor = Acceptor()
                result = service.execute(
                    execution,
                    component_lock=lock,
                    invalidation=_decision(
                        execution, names, "money", tuple(names.values())
                    ),
                    prepared_nodes=_prepared_nodes(execution, requests),
                    max_parallelism=2,
                )
                self.assertFalse(result.successful)
                self.assertTrue(released)
                self.assertFalse(any(event[0] == "accept" for event in ports.events))
                self.assertTrue(
                    any(
                        node.failure_evidence.phase.value == "accept"
                        for node in result.node_results
                        if node.failure_evidence is not None
                    )
                )

    def test_reserved_acceptance_cannot_substitute_valid_foreign_source_receipt(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = ContractEvidenceLifecyclePorts(execution, names)
        released = []

        class Reservation:
            def __init__(self, plan, test, executed):
                self.plan, self.test, self.executed = plan, test, executed

            def run(self):
                receipt = ports.accept(self.plan, self.test, self.executed)
                return replace(
                    receipt, source_generation_identity=_identity("foreign-source")
                )

            def release(self):
                released.append(self.plan.component_revision)

        class Acceptor:
            def accept(self, *args):
                raise AssertionError("scheduler bypassed ACCEPT reservation")

            def try_reserve_accept(self, plan, test, executed):
                return Reservation(plan, test, executed)

        service = _service(ports)
        service.acceptor = Acceptor()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        self.assertTrue(released)
        self.assertEqual(len(released), len(set(released)))
        failures = [
            node.failure_evidence
            for node in result.node_results
            if node.failure_evidence
        ]
        self.assertTrue(
            any(
                item.code == "standard_lifecycle.acceptance_evidence_mismatch"
                for item in failures
            )
        )
