"""Admit GENERATE input before a bounded, privately authorized child process."""

from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_worker_process import run_guarded_generation_process


def run_generate_worker_process(*, input_record, input_identity, deadline, **kwargs):
    GenerateWorkerInput.admit(input_record, input_identity, deadline)
    return run_guarded_generation_process(
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
        **kwargs,
    )
