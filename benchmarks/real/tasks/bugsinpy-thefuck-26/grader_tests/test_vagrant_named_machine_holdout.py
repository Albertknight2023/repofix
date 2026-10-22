from tests.utils import Command
from thefuck.rules.vagrant_up import get_new_command


def test_named_machine_offers_specific_and_all_instances(monkeypatch) -> None:
    monkeypatch.setattr("thefuck.shells.and_", lambda *commands: " && ".join(commands))
    command = Command(
        "vagrant ssh staging",
        stderr="VM must be running. Run `vagrant up` to start the virtual machine.",
    )
    assert get_new_command(command, None) == [
        "vagrant up staging && vagrant ssh staging",
        "vagrant up && vagrant ssh staging",
    ]
