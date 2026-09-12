import base64
from pathlib import Path

from clearact.domain.models import ChatMessage
from clearact.providers.attachments import attachment_prompt, ollama_message_payload, openai_message_payload


def test_text_attachment_is_exposed_as_workspace_path_without_embedding_binary(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    message = ChatMessage(
        role="user",
        content="summarize",
        metadata={
            "attachments": [
                {
                    "name": "notes.txt",
                    "path": ".clearact/attachments/u/01-notes.txt",
                    "storage_path": str(target),
                    "media_type": "text/plain",
                    "kind": "file",
                    "size": 5,
                }
            ]
        },
    )

    payload = openai_message_payload(message)

    assert isinstance(payload["content"], str)
    assert ".clearact/attachments/u/01-notes.txt" in payload["content"]
    assert "storage_path" not in str(payload)
    assert "untrusted data" in attachment_prompt(message)


def test_image_attachment_uses_each_provider_multimodal_format(tmp_path: Path):
    image = tmp_path / "image.png"
    raw = b"\x89PNG\r\n\x1a\n" + b"pixels"
    image.write_bytes(raw)
    message = ChatMessage(
        role="user",
        content="describe",
        metadata={
            "attachments": [
                {
                    "name": "image.png",
                    "path": ".clearact/attachments/u/01-image.png",
                    "storage_path": str(image),
                    "media_type": "image/png",
                    "kind": "image",
                    "size": len(raw),
                }
            ]
        },
    )

    openai = openai_message_payload(message)
    ollama = ollama_message_payload(message)

    assert openai["content"][1]["type"] == "image_url"
    assert openai["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert ollama["images"] == [base64.b64encode(raw).decode("ascii")]
