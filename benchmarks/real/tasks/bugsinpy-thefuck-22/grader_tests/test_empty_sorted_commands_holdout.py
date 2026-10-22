from thefuck.types import Settings, SortedCorrectedCommandsSequence


def test_empty_generator_has_zero_length() -> None:
    commands = SortedCorrectedCommandsSequence((x for x in ()), Settings({}))
    assert len(commands) == 0
