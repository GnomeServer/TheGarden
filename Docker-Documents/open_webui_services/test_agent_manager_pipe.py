from agent_manager_pipe import Pipe


def test_format_artifacts_preserves_signed_urls_and_displays_filename_and_size():
    signed_url = "https://manager.example/artifacts/id?expires=123&signature=abc"
    result = Pipe._format_artifacts(
        {
            "filename": "report [final].zip",
            "download_url": signed_url,
            "size_bytes": 2048,
        }
    )
    assert f"[report \\[final\\].zip]({signed_url})" in result
    assert "2,048 bytes" in result
    assert "api" not in result.lower()


def test_format_artifacts_without_artifacts_reports_failure():
    result = Pipe._format_artifacts(None)

    assert "completed" in result
    assert "no downloadable artifact" in result
