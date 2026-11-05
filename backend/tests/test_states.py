import pytest

from exradius.states import (
    TERMINAL,
    InvalidTransition,
    State,
    can_transition,
    check_transition,
    is_terminal,
)

S = State
NON_TERMINAL = [S.QUEUED, S.PREPARING, S.RUNNING, S.WAITING_FOR_INPUT, S.RESUMING]


def walk(*states):
    current = None
    for state in states:
        check_transition(current, state)
        current = state
    return current


def test_happy_path():
    assert walk(S.QUEUED, S.PREPARING, S.RUNNING, S.COMPLETED) == S.COMPLETED


def test_waiting_cycle_any_number_of_times():
    cycle = [S.WAITING_FOR_INPUT, S.RESUMING, S.RUNNING]
    assert walk(S.QUEUED, S.PREPARING, S.RUNNING, *cycle * 3, S.COMPLETED) == S.COMPLETED


def test_rejected_only_on_intake():
    check_transition(None, S.REJECTED)
    for state in State:
        if state is not S.REJECTED:
            assert not can_transition(state, S.REJECTED)


def test_intake_states():
    assert can_transition(None, S.QUEUED)
    for state in State:
        if state not in (S.QUEUED, S.REJECTED):
            assert not can_transition(None, state)


@pytest.mark.parametrize("state", NON_TERMINAL)
def test_any_non_terminal_state_can_be_cancelled_or_fail(state):
    assert can_transition(state, S.CANCELLED)
    assert can_transition(state, S.FAILED)


@pytest.mark.parametrize("state", sorted(TERMINAL))
def test_terminal_states_are_final(state):
    assert is_terminal(state)
    for target in State:
        assert not can_transition(state, target)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.QUEUED, S.RUNNING),
        (S.QUEUED, S.COMPLETED),
        (S.PREPARING, S.WAITING_FOR_INPUT),
        (S.WAITING_FOR_INPUT, S.RUNNING),  # must go through resuming
        (S.WAITING_FOR_INPUT, S.COMPLETED),
        (S.RESUMING, S.WAITING_FOR_INPUT),
        (S.RUNNING, S.QUEUED),
        (S.COMPLETED, S.RUNNING),
    ],
)
def test_invalid_transitions(current, new):
    with pytest.raises(InvalidTransition):
        check_transition(current, new)


def test_accepts_plain_strings():
    assert can_transition("running", "waiting_for_input")
    assert not is_terminal("running")
    assert not is_terminal(None)
