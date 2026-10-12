"""Standard registration preserves remote generation capacity and local behavior."""

import unittest
from unittest.mock import patch

import tests.support.fixtures_test_command_generator as fixture_module
from literate_ai.adapters.registered_generation import register_source_generator
from literate_ai.application.standard_lifecycle_ports import AdmittedSourceGenerator


class RegisteredGenerationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.CommandGeneratorTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.wrapper = register_source_generator(f.generator, f.fixture.registry)

    def test_wrapped_command_generator_preserves_capacity_and_source_registration(self):
        f = self.fixture
        self.assertIsInstance(self.wrapper, AdmittedSourceGenerator)
        occupied = f.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(self.wrapper.try_reserve_generate(f.fixture.prepared))
        self.assertFalse(f.marker.exists())
        occupied.release()
        reservation = self.wrapper.try_reserve_generate(f.fixture.prepared)
        result = reservation.run()
        self.assertEqual(result, f.fixture.fixture.output)
        self.assertEqual(
            f.fixture.registry.evidence(
                result.candidate.tree_identity
            ).source_generation_identity,
            result.identity,
        )
        reservation.release()
        f.assert_released()

    def test_plain_local_delegate_does_not_advertise_worker_admission(self):
        wrapper = register_source_generator(
            lambda node: None, self.fixture.fixture.registry
        )
        self.assertNotIsInstance(wrapper, AdmittedSourceGenerator)
        with self.assertRaises(RuntimeError):
            wrapper(self.fixture.fixture.prepared)

    def test_registration_failure_releases_reserved_capacity(self):
        f = self.fixture
        reservation = self.wrapper.try_reserve_generate(f.fixture.prepared)
        with patch.object(self.wrapper, "_register", side_effect=ValueError("refused")):
            with self.assertRaises(ValueError):
                reservation.run()
        f.assert_released()

    def test_releasing_before_run_does_not_dispatch(self):
        f = self.fixture
        reservation = self.wrapper.try_reserve_generate(f.fixture.prepared)
        reservation.release()
        reservation.release()
        self.assertFalse(f.marker.exists())
        f.assert_released()
