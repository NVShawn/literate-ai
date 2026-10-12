"""Preserve optional worker reservations while adding local source registration."""

from literate_ai.adapters.lifecycle.standard_local import (
    RegisteredSourceGenerationRunner,
)
from literate_ai.application.standard_lifecycle_ports import AdmittedSourceGenerator


class _RegisteredGenerationReservation:
    def __init__(self, owner, prepared, reservation):
        self.owner, self.prepared, self.reservation = owner, prepared, reservation

    def run(self):
        try:
            return self.owner._register(self.prepared, self.reservation.run())
        finally:
            self.reservation.release()

    def release(self):
        self.reservation.release()


class RegisteredAdmittedSourceGenerator(RegisteredSourceGenerationRunner):
    """Only admitted delegates expose nonblocking generation capacity."""

    def try_reserve_generate(self, prepared):
        reservation = self.delegate.try_reserve_generate(prepared)
        if reservation is None:
            return None
        return _RegisteredGenerationReservation(self, prepared, reservation)


def register_source_generator(delegate, registry):
    wrapper = (
        RegisteredAdmittedSourceGenerator
        if isinstance(delegate, AdmittedSourceGenerator)
        else RegisteredSourceGenerationRunner
    )
    return wrapper(delegate, registry)
