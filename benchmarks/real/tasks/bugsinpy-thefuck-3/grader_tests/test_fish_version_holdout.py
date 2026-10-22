from thefuck.shells import Fish


def test_info_reads_version_from_fish_version_output(mocker) -> None:
    process = mocker.patch("thefuck.shells.fish.Popen")
    process.return_value.stdout.read.return_value = b"fish, version 4.0.1\n"

    assert Fish().info() == "Fish Shell 4.0.1"
    assert process.call_args[0][0] == ["fish", "--version"]
